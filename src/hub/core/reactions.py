"""Очередь «реакция из хаба -> реакция в источнике» (Ф10-3, docs/architecture.md — они главнее текста выше них).
Устройство — по образцу `read_ack.py` (Ф8-7).

Одна строка на сообщение (`reaction_ack`, схема сообщения): клик владельца — одна операция `{emoji, on}`; в
строке — упорядоченный список операций `[[emoji, on], …]` (`gen += 1`, задержка 1.5 с от последнего клика,
новая операция — новые попытки). Slack: новая операция по тому же эмодзи заменяет прежнюю и встаёт в конец
(П1). Telegram: операции копятся по порядку (не длиннее `TELEGRAM_OPS_MAX`) — у него одна реакция, итог
зависит от порядка. Что звать у источника, решает коннектор (Slack трогает только эмодзи из операций;
Telegram проигрывает операции от текущих реакций источника).

Ф15-1 (docs/architecture.md и поправки П1–П5, П7 — главнее текста спеки):
- реакции ставят и сессии Claude Code (`origin="session"`): проект с включёнными реакциями сессий, пауза от
  времени сообщения (случайная точка — один раз на строку), сессия не трогает реакции владельца (пометки
  `reaction_by_session` — только по `done` строки сессии с неизменным `gen`, П3). Строка очереди несёт ОДИН
  `origin` (П1): постановка сессии при ждущих кликах владельца — 409, клик владельца выбрасывает ждущие операции сессии;
- почта (`mail`, решение 6): реакция уходит письмом Gmail — только адресатам на Google, только `on`; `off` —
  лишь отмена ещё не ушедшей операции; клик владельца ждёт 10 с (окно «передумал»).

Правду о реакциях по-прежнему пишет только событие `reaction` источника (почта — `mail_reaction`): здесь
`reactions_json` не меняется НИКОГДА (решение 3 Ф10-3).

Вызывать только внутри `Db.write()` (кроме читателей `wait_state`, `attach_can_react`, `session_policy`);
время — параметром `now_ms`, как везде в core.
"""

import json
import random
import re
import sqlite3
from typing import Literal, NamedTuple

import emoji as emoji_lib

from . import draft_channels
from . import new_recipient as new_recipient_mod
from . import outbox as outbox_mod
from .db import db_of

# fake — для тестов и стенда (`scripts/dev-demo.py`); ведёт себя как Slack (несколько своих, `:name:`).
# Ф40-1 D: Mattermost — как Slack.
SOURCES = frozenset({"telegram", "slack", "fake", "mail", "mattermost", "whatsapp"})
_NAMED_SOURCES = frozenset({"slack", "fake", "mattermost"})
# Ф62-1 П5: одна моя реакция на сообщение (как Telegram) — операции копятся по порядку, `on` вытесняет прочие.
_SINGLE_SOURCES = frozenset({"telegram", "whatsapp"})
DELAY_MS = 1_500
MAIL_UI_DELAY_MS = 10_000  # Ф15-1 решение 6: окно «передумал», как отмена отправки Gmail
TELEGRAM_OPS_MAX = 20
LEASE_MS = 60_000
MAX_ATTEMPTS = 5
BACKOFF_BASE_MS = 30_000
MAX_LEASE_LIMIT = 10
MAX_EMOJI_LEN = 64
MAIL_MY_MAX = 20  # ограничение Gmail: > 20 реакций одного человека на письмо — реакций нет
REPORT_OUTCOMES = frozenset({"done", "retry", "drop"})
ORIGINS = ("ui", "session")
# Отчёт `done` с этой причиной — операции сессии НЕ применены (Telegram: у сообщения реакция владельца, П1).
NO_MARK_REASONS = frozenset({"owner_reacted"})
_BLOCKED_STATUSES = frozenset({"limited", "auth_required"})
_VS16 = chr(0xFE0F)  # вариант-селектор 16 (эмодзи-представление)
# Ф18-1: обычные (не свои, не платные) реакции Telegram — весь набор, другие эмодзи Telegram отклоняет
# (`REACTION_INVALID`); Premium добавляет только свои эмодзи (`custom`). Список — Bot API `ReactionTypeEmoji`
# на 01.10.2026, без U+FE0F, как их шлёт Telegram. Группа может сузить набор у себя — тогда `drop` у
# коннектора.
TELEGRAM_FREE_REACTIONS = frozenset(
    """
        ❤ 👍 👎 🔥 🥰 👏 😁 🤔 🤯 😱 🤬 😢
        🎉 🤩 🤮 💩 🙏 👌 🕊 🤡 🥱 🥴 😍 🐳
        ❤‍🔥 🌚 🌭 💯 🤣 ⚡ 🍌 🏆 💔 🤨 😐 🍓
        🍾 💋 🖕 😈 😴 😭 🤓 👻 👨‍💻 👀 🎃 🙈
        😇 😨 🤝 ✍ 🤗 🫡 🎅 🎄 ☃ 💅 🤪 🗿
        🆒 💘 🙉 🦄 😘 💊 🙊 😎 👾 🤷‍♂ 🤷 🤷‍♀
        😡
    """.split()
)
# Свои эмодзи воркспейса Slack и уже стоящие на сообщении — `:name:` (A2).
_SLACK_NAME_RE = re.compile(r":[a-z0-9_+\-']{1,60}:")

# Ф15-1 A5: политика проекта `agent_policy_json.reactions` — `{"sessions", "delay_min_s", "delay_max_s"}`.
POLICY_KEY = "reactions"
# Владелец 29.09: паузу перед реакцией держит сама сессия по правилам проекта — у хаба по умолчанию её нет.
DEFAULT_DELAY_S = (0, 0)
MAX_DELAY_S = 3600
# Ф15-1 решение 8 + П7: не больше 60 реакций в час на сессию (pid + `starttime`).
SESSION_RATE_PER_HOUR = 60
_HOUR_MS = 3_600_000
# Ф15-1 решение 6: домены Google; плюс домены адресов почтовых аккаунтов хаба на Gmail (Workspace).
GOOGLE_DOMAINS = frozenset({"gmail.com", "googlemail.com"})

_rng = random.Random()  # noqa: S311 — пауза реакции, не криптография; тесты подменяют


class ReactionError(Exception):
    """Базовая ошибка очереди реакций."""


class NotFound(ReactionError):
    """Сообщения нет, оно скрыто у себя, или строки очереди нет / она чужого аккаунта — 404."""


class NotReactable(ReactionError):
    """Реакцию поставить некуда: нет `ext_id` источника (заготовка/черновик), служебное, удалённое (П8) —
    409."""


class Unsupported(ReactionError):
    """Источник без реакций (решение 6 Ф10-3) — 409 `unsupported`."""


class InvalidEmoji(ReactionError):
    """Элемент — не эмодзи и не `:name:` Slack — 422."""


class StaleLease(ReactionError):
    """`lease_gen` не совпал — задание перехвачено новой выдачей, 409."""


class Refused(ReactionError):
    """Ф15-1: отказ правилами хаба. `status` — HTTP-код, `detail` — метка ответа (A3)."""

    status = 409
    detail = "refused"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.detail)


class NotInSourceSet(Refused):
    """Ф18-1: в Telegram такой реакции нет (не из `TELEGRAM_FREE_REACTIONS`) — 422 сразу, а не `drop` у
    коннектора через минуту. Только для постановки: снять можно любую свою."""

    status = 422
    detail = "not in telegram set"


class SessionsOff(Refused):
    """Решение 1: реакции сессий в проекте выключены — 403."""

    status = 403
    detail = "session reactions off"


class NotOwn(Refused):
    """Решение 5: сессия снимает не свою реакцию."""

    detail = "not own"


class OwnerReaction(Refused):
    """Решение 5: Telegram — у сообщения уже есть реакция владельца, поставленная не сессией."""

    detail = "owner reaction"


class OwnerPending(Refused):
    """П1: в строке очереди ждут клики владельца — сессия не вмешивается."""

    detail = "owner pending"


class NotGoogle(Refused):
    """Решение 6: адресат реакции письма не на Google (или адресатов нет)."""

    detail = "not google"


class NoReply(Refused):
    """Решение 6: беседа трекера (`attrs.no_reply`)."""

    detail = "no reply"


class CannotUndo(Refused):
    """Решение 6: реакцию в письме после отправки не снять."""

    detail = "sent, cannot undo"


class TooMany(Refused):
    """Решение 6: моих реакций на письме уже 20."""

    detail = "too many"


class SendingNow(Refused):
    """П4: почта, `off` при живом лизе строки — письмо-реакция уже отправляется."""

    detail = "sending"


class ReceiveOnly(Refused):
    """П5: ящик подключён только на приём (`send_enabled=false`)."""

    detail = "receive only"


class EnqueueResult(NamedTuple):
    status: Literal["queued", "off", "already"]
    channel_id: int
    not_before_ms: int | None = None
    # Ф19-1 D + П11: эмодзи операции после `normalize_emoji` (Telegram без U+FE0F) — для аудита ручки.
    emoji: str | None = None


def _sql(schema: str, template: str, **parts: str) -> str:
    """Тот же приём, что `ingest._sql`: схема (`main`/`work`) и доверенные фрагменты, не ввод пользователя."""
    return template.format(schema=schema, **parts)


def reactions_off(config_json: str | None) -> bool:
    """Выключатель аккаунта (решение 5): `config_json.reactions` — JSON `false`. Всё прочее (нет ключа,
    `true`, битый JSON) — включено."""
    if not config_json:
        return False
    try:
        config = json.loads(config_json)
    except ValueError:
        return False
    return isinstance(config, dict) and config.get("reactions") is False


def _key(value: str) -> str:
    """Сравнение эмодзи — без U+FE0F (`❤️` и `❤` — одна реакция)."""
    return value.replace(_VS16, "")


def normalize_emoji(source: str, value: object) -> str:
    """A2 + П2: непустая строка ≤ 64 символов — одиночный эмодзи (`emoji.is_emoji`) или, для Slack, `:name:`.
    Telegram хранит эмодзи без U+FE0F (`❤️` -> `❤`: так их знает Telegram). Почта — одиночный эмодзи как
    есть (Ф15-1). Иначе `InvalidEmoji`."""
    if not isinstance(value, str) or not value or len(value) > MAX_EMOJI_LEN:
        raise InvalidEmoji("пустой или длинный элемент")
    if source in _NAMED_SOURCES and _SLACK_NAME_RE.fullmatch(value):
        return value
    if not emoji_lib.is_emoji(value):
        raise InvalidEmoji("не эмодзи")
    if source == "telegram":
        return _key(value)
    return value


def _account(conn: sqlite3.Connection, account_id: int) -> tuple[str, str, str, str | None] | None:
    """`(схема, статус, источник, config_json)` аккаунта или `None`."""
    row = conn.execute(
        "SELECT db, status, source, config_json FROM main.account WHERE id = ?", (account_id,)
    ).fetchone()
    return (row[0], row[1], row[2], row[3]) if row is not None else None


def _load_ops(ops_json: str | None) -> list[list]:
    """Операции строки: пары `[emoji, on]`. Ф15-1 П1: и тройки `[emoji, on, "s"]` (вид на случай строк,
    записанных иначе) читаются как пары — `origin` у строки один, колонкой."""
    try:
        ops = json.loads(ops_json) if ops_json else []
    except ValueError:
        return []
    if not isinstance(ops, list):
        return []
    return [
        [op[0], op[1]]
        for op in ops
        if isinstance(op, list)
        and len(op) in (2, 3)  # noqa: PLR2004
        and isinstance(op[0], str)
        and isinstance(op[1], bool)
    ]


# --- Ф15-1 A5: политика проекта ------------------------------------------------------------------------


def _policy_dict(policy_json: str | None) -> dict:
    if not policy_json:
        return {}
    try:
        value = json.loads(policy_json)
    except ValueError, TypeError:
        return {}
    return value if isinstance(value, dict) else {}


def _valid_delay(lo: object, hi: object) -> bool:
    return (
        isinstance(lo, int)
        and isinstance(hi, int)
        and not isinstance(lo, bool)
        and not isinstance(hi, bool)
        and 0 <= lo <= hi <= MAX_DELAY_S
    )


def session_policy(policy_json: str | None) -> dict:
    """`{"sessions": bool, "delay_min_s", "delay_max_s"}` из политики проекта. Выключено по умолчанию
    (решение 1); кривая пауза — умолчание (без паузы)."""
    raw = _policy_dict(policy_json).get(POLICY_KEY)
    raw = raw if isinstance(raw, dict) else {}
    lo, hi = raw.get("delay_min_s"), raw.get("delay_max_s")
    if not _valid_delay(lo, hi):
        lo, hi = DEFAULT_DELAY_S
    return {"sessions": raw.get("sessions") is True, "delay_min_s": lo, "delay_max_s": hi}


def parse_delay_minutes(value: object) -> tuple[int, int]:
    """E: `A-B` в минутах, `0 ≤ A ≤ B ≤ 60` -> `(A*60, B*60)` секунд; иначе `ValueError`."""
    match = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("reaction_delay: ожидается A-B в минутах, например 2-5")
    lo, hi = int(match.group(1)), int(match.group(2))
    if not 0 <= lo <= hi <= MAX_DELAY_S // 60:
        raise ValueError(f"reaction_delay: нужно 0 ≤ A ≤ B ≤ {MAX_DELAY_S // 60}")
    return lo * 60, hi * 60


def project_policy(conn: sqlite3.Connection, project_id: int | None) -> dict:
    if project_id is None:
        return session_policy(None)
    row = conn.execute("SELECT agent_policy_json FROM main.project WHERE id = ?", (project_id,)).fetchone()
    return session_policy(row[0] if row is not None else None)


def set_project_reactions(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    sessions: bool | None = None,
    delay_s: tuple[int, int] | None = None,
) -> dict | None:
    """A5/E: `hub project set P --session-reactions on|off --reaction-delay A-B` — ключ `reactions` политики
    проекта (прочие ключи не трогаются). Нет проекта — `None`; кривая пауза — `ValueError`."""
    row = conn.execute("SELECT agent_policy_json FROM main.project WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        return None
    policy = _policy_dict(row[0])
    current = session_policy(row[0])
    if sessions is not None:
        current["sessions"] = bool(sessions)
    if delay_s is not None:
        lo, hi = delay_s
        if not _valid_delay(lo, hi):
            raise ValueError(f"reaction_delay: нужно 0 ≤ A ≤ B ≤ {MAX_DELAY_S // 60} мин")
        current["delay_min_s"], current["delay_max_s"] = lo, hi
    policy[POLICY_KEY] = current
    conn.execute(
        "UPDATE main.project SET agent_policy_json = ? WHERE id = ?",
        (json.dumps(policy, ensure_ascii=False, sort_keys=True), project_id),
    )
    return current


# --- Ф15-1 решение 6: почта — адресаты на Google -------------------------------------------------------


def google_domains(conn: sqlite3.Connection) -> frozenset[str]:
    """`gmail.com`, `googlemail.com` и домены адресов почтовых аккаунтов хаба на Gmail (не `flavor=imap`)."""
    out = set(GOOGLE_DOMAINS)
    for (config_json,) in conn.execute("SELECT config_json FROM main.account WHERE source = 'mail'"):
        try:
            config = json.loads(config_json) if config_json else {}
        except ValueError:
            continue
        if not isinstance(config, dict) or config.get("flavor") == "imap":
            continue
        if config.get("provider", "gmail") != "gmail":
            continue
        address = config.get("address")
        if isinstance(address, str) and "@" in address:
            out.add(address.rsplit("@", 1)[1].strip().lower())
    return frozenset(out)


def mail_recipients(is_me: bool, mail_to_json: str | None, sender_address: str | None) -> list[str]:
    """Адресаты реакции — как у ответа (`reply_mode=sender`): чужое письмо — его отправитель, моё — его
    `mail_to`."""
    if is_me:
        try:
            value = json.loads(mail_to_json) if mail_to_json else []
        except ValueError:
            value = []
        if not isinstance(value, list):
            return []
        return [a.strip().lower() for a in value if isinstance(a, str) and "@" in a]
    return [sender_address.strip().lower()] if sender_address and "@" in sender_address else []


def on_google(recipients: list[str], domains: frozenset[str]) -> bool:
    return bool(recipients) and all(a.rsplit("@", 1)[1] in domains for a in recipients)


def _check_mail(
    conn: sqlite3.Connection,
    *,
    config_json: str | None,
    meta_json: str | None,
    is_me: bool,
    mail_to_json: str | None,
    sender_address: str | None,
) -> None:
    """Решение 6 + П5: ящик не «только на приём», беседа не трекера, все адресаты на Google."""
    if not new_recipient_mod.mail_send_enabled(config_json):
        raise ReceiveOnly()
    if outbox_mod.channel_no_reply(meta_json):
        raise NoReply()
    if not on_google(mail_recipients(is_me, mail_to_json, sender_address), google_domains(conn)):
        raise NotGoogle()


# --- мои реакции и пометки сессий ----------------------------------------------------------------------


def _mine(reactions_json: str | None) -> tuple[set[str], bool]:
    """Мои эмодзи (без U+FE0F) из `reactions_json` и есть ли моя реакция без эмодзи (кастомная)."""
    try:
        reactions = json.loads(reactions_json) if reactions_json else []
    except ValueError:
        return set(), False
    if not isinstance(reactions, list):
        return set(), False
    keys: set[str] = set()
    custom = False
    for r in reactions:
        if not isinstance(r, dict) or r.get("me") is not True:
            continue
        value = r.get("emoji")
        if isinstance(value, str) and value:
            keys.add(_key(value))
        else:
            custom = True
    return keys, custom


def _marks(conn: sqlite3.Connection, schema: str, message_id: int) -> set[str]:
    rows = conn.execute(
        _sql(schema, "SELECT emoji_key FROM {schema}.reaction_by_session WHERE message_id = ?"), (message_id,)
    ).fetchall()
    return {r[0] for r in rows}


def session_emojis(conn: sqlite3.Connection, message_id: int) -> set[str]:
    """Пометки сессий сообщения (ключи эмодзи)."""
    return _marks(conn, db_of(message_id), message_id)


# --- A2: постановка ------------------------------------------------------------------------------------


def enqueue(
    conn: sqlite3.Connection,
    message_id: int,
    emoji: object,
    on: bool,
    now_ms: int,
    *,
    origin: str = "ui",
    token_id: int | None = None,
    rng: random.Random | None = None,
    delay_s: tuple[int, int] | None = None,
) -> EnqueueResult:
    """A2 + П1/П8 (Ф10-3) и Ф15-1 A2 + П1–П5. Видимость принципалу и право сессии (подписка, П7) проверяет
    ручка в той же транзакции; здесь — состояние сообщения и источника: скрытое у себя -> `NotFound`; без
    `ext_id`, служебное, удалённое -> `NotReactable`; источник без реакций -> `Unsupported`; плохой элемент ->
    `InvalidEmoji`; выключатель `reactions=false` -> `off`, строки нет; правила хаба -> `Refused`. Лиз не
    трогается — идущее задание доработает старые операции, отчёт увидит смену `gen`.

    `delay_s` (секунды `(min, max)`) — пауза вместо паузы политики проекта; `None` — как было."""
    if origin not in ORIGINS:
        raise ValueError(origin)
    on = bool(on)
    schema = db_of(message_id)
    row = conn.execute(
        _sql(
            schema,
            "SELECT m.channel_id, m.ext_id, m.kind, m.deleted, m.hidden, c.account_id, a.source, "
            "a.config_json, c.ext_id, m.ts, m.is_me, m.reactions_json, m.mail_to_json, ct.ext_id, "
            "c.project_id, c.meta_json FROM {schema}.message m "
            "JOIN {schema}.channel c ON c.id = m.channel_id "
            "JOIN main.account a ON a.id = c.account_id "
            "LEFT JOIN {schema}.contact ct ON ct.id = m.sender_contact_id WHERE m.id = ?",
        ),
        (message_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f"message {message_id}")
    (
        channel_id,
        ext_id,
        kind,
        deleted,
        hidden,
        account_id,
        source,
        config_json,
        channel_ext_id,
        message_ts,
        is_me,
        reactions_json,
        mail_to_json,
        sender_address,
        project_id,
        meta_json,
    ) = row
    if hidden:
        raise NotFound(f"message {message_id}")
    # Ф12-1 A7: беседа-заготовка коннектору не видна — реакции в неё не ставятся.
    if ext_id is None or kind == "event" or deleted or draft_channels.is_temp_ext_id(channel_ext_id):
        raise NotReactable(f"message {message_id}")
    if source not in SOURCES:
        raise Unsupported(source)
    value = normalize_emoji(source, emoji)
    key = _key(value)
    if source == "telegram" and on and value not in TELEGRAM_FREE_REACTIONS:
        raise NotInSourceSet(value)
    if reactions_off(config_json):
        return EnqueueResult("off", channel_id, emoji=value)
    policy = None
    if origin == "session":
        policy = project_policy(conn, project_id)
        if not policy["sessions"]:
            raise SessionsOff()
    if source == "mail":
        _check_mail(
            conn,
            config_json=config_json,
            meta_json=meta_json,
            is_me=bool(is_me),
            mail_to_json=mail_to_json,
            sender_address=sender_address,
        )

    existing = conn.execute(
        _sql(
            schema,
            "SELECT ops_json, pause_until_ms, origin, not_before_ms, lease_until_ms "
            "FROM {schema}.reaction_ack WHERE message_id = ?",
        ),
        (message_id,),
    ).fetchone()
    ops = _load_ops(existing[0]) if existing is not None else []
    pause_until_ms = existing[1] if existing is not None else 0
    row_origin = existing[2] if existing is not None else origin
    leased = existing is not None and existing[4] is not None and existing[4] > now_ms
    mine, mine_custom = _mine(reactions_json)
    marks = _marks(conn, schema, message_id)

    if origin == "session":
        if existing is not None and row_origin == "ui":
            if ops:
                raise OwnerPending()  # П1: владелец сам занимается сообщением
            ops = []
        pending_any = any(_key(op[0]) == key for op in ops)
        pending_on = any(_key(op[0]) == key and op[1] for op in ops)
        if on:
            if key in mine and not pending_any:
                return EnqueueResult("already", channel_id, emoji=value)  # П3: уже моя — пометки нет
            if source in _SINGLE_SOURCES and ((mine - marks - {key}) or mine_custom):
                raise OwnerReaction()  # решение 5: одна реакция, и она владельца
        elif key not in marks and not pending_on:
            raise NotOwn()
    else:
        if row_origin == "session":
            ops = []  # решение 4: владелец сам занялся сообщением — ждущие операции сессии прочь
        # Решение 5: клик владельца по эмодзи, помнящемуся сессионным, — реакция становится реакцией владельца. Telegram
        # держит одну реакцию: `on` владельца заменяет и прочие — их пометки тоже больше не значат ничего.
        if source in _SINGLE_SOURCES and on:
            conn.execute(
                _sql(schema, "DELETE FROM {schema}.reaction_by_session WHERE message_id = ?"), (message_id,)
            )
        conn.execute(
            _sql(schema, "DELETE FROM {schema}.reaction_by_session WHERE message_id = ? AND emoji_key = ?"),
            (message_id, key),
        )

    if source == "mail":
        pending_on = any(_key(op[0]) == key and op[1] for op in ops)
        if not on:
            # Решение 6: наружу уходит только `on`; `off` — отмена ещё не ушедшей операции.
            if not pending_on:
                raise CannotUndo()
            if leased:
                raise SendingNow()  # П4
            ops = [op for op in ops if _key(op[0]) != key]
            if not ops:
                conn.execute(
                    _sql(schema, "DELETE FROM {schema}.reaction_ack WHERE message_id = ?"), (message_id,)
                )
                return EnqueueResult("queued", channel_id, emoji=value)
            return _write(
                conn,
                schema,
                message_id,
                channel_id,
                account_id,
                ops,
                origin=row_origin if existing is not None else origin,
                token_id=token_id,
                not_before_ms=existing[3],
                emoji=value,
            )
        if key in mine and not pending_on:
            return EnqueueResult("already", channel_id, emoji=value)
        pending_keys = {_key(op[0]) for op in ops if op[1]}
        if key not in mine | pending_keys and len(mine | pending_keys) >= MAIL_MY_MAX:
            raise TooMany()

    if source in _SINGLE_SOURCES:
        # Приёмка Ф10-3: у Telegram одна реакция, итог зависит от порядка («👍, 🔥, снять 🔥» -> ничего) —
        # операции копятся как есть, коннектор проигрывает их от текущего состояния; хвост ограничен.
        ops = [*ops, [value, on]][-TELEGRAM_OPS_MAX:]
    else:
        # П1: новая операция по тому же эмодзи заменяет прежнюю и встаёт в конец.
        ops = [op for op in ops if _key(op[0]) != key] + [[value, on]]

    if origin == "session":
        not_before_ms = max(pause_until_ms, now_ms + DELAY_MS)
        if existing is not None and row_origin == "session":
            # Решение 3: случайная точка — одна на строку; следующие операции — не раньше неё.
            not_before_ms = max(not_before_ms, existing[3])
        else:
            assert policy is not None  # noqa: S101 — выставлена выше для сессии
            lo, hi = delay_s if delay_s is not None else (policy["delay_min_s"], policy["delay_max_s"])
            pause_ms = (rng or _rng).randint(lo * 1000, hi * 1000)
            not_before_ms = max(not_before_ms, int(message_ts or 0) + pause_ms)
    else:
        delay_ms = MAIL_UI_DELAY_MS if source == "mail" else DELAY_MS
        not_before_ms = max(pause_until_ms, now_ms + delay_ms)
    return _write(
        conn,
        schema,
        message_id,
        channel_id,
        account_id,
        ops,
        origin=origin,
        token_id=token_id if origin == "session" else None,
        not_before_ms=not_before_ms,
        emoji=value,
    )


def _write(
    conn: sqlite3.Connection,
    schema: str,
    message_id: int,
    channel_id: int,
    account_id: int,
    ops: list[list],
    *,
    origin: str,
    token_id: int | None,
    not_before_ms: int,
    emoji: str | None = None,
) -> EnqueueResult:
    conn.execute(
        _sql(
            schema,
            "INSERT INTO {schema}.reaction_ack (message_id, channel_id, account_id, ops_json, gen, "
            "not_before_ms, origin, token_id) VALUES (?, ?, ?, ?, 1, ?, ?, ?) ON CONFLICT (message_id) "
            "DO UPDATE SET channel_id = excluded.channel_id, account_id = excluded.account_id, "
            "ops_json = excluded.ops_json, gen = gen + 1, attempts = 0, "
            "not_before_ms = excluded.not_before_ms, origin = excluded.origin, token_id = excluded.token_id",
        ),
        (
            message_id,
            channel_id,
            account_id,
            json.dumps(ops, ensure_ascii=False),
            not_before_ms,
            origin,
            token_id,
        ),
    )
    return EnqueueResult("queued", channel_id, not_before_ms, emoji)


# --- Ф15-1 П7 и решение 8: право сессии и лимит ---------------------------------------------------------


def session_subscribed(
    conn: sqlite3.Connection,
    *,
    token_id: int,
    pid: int,
    proc_start: int,
    session_id: str,
    channel_id: int,
    project_id: int | None,
) -> bool:
    """П7: у ЭТОЙ сессии (токен, pid + `starttime`, разговор) есть живая подписка на проект беседы или на
    саму беседу."""
    if project_id is not None:
        row = conn.execute(
            "SELECT 1 FROM main.session_sub WHERE project_id = ? AND api_token_id = ? AND session_pid = ? "
            "AND session_proc_start = ? AND session_id = ? AND ended_ts IS NULL LIMIT 1",
            (project_id, token_id, pid, proc_start, session_id),
        ).fetchone()
        if row is not None:
            return True
    row = conn.execute(
        "SELECT 1 FROM main.session_chan_sub WHERE channel_id = ? AND api_token_id = ? AND session_pid = ? "
        "AND session_proc_start = ? AND session_id = ? AND ended_ts IS NULL LIMIT 1",
        (channel_id, token_id, pid, proc_start, session_id),
    ).fetchone()
    return row is not None


def session_rate_count(
    conn: sqlite3.Connection,
    *,
    token_id: int,
    pid: int,
    proc_start: int,
    now_ms: int,
    session_id: str | None = None,
) -> int:
    """Решение 8 + П7: принятых реакций этой сессии за час — по аудиту `reaction_set`. Ф16-4 (П1):
    `session_id` (сессии OpenCode: один процесс — много сессий) — ключ `(pid, proc_start, session_id)`, в
    аудите такой реакции есть `session_id`; без него — как раньше, по процессу."""
    sql = (
        "SELECT COUNT(*) FROM main.audit WHERE action = 'reaction_set' AND ts > ? AND result = 'queued' "
        "AND api_token_id = ? AND json_extract(detail_json, '$.pid') = ? "
        "AND json_extract(detail_json, '$.proc_start') = ?"
    )
    params: list = [now_ms - _HOUR_MS, token_id, pid, proc_start]
    if session_id is not None:
        sql += " AND json_extract(detail_json, '$.session_id') = ?"
        params.append(session_id)
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row is not None else 0


def message_project(conn: sqlite3.Connection, message_id: int) -> tuple[int, int | None] | None:
    """`(channel_id, project_id)` сообщения или `None`."""
    schema = db_of(message_id)
    row = conn.execute(
        _sql(
            schema,
            "SELECT m.channel_id, c.project_id FROM {schema}.message m "
            "JOIN {schema}.channel c ON c.id = m.channel_id WHERE m.id = ?",
        ),
        (message_id,),
    ).fetchone()
    return (row[0], row[1]) if row is not None else None


# --- A6: признак `can_react` у писем -------------------------------------------------------------------


def attach_can_react(conn: sqlite3.Connection, schema: str, messages: list[dict]) -> None:
    """A6: у письма в выдаче — `can_react` (правило 6 без учёта очереди: ящик с `send_enabled`, беседа не
    трекера, адресаты на Google, сообщение с `ext_id`, не удалено, не служебное). У прочих источников поля
    нет. Один запрос на каналы страницы."""
    if not messages:
        return
    channel_ids = sorted({m["channel_id"] for m in messages})
    info: dict[int, tuple] = {}
    for start in range(0, len(channel_ids), 500):
        chunk = channel_ids[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        for cid, c_ext, meta_json, source, config_json in conn.execute(
            _sql(
                schema,
                "SELECT c.id, c.ext_id, c.meta_json, a.source, a.config_json FROM {schema}.channel c "
                "JOIN main.account a ON a.id = c.account_id WHERE c.id IN ({ph})",
                ph=placeholders,
            ),
            chunk,
        ):
            info[cid] = (c_ext, meta_json, source, config_json)
    domains: frozenset[str] | None = None
    for m in messages:
        c = info.get(m["channel_id"])
        if c is None or c[2] != "mail":
            continue
        c_ext, meta_json, _source, config_json = c
        can = (
            m.get("ext_id") is not None
            and not m.get("deleted")
            and not m.get("hidden")
            and m.get("kind") != "event"
            and not draft_channels.is_temp_ext_id(c_ext)
            and not reactions_off(config_json)
            and new_recipient_mod.mail_send_enabled(config_json)
            and not outbox_mod.channel_no_reply(meta_json)
        )
        if can:
            if domains is None:
                domains = google_domains(conn)
            sender = m.get("sender") or {}
            address = sender.get("handle") if isinstance(sender, dict) else None
            can = on_google(mail_recipients(bool(m.get("is_me")), m.get("mail_to_json"), address), domains)
        m["can_react"] = bool(can)


# --- выдача -------------------------------------------------------------------------------------------


def _current_mine(reactions_json: str | None) -> list[str]:
    """П6: свои эмодзи из `reactions_json` (подсказка для Telegram и почты, не правда), без U+FE0F."""
    try:
        reactions = json.loads(reactions_json) if reactions_json else []
    except ValueError:
        return []
    if not isinstance(reactions, list):
        return []
    return [
        _key(r["emoji"])
        for r in reactions
        if isinstance(r, dict) and r.get("me") is True and isinstance(r.get("emoji"), str) and r["emoji"]
    ]


def _resolve(conn: sqlite3.Connection, schema: str, account_id: int, message_id: int) -> dict | None:
    """Канал и сообщение задания или `None` — строку надо удалить (сообщения/канала нет, скрыто у себя,
    удалено, без `ext_id`, чужой аккаунт)."""
    row = conn.execute(
        _sql(
            schema,
            "SELECT m.channel_id, m.ext_id, m.kind, m.deleted, m.hidden, m.reactions_json, c.account_id, "
            "c.ext_id, c.kind, p.ext_id, c.project_id FROM {schema}.message m "
            "JOIN {schema}.channel c ON c.id = m.channel_id "
            "LEFT JOIN {schema}.channel p ON p.id = c.parent_channel_id WHERE m.id = ?",
        ),
        (message_id,),
    ).fetchone()
    if row is None:
        return None
    (
        channel_id,
        m_ext,
        m_kind,
        deleted,
        hidden,
        reactions_json,
        c_account,
        c_ext,
        c_kind,
        parent_ext,
        project_id,
    ) = row
    if c_account != account_id or hidden or deleted or m_ext is None or m_kind == "event":
        return None
    if draft_channels.is_temp_ext_id(c_ext):  # Ф12-1 A7
        return None
    return {
        "channel_id": channel_id,
        "channel": {"ext_id": c_ext, "kind": c_kind, "parent_ext_id": parent_ext},
        "message_ext_id": m_ext,
        "reactions_json": reactions_json,
        "project_id": project_id,
    }


def lease(conn: sqlite3.Connection, *, account_id: int, now_ms: int, limit: int = 1) -> list[dict]:
    """A2 + П6/П9: задания своего аккаунта (по умолчанию одно), лиз 60 с. `reactions=false` — строки аккаунта
    удаляются; `limited`/`auth_required` — пусто, строки ждут. Скрытое/удалённое после постановки — строка
    удаляется по пути, не выдаётся.

    Ф15-1 П1: строка сессии перепроверяется — реакции сессий в проекте выключены -> строка удаляется; в
    задании `origin` и у строки сессии `session_emojis` (пометки + эмодзи операций `on` строки). Почта —
    `current_mine` (П2), как Telegram."""
    info = _account(conn, account_id)
    if info is None:
        return []
    schema, status, source, config_json = info
    if reactions_off(config_json):
        conn.execute(_sql(schema, "DELETE FROM {schema}.reaction_ack WHERE account_id = ?"), (account_id,))
        return []
    if status in _BLOCKED_STATUSES:
        return []
    limit = max(1, min(int(limit), MAX_LEASE_LIMIT))
    rows = conn.execute(
        _sql(
            schema,
            "SELECT message_id, ops_json, gen, origin FROM {schema}.reaction_ack WHERE account_id = ? "
            "AND not_before_ms <= ? AND pause_until_ms <= ? "
            "AND (lease_until_ms IS NULL OR lease_until_ms <= ?) "
            "ORDER BY not_before_ms, message_id",
        ),
        (account_id, now_ms, now_ms, now_ms),
    ).fetchall()
    items: list[dict] = []
    for message_id, ops_json, gen, origin in rows:
        if len(items) >= limit:
            break
        resolved = _resolve(conn, schema, account_id, message_id)
        ops = _load_ops(ops_json)
        drop = resolved is None or not ops
        if not drop and origin == "session":
            drop = not project_policy(conn, resolved["project_id"])["sessions"]
            if not drop:
                # Ф15-1 ревью 1: эмодзи, уже мой без пометки сессии (владелец поставил его сам за паузу), —
                # реакция владельца: операции сессии по нему выбрасываются (иначе `done` повесил бы на него
                # пометку, и сессия смогла бы снять или заменить реакцию владельца).
                owner_side = set(_current_mine(resolved["reactions_json"])) - _marks(conn, schema, message_id)
                kept = [op for op in ops if _key(op[0]) not in owner_side]
                if len(kept) != len(ops):
                    ops = kept
                    drop = not ops
                    if ops:
                        conn.execute(
                            _sql(
                                schema, "UPDATE {schema}.reaction_ack SET ops_json = ? WHERE message_id = ?"
                            ),
                            (json.dumps(ops, ensure_ascii=False), message_id),
                        )
        if drop:
            conn.execute(
                _sql(schema, "DELETE FROM {schema}.reaction_ack WHERE message_id = ?"), (message_id,)
            )
            continue
        lease_gen = conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.reaction_ack SET lease_until_ms = ?, lease_gen = lease_gen + 1, "
                "leased_gen = gen WHERE message_id = ? RETURNING lease_gen",
            ),
            (now_ms + LEASE_MS, message_id),
        ).fetchone()[0]
        item = {
            "message_id": message_id,
            "channel_id": resolved["channel_id"],
            "gen": gen,
            "lease_gen": lease_gen,
            "channel": resolved["channel"],
            "message_ext_id": resolved["message_ext_id"],
            "ops": ops,
            "origin": origin,
        }
        if origin == "session":
            marks = _marks(conn, schema, message_id)
            item["session_emojis"] = sorted(marks | {_key(op[0]) for op in ops if op[1]})
        if source in ("telegram", "mail", "whatsapp"):
            item["current_mine"] = _current_mine(resolved["reactions_json"])
        items.append(item)
    return items


def wait_state(conn: sqlite3.Connection, *, account_id: int, now_ms: int) -> tuple[bool, int | None]:
    """Читатель для `wait` (как `read_ack.wait_state`): `(готово, ближайший срок в мс)`. Срок — только когда
    аккаунт вообще может получить задания (`reactions=false`, `limited`, `auth_required` — `(False, None)`:
    `wait` держит полный таймаут, горячего цикла нет)."""
    info = _account(conn, account_id)
    if info is None:
        return False, None
    schema, status, _source, config_json = info
    if status in _BLOCKED_STATUSES or reactions_off(config_json):
        return False, None
    row = conn.execute(
        _sql(
            schema,
            "SELECT MIN(MAX(not_before_ms, pause_until_ms, COALESCE(lease_until_ms, 0))) "
            "FROM {schema}.reaction_ack WHERE account_id = ?",
        ),
        (account_id,),
    ).fetchone()
    due = row[0] if row is not None else None
    if due is None:
        return False, None
    if due <= now_ms:
        return True, None
    return False, due


# --- отчёт --------------------------------------------------------------------------------------------


def backoff_ms(attempt: int) -> int:
    """Пауза после `attempt`-й неудачной попытки (с 1): 30 с, 60 с, 120 с, …"""
    return BACKOFF_BASE_MS * 2 ** (max(attempt, 1) - 1)


def _apply_marks(
    conn: sqlite3.Connection,
    schema: str,
    message_id: int,
    *,
    source: str,
    ops: list[list],
    token_id: int | None,
    now_ms: int,
) -> None:
    """П3: `done` строки сессии — пометки по операциям: `on` -> пометка, `off` -> снять. Telegram держит одну
    реакцию: `on X` вытесняет прочие пометки сообщения."""
    old = _marks(conn, schema, message_id)
    new = set(old)
    for value, on in ops:
        key = _key(value)
        if on:
            new = {key} if source in _SINGLE_SOURCES else new | {key}
        else:
            new.discard(key)
    for key in old - new:
        conn.execute(
            _sql(schema, "DELETE FROM {schema}.reaction_by_session WHERE message_id = ? AND emoji_key = ?"),
            (message_id, key),
        )
    for key in new - old:
        conn.execute(
            _sql(
                schema,
                "INSERT OR REPLACE INTO {schema}.reaction_by_session (message_id, emoji_key, token_id, ts) "
                "VALUES (?, ?, ?, ?)",
            ),
            (message_id, key, token_id, now_ms),
        )


def report(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    message_id: int,
    lease_gen: int,
    outcome: str,
    now_ms: int,
    retry_after_ms: int | None = None,
    reason: str | None = None,
) -> str:
    """A2: итог для журнала — `deleted` (строка снята), `released` (лиз снят, ждёт новая операция UI),
    `paused` (повтор позже), `gave_up` (пятая неудача — строка снята). Нет строки или чужой аккаунт —
    `NotFound`; устаревший `lease_gen` — `StaleLease`.

    Ф15-1 П3: строка сессии, `done` с неизменным `gen` (и не `owner_reacted`) — пометки по операциям; `drop` —
    пометки эмодзи операций `on` снимаются (реакции нет). Выброшенные, сдавшиеся и удалённые строки пометок не
    оставляют."""
    if outcome not in REPORT_OUTCOMES:
        raise ValueError(outcome)
    schema = db_of(message_id)
    row = conn.execute(
        _sql(
            schema,
            "SELECT account_id, gen, leased_gen, lease_gen, attempts, not_before_ms, origin, token_id, "
            "ops_json FROM {schema}.reaction_ack WHERE message_id = ?",
        ),
        (message_id,),
    ).fetchone()
    if row is None or row[0] != account_id:
        raise NotFound(f"reaction_ack {message_id}")
    _acc, gen, leased_gen, row_lease_gen, attempts, not_before_ms, origin, token_id, ops_json = row
    if row_lease_gen != lease_gen:
        raise StaleLease(f"reaction_ack {message_id}: lease_gen {lease_gen} != {row_lease_gen}")
    same_target = leased_gen == gen

    if outcome in ("done", "drop"):
        if same_target:
            if origin == "session":
                ops = _load_ops(ops_json)
                if outcome == "done" and reason not in NO_MARK_REASONS:
                    info = _account(conn, account_id)
                    _apply_marks(
                        conn,
                        schema,
                        message_id,
                        source=info[2] if info is not None else "",
                        ops=ops,
                        token_id=token_id,
                        now_ms=now_ms,
                    )
                elif outcome == "drop":
                    for value, on in ops:
                        if on:
                            conn.execute(
                                _sql(
                                    schema,
                                    "DELETE FROM {schema}.reaction_by_session WHERE message_id = ? "
                                    "AND emoji_key = ?",
                                ),
                                (message_id, _key(value)),
                            )
            conn.execute(
                _sql(schema, "DELETE FROM {schema}.reaction_ack WHERE message_id = ?"), (message_id,)
            )
            return "deleted"
        conn.execute(
            _sql(schema, "UPDATE {schema}.reaction_ack SET lease_until_ms = NULL WHERE message_id = ?"),
            (message_id,),
        )
        return "released"

    attempt = attempts + 1
    pause_until_ms = now_ms + max(retry_after_ms or 0, backoff_ms(attempt))
    if same_target:
        if attempt >= MAX_ATTEMPTS:
            conn.execute(
                _sql(schema, "DELETE FROM {schema}.reaction_ack WHERE message_id = ?"), (message_id,)
            )
            return "gave_up"
        conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.reaction_ack SET attempts = ?, lease_until_ms = NULL, pause_until_ms = ?, "
                "not_before_ms = ? WHERE message_id = ?",
            ),
            (attempt, pause_until_ms, pause_until_ms, message_id),
        )
        return "paused"
    # Попытка была по старым операциям — новым попытку не засчитываем, но паузу источника соблюдаем.
    conn.execute(
        _sql(
            schema,
            "UPDATE {schema}.reaction_ack SET lease_until_ms = NULL, pause_until_ms = ?, not_before_ms = ? "
            "WHERE message_id = ?",
        ),
        (pause_until_ms, max(not_before_ms, pause_until_ms), message_id),
    )
    return "paused"
