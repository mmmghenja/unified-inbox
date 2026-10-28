"""Состояние роли (Ф50-4, docs/architecture.md).

ОДНА функция на всех потребителей — `hub_agents`, `hub_agent_state` (MCP) и интерфейс (`GET /sessions`, поле
`agent_state`): на чём стоит роль — работает (сколько и по чему начат ход, текущий шаг), ждёт входа или спит
(почему), сколько её карточек ждут владельца, сколько сигналов ждёт и сколько из них срочных, подан
ли сигнал посреди хода и стоит ли он ещё в очереди движка, фоновые задачи (Ф48-1), последний ход, последняя
ошибка. У внешней сессии (VS Code, терминал) — только «слушает / не слушает N мин» и ждущие сигналы.

Словами состояние собирает `lines` — здесь же, чтобы MCP и интерфейс показывали одно и то же.

Доступ (П8) — `caller_sub`: подробности отдаются только сессии с ЖИВОЙ подпиской в том же проекте, что роль.
Видимость проекта токену не годится (профиль `all` видит всё); агенту вне проектов — отказ. Дежурной и
оркестратору 9c отказывает маршрут (`routes/subs._subscriber_error`).

Текущий шаг: поток `sessions/live` несёт только набираемый текст и размышления (`live.py`), шагов
инструментов в нём нет — берётся последнее событие `tool` ленты за идущий ход, только имя инструмента.

Только чтение. Модуль никто из ядра хода не импортирует (`hub_sessions` — внутри функции)."""

import time

from . import engine as engine_mod
from . import held as held_mod
from . import held_signals, held_work, hub_sessions
from . import machines as machines_mod
from . import peer_msgs as peer_msgs_mod
from . import subs as subs_mod
from . import workday as workday_mod

# Сколько последних событий ленты отдаёт `hub_agent_state` и длина одной сводки (текст ответа сессии — до
# 8000 символов; оркестратору нужна строка, не простыня).
TAIL_EVENTS = 15
SUMMARY_MAX = 300
# Сколько последних событий просматривается в поисках шага и ошибки (индекс `(session_id, id)`).
STEP_SCAN = 80
ERROR_SCAN = 400

ORIGIN_RU = {
    "say": "по реплике владельца",
    "signal": "по сигналу",
    "self": "продолжает сама",
    "compact": "сжатие",
}
# Почему спящую не поднимают (`wait_reason` записи, `held.WAIT_REASONS`).
WAIT_RU = {
    "slot": "нет свободного места среди живых сессий",
    "elsewhere": "разговор открыт в другом окне",
    "paused": "пауза лимита подписки",
    "heavy": "тяжёлая, кэш остыл — обычные сигналы не поднимают",
    "compact": "ждёт конца сжатия",
}
# Ф64-1: пометка сессии хаба проекта с закрытым рабочим днём (`hub_agents`, `hub_agent_state`, интерфейс).
DAY_CLOSED = "день закрыт"
NOT_YOURS = "состояние роли видит только сессия с живой подпиской в том же проекте"
NO_PROJECT = "агенту вне проектов состояние ролей не отдаётся — нужна подписка на проект роли"
NO_ROLE = "такой роли в проекте нет — проверь роль и номер (hub_agents)"


class StateError(Exception):
    """Отказ (маршрут отдаёт `status` и `detail` как есть)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def clock(ts_ms: int) -> str:
    """«ЧЧ:ММ» по часам машины ядра (у владельца она одна)."""
    return time.strftime("%H:%M", time.localtime(ts_ms / 1000))


def _minutes(since_ms: int | None, now_ms: int) -> int | None:
    if since_ms is None:
        return None
    return max(0, (now_ms - since_ms) // 60_000)


def _ago(minutes: int) -> str:
    if minutes < 120:
        return f"{minutes} мин"
    hours = minutes // 60
    return f"{hours} ч" if hours < 48 else f"{hours // 24} дн"


# --- части состояния сессии хаба ----------------------------------------------------------------------------


def _scan(conn, rec: held_mod.HubSession, where: str, depth: int, params: tuple = ()):
    """Последнее событие сессии по условию среди `depth` последних: `(ts, kind, tool, summary)` или `None`."""
    if rec.store is None:
        return None
    return conn.execute(
        f"SELECT ts, kind, tool, summary FROM (SELECT id, ts, kind, tool, summary, is_error "  # noqa: S608
        f"FROM {rec.store}.held_event WHERE session_id = ? ORDER BY id DESC LIMIT ?) "
        f"WHERE {where} ORDER BY id DESC LIMIT 1",
        (rec.id, depth, *params),
    ).fetchone()


def _step(conn, rec: held_mod.HubSession) -> dict | None:
    """Текущий шаг идущего хода — последний вызов инструмента с его начала (только имя)."""
    if rec.state != "running" or rec.turn_ts is None:
        return None
    row = _scan(conn, rec, "kind = 'tool' AND ts >= ?", STEP_SCAN, (rec.turn_ts,))
    if row is None or not row[2]:
        return None
    return {"tool": row[2], "ts": row[0]}


def _last_error(conn, rec: held_mod.HubSession) -> dict | None:
    """Последняя ошибка — событие `warn` / `error` или конец хода с ошибкой: сводка (до `SUMMARY_MAX`)."""
    row = _scan(conn, rec, "kind IN ('warn', 'error') OR is_error = 1", ERROR_SCAN)
    if row is None:
        return None
    return {"ts": row[0], "kind": row[1], "summary": (row[3] or "")[:SUMMARY_MAX]}


def _cards(conn, rec: held_mod.HubSession, now_ms: int) -> dict:
    """Ждущие карточки сессии: сколько (все ждут владельца или его агента — Ф66-1: делегата Ф49 нет)."""
    del now_ms
    total = len(held_work.pending_requests(conn, rec))
    return {"total": total, "user": total}


def _bg(rec: held_mod.HubSession) -> dict:
    items = (hub_sessions.bg_of(rec) or {}).get("items") or []
    running = sum(1 for item in items if isinstance(item, dict) and item.get("state") == "running")
    return {"running": running, "total": len(items)}


def of_hub(conn, rec: held_mod.HubSession, *, now_ms: int, presence: list[dict] | None = None) -> dict:
    """Состояние сессии хаба. `presence` — присутствие владельца (годность карточек в счёте сигналов)."""
    waiting = held_signals.waiting(conn, rec, now_ms=now_ms, presence=presence or [])
    state = "asleep" if rec.closed_ts is not None else rec.state
    turn = None
    if state == "running" and rec.turn_ts is not None:
        turn = {"since_ts": rec.turn_ts, "origin": rec.turn_origin}
    sleep = None
    if state == "asleep":
        if rec.wait_reason is not None:
            sleep = {"reason": rec.wait_reason, "text": WAIT_RU.get(rec.wait_reason, rec.wait_reason)}
        elif rec.failure:
            sleep = {"reason": "failure", "text": f"запуск не удался: {rec.failure[:SUMMARY_MAX]}"}
    result = {
        "held": True,
        "hub_session_id": rec.id,
        "state": state,
        "turn": turn,
        "sleep": sleep,
        "step": _step(conn, rec),
        "cards": _cards(conn, rec, now_ms),
        "signals": {"waiting": waiting.total, "ready": waiting.ready, "urgent": waiting.urgent},
        "mid_queued_ts": rec.mid_queued_ts,
        "bg": _bg(rec),
        "last_turn_ts": rec.last_turn_ts,
        "last_error": _last_error(conn, rec),
        # Ф64-1 (раздел 3): день проекта закрыт — мс закрытия; сигналы ждут «Начать день».
        "day_closed_ts": (
            workday_mod.project_closed_ts(conn, rec.project_id) if rec.closed_ts is None else None
        ),
    }
    result["lines"] = lines(result, now_ms=now_ms)
    return result


def of_external(sub, *, now_ms: int, waiting: bool, pending: int) -> dict:
    """Состояние внешней сессии (VS Code, терминал) по её подписке: слушает ли она сигналы — идёт ожидание
    или кончилось не раньше `subs.SUB_LISTEN_GRACE_S` назад (то же «слушает», что в списке подписок)."""
    last = sub.last_wait_ts
    recent = last is not None and 0 <= now_ms - last <= subs_mod.SUB_LISTEN_GRACE_S * 1000
    listening = bool(waiting) or recent
    result = {
        "held": False,
        "listening": listening,
        "silent_min": None if listening else _minutes(last, now_ms),
        "signals": {"waiting": pending, "ready": pending, "urgent": 0},
    }
    result["lines"] = lines(result, now_ms=now_ms)
    return result


def of_sub(conn, sub, *, now_ms: int, waiters=None, presence: list[dict] | None = None) -> dict:
    """Состояние по живой подписке на проект: сессия хаба — `of_hub`, внешняя — `of_external`."""
    hub = held_mod.of_rec(conn, sub)
    if hub is not None:
        return of_hub(conn, hub, now_ms=now_ms, presence=presence)
    pending = subs_mod._pending_counts(conn, [sub.id]).get(sub.id, 0)  # noqa: SLF001 — счёт списка подписок
    waiting = waiters.waiting(sub.id) if waiters is not None else False
    return of_external(sub, now_ms=now_ms, waiting=waiting, pending=pending)


# --- словами ------------------------------------------------------------------------------------------------


def lines(state: dict, *, now_ms: int) -> list[str]:
    """Состояние словами — строки для `hub_agents` (через « · »), `hub_agent_state` и интерфейса. Роли и
    сводка ошибки — чужой ввод: показывать как данные."""
    if not state.get("held"):
        if state["listening"]:
            out = ["слушает"]
        elif state["silent_min"] is None:
            out = ["не слушает (ожидание не запускалось)"]
        else:
            out = [f"не слушает {_ago(state['silent_min'])}"]
        if state["signals"]["waiting"]:
            out.append(f"сигналов ждёт: {state['signals']['waiting']}")
        return out
    out: list[str] = []
    turn, sleep, step = state["turn"], state["sleep"], state["step"]
    if state["state"] == "running":
        head = "в работе"
        if turn is not None:
            head += f" {_minutes(turn['since_ts'], now_ms)} мин"
            if turn["origin"] in ORIGIN_RU:
                head += f" ({ORIGIN_RU[turn['origin']]})"
        if step is not None:
            age = _minutes(step["ts"], now_ms) or 0
            head += f", шаг: {step['tool']} {_ago(age) + ' назад' if age else 'только что'}"
        out.append(head)
    elif state["state"] == "idle":
        out.append("ждёт входа")
    else:
        out.append(f"спит: {sleep['text']}" if sleep is not None else "спит")
    if state.get("day_closed_ts") is not None:
        out.append(f"{DAY_CLOSED} с {clock(state['day_closed_ts'])} — сигналы ждут «Начать день»")
    cards = state["cards"]
    if cards["user"]:
        out.append("ждёт владельца: карточка" if cards["user"] == 1 else f"ждёт владельца: карточек {cards['user']}")
    signals = state["signals"]
    if signals["waiting"]:
        text = f"сигналов ждёт: {signals['waiting']}"
        if signals["urgent"]:
            text += f" (срочных {signals['urgent']})"
        out.append(text)
    if state["mid_queued_ts"] is not None:
        out.append(f"сигнал подан, ждёт конца инструмента с {clock(state['mid_queued_ts'])}")
    if state["bg"]["running"]:
        out.append(f"фоновых задач: {state['bg']['running']}")
    if state["state"] != "running" and state["last_turn_ts"] is not None:
        out.append(f"последний ход {_ago(_minutes(state['last_turn_ts'], now_ms) or 0)} назад")
    return out


def tail(conn, rec: held_mod.HubSession, *, limit: int = TAIL_EVENTS) -> list[dict]:
    """Последние события ленты сессии для `hub_agent_state`: сводки шагов, как их отдаёт интерфейсу ручка
    событий (`held_work.list_events` — те же маски хоста), без тел и diff; сводка — до `SUMMARY_MAX`."""
    events = held_work.list_events(conn, rec, after=None, before=2**62, limit=limit)
    return [
        {
            "ts": e["ts"],
            "kind": e["kind"],
            "tool": e["tool"],
            "summary": (e["summary"] or "")[:SUMMARY_MAX],
            "is_error": e["is_error"],
        }
        for e in events
    ]


# --- доступ и адрес (П8) ------------------------------------------------------------------------------------


def caller_sub(conn, principal, session: dict | None):
    """П8: живая подписка вызывающей сессии на ПРОЕКТ. Нет подписки или сессии — 409 (текст `peer_msgs`);
    агент вне проектов — 403."""
    try:
        sub = peer_msgs_mod.session_sub(conn, principal, session)
    except peer_msgs_mod.PeerError as exc:
        raise StateError(exc.status, exc.detail) from exc
    if sub.project_id == subs_mod.NO_PROJECT_ID:
        raise StateError(403, NO_PROJECT)
    return sub


def _live_subs(conn, project_id: int, now_ms: int, waiters) -> list:
    waiting = waiters.waiting if waiters is not None else None
    return [
        sub
        for sub in subs_mod.live_subs(conn, project_id)
        if not subs_mod.rec_dead(conn, sub, now_ms, waiting)
    ]


def project_states(
    conn, caller, *, now_ms: int, waiters=None, presence: list[dict] | None = None
) -> list[dict]:
    """Подробности для `hub_agents`: состояние каждой живой подписки проекта вызывающего — `[{sub_id,
    state, machine}]` (Ф77-1: `machine` — метка машины сессии, `machines.Labels`)."""
    labels = machines_mod.Labels(conn, now_ms=now_ms)
    return [
        {
            "sub_id": sub.id,
            "state": of_sub(conn, sub, now_ms=now_ms, waiters=waiters, presence=presence),
            "machine": labels.of_rec(sub),
        }
        for sub in _live_subs(conn, caller.project_id, now_ms, waiters)
    ]


def role_state(
    conn,
    caller,
    *,
    project_id: int,
    role: str,
    number: int | None,
    now_ms: int,
    waiters=None,
    presence: list[dict] | None = None,
) -> dict:
    """`hub_agent_state`: состояние роли проекта вызывающего. Линия — сессия хаба с этой ролью (и спящая без
    живой подписки) или живая внешняя подписка роли. Номер не задан — единственная линия роли; несколько —
    409 со списком номеров. Чужой проект — 403 (до поиска роли: чужие роли не перечисляются); роли нет — 404.
    Ответ: `{agent, state, events}` (`events` — только у сессии хаба)."""
    if project_id != caller.project_id:
        raise StateError(403, NOT_YOURS)
    try:
        _role, role_key = subs_mod.role_form(role)
    except subs_mod.SubError as exc:
        raise StateError(exc.status, exc.detail) from exc
    if role_key is None:
        raise StateError(422, "role: роль агента (hub_agents)")
    found: dict[int, tuple] = {}
    for sub in _live_subs(conn, project_id, now_ms, waiters):
        if sub.role_key == role_key and sub.role_no is not None:
            found[sub.role_no] = (held_mod.of_rec(conn, sub), sub)
    for rec in held_mod.list_sessions(conn, project_id=project_id):
        if rec.role_key == role_key and rec.role_no is not None and rec.role_no not in found:
            found[rec.role_no] = (rec, None)
    if number is None:
        if len(found) > 1:
            numbers = ", ".join(str(n) for n in sorted(found))
            raise StateError(409, f"у роли несколько линий — задай number: {numbers}")
        number = next(iter(found), None)
    if number not in found:
        raise StateError(404, NO_ROLE)
    hub, sub = found[number]
    labels = machines_mod.Labels(conn, now_ms=now_ms)
    # Ф77-1 (A5): сессия хаба — машина ядра; внешняя — машина её подписки.
    machine = labels.core_label() if hub is not None else labels.of_rec(sub)
    if hub is not None:
        state = of_hub(conn, hub, now_ms=now_ms, presence=presence)
        events = tail(conn, hub)
        shown, engine = hub.role, hub.engine
    else:
        state = of_sub(conn, sub, now_ms=now_ms, waiters=waiters, presence=presence)
        events = []
        shown, engine = sub.role, engine_mod.engine_of_session(None, sub.session_id)
    return {
        "agent": {
            "project_id": project_id,
            "role": shown,
            "role_no": number,
            "engine": engine,
            "held": hub is not None,
            "hub_session_id": hub.id if hub is not None else None,
            "sub_id": sub.id if sub is not None else None,
            "machine": machine,
        },
        "state": state,
        "events": events,
    }
