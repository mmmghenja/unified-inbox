"""Рабочий день проекта (Ф64-1, docs/architecture.md приоритетнее).

Проект «по времени» (`workday`) владелец открывает и закрывает кнопкой. День закрыт — сон под замком: подписки
сессий хаба проекта живы, сигналы копятся, но хост их не поднимает и не подаёт им сигналы (только реплики владельца
и «Поднять»); разборщик, бот и агент владельца по проекту молчат. Внешних сессий
(VS Code, терминал) правило не касается.

Где лежит: `project.agent_policy_json` — `workday: true` (нет ключа — выкл) и `day: {"open": bool, "ts": мс}`
(нет ключа — открыт). Выключение `workday` открывает день (ключ `day` удаляется).

Пишут только владелец (кука интерфейса) и CLI через админ-сокет (`routes/workday.py`). Запись — только внутри
`Db.write()`; время — параметром `now_ms`."""

import json

from .audit import write_audit
from .db import next_seq

POLICY_KEY = "workday"
DAY_KEY = "day"
# Ф70-1: последний закончившийся сон `{"from": закрытие, "to": открытие}` — окно опоздания `send_at` строк,
# чей срок пришёлся на сон аккаунта, считается от пробуждения (`conn_sleep.last_sleep`).
SLEPT_KEY = "slept"
WORKDAY_OFF = "workday_off"


class WorkdayError(Exception):
    """Отказ (маршрут отдаёт `status` и `detail` как есть)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _policy(policy_json: str | None) -> dict:
    if not policy_json:
        return {}
    try:
        value = json.loads(policy_json)
    except ValueError, TypeError:
        return {}
    return value if isinstance(value, dict) else {}


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def enabled(policy_json: str | None) -> bool:
    """Проект «по времени»: `workday: true`; нет ключа или кривой — нет."""
    return _policy(policy_json).get(POLICY_KEY) is True


def day_of(policy_json: str | None) -> tuple[bool, int | None]:
    """(открыт ли день, время последней смены). Проект не «по времени» — всегда открыт, времени нет; кривая
    запись дня — открыт."""
    policy = _policy(policy_json)
    if policy.get(POLICY_KEY) is not True:
        return True, None
    day = policy.get(DAY_KEY)
    if not isinstance(day, dict):
        return True, None
    ts = _int(day.get("ts"))
    if day.get("open") is False and ts is not None:
        return False, ts
    return True, ts


def closed_ts(policy_json: str | None) -> int | None:
    """Время закрытия дня или `None` (день открыт либо проект не «по времени»)."""
    is_open, ts = day_of(policy_json)
    return None if is_open else ts


def fields(policy_json: str | None) -> dict:
    """Поля выдачи проекта (`GET /projects`, `hub project list`): `workday`, `day_open`, `day_ts`."""
    is_open, ts = day_of(policy_json)
    return {"workday": enabled(policy_json), "day_open": is_open, "day_ts": ts}


def _row(conn, project_id: int | None):
    if project_id is None:
        return None
    return conn.execute("SELECT agent_policy_json FROM main.project WHERE id = ?", (project_id,)).fetchone()


def project_closed_ts(conn, project_id: int | None) -> int | None:
    """Время закрытия дня проекта или `None`."""
    row = _row(conn, project_id)
    return closed_ts(row[0]) if row is not None else None


def closed_projects(conn) -> dict[int, int]:
    """`{проект: время закрытия}` — все проекты с закрытым днём (одним проходом по проектам)."""
    out: dict[int, int] = {}
    for project_id, policy_json in conn.execute(
        "SELECT id, agent_policy_json FROM main.project WHERE agent_policy_json LIKE '%workday%'"
    ):
        ts = closed_ts(policy_json)
        if ts is not None:
            out[project_id] = ts
    return out


def slept_of(policy_json: str | None) -> tuple[int, int] | None:
    """Ф70-1: последний закончившийся сон проекта `(закрытие, открытие)` или `None`."""
    slept = _policy(policy_json).get(SLEPT_KEY)
    if not isinstance(slept, dict):
        return None
    start, end = _int(slept.get("from")), _int(slept.get("to"))
    return (start, end) if start is not None and end is not None and start <= end else None


def _write(conn, project_id: int, policy: dict) -> None:
    conn.execute(
        "UPDATE main.project SET agent_policy_json = ? WHERE id = ?",
        (json.dumps(policy, ensure_ascii=False, sort_keys=True), project_id),
    )


def bump_sessions(conn, project_id: int, now_ms: int) -> None:
    """П12: смена дня — ОДИН `UPDATE` `seq` всех открытых сессий хаба проекта (хост и интерфейс узнают сразу,
    запись «для хоста» несёт `day_closed_ts`)."""
    conn.execute(
        "UPDATE main.hub_session SET seq = ?, updated_ts = ? WHERE project_id = ? AND closed_ts IS NULL",
        (next_seq(conn), now_ms, project_id),
    )


def _on_open(conn, project_id: int, *, now_ms: int) -> None:
    """День открылся (кнопкой или выключением `workday`): ждущие заявки «нужен человек» — новым `seq` боту
    (П9). Ф66-1: делегат карточек Ф49 убран — его срока и служебного «ОТ ВЛАДЕЛЬЦА» делегату больше нет."""
    del now_ms
    from . import human_requests as human_mod  # импорт внутри: модуль заявок читает этот

    human_mod.bump_project(conn, project_id)


def _audit(conn, *, now_ms: int, audit: dict, action: str, result: str, detail: dict) -> None:
    write_audit(conn, now_ms=now_ms, action=action, result=result, detail=detail, **audit)


def _via(audit: dict) -> str:
    return "ui" if audit.get("actor") == "ui" else "cli"


def set_workday(conn, project_id: int, on: bool, *, now_ms: int) -> bool | None:
    """Настройка «проект по времени». Выключение удаляет `day` (день открыт; был закрыт — то же, что
    открытие). Нет проекта — `None`. Аудит пишет маршрут (`project_update` /
    `project_workday`)."""
    row = _row(conn, project_id)
    if row is None:
        return None
    policy = _policy(row[0])
    was_closed = closed_ts(row[0]) is not None
    if was_closed and not on:
        policy[SLEPT_KEY] = {"from": closed_ts(row[0]), "to": now_ms}
    if on:
        policy[POLICY_KEY] = True
    else:
        policy.pop(POLICY_KEY, None)
        policy.pop(DAY_KEY, None)
    _write(conn, project_id, policy)
    if was_closed and not on:
        _on_open(conn, project_id, now_ms=now_ms)
        bump_sessions(conn, project_id, now_ms)
    return bool(on)


def set_day(conn, project_id: int, open_: bool, *, now_ms: int, audit: dict, waiting=None) -> dict:
    """«Начать день» / «Закончить день». Нет проекта — 404; проект не «по времени» — 409 `workday_off`; то же
    состояние — без изменений (`ts` прежний, аудита нет). Ответ — `{open, ts, changed}`.

    Закрытие: только `day` и `seq` сессий. Открытие: заявки боту и `seq` сессий (Ф66-1: служебного «ОТ ВЛАДЕЛЬЦА»
    делегату карточек больше нет — делегат убран). `waiting` — прежний параметр, не читается."""
    row = _row(conn, project_id)
    if row is None:
        raise WorkdayError(404, "not found")
    if not enabled(row[0]):
        raise WorkdayError(409, WORKDAY_OFF)
    is_open, ts = day_of(row[0])
    if is_open == open_:
        # Повтор того же (и «Начать день», когда ключа `day` ещё нет — открыт по умолчанию) ничего не пишет.
        return {"open": is_open, "ts": ts, "changed": False}
    policy = _policy(row[0])
    policy[DAY_KEY] = {"open": bool(open_), "ts": now_ms}
    if open_ and ts is not None:
        policy[SLEPT_KEY] = {"from": ts, "to": now_ms}
    _write(conn, project_id, policy)
    if open_:
        _on_open(conn, project_id, now_ms=now_ms)
    bump_sessions(conn, project_id, now_ms)
    _audit(
        conn,
        now_ms=now_ms,
        audit=audit,
        action="project_day",
        result="open" if open_ else "close",
        detail={"project_id": project_id, "open": bool(open_), "via": _via(audit)},
    )
    return {"open": bool(open_), "ts": now_ms, "changed": True}
