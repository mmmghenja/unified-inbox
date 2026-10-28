"""Кто отвечает на карточки проекта (Ф66-1, docs/architecture.md главнее
разделов A–G).

Одна таблица на проект: `project.agent_policy_json["card_route"] = {группа: "me"|"agent"|"auto"}`; группы —
`GROUPS` (переехали сюда из `agent_auto.py`). Нет ключа или группы — `me` (новый проект: всё владельцу).

- `me` — карточка владельцу (как карточка «вне канала» до Ф66);
- `agent` — карточку берёт агент владельца сам (канал агента в проекте включён);
- `auto` — «сразу да»: ядро отвечает утвердительно в момент появления карточки тем же кодом, что клик владельца
  (`auto_answer`, раздел C). Только группы `AUTO_GROUPS`; виды без однозначного «да» (`stuck`, `stall`,
  `interrupted` — П10) при `auto` идут как `agent`.
- 23.10 (владелец: «мусорные карточки, которые от агента требуют только принять»): `auto` у «Ответа сессии»
  (`turn`) — ответ сессии без строки «ВЛАДЕЛЬЦУ:» карточки не заводит вовсе (`turn_silent`), ответ со «ВЛАДЕЛЬЦУ:» идёт
  как `agent`.

Карточки ручной сессии хаба — всегда `me`, мимо таблицы (решение 6а/3). Проект 0 (агенты вне проектов) —
`me`.

Ф93-1 (docs/architecture.md): строка «Деньги» (`money`) — не вид, а признак карточки
(`is_money`): ход сессии с денежным «ВЛАДЕЛЬЦУ:», сообщение агента владельцу о тратах, заявка «нужен человек» вида
`payment`, вопрос сессии со словами трат. Такая карточка идёт по строке `money` (только `me` / `agent`, по
умолчанию `me`) мимо строки своего вида; «сразу да» для неё не бывает.

Перенос прежних настроек (A.4, П1, П7, П8) — `migrate` на старте ядра, один раз на проект: нет ключа
`card_route` — вычислить по делегату, «каналу владельца» и выключателю `human_agent` и записать; есть — не трогать.
Старые ключи `card_delegate`, `agent_auto` остаются мёртвыми (П8).

Модуль без зависимостей от очереди и модулей видов на уровне импорта (они зовут его); «сразу да» берёт их
импортом внутри функций. Запись — только внутри `Db.write()`, время — параметром `now_ms`."""

import dataclasses
import json
import re
from typing import NamedTuple

from .. import autopilot as catalog
from . import workday as workday_mod
from .audit import write_audit
from .db import next_seq

POLICY_KEY = "card_route"
ME, AGENT, AUTO = "me", "agent", "auto"
ROUTES = (ME, AGENT, AUTO)

# Группа -> виды карточек очереди (`owner_queue.KINDS`); порядок — порядок строк таблицы в интерфейсе.
GROUPS: dict[str, tuple[str, ...]] = {
    "permission": ("permission",),
    "question": ("question",),
    "turn": ("turn",),
    "human": ("human",),
    "draft": ("draft",),
    "press": ("press",),
    "join": ("join", "leave", "group_op"),  # Ф62-6: группы WhatsApp — рядом со вступлением и выходом
    "requests": ("agent_request", "rule"),
    "states": ("heavy", "paused", "interrupted", "broken", "dead", "stuck", "stall"),
    "reminder": ("reminder",),
    "agent_msg": ("agent_msg",),
    # Ф93-1: признак карточки, а не вид (`is_money`) — видов нет, `GROUP_OF` её не знает.
    "money": (),
}
GROUP_OF: dict[str, str] = {kind: group for group, kinds in GROUPS.items() for kind in kinds}
# A.2: «сразу да» — только здесь; у прочих групп — только `me` / `agent` (иначе 422).
AUTO_GROUPS = ("permission", "draft", "press", "turn", "join", "requests", "states", "reminder")
# П10: виды, у которых нет «да» без текста: при `auto` — как `agent`.
NO_YES_KINDS = frozenset({"stuck", "stall", "interrupted", "turn"})
# Ревью Р6: пауза лимита подписки (и «работа сверх подписки») при `auto` — владельцу: «сразу да» её не снимает.
ME_KINDS = frozenset({"paused"})
# Ф89-1 (П1): подвид карточки повторяемого напоминания — «сразу да» за сессию «сделано» не ставит: при `auto`
# карточка идёт агенту (как `turn`).
REPEAT_SUB = "repeat"
MONEY = "money"

# Группы, которые решал делегат Ф49 (перенос, A.4).
_DELEGATE_GROUPS = ("permission", "question")
_LEGACY_DELEGATE = "card_delegate"
_LEGACY_HEAD = "head_channel"
_LEGACY_AUTO = "agent_auto"
_LEGACY_HUMAN_SWITCH = "human_agent"
_LEGACY_HOLD = "autopilot_hold"
NO_PROJECT_ID = 0  # как `subs.NO_PROJECT_ID` (импорт `subs` здесь дал бы цикл)


class RouteError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _policy_dict(policy_json) -> dict:
    if not policy_json:
        return {}
    try:
        value = json.loads(policy_json)
    except ValueError, TypeError:
        return {}
    return value if isinstance(value, dict) else {}


def of_policy(policy_json) -> dict[str, str]:
    """Все группы с действующим значением: нет ключа, нет группы или кривое значение — `me`; `auto` у группы
    без «сразу да» — `me` (так записать нельзя, но кривую запись не исполняем)."""
    raw = _policy_dict(policy_json).get(POLICY_KEY)
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, str] = {}
    for group in GROUPS:
        value = raw.get(group)
        if value not in ROUTES or (value == AUTO and group not in AUTO_GROUPS):
            value = ME
        out[group] = value
    return out


def table(conn, project_id: int | None) -> dict[str, str]:
    """Таблица проекта; нет проекта (и проект 0) — все `me`."""
    if project_id is None or project_id == NO_PROJECT_ID:
        return of_policy(None)
    row = conn.execute("SELECT agent_policy_json FROM main.project WHERE id = ?", (project_id,)).fetchone()
    return of_policy(row[0] if row is not None else None)


def effective(raw: str, kind: str, sub: str | None = None) -> str:
    """Значение группы -> маршрут вида: `auto` у вида без «да» (П10) — `agent`; у паузы лимита подписки —
    `me` (ревью Р6: таблица лимит подписки не обходит, для этого у владельца)."""
    if raw == AUTO and kind in ME_KINDS:
        return ME
    if raw == AUTO and kind in NO_YES_KINDS:
        return AGENT
    if raw == AUTO and kind == "reminder" and sub == REPEAT_SUB:
        return AGENT
    return raw


# --- денежная карточка (Ф93-1, A.2–A.3) ---------------------------------------------------------------------

# Корни трат (нижний регистр). Голое «₽» — не признак: менеджеры постоянно пишут бюджеты заказов.
_MONEY_ROOTS = re.compile(
    r"оплат|заплат|покуп|купить|купим|купи(?![а-яёa-z])|платн|тариф|пополн|предоплат|списан|сч[её]т на|деньг"
)
_MONEY_SUB = re.compile(r"подписк")
_MONEY_CURRENCY = re.compile(r"₽|руб|\$|€|usd")
_MONEY_NEAR = 40
# Строка «ВЛАДЕЛЬЦУ:» — как её ищет хост (`work.held._OWNER_RE`): без учёта регистра, с жирным markdown.
_OWNER_LINE = re.compile(
    r"^[ \t]*(?:\*\*|__)?[ \t]*владельцу[ \t]*(?:\*\*|__)?[ \t]*:", re.IGNORECASE | re.MULTILINE
)


def money_text(text) -> bool:
    """Текст говорит о тратах: корни `_MONEY_ROOTS` или «подписк…» с валютой не дальше `_MONEY_NEAR`
    символов (иначе ловит «подписку на проект» хаба). Ложное срабатывание — карточка владельцу (безопасная
    сторона)."""
    if not isinstance(text, str) or not text:
        return False
    low = text.lower()
    if _MONEY_ROOTS.search(low):
        return True
    for m in _MONEY_SUB.finditer(low):
        near = low[max(0, m.start() - _MONEY_NEAR) : m.end() + _MONEY_NEAR]
        if _MONEY_CURRENCY.search(near):
            return True
    return False


def turn_money(full) -> bool:
    """Денежный ли ход сессии: по тексту после последнего «ВЛАДЕЛЬЦУ:» ответа, а без «ВЛАДЕЛЬЦУ:» — по последнему
    абзацу (A.3)."""
    if not isinstance(full, str) or not full.strip():
        return False
    marks = list(_OWNER_LINE.finditer(full))
    if marks:
        return money_text(full[marks[-1].end() :])
    paras = [p for p in re.split(r"\n\s*\n", full) if p.strip()]
    return money_text(paras[-1]) if paras else False


def _question_texts(data: dict) -> list[str]:
    """Тексты вопросов и вариантов карточки `question` — из вида карточки (`card_view`: `questions` с
    `question`/`options` строками) или из входа инструмента (`options` — словари с `label`)."""
    out: list[str] = []
    raw = data.get("questions")
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("question"), str):
            out.append(item["question"])
        options = item.get("options")
        for option in options if isinstance(options, list) else []:
            if isinstance(option, str):
                out.append(option)
            elif isinstance(option, dict) and isinstance(option.get("label"), str):
                out.append(option["label"])
    return out


def is_money(kind: str, data) -> bool:
    """Ф93-1 (A.2): денежная ли карточка вида `kind` по её данным: `turn` — `payload["money"] is True`;
    `agent_msg` — признак сообщения `money` (словарь или строка `peer_msgs.Rec`); `human` — заявка вида
    `payment`; `question` — тексты вопросов и вариантов проходят `money_text`. Прочие виды — нет."""
    if kind == "agent_msg":
        value = data.get("money") if isinstance(data, dict) else getattr(data, "money", None)
        return value is True or (isinstance(value, int) and not isinstance(value, bool) and value == 1)
    if not isinstance(data, dict):
        return False
    if kind == "turn":
        return data.get("money") is True
    if kind == "human":
        return data.get("kind") == "payment"
    if kind == "question":
        return any(money_text(t) for t in _question_texts(data))
    return False


def money_of(conn, kind: str, ref: str | None) -> bool:
    """`is_money` по карточке `ref` из базы (для мест, где у маршрута есть только `ref`). Нет строки — нет."""
    if ref is None or kind not in ("turn", "agent_msg", "human", "question"):
        return False
    if kind == "turn":
        row = conn.execute(
            "SELECT payload_json FROM main.owner_item WHERE ref = ? AND kind = 'turn' "
            "ORDER BY id DESC LIMIT 1",
            (ref,),
        ).fetchone()
        return row is not None and is_money(kind, _policy_dict(row[0]))
    _prefix, _sep, number = ref.partition(":")
    if not number.isdigit():
        return False
    if kind == "agent_msg":
        row = conn.execute("SELECT money FROM main.peer_msg WHERE id = ?", (int(number),)).fetchone()
        return row is not None and is_money(kind, {"money": row[0]})
    if kind == "human":
        row = conn.execute("SELECT kind FROM main.human_request WHERE id = ?", (int(number),)).fetchone()
        return row is not None and is_money(kind, {"kind": row[0]})
    from . import held_work  # noqa: PLC0415 — held_work опирается на этот модуль
    from .hub_sessions import SessionError  # noqa: PLC0415

    try:
        request, _rec = held_work._request(conn, int(number))  # noqa: SLF001
    except SessionError:
        return False
    try:
        tool_input = json.loads(request.get("input_json") or "{}")
    except ValueError, TypeError:
        return False
    return isinstance(tool_input, dict) and is_money(kind, tool_input)


def money_route(raw: dict[str, str]) -> str:
    """Значение строки «Деньги» таблицы: только `me` / `agent` (кривое — `me`)."""
    value = raw.get(MONEY)
    return AGENT if value == AGENT else ME


def route_of(conn, project_id: int | None, kind: str, *, sub: str | None = None) -> str:
    """Маршрут вида `kind` проекта по таблице (без правила ручной сессии — его добавляет `for_card`). Вид вне
    групп (поручение `errand`) — `me`."""
    group = GROUP_OF.get(kind)
    if group is None:
        return ME
    return effective(table(conn, project_id)[group], kind, sub)


def turn_silent(conn, project_id: int | None, hub_session_id: int | None) -> bool:
    """23.10: «Ответ сессии» проекта — «сразу да», сессия не ручная: ответ без «ВЛАДЕЛЬЦУ:» карточки не заводит."""
    if manual_session(conn, hub_session_id):
        return False
    return table(conn, project_id)["turn"] == AUTO


OWNER_TURN_HOLD_MS = 30 * 60_000  # 24.10: не ответил за 30 минут — подключается агент


def asked_by_owner(conn, ref: str | None) -> bool:
    """24.10: ход сессии, в который владелец сам написал ей из интерфейса (`by_ui_say` карточки
    `turn`), — ответ на его же вопрос: читает он сам, агенту и «сразу да» карточку не дают. Не ответил за
    `OWNER_TURN_HOLD_MS` — минутный цикл ставит `owner_released` (`release_owner_turns`), и карточка идёт
    по таблице проекта; повторный вопрос сессии (новый ход не по его реплике) — сразу по таблице."""
    if ref is None or not ref.startswith("turn:"):
        return False
    row = conn.execute(
        "SELECT payload_json FROM main.owner_item WHERE ref = ? AND kind = 'turn' ORDER BY id DESC LIMIT 1",
        (ref,),
    ).fetchone()
    if row is None:
        return False
    try:
        payload = json.loads(row[0] or "{}")
    except ValueError:
        return False
    return _held_for_owner(payload)


def _held_for_owner(payload) -> bool:
    if not isinstance(payload, dict):
        return False
    return payload.get("by_ui_say") is True and not payload.get("owner_released")


def release_owner_turns(conn, *, now_ms: int) -> list[str]:
    """Минутный цикл: открытые карточки `turn` по реплике владельца старше `OWNER_TURN_HOLD_MS` — отпустить к
    агенту (`owner_released`, новый `seq` — очередь агента её увидит). Возвращает `ref` отпущенных."""
    released = []
    rows = conn.execute(
        "SELECT id, ref, payload_json FROM main.owner_item WHERE kind = 'turn' AND closed_ts IS NULL "
        "AND created_ts <= ?",
        (now_ms - OWNER_TURN_HOLD_MS,),
    ).fetchall()
    for row_id, ref, raw in rows:
        try:
            payload = json.loads(raw or "{}")
        except ValueError:
            continue
        if not _held_for_owner(payload):
            continue
        payload["owner_released"] = True
        conn.execute(
            "UPDATE main.owner_item SET payload_json = ?, seq = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), next_seq(conn), row_id),
        )
        released.append(ref)
    return released


def manual_session(conn, hub_session_id: int | None) -> bool:
    """Сессия-автор — ручная сессия хаба (её карточки всегда владельцу)."""
    if hub_session_id is None:
        return False
    row = conn.execute("SELECT mode FROM main.hub_session WHERE id = ?", (hub_session_id,)).fetchone()
    return row is not None and row[0] != "auto"


def for_card(
    conn,
    project_id: int | None,
    kind: str,
    hub_session_id: int | None,
    *,
    sub: str | None = None,
    ref: str | None = None,
    money: bool | None = None,
) -> str:
    """Маршрут КАРТОЧКИ: ручная сессия хаба — `me`; иначе — по таблице проекта (`route_of`). Ф76-1 (П7):
    карточка `ref`, чей отпечаток под «всегда меня» проекта (или общим), — `me` (агенту и «сразу да» её не
    дают). Ф93-1 (A.5): денежная карточка (`money`; не передан — `money_of` по `ref`) — по строке «Деньги»
    (`me` / `agent`) мимо строки своего вида."""
    if manual_session(conn, hub_session_id):
        return ME
    if kind == "turn" and asked_by_owner(conn, ref):
        return ME
    if money is None:
        money = money_of(conn, kind, ref)
    if money:
        route = money_route(table(conn, project_id))
    else:
        route = route_of(conn, project_id, kind, sub=sub)
    if route != ME and ref is not None:
        from . import decisions  # noqa: PLC0415 — журнал решений опирается на очередь, та — на этот модуль

        if decisions.always_me_card(conn, project_id, ref):
            return ME
    return route


# --- сессия хаба — автор карточки (ревью Р1) ----------------------------------------------------------------

# Ревью Р1: сессию хаба — автора черновика пишет в журнал ручка, создавшая строку (по условной тройке
# запроса), а не выводит очередь из подписки (у ручной сессии хаба без подписки её нет). Без нового столбца:
# ключ `hub_session` в записи `message_send` / `draft_new` той же транзакции (журнал виден только владельцу).
DRAFT_HUB_KEY = "hub_session"
_DRAFT_AUDIT_ACTIONS = ("message_send", "draft_new")


def hub_of_triple(session) -> int | None:
    """Номер сессии хаба по условной тройке (словарь или модель запроса с `pid`, `proc_start`, `session_id`)
    — и закрытой (у закрытой строка с режимом осталась); внешняя сессия, чужая тройка — `None`."""
    from . import held as held_mod  # noqa: PLC0415 — held тянет subs, тот — этот модуль

    if session is None:
        return None
    if isinstance(session, dict):
        triple = (session.get("pid"), session.get("proc_start"), session.get("session_id"))
    else:
        triple = (
            getattr(session, "pid", None),
            getattr(session, "proc_start", None),
            getattr(session, "session_id", None),
        )
    hub_id = held_mod.held_id(triple[0])
    if hub_id is None or triple != held_mod.triple(hub_id):
        return None
    return hub_id


def creating_hub(conn, session) -> int | None:
    """Ручка, создающая строку черновика: номер НЕЗАКРЫТОЙ сессии хаба запроса (`held.open_id_of`) или `None`
    — значение ключа `DRAFT_HUB_KEY` в её записи журнала."""
    from . import held as held_mod  # noqa: PLC0415

    hub_id = hub_of_triple(session)
    return hub_id if hub_id is not None and held_mod.get_open(conn, hub_id) is not None else None


def draft_hub(conn, outbox_id: int, created_ts: int | None) -> int | None:
    """Сессия хаба — автор черновика по записи журнала, создавшей строку (`DRAFT_HUB_KEY`); нет записи или
    ключа — `None`. Поиск — по индексу `(action, ts)` от времени создания строки."""
    if created_ts is None:
        return None
    for action in _DRAFT_AUDIT_ACTIONS:
        row = conn.execute(
            "SELECT json_extract(detail_json, ?) FROM main.audit "
            "WHERE action = ? AND ts >= ? AND outbox_id = ? ORDER BY ts, id LIMIT 1",
            (f"$.{DRAFT_HUB_KEY}", action, created_ts, outbox_id),
        ).fetchone()
        if row is not None:
            value = row[0]
            return value if isinstance(value, int) and not isinstance(value, bool) else None
    return None


def projects_tables(conn) -> dict[int, dict[str, str]]:
    """`{проект: таблица}` одним проходом (очередь читает таблицу на каждую карточку)."""
    return {
        pid: of_policy(policy)
        for pid, policy in conn.execute("SELECT id, agent_policy_json FROM main.project")
    }


# --- запись (A.6) -------------------------------------------------------------------------------------------


def set_project(conn, project_id: int, changes, *, now_ms: int, audit: dict) -> dict[str, str] | None:
    """Слить `{группа: "me"|"agent"|"auto"}` в таблицу проекта (прочие ключи политики не трогаются).
    Неизвестная группа, кривое значение или `auto` у группы без «сразу да» — 422; нет проекта — `None`.
    Двигает `seq` (очередь агента и интерфейс перечитают маршрут); аудит `card_route` `{project_id, old,
    new}`. Возвращает всю таблицу."""
    if not isinstance(changes, dict) or not changes:
        raise RouteError(422, "card_route: {группа: me|agent|auto}")
    unknown = sorted(set(changes) - set(GROUPS))
    if unknown:
        raise RouteError(422, f"card_route: неизвестная группа {', '.join(map(str, unknown))}")
    for group, value in changes.items():
        if value not in ROUTES:
            raise RouteError(422, f"card_route: {group} — me, agent или auto")
        if value == AUTO and group not in AUTO_GROUPS:
            raise RouteError(422, f"card_route: у группы {group} «сразу да» нет — только me или agent")
    if project_id == NO_PROJECT_ID:
        return None
    row = conn.execute("SELECT agent_policy_json FROM main.project WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        return None
    policy = _policy_dict(row[0])
    old = of_policy(row[0])
    new = {**old, **changes}
    policy[POLICY_KEY] = new
    conn.execute(
        "UPDATE main.project SET agent_policy_json = ? WHERE id = ?",
        (json.dumps(policy, ensure_ascii=False, sort_keys=True), project_id),
    )
    next_seq(conn)
    write_audit(
        conn,
        now_ms=now_ms,
        action="card_route",
        result="ok",
        detail={"project_id": project_id, "old": old, "new": new},
        **audit,
    )
    return new


# --- перенос прежних настроек (A.4, П1, П7, П8) -------------------------------------------------------------


def _raw_hold(value) -> dict:
    """Сырые значения выключателей (без отсева по каталогу: ключ `human_agent` из каталога убран, П1)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def legacy_table(conn, project_id: int, policy: dict, global_hold: dict) -> dict[str, str]:
    """Таблица проекта по прежним настройкам (6а.1, П7):
    - канал проекта был включён = `human_agent` снят для проекта И («головная закрывает проект» ИЛИ есть
      незакрытая сессия хаба проекта с `via_agent=1`);
    - группа `agent`, если (канал включён и `agent_auto[группа]` не `false`) ИЛИ (у проекта назначен делегат
      и группа — разрешения или вопросы) ИЛИ (группа `human`, `human_agent` снят и `agent_auto.human` не
      `false`);
    - иначе `me`; `auto` не ставится никогда."""
    human_free = not catalog.effective(_LEGACY_HUMAN_SWITCH, global_hold, _raw_hold(policy.get(_LEGACY_HOLD)))
    delegate = policy.get(_LEGACY_DELEGATE)
    delegate = delegate if isinstance(delegate, dict) else {}
    head = delegate.get(_LEGACY_HEAD) is True
    assigned = isinstance(delegate.get("sub_id"), int) and not isinstance(delegate.get("sub_id"), bool)
    via = (
        conn.execute(
            "SELECT 1 FROM main.hub_session "
            "WHERE project_id = ? AND via_agent = 1 AND closed_ts IS NULL LIMIT 1",
            (project_id,),
        ).fetchone()
        is not None
    )
    channel = human_free and (head or via)
    auto = policy.get(_LEGACY_AUTO)
    auto = auto if isinstance(auto, dict) else {}
    out: dict[str, str] = {}
    for group in GROUPS:
        takes = auto.get(group) is not False
        agent = (
            (channel and takes)
            or (assigned and group in _DELEGATE_GROUPS)
            or (group == "human" and human_free and takes)
        )
        # Ф93-1: строку «Деньги» перенос не ставит — `me`.
        out[group] = AGENT if agent and group != MONEY else ME
    return out


# Ревью Р4: перенос — один раз на базу; после него проектам без ключа таблица по прежним настройкам не
# считается никогда (новый проект получает «всё мне» при создании — `initial_policy`).
MIGRATED_KEY = "card_route_migrated"


def initial_policy() -> str:
    """Политика нового проекта (ревью Р4): таблица «всё мне» сразу при создании — интерфейс и ядро видят одно
    и то же, перенос по прежним настройкам её не трогает."""
    return json.dumps({POLICY_KEY: of_policy(None)}, ensure_ascii=False, sort_keys=True)


def migrate(conn, *, now_ms: int) -> list[int]:
    """Первый старт ядра с Ф66-1: проектам без ключа `card_route` — таблица по прежним настройкам
    (`legacy_table`), в той же записи (старые ключи остаются, П8); затем флаг `MIGRATED_KEY` в `main.meta`, и
    следующие старты ничего не переносят (ревью Р4: проект, заведённый после выкладки, по мёртвым прежним
    настройкам не пересчитывается). Ключ уже есть — проект не трогается. Аудит `card_route_migrate` на каждый
    перенесённый проект. Возвращает номера перенесённых проектов."""
    if conn.execute("SELECT 1 FROM main.meta WHERE key = ?", (MIGRATED_KEY,)).fetchone() is not None:
        return []
    row = conn.execute("SELECT value FROM main.meta WHERE key = ?", (_LEGACY_HOLD,)).fetchone()
    global_hold = _raw_hold(row[0]) if row is not None else {}
    done: list[int] = []
    for project_id, policy_json in conn.execute(
        "SELECT id, agent_policy_json FROM main.project ORDER BY id"
    ).fetchall():
        if project_id == NO_PROJECT_ID:
            continue
        policy = _policy_dict(policy_json)
        if POLICY_KEY in policy:
            continue
        new = legacy_table(conn, project_id, policy, global_hold)
        policy[POLICY_KEY] = new
        conn.execute(
            "UPDATE main.project SET agent_policy_json = ? WHERE id = ?",
            (json.dumps(policy, ensure_ascii=False, sort_keys=True), project_id),
        )
        write_audit(
            conn,
            now_ms=now_ms,
            actor="core",
            api_token_id=None,
            client_name=None,
            request_id="",
            action="card_route_migrate",
            result="ok",
            detail={"project_id": project_id, "new": new},
        )
        done.append(project_id)
    conn.execute(
        "INSERT INTO main.meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING",
        (MIGRATED_KEY, str(now_ms)),
    )
    if done:
        next_seq(conn)
    return done


# --- «сразу да» (раздел C, П3–П5, П9–П13, П15) -------------------------------------------------------------

AUDIT_ACTION = "card_route_auto"
# Ответ «да» по виду — то же действие, что кнопка владельца (`owner_queue.ANSWERS`). Нет вида — «да» нет.
YES = {
    "permission": "allow",
    "draft": "approve",
    "press": "approve",
    "join": "approve",
    "leave": "approve",
    "group_op": "approve",
    "rule": "approve",
    "agent_request": "approve",
    "reminder": "done",
    "heavy": "wake",
    "broken": "wake",
    "dead": "retry",
}
# Ревью Р6: у паузы лимита подписки (`paused`) «да» нет — таблица лимит подписки не обходит (`ME_KINDS`).
# Карточки состояний, на которые «да» — не чаще одного раза на состояние (П10): ключ — `turns` сессии.
ONCE_KINDS = ("heavy", "broken", "dead")
# Виды, которые «сразу да» берёт минутный проход (`sweep`), а не точка создания строки.
SWEEP_KINDS = frozenset({"reminder", *ONCE_KINDS})
# Ревью Р3: карточка с маршрутом `auto`, появившаяся при закрытом дне проекта, — пометка в журнале
# (`held`, `why = day_closed`); после «Начать день» её берёт минутный проход (не старше `DEFER_MS`).
DAY_CLOSED = "day_closed"
DEFER_MS = 7 * 24 * 60 * 60 * 1000
# Ревью Р7: «пробовали ли» по журналу — не глубже этого срока.
TRIED_MS = 30 * 24 * 60 * 60 * 1000
_HOUR_MS = 60 * 60 * 1000
_DAY_MS = 24 * _HOUR_MS
# Аудит ядра: «сразу да» идёт тем же кодом, что клик владельца, но от имени ядра.
_AUDIT = {"actor": "core", "api_token_id": None, "client_name": None, "request_id": ""}


class Auto(NamedTuple):
    """Итог `auto_answer`: `done` — ответ дан; `session` — сессия хаба, заведённая ответом (заявка агента:
    маршрут после коммита — в реестр хоста, как у клика)."""

    done: bool
    session: object = None


def _system():
    """П15: системный принципал интерфейса — как у названий карточек очереди (`_TITLE_VIEWER`), но без номера
    доступа: ответ ядра пишет аудит, а строки `api_token` с номером 0 нет (внешний ключ)."""
    from .owner_queue import _TITLE_VIEWER  # noqa: PLC0415 — очередь опирается на этот модуль

    return dataclasses.replace(_TITLE_VIEWER, token_id=None)


def _count(conn, since: int, result: str, *, channel_id=None, key: str | None = None, value=None) -> int:
    """Сколько «сразу да» вида `result` записано в аудит после `since` (по беседе или по полю `detail`).
    Отдельно не применяется: пределы общие с авто-отправкой и авто-нажатием (`route_count`)."""
    sql = "SELECT COUNT(*) FROM main.audit WHERE action = ? AND result = ? AND ts > ?"
    params: list = [AUDIT_ACTION, result, since]
    if channel_id is not None:
        sql += " AND channel_id = ?"
        params.append(channel_id)
    if key is not None:
        sql += f" AND json_extract(detail_json, '$.{key}') = ?"
        params.append(value)
    return conn.execute(sql, params).fetchone()[0]


def route_count(conn, result: str, since: int, *, channel_id=None, token_id=None, session_id=None) -> int:
    """Ревью Р5: «сразу да» вида `result` (`draft` / `press`) после `since` — по беседе, по доступу автора или
    по сессии хаба — для общих пределов с авто-отправкой (`autosend._sent_count`, `_session_count`) и
    авто-нажатием (`presses._auto_count`)."""
    if channel_id is not None:
        return _count(conn, since, result, channel_id=channel_id)
    if token_id is not None:
        return _count(conn, since, result, key="token_id", value=token_id)
    if session_id is not None:
        return _count(conn, since, result, key="session_id", value=session_id)
    return 0


def _limits_draft(conn, number: int, hub_id: int | None, now_ms: int) -> tuple[str | None, dict]:
    from . import autosend, bot_guard
    from .db import db_of

    row = conn.execute(
        f"SELECT channel_id, api_token_id FROM {db_of(number)}.outbox WHERE id = ?",  # noqa: S608
        (number,),
    ).fetchone()
    if row is None:
        return "gone", {}
    channel_id, token_id = row
    extra = {"channel_id": channel_id, "token_id": token_id, "outbox_id": number}
    hour = now_ms - _HOUR_MS
    # Ревью Р5: пределы общие с авто-отправкой (список Ф37-1 и выключатель) — счёт обеих в `autosend`.
    if autosend._sent_count(conn, now_ms, channel_id=channel_id) >= autosend.AUTOSEND_PER_HOUR:  # noqa: SLF001
        return "limit_channel", extra
    if token_id is not None and (
        autosend._sent_count(conn, now_ms, token_id=token_id) >= autosend.AUTOSEND_TOKEN_PER_HOUR  # noqa: SLF001
    ):
        return "limit_token", extra
    if hub_id is not None and (
        autosend._session_count(conn, hub_id, hour) >= autosend.AUTOPILOT_SESSION_PER_HOUR  # noqa: SLF001
    ):
        return "limit_session", extra
    hit = bot_guard.outbox_category(conn, number, now_ms=now_ms)
    if hit is not None:
        bot_guard.audit_hit(
            conn, category=hit, channel_id=channel_id, path="route", now_ms=now_ms, outbox_id=number
        )
        return f"guard:{hit}", extra
    return None, extra


def _limits_press(conn, number: int, hub_id: int | None, now_ms: int) -> tuple[str | None, dict]:
    from . import bot_guard, presses

    press = presses.get(conn, number)
    if press is None:
        return "gone", {}
    extra = {"channel_id": press.channel_id, "token_id": press.token_id}
    hour = now_ms - _HOUR_MS
    # Ревью Р5: пределы общие с авто-нажатием (`presses._auto_count` считает и «сразу да»).
    if presses._auto_count(conn, hour, "channel_id", press.channel_id) >= presses.PRESS_PER_HOUR:  # noqa: SLF001
        return "limit_channel", extra
    if press.token_id is not None and (
        presses._auto_count(conn, hour, "token_id", press.token_id) >= presses.PRESS_TOKEN_PER_HOUR  # noqa: SLF001
    ):
        return "limit_token", extra
    if hub_id is not None and (
        presses._auto_count(conn, hour, "autopilot_session", hub_id) >= presses.PRESS_SESSION_PER_HOUR  # noqa: SLF001
    ):
        return "limit_session", extra
    hit = presses._press_guard(conn, press.channel_id, press.message_id, press.text)  # noqa: SLF001
    if hit is not None:
        bot_guard.audit_hit(
            conn, category=hit, channel_id=press.channel_id, path="route", now_ms=now_ms, press_id=press.id
        )
        return f"guard:{hit}", extra
    return None, extra


def _limits_join(conn, number: int, now_ms: int) -> tuple[str | None, dict]:
    from . import join

    req = join.get(conn, number)
    if req is None:
        return "gone", {}
    extra = {"account_id": req.account_id}
    day = now_ms - _DAY_MS
    done = conn.execute(
        "SELECT COUNT(*) FROM main.join_request WHERE account_id = ? AND auto = 1 AND decided_ts > ?",
        (req.account_id, day),
    ).fetchone()[0]
    done += _count(conn, day, "join", key="account_id", value=req.account_id)
    if done >= join.AUTO_DAILY:
        return "limit_daily", extra
    return None, extra


def _limits(conn, loc, now_ms: int) -> tuple[str | None, dict]:
    """Пределы и страховки пути «сразу» (C.3, П4): `(почему держим | None, поля аудита)`. Держим — карточка
    остаётся владельцу. Черновик — 40 в час на беседу, 60 на токен автора и 60 на сессию хаба (константы
    `autosend`, счёт вместе с авто-отправкой — ревью Р5) и страховка беседы с ботом; нажатие — пределы
    `presses` (вместе с авто-нажатием) и `_press_guard`; вступление — `join.AUTO_DAILY` в сутки на
    аккаунт (вместе с автоодобрениями)."""
    if loc.kind == "draft":
        return _limits_draft(conn, loc.number, loc.hub_id, now_ms)
    if loc.kind == "press":
        return _limits_press(conn, loc.number, loc.hub_id, now_ms)
    if loc.kind == "join":
        return _limits_join(conn, loc.number, now_ms)
    return None, {}


def _join_gap(conn, request_id: int, account_id: int, now_ms: int) -> None:
    """Вступление «сразу да» — не чаще `join.AUTO_GAP_MS` на аккаунт, как автоодобрение (П4): срок шага
    отодвигается от прошлого «сразу да» этого аккаунта."""
    from . import join

    row = conn.execute(
        "SELECT MAX(ts) FROM main.audit WHERE action = ? AND result = 'join' "
        "AND json_extract(detail_json, '$.account_id') = ?",
        (AUDIT_ACTION, account_id),
    ).fetchone()
    last = row[0] if row is not None else None
    if last is None:
        return
    req = join.get(conn, request_id)
    due = last + join.AUTO_GAP_MS
    if req is not None and req.not_before_ms < due:
        join._update(conn, request_id, now_ms, not_before_ms=due)  # noqa: SLF001


def _state_key(conn, loc) -> int | None:
    """П10: состояние сессии для «один раз на состояние» — число её законченных ходов (`turns`)."""
    if loc.kind not in ONCE_KINDS or loc.hub_id is None:
        return None
    row = conn.execute("SELECT turns FROM main.hub_session WHERE id = ?", (loc.hub_id,)).fetchone()
    return int(row[0] or 0) if row is not None else None


def _answered_state(conn, kind: str, hub_id: int, turns: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM main.owner_item WHERE kind = ? AND session_id = ? AND closed_by = 'system' "
        "AND json_extract(payload_json, '$.by') = 'auto' "
        "AND json_extract(payload_json, '$.turns') = ? LIMIT 1",
        (kind, hub_id, turns),
    ).fetchone()
    return row is not None


def record(
    conn,
    *,
    ref: str,
    kind: str,
    project_id,
    session_id,
    now_ms: int,
    payload: dict | None = None,
    detail: dict | None = None,
    row_id: int | None = None,
) -> None:
    """История и журнал «сразу да» (П13, C.5): строка `owner_item` (`closed_by='system'`, `payload.by =
    "auto"`) — или пометка уже закрытой строки карточки состояния (`row_id`); аудит `card_route_auto`
    (`result` — вид, `detail.by = "route"`, без текстов). Сессия ни того, ни другого не видит."""
    full = {"by": "auto", "action": YES.get(kind), **(payload or {})}
    if row_id is not None:
        old = conn.execute("SELECT payload_json FROM main.owner_item WHERE id = ?", (row_id,)).fetchone()
        merged = {**_policy_dict(old[0] if old is not None else None), **full}
        conn.execute(
            "UPDATE main.owner_item SET closed_by = 'system', closed_token_id = NULL, payload_json = ?, "
            "seq = ? WHERE id = ?",
            (json.dumps(merged, ensure_ascii=False), next_seq(conn), row_id),
        )
    else:
        conn.execute(
            "INSERT INTO main.owner_item (ref, kind, project_id, session_id, created_ts, closed_ts, "
            "closed_by, payload_json, seq) VALUES (?, ?, ?, ?, ?, ?, 'system', ?, ?)",
            (ref, kind, project_id, session_id, now_ms, now_ms, json.dumps(full, ensure_ascii=False),
             next_seq(conn)),
        )  # fmt: skip
    if full["action"] is not None:
        from . import decisions  # noqa: PLC0415

        decisions.record(conn, ref, decisions.BY_AUTO, full["action"], now_ms=now_ms)  # Ф76-1 (П5)
    extra = dict(detail or {})
    channel_id = extra.pop("channel_id", None)
    outbox_id = extra.pop("outbox_id", None)
    write_audit(
        conn,
        now_ms=now_ms,
        **_AUDIT,
        action=AUDIT_ACTION,
        result=kind,
        channel_id=channel_id,
        outbox_id=outbox_id,
        detail={"by": "route", "ref": ref, "project_id": project_id, "session_id": session_id, **extra},
    )


def _held_audit(conn, ref: str, why: str, extra: dict, now_ms: int) -> None:
    detail = {"by": "route", "ref": ref, "why": why}
    detail.update({k: v for k, v in extra.items() if k not in ("channel_id", "outbox_id")})
    write_audit(
        conn,
        now_ms=now_ms,
        **_AUDIT,
        action=AUDIT_ACTION,
        result="held",
        channel_id=extra.get("channel_id"),
        detail=detail,
    )


def tried(conn, ref: str, turns: int | None = None, *, now_ms: int | None = None) -> bool:
    """Ревью Р7: по карточке `ref` (у карточки состояния — на этом состоянии, `turns`) «сразу да» уже
    отвечало — успешно или с отказом ядра; пометка закрытого дня (`DAY_CLOSED`) — не попытка. Минутный проход
    второй раз не отвечает: после отказа карточка остаётся владельцу."""
    sql = (
        "SELECT 1 FROM main.audit WHERE action = ? AND json_extract(detail_json, '$.ref') = ? "
        "AND COALESCE(json_extract(detail_json, '$.why'), '') != ?"
    )
    params: list = [AUDIT_ACTION, ref, DAY_CLOSED]
    if now_ms is not None:
        sql += " AND ts > ?"
        params.append(now_ms - TRIED_MS)
    if turns is not None:
        sql += " AND json_extract(detail_json, '$.turns') = ?"
        params.append(turns)
    return conn.execute(sql + " LIMIT 1", params).fetchone() is not None


def day_closed(conn, project_id: int | None) -> bool:
    """Ревью Р3: день проекта закрыт (Ф64-1) — «сразу да» по проекту не действует ни в одной группе."""
    return workday_mod.project_closed_ts(conn, project_id) is not None


def defer(conn, ref: str, *, project_id, session_id, now_ms: int) -> None:
    """Ревью Р3: карточка с маршрутом `auto` появилась при закрытом дне — пометка в журнале; после «Начать
    день» её возьмёт минутный проход (`_deferred`). Карточка до того — владельцу, как `me`."""
    _held_audit(conn, ref, DAY_CLOSED, {"project_id": project_id, "session_id": session_id}, now_ms)


def auto_answer(conn, ref: str, *, now_ms: int, waiting=None, presence: list[dict] | None = None) -> Auto:
    """«Сразу да» по карточке `ref`, если её маршрут — `auto` (C.1): тем же вызовом ядра, что клик владельца
    (`owner_queue._execute` с системным принципалом интерфейса, П15), в ТОЙ ЖЕ транзакции, что появление
    карточки. Пределы и страховки (`_limits`) не пройдены, ответ ядра — отказ, или вид без «да» — карточка
    остаётся владельцу (`Auto(False)`; аудит `card_route_auto` / `held`); частичных записей нет (точка
    сохранения). Черновик — `approve_draft` с источником `auto` (без отметки прочтения, П12). Сессия видит то
    же, что после клика (C.4, П4).

    Ревью Р3: день проекта закрыт — не отвечает; карточке, появившейся сейчас (не из минутного прохода), —
    пометка `defer`. Ревью Р7: карточку минутного прохода (`SWEEP_KINDS`), по которой уже отвечали (и с
    отказом), второй раз не трогает."""
    from . import owner_queue as zq  # noqa: PLC0415 — очередь опирается на этот модуль

    try:
        prefix, number = zq.parse_ref(ref)
    except zq.OwnerError:
        return Auto(False)
    loc = zq._locate(conn, prefix, number, now_ms, ref)  # noqa: SLF001
    if loc is None or for_card(conn, loc.project_id, loc.kind, loc.hub_id, sub=loc.sub, ref=ref) != AUTO:
        return Auto(False)
    action = YES.get(loc.kind)
    if action is None:
        return Auto(False)
    if day_closed(conn, loc.project_id):
        if loc.kind not in SWEEP_KINDS:
            defer(conn, ref, project_id=loc.project_id, session_id=loc.hub_id, now_ms=now_ms)
        return Auto(False)
    turns = _state_key(conn, loc)
    if turns is not None and _answered_state(conn, loc.kind, loc.hub_id, turns):
        return Auto(False)
    if loc.kind in SWEEP_KINDS and tried(conn, ref, turns, now_ms=now_ms):
        return Auto(False)
    why, extra = _limits(conn, loc, now_ms)
    if why is not None:
        _held_audit(conn, ref, why, extra, now_ms)
        return Auto(False)
    if turns is not None:
        extra = {**extra, "turns": turns}
    conn.execute("SAVEPOINT card_route_auto")
    try:
        session = None
        if loc.kind == "draft":
            from . import outbox_ops

            outbox_ops.approve_draft(
                conn,
                loc.number,
                principal=_system(),
                text=None,
                send_now=False,
                now_ms=now_ms,
                source="auto",
                audit=dict(_AUDIT),
            )
        else:
            _status, session = zq._execute(  # noqa: SLF001
                conn,
                loc,
                action,
                text=None,
                answers=None,
                send_now=False,
                principal=_system(),
                source="system",
                now_ms=now_ms,
                audit=dict(_AUDIT),
                waiting=waiting,
                presence=presence or [],
                ref=ref,
            )
        if loc.kind == "join":
            _join_gap(conn, loc.number, extra["account_id"], now_ms)
        record(
            conn,
            ref=ref,
            kind=loc.kind,
            project_id=loc.project_id,
            session_id=loc.hub_id,
            now_ms=now_ms,
            payload={"turns": turns} if turns is not None else None,
            detail=extra,
            row_id=loc.number if loc.kind in ("broken", "dead") else None,
        )
    except Exception as exc:  # noqa: BLE001 — любой отказ ядра: карточка остаётся владельцу, записей нет
        conn.execute("ROLLBACK TO card_route_auto")
        conn.execute("RELEASE card_route_auto")
        # Ревью Р7: запись отказа (с `turns` у карточки состояния) — минутный проход её не повторит.
        _held_audit(conn, ref, f"error:{type(exc).__name__}", extra, now_ms)
        return Auto(False)
    conn.execute("RELEASE card_route_auto")
    return Auto(True, session)


def _deferred(conn, now_ms: int) -> list[str]:
    """Ревью Р3: карточки, отложенные закрытым днём (`defer`, не старше `DEFER_MS`), по которым после пометки
    ничего не записано, — в проектах, где день уже открыт."""
    rows = conn.execute(
        "SELECT a.id, json_extract(a.detail_json, '$.ref'), json_extract(a.detail_json, '$.project_id') "
        "FROM main.audit a WHERE a.action = ? AND a.ts > ? AND a.result = 'held' "
        "AND json_extract(a.detail_json, '$.why') = ? ORDER BY a.id",
        (AUDIT_ACTION, now_ms - DEFER_MS, DAY_CLOSED),
    ).fetchall()
    found: list[str] = []
    for mark_id, ref, project_id in rows:
        if not isinstance(ref, str) or ref in found or day_closed(conn, project_id):
            continue
        later = conn.execute(
            "SELECT 1 FROM main.audit WHERE action = ? AND id > ? AND json_extract(detail_json, '$.ref') = ? "
            "LIMIT 1",
            (AUDIT_ACTION, mark_id, ref),
        ).fetchone()
        if later is None:
            found.append(ref)
    return found


def _last_mark(conn, ref: str) -> int | None:
    row = conn.execute(
        "SELECT MAX(id) FROM main.audit WHERE action = ? AND json_extract(detail_json, '$.ref') = ?",
        (AUDIT_ACTION, ref),
    ).fetchone()
    return row[0] if row is not None else None


def sweep(conn, *, now_ms: int, waiting=None) -> list[str]:
    """Минутный цикл ядра (П5, П9, П10): «сразу да» на карточки, которых нет в момент создания строки —
    `heavy` (вычисляется при чтении очереди), открытые `broken`/`dead` и сработавшие напоминания, чей сигнал
    сессии уже дошёл (`signaled_ts`); ревью Р3 — и карточки, отложенные закрытым днём, когда день открылся.
    Ревью Р6: пауза лимита (`paused`) — не здесь. Ревью Р7: каждая карточка (состояние) — один раз, и после
    отказа ядра тоже. Возвращает `ref` отвеченных."""
    from . import held as held_mod  # noqa: PLC0415
    from . import owner_queue as zq  # noqa: PLC0415

    answered: list[str] = []
    release_owner_turns(conn, now_ms=now_ms)
    deferred = _deferred(conn, now_ms)
    if deferred:
        open_refs = {c["ref"] for c in zq._collect(conn, _system(), now_ms=now_ms, presence=[])}  # noqa: SLF001
        for ref in deferred:
            mark = _last_mark(conn, ref)
            auto = auto_answer(conn, ref, now_ms=now_ms, waiting=waiting) if ref in open_refs else Auto(False)
            if auto.done:
                answered.append(ref)
                if auto.session is not None:
                    # Заявка агента «новая сессия»: в реестр хоста, как после клика (здесь — до коммита).
                    held_mod.current().add(auto.session.id)
            elif _last_mark(conn, ref) == mark:
                # Карточка закрыта или маршрут сменился — пометка «не взята», проход её не повторит.
                _held_audit(conn, ref, "gone" if ref not in open_refs else "skipped", {}, now_ms)
    tables = projects_tables(conn)
    if not any(t["states"] == AUTO or t["reminder"] == AUTO for t in tables.values()):
        return answered
    refs = [
        f"reminder:{row[0]}"
        for row in conn.execute(
            # Ф89-1 (П1): повторяемые проход не берёт вовсе — срок ведёт сама сессия.
            "SELECT id FROM main.reminder WHERE status = 'fired' AND signaled_ts IS NOT NULL "
            "AND api_token_id IS NOT NULL AND every IS NULL ORDER BY id"
        ).fetchall()
    ]
    refs += [
        row[0]
        for row in conn.execute(
            "SELECT ref FROM main.owner_item WHERE kind IN ('broken', 'dead') AND closed_ts IS NULL "
            "AND hold_ts IS NULL ORDER BY id"
        ).fetchall()
    ]
    ctx = zq._Ctx(conn)  # noqa: SLF001
    refs += [c["ref"] for c in zq._session_waits(ctx, now_ms, []) if c["kind"] == "heavy"]  # noqa: SLF001
    return answered + [ref for ref in refs if auto_answer(conn, ref, now_ms=now_ms, waiting=waiting).done]


def auto_draft(conn, outbox_id: int, *, now_ms: int, waiting=None) -> bool:
    """Точка «сразу да» черновика (П9): новая строка сессии всё ещё ждёт подтверждения (авто-отправка списка
    владельца или автопилота её не взяла) — ответ по таблице маршрута проекта беседы."""
    from . import outbox as outbox_mod  # noqa: PLC0415

    if outbox_mod.status_of(conn, outbox_id) != "pending_approval":
        return False
    return auto_answer(conn, f"draft:{outbox_id}", now_ms=now_ms, waiting=waiting).done
