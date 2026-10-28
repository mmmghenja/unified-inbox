"""Доступ с телефона (Ф10-7, docs/architecture.md и раздел «API»).

Публичные: `GET /remote` (UI, оба слушателя), `PUT /remote` и `PUT /remote/password` (UI, только локальный).
Внутренние (админ-сокет): `GET /admin/remote`, `POST /admin/remote/off`. Состояние в памяти — общее
`app.state.remote` (П1); выключение меняет его ДО записи (закрыто по умолчанию), прочие изменения — после
коммита. Маршруты не пишут SQL: запись — через `core/remote.py` внутри `Db.write()`.
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from .. import connector_auth
from .. import remote as remote_mod
from ..security import Principal, principal_dependency, request_is_remote

public_router = APIRouter(tags=["remote"])
internal_router = APIRouter(tags=["remote-internal"])

_authed = Depends(principal_dependency())
_admin = Depends(connector_auth.admin_dependency())


def _now_ms(request: Request) -> int:
    return int(request.app.state.clock() * 1000)


def _require_ui(principal: Principal) -> None:
    if not principal.is_ui:
        raise HTTPException(403, "forbidden: ui session required")


def _require_local(request: Request) -> None:
    if request_is_remote(request):
        raise HTTPException(403, "forbidden: local only")


def _status(request: Request) -> dict:
    with request.app.state.db.reader() as conn:
        return remote_mod.status(conn, request.app.state.remote, _now_ms(request))


def _reload_state(request: Request) -> None:
    with request.app.state.db.reader() as conn:
        request.app.state.remote.load(conn)


@public_router.get("/remote")
async def get_remote(request: Request, principal: Principal = _authed) -> dict:
    _require_ui(principal)
    return _status(request)


class RemotePutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    hosts: list[str] | None = None


@public_router.put("/remote")
async def put_remote(body: RemotePutIn, request: Request, principal: Principal = _authed) -> dict:
    _require_ui(principal)
    _require_local(request)
    try:
        hosts = remote_mod.normalize_hosts(body.hosts) if body.hosts is not None else None
    except remote_mod.RemoteConfigError as exc:
        raise HTTPException(422, str(exc)) from exc

    state = request.app.state.remote
    db = request.app.state.db
    now_ms = _now_ms(request)
    if body.enabled is False:
        state.enabled = False  # стоп-кран: удалённый слушатель закрывается раньше записи

    try:
        await db.write(
            lambda conn: remote_mod.update_config(
                conn, enabled=body.enabled, hosts=hosts, now_ms=now_ms, actor="ui"
            )
        )
    except remote_mod.RemoteConfigError as exc:
        _reload_state(request)
        raise HTTPException(422, str(exc)) from exc
    except BaseException:
        if body.enabled is not False:
            # Выключение не откатывается в памяти даже при сбое записи — стоп-кран остаётся нажатым.
            _reload_state(request)
        raise
    _reload_state(request)
    return _status(request)


class PasswordPutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str


@public_router.put("/remote/password")
async def put_remote_password(body: PasswordPutIn, request: Request, principal: Principal = _authed) -> dict:
    _require_ui(principal)
    _require_local(request)
    try:
        remote_mod.check_password_length(body.password)
    except remote_mod.RemoteConfigError as exc:
        raise HTTPException(422, str(exc)) from exc
    stored = await asyncio.to_thread(remote_mod.hash_password, body.password)
    db = request.app.state.db
    now_ms = _now_ms(request)
    revoked = await db.write(lambda conn: remote_mod.set_password(conn, stored=stored, now_ms=now_ms))
    _reload_state(request)
    return {"ok": True, "revoked_remote": revoked}


# --- админ-сокет -------------------------------------------------------------------------------------------


@internal_router.get("/admin/remote")
async def get_admin_remote(request: Request, _: None = _admin) -> dict:
    return _status(request)


@internal_router.post("/admin/remote/off")
async def post_admin_remote_off(request: Request, _: None = _admin) -> dict:
    """`hub remote off` — аварийный путь: выключить и отозвать удалённые сессии. П1: состояние в памяти —
    общее с публичным приложением, поэтому вход паролем закрывается сразу, без рестарта."""
    state = request.app.state.remote
    state.enabled = False
    db = request.app.state.db
    now_ms = _now_ms(request)
    # Сбой записи не откатывает выключение в памяти: стоп-кран остаётся нажатым до рестарта.
    revoked = await db.write(lambda conn: remote_mod.turn_off(conn, now_ms=now_ms, actor="admin"))
    _reload_state(request)
    return {"revoked_remote": revoked}
