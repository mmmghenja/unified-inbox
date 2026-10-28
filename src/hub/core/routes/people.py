"""Люди (Ф21-1, docs/architecture.md).

Чтение (`GET /people`, `/people/{id}`, `/people/of-contact/{id}`, `/channels/{id}/people`) — скоуп `read`, без
ограничения проектом для самих людей и цифр; беседы карточки и тексты-образцы — в пределах
проектов токена. Правка и предложения склейки — только UI-сессия (`ui_only`: bearer -> 403 с аудитом
`ui_only_denied`), аудит — `write_audit` с id без имён. Маршруты не пишут SQL: всё в `people`.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from .. import people, queries
from ..audit import write_audit
from ..security import Principal, principal_dependency
from .write import _client_name, _new_request_id, _now_ms, ui_only

public_router = APIRouter(tags=["people"])

_read = Depends(principal_dependency("read"))

LIST_LIMIT_MAX = 200
QUERY_MAX = 100


def _not_found() -> HTTPException:
    return HTTPException(404, "not_found")


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


# --- чтение -------------------------------------------------------------------------------------------------


@public_router.get("/people")
async def get_people(
    request: Request,
    q: str | None = Query(None, max_length=QUERY_MAX),
    scope: Literal["known", "all"] = "known",
    limit: int = Query(50, ge=1, le=LIST_LIMIT_MAX),
    offset: int = Query(0, ge=0),
    principal: Principal = _read,
) -> dict:
    with request.app.state.db.reader() as conn:
        return people.list_people(conn, principal, q=q, scope=scope, limit=limit, offset=offset)


@public_router.get("/people/suggestions")
async def get_suggestions(request: Request, principal: Principal = ui_only) -> list[dict]:
    with request.app.state.db.reader() as conn:
        return people.suggestions(conn, principal)


@public_router.get("/people/of-contact/{contact_id}")
async def get_person_of_contact(contact_id: int, request: Request, principal: Principal = _read) -> dict:
    """E2: клик по отправителю. Контакт без человека — человек создаётся сразу (той же проверкой, что B1)."""
    db = request.app.state.db
    try:
        with db.reader() as conn:
            person_id = people.contact_person(conn, contact_id)
        if person_id is None:
            now_ms = _now_ms(request)
            person_id = await db.write(lambda conn: people.ensure_person(conn, contact_id, now_ms))
        with db.reader() as conn:
            return people.card(conn, principal, person_id)
    except people.NotPerson as exc:
        raise HTTPException(404, exc.code) from exc
    except queries.NotFound as exc:
        raise _not_found() from exc


@public_router.get("/people/{person_id}")
async def get_person(person_id: int, request: Request, principal: Principal = _read) -> dict:
    try:
        with request.app.state.db.reader() as conn:
            return people.card(conn, principal, person_id)
    except queries.NotFound as exc:
        raise _not_found() from exc


@public_router.get("/channels/{channel_id}/people")
async def get_channel_people(channel_id: int, request: Request, principal: Principal = _read) -> list[dict]:
    """Участники беседы. Беседа чужая для токена — тот же ответ, что у `GET /channels/{id}`."""
    try:
        with request.app.state.db.reader() as conn:
            return people.channel_people(conn, principal, channel_id, _now_ms(request))
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc


# --- правка (только UI) -------------------------------------------------------------------------------------


class MergeIn(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    into: StrictInt
    from_: list[StrictInt] = Field(alias="from", min_length=1, max_length=50)


class SplitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_ids: list[StrictInt] = Field(min_length=1, max_length=200)


class RenameIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: StrictStr | None


class NotSameIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    a: StrictInt
    b: StrictInt


async def _write(request: Request, fn):
    try:
        return await request.app.state.db.write(fn)
    except people.Invalid as exc:
        raise HTTPException(422, str(exc)) from exc
    except queries.NotFound as exc:
        raise _not_found() from exc


@public_router.post("/people/merge")
async def post_merge(
    body: MergeIn, request: Request, response: Response, principal: Principal = ui_only
) -> dict:
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)

    def _fn(conn):
        people.merge(conn, body.into, body.from_)
        _audit(
            conn,
            request,
            principal,
            request_id,
            now_ms,
            "person_merge",
            {"into": body.into, "from": body.from_},
        )
        return people.card(conn, principal, body.into)

    return await _write(request, _fn)


@public_router.post("/people/split")
async def post_split(
    body: SplitIn, request: Request, response: Response, principal: Principal = ui_only
) -> dict:
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)

    def _fn(conn):
        new_id = people.split(conn, body.contact_ids, now_ms)
        _audit(
            conn,
            request,
            principal,
            request_id,
            now_ms,
            "person_split",
            {"person_id": new_id, "contact_ids": body.contact_ids},
        )
        return people.card(conn, principal, new_id)

    return await _write(request, _fn)


@public_router.patch("/people/{person_id}")
async def patch_person(
    person_id: int, body: RenameIn, request: Request, response: Response, principal: Principal = ui_only
) -> dict:
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)

    def _fn(conn):
        people.rename(conn, person_id, body.name)
        _audit(
            conn,
            request,
            principal,
            request_id,
            now_ms,
            "person_rename",
            {"person_id": person_id, "manual": body.name is not None},
        )
        return people.card(conn, principal, person_id)

    return await _write(request, _fn)


@public_router.post("/people/not-same", status_code=204)
async def post_not_same(
    body: NotSameIn, request: Request, response: Response, principal: Principal = ui_only
):
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)

    def _fn(conn):
        people.mark_not_same(conn, body.a, body.b, now_ms)
        _audit(conn, request, principal, request_id, now_ms, "person_not_same", {"a": body.a, "b": body.b})

    await _write(request, _fn)
    return Response(status_code=204, headers={"X-Request-Id": request_id})
