"""Факты о людях (Ф24-1, docs/architecture.md и поправки П2, П4, П5).

- Служба `hub facts run` — bearer со скоупом `facts` (только background-токен на все проекты, ровно `{read,
  facts}`, `security.create_api_token`): `GET /people/facts/due`, `GET /people/{id}/facts/context`,
  `POST /people/{id}/facts/apply`. Чтение — в потоке (`asyncio.to_thread`, своё `db.reader()` внутри
  потока, П4): разбор переписки за год не должен стоять в цикле событий (SSE, приём). Аудит записи — только
  id и числа.
- UI (`ui_only`: bearer -> 403 и аудит `ui_only_denied`): `POST /people/{id}/facts`, `PATCH`/`DELETE
  /people/{id}/facts/{fact_id}` — ответ: карточка человека. Аудит — с id, без текстов.
- Факты в карточке `GET /people/{id}` (скоуп `read`) — поле `facts` (`people.card`).

Маршруты не пишут SQL: всё в `person_facts`.
"""

import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr

from .. import people, person_facts, queries
from ..audit import write_audit
from ..security import Principal, principal_dependency
from .write import _client_name, _new_request_id, _now_ms, ui_only

public_router = APIRouter(tags=["person_facts"])

_facts_scope = principal_dependency("facts")


async def _worker(request: Request) -> Principal:
    """Скоуп `facts` — только bearer службы (UI-сессии он не выдаётся; проверка — на всякий случай)."""
    principal = await _facts_scope(request)
    if principal.is_ui:
        raise HTTPException(403, "forbidden")
    return principal


_worker_dep = Depends(_worker)


def _audit(conn, request: Request, principal: Principal, request_id: str, now_ms: int, action: str, detail):
    write_audit(
        conn,
        now_ms=now_ms,
        actor=principal.kind,
        api_token_id=principal.token_id,
        client_name=_client_name(request),
        request_id=request_id,
        action=action,
        result="ok",
        detail=detail,
    )


# --- служба -------------------------------------------------------------------------------------------------


@public_router.get("/people/facts/due")
async def get_due(
    request: Request,
    limit: int = Query(20, ge=1, le=person_facts.DUE_LIMIT_MAX),
    principal: Principal = _worker_dep,
) -> list[dict]:
    """Созревшие (человек, файл). До первого вызова метки старта нет — записывается, ответ пустой."""
    db = request.app.state.db
    now_ms = _now_ms(request)

    def _read():
        with db.reader() as conn:
            start = person_facts.start_ts(conn)
            if start is None:
                return None
            return person_facts.due(conn, now_ms, limit, start)

    items = await asyncio.to_thread(_read)
    if items is None:
        await db.write(lambda conn: person_facts.ensure_start(conn, now_ms))
        return []
    return items


@public_router.get("/people/{person_id}/facts/context")
async def get_context(
    person_id: int,
    request: Request,
    db: Literal["main", "work"] = Query(...),
    principal: Principal = _worker_dep,
) -> dict:
    database = request.app.state.db
    now_ms = _now_ms(request)

    def _read():
        with database.reader() as conn:
            return person_facts.context(conn, person_id, db, now_ms)

    try:
        return await asyncio.to_thread(_read)
    except queries.NotFound as exc:
        raise HTTPException(404, "not_found") from exc


class UpdateItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: StrictInt
    text: StrictStr


class ApplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: StrictInt
    cursor_ts: StrictInt
    add: list[StrictStr] = []
    update: list[UpdateItem] = []
    delete: list[StrictInt] = []
    failed: StrictBool = False
    project_ids: list[StrictInt] | None = None  # П2: проекты контекста как есть (обязателен при add/update)


@public_router.post("/people/{person_id}/facts/apply")
async def post_apply(
    person_id: int,
    body: ApplyIn,
    request: Request,
    response: Response,
    db: Literal["main", "work"] = Query(...),
    principal: Principal = _worker_dep,
) -> dict:
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    payload = body.model_dump()

    def _fn(conn):
        result = person_facts.apply(conn, person_id, db, payload, now_ms)
        detail = {"person_id": person_id, "db": db}
        detail.update({k: v for k, v in result.items() if isinstance(v, int | bool)})
        _audit(conn, request, principal, request_id, now_ms, "person_facts_apply", detail)
        return result

    try:
        return await request.app.state.db.write(_fn)
    except person_facts.Conflict as exc:
        raise HTTPException(409, "conflict") from exc
    except person_facts.Invalid as exc:
        raise HTTPException(422, str(exc)) from exc
    except queries.NotFound as exc:
        raise HTTPException(404, "not_found") from exc


# --- правка владельца (только UI) --------------------------------------------------------------------------------


class FactIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: StrictStr


async def _ui_write(request: Request, fn):
    try:
        return await request.app.state.db.write(fn)
    except person_facts.Invalid as exc:
        raise HTTPException(422, str(exc)) from exc
    except queries.NotFound as exc:
        raise HTTPException(404, "not_found") from exc


@public_router.post("/people/{person_id}/facts")
async def post_fact(
    person_id: int, body: FactIn, request: Request, response: Response, principal: Principal = ui_only
) -> dict:
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)

    def _fn(conn):
        fact_id = person_facts.add_manual(conn, person_id, body.text, now_ms)
        _audit(
            conn,
            request,
            principal,
            request_id,
            now_ms,
            "person_fact_add",
            {"person_id": person_id, "fact_id": fact_id},
        )
        return people.card(conn, principal, person_id)

    return await _ui_write(request, _fn)


@public_router.patch("/people/{person_id}/facts/{fact_id}")
async def patch_fact(
    person_id: int,
    fact_id: int,
    body: FactIn,
    request: Request,
    response: Response,
    principal: Principal = ui_only,
) -> dict:
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)

    def _fn(conn):
        person_facts.edit(conn, person_id, fact_id, body.text, now_ms)
        _audit(
            conn,
            request,
            principal,
            request_id,
            now_ms,
            "person_fact_edit",
            {"person_id": person_id, "fact_id": fact_id},
        )
        return people.card(conn, principal, person_id)

    return await _ui_write(request, _fn)


@public_router.delete("/people/{person_id}/facts/{fact_id}")
async def delete_fact(
    person_id: int, fact_id: int, request: Request, response: Response, principal: Principal = ui_only
) -> dict:
    """«Убрать» = отклонить: факт пропадает из карточки, служба не добавит его снова."""
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)

    def _fn(conn):
        person_facts.reject(conn, person_id, fact_id, now_ms)
        _audit(
            conn,
            request,
            principal,
            request_id,
            now_ms,
            "person_fact_reject",
            {"person_id": person_id, "fact_id": fact_id},
        )
        return people.card(conn, principal, person_id)

    return await _ui_write(request, _fn)
