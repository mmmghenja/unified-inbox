"""Внимание: режим беседы и внимание к сообщению. Спецификация: docs/architecture.md, A2–A5 и
поправки П1, П2, П5, П12, П15.

ОДИН модуль — правила режима живут только здесь, в Python (`effective_mode`, `message_attention`), и
регистрируются в SQLite функциями (`register`) — в писателе и в каждом читателе (`db.py`). SQL выдач и
счётчиков зовёт эти функции через построители ниже (`channel_mode_sql`, `message_attention_sql`), второй
SQL-копии правил нет (П5).

Режимы: `react` (реагирую), `info` (к сведению) и `muted` (заглушено —
`channel.muted`/`project.muted`, не хранится в `channel.attention`). Режима `triage` («разбор моделью») с
Ф57-1 нет, режима `digest` («сводка раз в день») — с Ф57-2: значение, оставшееся в данных (ручной режим
беседы, политика проекта), читается как отсутствующее (миграции 0087/0088 переводят его в `info`).
"""

import functools
import json
import re

from . import threads as threads_mod

MODES = ("react", "info")
MUTED = "muted"
# A2: ключи политики — значения `channel.kind` и `mpim` (групповая личка Slack, `attrs.slack_type`), плюс
# `default`. Совпадает с CHECK `channel.kind` (0001_shared.sql).
POLICY_KINDS = ("dm", "group", "channel", "topic", "mail_thread", "issue", "card", "task", "mpim")
POLICY_KEYS = (*POLICY_KINDS, "default")
# Ф10-5 B1: уведомления трекеров (задача Jira, карточка Trello, задача) — `default` проекта на них не
# действует: только явный ключ вида, иначе встроенное «к сведению»; «для меня» поднимает до «реагирую»
# (`message_attention`).
TASK_KINDS = ("issue", "card", "task")
# Ф13-1 A: ключ политики «аккаунт» — `account:<id>` (id — целое > 0); сильнее вида и `default`.
ACCOUNT_KEY_RE = re.compile(r"^account:[1-9]\d{0,17}$")
ACCOUNT_KEY_PREFIX = "account:"

SQL_CHANNEL_MODE = "hub_attention"
SQL_MESSAGE_ATTENTION = "hub_message_attention"


@functools.lru_cache(maxsize=256)
def _policy(policy_json: str | None) -> dict:
    """Ключ `attention` из `project.agent_policy_json` (кэш по строке: функция SQLite зовётся на каждую
    строку выборки). Битый JSON или не словарь — пустая политика."""
    if not policy_json:
        return {}
    try:
        value = json.loads(policy_json)
    except ValueError, TypeError:
        return {}
    attention = value.get("attention") if isinstance(value, dict) else None
    return attention if isinstance(attention, dict) else {}


def policy_of(policy_json: str | None) -> dict:
    """Ключ `attention` политики проекта — копия (выдача `/projects`, `hub project list`)."""
    return dict(_policy(policy_json))


def _mode_of(policy: dict, key: str) -> str | None:
    value = policy.get(key)
    return value if value in MODES else None


def effective_mode(
    channel_muted,
    project_muted,
    manual: str | None,
    project_id: int | None,
    kind: str | None,
    slack_type: str | None,
    policy_json: str | None,
    account_id: int | None = None,
) -> str:
    """A3 в порядке П2: заглушено -> ручной режим -> Неразобранное (Ф57-1: лички и почта `react`, остальное
    `info`) ->
    Ф13-1 A: ключ `account:<id>` аккаунта беседы (сильнее вида и `default`, в т.ч. для задач трекеров) ->
    политика проекта по виду (`mpim` — только ключ `mpim`, см. ниже) -> встроенное «лички — реагирую» ->
    `default` проекта -> встроенное (`mail_thread` — `react`, остальное — `info`).

    Ф10-5 B1: для задач трекеров (`issue`/`card`/`task`, `TASK_KINDS`) шаг «`default` проекта» пропускается —
    действует только явный ключ вида (`hub project set … --attention card=react`), иначе встроенное `info`;
    упоминания и комментарии людей поднимает до `react` `message_attention` по «для меня».

    `mpim` (групповая личка Slack) у коннектора — `kind='group'` с `attrs.slack_type='mpim'`, как и обычные
    каналы Slack (`slack/convert.channel_kind`): ключ политики `group` его НЕ затрагивает — иначе политика
    «каналы Slack — к сведению» заглушила бы групповые лички мимо П2 («лички глушит только явный ключ»)."""
    if channel_muted or project_muted:
        return MUTED
    if manual in MODES:
        return manual
    is_mpim = slack_type == "mpim"
    personal = kind == "dm" or is_mpim
    if project_id is None:
        return "react" if personal or kind == "mail_thread" else "info"
    policy = _policy(policy_json)
    if account_id is not None:
        by_account = _mode_of(policy, f"{ACCOUNT_KEY_PREFIX}{account_id}")
        if by_account is not None:
            return by_account
    by_kind = _mode_of(policy, "mpim") if is_mpim else _mode_of(policy, kind or "")
    if by_kind is not None:
        return by_kind
    if personal:
        return "react"
    if kind in TASK_KINDS:
        return "info"
    by_default = _mode_of(policy, "default")
    if by_default is not None:
        return by_default
    return "react" if kind == "mail_thread" else "info"


def message_attention(
    mode: str | None,
    for_me,
    kind: str | None,
    is_bot=False,
    bulk=False,
    manual: str | None = None,
    rule: str | None = None,
) -> str | None:
    """A4 + П1: внимание к сообщению. `muted` — `muted`; `info` поднимается до `react` по `for_me` ТОЛЬКО в
    чатах (у почты `mentions_me` = «мой адрес в Кому», `mail/convert.py`).

    Ф63-1 П5: `rule` — внимание, назначенное правилом входящих проекта (`message.rule_attention`), `manual` —
    ручной режим беседы (`channel.attention`). Заглушка и ручной режим владельца сильнее правила. Правило `react`
    — `react` (поднимает рассылку и сообщение бота до карточки); `info` гасит, но НЕ лички (`dm`, групповая
    личка Slack — `kind` `mpim` от построителя) и не прямое упоминание или ответ в моём треде в чатах
    (`for_me` не у почты) — они считаются как без правила. У почты «мой адрес в Кому» правило гасит.

    Ф20-1 A4: у сообщения бота `for_me` не учитывается и `react` опускается до `info` — бот не даёт «ждёт
    ответа», `unread_react`, «Нужна реакция» и карточку по правилам; `muted` — как есть.

    Ф57-1 П1: письмо-рассылка (`message.bulk`, ставит только коннектор почты) в почтовой беседе опускает
    `react` до `info` — то же, что бот, но признак свой: «Люди», подписки `messages`/`personal`, напоминания
    и автоотправка его не читают."""
    if mode is None:
        return None
    if rule in MODES and mode != MUTED and manual not in MODES:
        if rule == "react":
            return "react"
        if not (kind in ("dm", "mpim") or (for_me and kind != "mail_thread")):
            return "info"
    if is_bot:
        return "info" if mode == "react" else mode
    if bulk and mode == "react" and kind == "mail_thread":
        return "info"
    if mode == "info" and for_me and kind != "mail_thread":
        return "react"
    return mode


def register(conn) -> None:
    """Функции SQLite (П5) — в писателе и в каждом читателе (`db.py`)."""
    conn.create_function(SQL_CHANNEL_MODE, 8, effective_mode, deterministic=True)
    conn.create_function(SQL_MESSAGE_ATTENTION, 7, message_attention, deterministic=True)


# --- построители SQL ------------------------------------------------------------------------------------


def channel_mode_sql(channel_alias: str = "c", project_alias: str = "p", *, manual: bool = True) -> str:
    """Эффективный режим канала. `project_alias` — LEFT JOIN `main.project` по `project_id` канала (канал в
    work — тот же `main.project`). `manual=False` — режим без ручного (`attention_auto`, П15)."""
    c, p = channel_alias, project_alias
    manual_expr = f"{c}.attention" if manual else "NULL"
    return (
        f"{SQL_CHANNEL_MODE}({c}.muted, {p}.muted, {manual_expr}, {c}.project_id, {c}.kind, "
        f"json_extract({c}.meta_json, '$.attrs.slack_type'), {p}.agent_policy_json, {c}.account_id)"
    )


def for_me_sql(message_alias: str = "m", thread_alias: str = "t") -> str:
    """П12: `for_me := личное упоминание OR (настоящий ответ, не broadcast, тред подписан)` — 0/1.
    `thread_alias` присоединён LEFT JOIN-ом по `(channel_id, ext_id = m.thread_ext_id)`. Ф20-1 B1/П7:
    упоминание — только личное (`threads.direct_mention_sql`): `@channel`/`@here`/`@everyone` — не «для
    меня»."""
    m, t = message_alias, thread_alias
    real_reply = threads_mod.real_reply_sql(f"{m}.thread_ext_id", f"{m}.ext_id")
    return (
        f"(CASE WHEN {threads_mod.direct_mention_sql(m)} OR ({real_reply} AND {m}.thread_broadcast = 0 "
        f"AND {t}.subscribed = 1) THEN 1 ELSE 0 END)"
    )


def message_attention_sql(
    message_alias: str = "m", channel_alias: str = "c", project_alias: str = "p", thread_alias: str = "t"
) -> str:
    """Поле `attention` сообщения (A4) — тем же `hub_message_attention` поверх режима канала. Ф63-1 П5: 7
    аргументов — ещё ручной режим беседы и внимание правила входящих; вид беседы групповой лички Slack
    передаётся как `mpim` (правило «к сведению» её не гасит, как и `dm`)."""
    c, m = channel_alias, message_alias
    mode = channel_mode_sql(c, project_alias)
    for_me = for_me_sql(m, thread_alias)
    kind = (
        f"CASE WHEN json_extract({c}.meta_json, '$.attrs.slack_type') = 'mpim' THEN 'mpim' ELSE {c}.kind END"
    )
    return (
        f"{SQL_MESSAGE_ATTENTION}({mode}, {for_me}, {kind}, {m}.is_bot, {m}.bulk, {c}.attention, "
        f"{m}.rule_attention)"
    )


def unread_react_pred(
    message_alias: str = "m", channel_alias: str = "c", project_alias: str = "p", thread_alias: str = "t"
) -> str:
    """A5 + П12: сообщение входит в `unread_react` — непрочитано (те же построители, что поле `unread`) и
    его внимание `react`. Поле сообщения и счётчик считают ровно одно и то же."""
    unread = threads_mod.message_unread_sql(message_alias, channel_alias, thread_alias)
    attention = message_attention_sql(message_alias, channel_alias, project_alias, thread_alias)
    return f"({unread} = 1 AND {attention} = 'react')"


def _sql(schema: str, template: str, **parts: str) -> str:
    """Тот же приём, что в ingest.py/queries.py: схема и доверенные фрагменты подставляются здесь."""
    return template.format(schema=schema, **parts)


_UNREAD_REACT_TEMPLATE = (
    "SELECT {group}, COUNT(*) FROM {schema}.message m "
    "JOIN {schema}.channel c ON c.id = m.channel_id "
    "LEFT JOIN main.project p ON p.id = c.project_id "
    "LEFT JOIN {schema}.thread t ON t.channel_id = m.channel_id AND t.ext_id = m.thread_ext_id "
    "WHERE ({where}) AND {pred} "
    "GROUP BY {group}"
)


def unread_react_counts(conn, schema: str, *, where: str, params: list, group: str = "m.channel_id") -> dict:
    """Счётчик `unread_react` одним сгруппированным запросом на схему (П5, без N+1): `where` — фильтр по
    `c.*` (каналы выдачи или скоуп принципала), `group` — `m.channel_id` (каналы) или `c.project_id`
    (проекты)."""
    rows = conn.execute(
        _sql(schema, _UNREAD_REACT_TEMPLATE, group=group, where=where, pred=unread_react_pred()),
        params,
    ).fetchall()
    return {key: count for key, count in rows}


_LAST_UNREAD_REACT_TEMPLATE = (
    "SELECT channel_id, id, ts FROM ("
    "  SELECT m.channel_id AS channel_id, m.id AS id, m.ts AS ts, ROW_NUMBER() OVER ("
    "    PARTITION BY m.channel_id ORDER BY m.ts DESC, m.ext_sort DESC, m.id DESC) AS rn "
    "  FROM {schema}.message m "
    "  JOIN {schema}.channel c ON c.id = m.channel_id "
    "  LEFT JOIN main.project p ON p.id = c.project_id "
    "  LEFT JOIN {schema}.thread t ON t.channel_id = m.channel_id AND t.ext_id = m.thread_ext_id "
    "  WHERE m.channel_id IN ({ids}) AND {pred}"
    ") WHERE rn = 1"
)


def last_unread_react(conn, schema: str, channel_ids: list[int]) -> dict[int, tuple[int, int]]:
    """Ф8-6 A3: последнее непрочитанное сообщение, требующее реакции, в каждой из бесед — `channel_id ->
    (message_id, ts)`. Тот же предикат, что счётчик `unread_react` (`unread_react_pred`), — главная и
    счётчики считают одно и то же."""
    if not channel_ids:
        return {}
    rows = conn.execute(
        _sql(
            schema,
            _LAST_UNREAD_REACT_TEMPLATE,
            ids=",".join("?" for _ in channel_ids),
            pred=unread_react_pred(),
        ),
        list(channel_ids),
    ).fetchall()
    return {cid: (mid, ts) for cid, mid, ts in rows}


def normalize_policy_changes(changes: dict) -> dict:
    """A6: `hub project set --attention kind=mode …` — ключ из `POLICY_KEYS` или `account:<id>` (Ф13-1 A),
    значение из `MODES` или `None` (удалить ключ). Неверное — `ValueError`."""
    result: dict = {}
    for key, value in changes.items():
        if key not in POLICY_KEYS and not (isinstance(key, str) and ACCOUNT_KEY_RE.match(key)):
            raise ValueError(f"attention: неизвестный вид беседы {key!r}")
        if value is not None and value not in MODES:
            raise ValueError(f"attention: неизвестный режим {value!r}")
        result[key] = value
    return result
