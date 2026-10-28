"""Ручки выключателей автопилота (Ф41-5, docs/architecture.md, контракт — раздел 12).

- публичные (`/api/v1`): `GET /autopilot`, `PUT /autopilot` и `POST /autopilot/hold-all` — только кука
  интерфейса, с ноутбука и с телефона (слова владельца;
  прежнее «только локальный слушатель» Ф41-5 П14 снято). Поле `local` ответа — честно, через какой слушатель
  пришёл запрос. Bearer любого скоупа (сессии, бот, агент) — 403.
- внутренние (`/internal/v1`, админ-сокет): `GET /admin/autopilot`, `POST /admin/autopilot/hold-all` — для
  `hub autopilot` (авария). Настройки отдельных выключателей в CLI нет намеренно.
- Ф66-1: делегат карточек Ф49 убран вместе с ручками `PUT /autopilot/delegate` и
  `POST /admin/autopilot/delegate`; кому карточки — таблица маршрута проекта (`PATCH /projects/{id}`,
  поле `card_route`).

Маршруты не пишут SQL: чтение и запись — `core/autopilot.py` (запись внутри `Db.write()`; длинные ожидания
хоста будит сама запись)."""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from .. import autopilot as autopilot_mod
from .. import connector_auth
from ..security import Principal, request_is_remote
from .write import ui_only

public_router = APIRouter(tags=["autopilot"])
internal_router = APIRouter(tags=["autopilot-internal"])

_admin = Depends(connector_auth.admin_dependency())


def _now_ms(request: Request) -> int:
    return int(request.app.state.clock() * 1000)


def _state(request: Request, *, local: bool) -> dict:
    with request.app.state.db.reader() as conn:
        return autopilot_mod.state(conn, local=local)


def _ui_audit(request: Request, principal: Principal) -> dict:
    name = getattr(request.state, "client_name", None)
    return {
        "actor": principal.kind,
        "api_token_id": principal.token_id,
        "client_name": name if isinstance(name, str) else None,
        "request_id": uuid.uuid4().hex,
    }


_ADMIN_AUDIT = {"actor": "admin", "api_token_id": None, "client_name": None}


async def _write(request: Request, fn) -> None:
    try:
        await request.app.state.db.write(fn)
    except autopilot_mod.AutopilotError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


class HoldIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["global"] | StrictInt
    key: str = Field(min_length=1, max_length=64)
    hold: StrictBool | None


@public_router.get("/autopilot")
async def get_autopilot(request: Request, principal: Principal = ui_only) -> dict:
    return _state(request, local=not request_is_remote(request))


@public_router.put("/autopilot")
async def put_autopilot(body: HoldIn, request: Request, principal: Principal = ui_only) -> dict:
    now_ms = _now_ms(request)
    audit = _ui_audit(request, principal)
    await _write(
        request,
        lambda conn: autopilot_mod.set_hold(
            conn, body.scope, body.key, body.hold, now_ms=now_ms, audit=audit
        ),
    )
    return _state(request, local=not request_is_remote(request))


@public_router.post("/autopilot/hold-all")
async def post_autopilot_hold_all(request: Request, principal: Principal = ui_only) -> dict:
    now_ms = _now_ms(request)
    audit = _ui_audit(request, principal)
    await _write(request, lambda conn: autopilot_mod.hold_all(conn, now_ms=now_ms, audit=audit))
    return _state(request, local=not request_is_remote(request))


# --- админ-сокет -------------------------------------------------------------------------------------------


@internal_router.get("/admin/autopilot")
async def get_admin_autopilot(request: Request, _: None = _admin) -> dict:
    return _state(request, local=True)


@internal_router.post("/admin/autopilot/hold-all")
async def post_admin_autopilot_hold_all(request: Request, _: None = _admin) -> dict:
    now_ms = _now_ms(request)
    audit = {**_ADMIN_AUDIT, "request_id": uuid.uuid4().hex}
    await _write(request, lambda conn: autopilot_mod.hold_all(conn, now_ms=now_ms, audit=audit))
    return _state(request, local=True)
