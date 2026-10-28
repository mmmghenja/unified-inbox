"""Ручки состояния роли (Ф50-4, docs/architecture.md).
Маршруты не пишут SQL: `core/agent_state.py` внутри `db.reader()`.

- `GET /agents/states` — подробности для `hub_agents`: состояние каждой живой подписки проекта вызывающего;
- `GET /agents/state` — `hub_agent_state`: состояние одной роли и хвост ленты её сессии.

Обе — только bearer ПОДПИСАННОЙ сессии (`pid`, `proc_start`, `session_id` — все три) с живой подпиской в том
же проекте, что роль (П8): проверка здесь, а не по видимости проекта токену. Дежурной, оркестратору 9c и
рабочим сессиям — 403 тем же правилом, что подписка (`_subscriber_error`); куке интерфейса — 403 (она берёт
то же состояние полем `agent_state` в `GET /sessions`); агенту вне проектов — 403; сессии без подписки — 409.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .. import agent_state as agent_state_mod
from .. import subs as subs_mod
from ..security import Principal, principal_dependency
from .subs import (
    _PID_MAX,
    _PROC_START_MAX,
    HOST_QUERY,
    _subscriber_error,
    _waiters,
    query_session,
    triple_query,
)
from .write import _now_ms

public_router = APIRouter(tags=["agent-state"])

_read = Depends(principal_dependency("read"))


def _presence(request: Request, now_ms: int) -> list[dict]:
    """Присутствие владельца — годность карточек в счёте ждущих сигналов (как у `routes/sessions`)."""
    store = getattr(request.app.state, "presence", None)
    if store is None:
        return []
    return [{"channel_id": c, "thread_ext_id": t} for c, t in store.items(now_ms)]


def _caller(conn, principal: Principal, pid, proc_start, session_id, host=None):
    error = _subscriber_error(conn, principal)
    if error is not None:
        raise agent_state_mod.StateError(403, error)
    if pid is None:
        raise agent_state_mod.StateError(403, agent_state_mod.NOT_YOURS)
    session = query_session(pid, proc_start, session_id, host)
    return agent_state_mod.caller_sub(conn, principal, session)


@public_router.get("/agents/states")
async def get_agent_states(
    request: Request,
    pid: int | None = Query(None, ge=-_PID_MAX, le=_PID_MAX),
    proc_start: int | None = Query(None, ge=0, le=_PROC_START_MAX),
    session_id: str | None = Query(None, min_length=1, max_length=128),
    host: str | None = HOST_QUERY,
    principal: Principal = _read,
) -> dict:
    """`{"project_id", "items": [{"sub_id", "state"}]}` — состояние живых подписок проекта вызывающего
    (`state` — `agent_state.of_sub`, с `lines` словами). 403 — токену не положено, агент вне проектов; 409 —
    у сессии нет подписки."""
    triple_query(pid, proc_start, session_id)
    now_ms = _now_ms(request)
    waiters = _waiters(request)
    presence = _presence(request, now_ms)
    try:
        with request.app.state.db.reader() as conn:
            caller = _caller(conn, principal, pid, proc_start, session_id, host)
            items = agent_state_mod.project_states(
                conn, caller, now_ms=now_ms, waiters=waiters, presence=presence
            )
    except agent_state_mod.StateError as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    return {"project_id": caller.project_id, "items": items}


@public_router.get("/agents/state")
async def get_agent_state(
    request: Request,
    project_id: int = Query(ge=0, le=2**53),
    role: str = Query(min_length=1, max_length=128),
    number: int | None = Query(None, ge=1, le=subs_mod.ROLE_NO_MAX),
    pid: int | None = Query(None, ge=-_PID_MAX, le=_PID_MAX),
    proc_start: int | None = Query(None, ge=0, le=_PROC_START_MAX),
    session_id: str | None = Query(None, min_length=1, max_length=128),
    host: str | None = HOST_QUERY,
    principal: Principal = _read,
) -> dict:
    """`{"agent", "state", "events"}` — состояние роли `role` (и `number`) проекта `project_id` и последние
    события ленты её сессии хаба (сводки без тел и diff). 403 — токену не положено, чужой проект, агент вне
    проектов; 404 — роли нет; 409 — у сессии нет подписки или у роли несколько линий (нужен `number`)."""
    triple_query(pid, proc_start, session_id)
    now_ms = _now_ms(request)
    waiters = _waiters(request)
    presence = _presence(request, now_ms)
    try:
        with request.app.state.db.reader() as conn:
            caller = _caller(conn, principal, pid, proc_start, session_id, host)
            return agent_state_mod.role_state(
                conn,
                caller,
                project_id=project_id,
                role=role,
                number=number,
                now_ms=now_ms,
                waiters=waiters,
                presence=presence,
            )
    except agent_state_mod.StateError as exc:
        raise HTTPException(exc.status, exc.detail) from exc
