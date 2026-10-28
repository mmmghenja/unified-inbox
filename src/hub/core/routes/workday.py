"""Ручки рабочего дня проекта (Ф64-1, docs/architecture.md; контракт — раздел 10).

- публичная (`/api/v1`): `POST /projects/{id}/day` — только кука интерфейса (и с телефона); bearer любого
  скоупа (сессии, агент владельца, бот) — 403.
- внутренняя (`/internal/v1`, админ-сокет): `POST /admin/projects/{id}/day` — `hub project day N open|close`.

Чтение и запись — `core/workday.py` (внутри `Db.write()`; ожидания хоста будит сама запись)."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictBool

from .. import connector_auth
from .. import workday as workday_mod
from ..security import Principal
from .write import ui_only

public_router = APIRouter(tags=["workday"])
internal_router = APIRouter(tags=["workday-internal"])

_admin = Depends(connector_auth.admin_dependency())


class DayIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    open: StrictBool


def _now_ms(request: Request) -> int:
    return int(request.app.state.clock() * 1000)


def _waiting(request: Request):
    waiters = getattr(request.app.state, "sub_waiters", None)
    return waiters.waiting if waiters is not None else None


async def _set(request: Request, project_id: int, body: DayIn, audit: dict) -> dict:
    if project_id <= 0:
        raise HTTPException(404, "not found")
    now_ms = _now_ms(request)
    waiting = _waiting(request)

    def _fn(conn):
        return workday_mod.set_day(conn, project_id, body.open, now_ms=now_ms, audit=audit, waiting=waiting)

    try:
        result = await request.app.state.db.write(_fn)
    except workday_mod.WorkdayError as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    return {"open": result["open"], "ts": result["ts"]}


@public_router.post("/projects/{project_id}/day")
async def post_project_day(
    project_id: int, body: DayIn, request: Request, principal: Principal = ui_only
) -> dict:
    """«Начать день» (`open: true`) / «Закончить день» (`open: false`). 200 `{open, ts}`; повтор того же —
    200 без изменений (`ts` прежний); 404 — нет проекта; 409 `workday_off` — проект не «по времени»."""
    name = getattr(request.state, "client_name", None)
    audit = {
        "actor": principal.kind,
        "api_token_id": principal.token_id,
        "client_name": name if isinstance(name, str) else None,
        "request_id": uuid.uuid4().hex,
    }
    return await _set(request, project_id, body, audit)


@internal_router.post("/admin/projects/{project_id}/day")
async def post_admin_project_day(project_id: int, body: DayIn, request: Request, _: None = _admin) -> dict:
    """`hub project day N open|close` — то же, что публичная ручка."""
    audit = {"actor": "admin", "api_token_id": None, "client_name": None, "request_id": uuid.uuid4().hex}
    return await _set(request, project_id, body, audit)
