"""Ручки учёта нагрузки ноутбука (Ф52-1, docs/architecture.md).

- публичные (`/api/v1`): `GET /load` — любой авторизованный (кука интерфейса, в том числе с телефона, и
  bearer любого профиля, без скоупа: сведения не чувствительные); `POST /load/claims` и
  `DELETE /load/claims/{id}` — те же, но только локальный слушатель (с телефона — только смотреть);
- внутренние (`/internal/v1`, админ-сокет): `GET /admin/load`, `POST /admin/load/claims`,
  `DELETE /admin/load/claims/{id}` — для `hub load` (как `hub engine`, `hub autopilot`).

Хаб ничего не решает и никого не блокирует. Маршруты не пишут SQL: всё в `core/load.py`. Чтение сначала
смотрит отжившие записи читающим соединением и идёт в поток-писатель, только когда есть что удалять:
иначе каждый `GET /load` был бы пустой записью и будил длинные ожидания (`Db.on_commit`).
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr

from .. import connector_auth
from .. import load as load_mod
from ..security import Principal, principal_dependency, request_is_remote

public_router = APIRouter(tags=["load"])
internal_router = APIRouter(tags=["load-internal"])

_authed = Depends(principal_dependency())
_admin = Depends(connector_auth.admin_dependency())


def _now_ms(request: Request) -> int:
    return int(request.app.state.clock() * 1000)


def _require_local(request: Request) -> None:
    if request_is_remote(request):
        raise HTTPException(403, "forbidden: local only")


class ClaimIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    who: StrictStr
    what: StrictStr
    cores: StrictInt
    mem_mb: StrictInt
    pid: StrictInt
    minutes: StrictInt
    host: StrictStr | None = None


async def _show(request: Request) -> dict:
    db = request.app.state.db
    now_ms = _now_ms(request)
    with db.reader() as conn:
        stale = load_mod.stale_ids(conn, now_ms)
        body = load_mod.read(conn, now_ms, skip=stale)
    if stale:
        await db.write(lambda conn: load_mod.purge(conn, now_ms))
    return body


def _owners(request: Request) -> dict[int, int]:
    """Ревью Ф52-1: чья запись — `{id записи: id bearer-токена}`, только в памяти ядра (схема не менялась).
    После перезапуска ядра счёт токена начинается заново; записи CLI и интерфейса сюда не попадают."""
    state = request.app.state
    if not hasattr(state, "load_owners"):
        state.load_owners = {}
    return state.load_owners


async def _add(request: Request, body: ClaimIn, token_id: int | None = None) -> dict:
    """`token_id` — bearer-токен публичной ручки: у него предел живых записей; `None` — без предела."""
    now_ms = _now_ms(request)
    owners = _owners(request)

    def _write(conn) -> dict:  # поток-писатель: записи идут по одной, счёт токена не гоняется
        if token_id is None:
            return load_mod.add(conn, **body.model_dump(), now_ms=now_ms)
        mine = [claim_id for claim_id, owner in owners.items() if owner == token_id]
        claim = load_mod.add(conn, **body.model_dump(), now_ms=now_ms, owned=mine)
        live = load_mod.live_ids(conn)
        for claim_id in [claim_id for claim_id in owners if claim_id not in live]:
            del owners[claim_id]
        owners[claim["id"]] = token_id
        return claim

    try:
        return await request.app.state.db.write(_write)
    except load_mod.LoadError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


async def _done(request: Request, claim_id: int) -> dict:
    removed = await request.app.state.db.write(lambda conn: load_mod.done(conn, claim_id))
    return {"ok": True, "removed": removed}


@public_router.get("/load")
async def get_load(request: Request, principal: Principal = _authed) -> dict:
    return await _show(request)


@public_router.post("/load/claims")
async def post_claim(body: ClaimIn, request: Request, principal: Principal = _authed) -> dict:
    _require_local(request)
    return await _add(request, body, None if principal.is_ui else principal.token_id)


@public_router.delete("/load/claims/{claim_id}")
async def delete_claim(claim_id: int, request: Request, principal: Principal = _authed) -> dict:
    _require_local(request)
    return await _done(request, claim_id)


# --- админ-сокет -------------------------------------------------------------------------------------------


@internal_router.get("/admin/load")
async def get_admin_load(request: Request, _: None = _admin) -> dict:
    return await _show(request)


@internal_router.post("/admin/load/claims")
async def post_admin_claim(body: ClaimIn, request: Request, _: None = _admin) -> dict:
    return await _add(request, body)


@internal_router.delete("/admin/load/claims/{claim_id}")
async def delete_admin_claim(claim_id: int, request: Request, _: None = _admin) -> dict:
    return await _done(request, claim_id)
