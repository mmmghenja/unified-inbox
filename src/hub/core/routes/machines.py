"""Ф77-1 (docs/architecture.md): реестр машин. Маршруты не пишут SQL —
`core/machines.py`.

- `public_router` (`/api/v1`): `GET /machines` — список (любой токен с `read` и кука интерфейса);
  `PATCH /machines/{host}` `{"name": "…"}` — имя машины, только кука интерфейса (bearer — 403 с аудитом
  `ui_only_denied`), пустая строка снимает имя.
- `internal_router` (`/internal/v1`, админ-сокет): `GET /admin/machines`, `PATCH /admin/machines/{host}` —
  для `hub machine list|name`.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .. import connector_auth
from .. import machines as machines_mod
from ..security import Principal, principal_dependency
from .write import ui_only

public_router = APIRouter(tags=["machines"])
internal_router = APIRouter(tags=["machines-admin"])

_read = Depends(principal_dependency("read"))
_admin = Depends(connector_auth.admin_dependency())


class RenameIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Длину и печатность проверяет `machines.clean_name` (422 с текстом); здесь — только грубый предел.
    name: str = Field(max_length=256)


def _now_ms(request: Request) -> int:
    return int(request.app.state.clock() * 1000)


def _list(request: Request) -> dict:
    now_ms = _now_ms(request)
    with request.app.state.db.reader() as conn:
        items = machines_mod.list_all(conn, now_ms=now_ms)
    return {"items": items, "core": machines_mod.core_host()}


async def _rename(request: Request, host: str, body: RenameIn) -> dict:
    now_ms = _now_ms(request)
    try:
        machine = await request.app.state.db.write(
            lambda conn: machines_mod.rename(conn, host, body.name, now_ms=now_ms)
        )
    except machines_mod.MachineError as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    return {"machine": machine}


@public_router.get("/machines")
async def get_machines(request: Request, principal: Principal = _read) -> dict:
    """`{"items": [Machine], "core": host машины ядра}` — машина ядра первой."""
    return _list(request)


@public_router.patch("/machines/{host}")
async def patch_machine(host: str, body: RenameIn, request: Request, principal: Principal = ui_only) -> dict:
    """`{"machine": Machine}`. 404 — нет такой машины; 422 — имя длиннее 32 или с управляющими символами."""
    return await _rename(request, host, body)


@internal_router.get("/admin/machines")
async def get_admin_machines(request: Request, _admin_ok: None = _admin) -> dict:
    return _list(request)


@internal_router.patch("/admin/machines/{host}")
async def patch_admin_machine(host: str, body: RenameIn, request: Request, _admin_ok: None = _admin) -> dict:
    return await _rename(request, host, body)
