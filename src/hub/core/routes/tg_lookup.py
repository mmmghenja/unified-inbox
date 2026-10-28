"""Ручки поиска каналов и групп Telegram и чтения публичных без вступления (Ф29-1,
docs/architecture.md).

Публичные: `POST /api/v1/tg/search`, `POST /api/v1/tg/peek` — скоуп `draft`, запреты `check_attach` (только
интерактивная сессия: не UI, не дежурная, не оркестратор); аккаунт — как у `hub_join`; аудит одной строкой без
строки поиска. Внутренние (сокет коннектора): `POST /lookup/next` (long-poll, 204 — пусто), `POST
/lookup/result`. Посредник — `core/tg_lookup.TgLookup` в `app.state.tg_lookup`; БД — только чтение и аудит.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from .. import connector_auth
from .. import join as join_mod
from .. import tg_lookup as lookup_mod
from ..audit import write_audit
from ..connector_auth import ConnectorPrincipal
from ..security import Principal, principal_dependency
from .write import _client_name, _new_request_id, _now_ms

public_router = APIRouter(tags=["tg-lookup"])
internal_router = APIRouter(tags=["internal-tg-lookup"])

_draft = Depends(principal_dependency("draft"))
_connector = Depends(connector_auth.connector_dependency())

_REASON_MAX = 200


class SearchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: StrictStr = Field(max_length=256)
    account_id: StrictInt | None = Field(default=None, ge=1, le=2**53)
    limit: StrictInt | None = None


class PeekIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: StrictStr = Field(max_length=join_mod.TARGET_INPUT_MAX)
    account_id: StrictInt | None = Field(default=None, ge=1, le=2**53)
    limit: StrictInt | None = None
    before: StrictInt | None = Field(default=None, ge=1, le=2**53)


def _limit(value: int | None, *, default: int, top: int) -> int:
    if value is None:
        return default
    if not 1 <= value <= top:
        raise lookup_mod.LookupFailed(422, f"limit: от 1 до {top}", reason="invalid")
    return value


async def _audit(
    request: Request,
    principal: Principal,
    *,
    action: str,
    result: str,
    request_id: str,
    account_id: int | None,
    detail: dict | None = None,
) -> None:
    now_ms = _now_ms(request)
    client_name = _client_name(request)

    def _fn(conn):
        write_audit(
            conn,
            now_ms=now_ms,
            actor=principal.kind,
            api_token_id=principal.token_id,
            client_name=client_name,
            request_id=request_id,
            action=action,
            result=result,
            detail={"account_id": account_id, **(detail or {})},
        )

    await request.app.state.db.write(_fn)


async def _ask(
    request: Request, principal: Principal, account_id: int | None, kind: str, params: dict, held: list
):
    """Общий путь: проверки -> заявка -> ответ коннектора (`LookupFailed` — отказ). Выбранный аккаунт —
    в `held` (аудит видит его и при отказе)."""
    now_ms = _now_ms(request)
    with request.app.state.db.reader() as conn:
        picked = lookup_mod.prepare(conn, principal, account_id, now_ms=now_ms)
    held.append(picked)
    lookup: lookup_mod.TgLookup = request.app.state.tg_lookup
    return await lookup.wait(lookup.submit(picked, kind, params, now_ms=now_ms))


@public_router.post("/tg/search")
async def post_tg_search(
    body: SearchIn, request: Request, response: Response, principal: Principal = _draft
) -> dict:
    """A2: `{account_id, results: [...]}` — каналы и группы по слову (C2), у известных хабу — `channel_id`."""
    request_id = _new_request_id(response)
    held: list[int] = []
    try:
        query = body.query.strip()
        if not lookup_mod.SEARCH_QUERY_MIN <= len(query) <= lookup_mod.SEARCH_QUERY_MAX:
            raise lookup_mod.LookupFailed(
                422,
                f"query: от {lookup_mod.SEARCH_QUERY_MIN} до {lookup_mod.SEARCH_QUERY_MAX} символов",
                reason="invalid",
            )
        limit = _limit(body.limit, default=lookup_mod.SEARCH_LIMIT_DEFAULT, top=lookup_mod.SEARCH_LIMIT_MAX)
        payload = await _ask(
            request, principal, body.account_id, "search", {"query": query, "limit": limit}, held
        )
        with request.app.state.db.reader() as conn:
            out = lookup_mod.search_response(conn, held[0], payload)
    except lookup_mod.LookupFailed as exc:
        account_id = held[0] if held else body.account_id
        await _audit(
            request,
            principal,
            action="tg_search",
            result=exc.reason,
            request_id=request_id,
            account_id=account_id,
        )
        raise HTTPException(exc.status, exc.text) from exc
    await _audit(
        request,
        principal,
        action="tg_search",
        result="ok",
        request_id=request_id,
        account_id=held[0],
        detail={"count": len(out["results"])},
    )
    return out


@public_router.post("/tg/peek")
async def post_tg_peek(
    body: PeekIn, request: Request, response: Response, principal: Principal = _draft
) -> dict:
    """A2: `{account_id, chat: {...}, messages: [...], next_before}` — последние сообщения публичного
    канала или группы без вступления (C3); беседа уже в хабе — `chat.channel_id`."""
    request_id = _new_request_id(response)
    held: list[int] = []
    target: str | None = None
    try:
        try:
            target_kind, target = join_mod.parse_target(body.target)
        except ValueError as exc:
            raise lookup_mod.LookupFailed(422, str(exc), reason="invalid") from exc
        if target_kind != "username":
            target = None
            raise lookup_mod.LookupFailed(
                422, "по ссылке-приглашению читать нельзя — только вступить (hub_join)", reason="invite"
            )
        limit = _limit(body.limit, default=lookup_mod.PEEK_LIMIT_DEFAULT, top=lookup_mod.PEEK_LIMIT_MAX)
        params = {"username": target, "limit": limit, "before": body.before}
        payload = await _ask(request, principal, body.account_id, "peek", params, held)
        with request.app.state.db.reader() as conn:
            out = lookup_mod.peek_response(conn, held[0], payload)
    except lookup_mod.LookupFailed as exc:
        account_id = held[0] if held else body.account_id
        await _audit(
            request,
            principal,
            action="tg_peek",
            result=exc.reason,
            request_id=request_id,
            account_id=account_id,
            detail={"target": target},
        )
        raise HTTPException(exc.status, exc.text) from exc
    await _audit(
        request,
        principal,
        action="tg_peek",
        result="ok",
        request_id=request_id,
        account_id=held[0],
        detail={"target": target, "count": len(out["messages"])},
    )
    return out


# --- внутренние (сокет коннектора) -------------------------------------------------------------------------


class NextIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_s: float = Field(default=lookup_mod.NEXT_MAX_WAIT_S, ge=0, le=600)


@internal_router.post("/lookup/next", response_model=None)
async def lookup_next(
    body: NextIn, request: Request, principal: ConnectorPrincipal = _connector
) -> dict | Response:
    """Long-poll: одна заявка `{id, kind: search|peek|resolve, params}` своего аккаунта или 204."""
    lookup: lookup_mod.TgLookup = request.app.state.tg_lookup
    item = await lookup.next(principal.account_id, body.timeout_s, lambda: _now_ms(request))
    if item is None:
        return Response(status_code=204)
    return item


class ResultIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: StrictInt
    outcome: Literal["ok", "error"]
    payload: dict | None = None
    reason: str | None = Field(default=None, max_length=_REASON_MAX)
    retry_after_s: float | None = Field(default=None, ge=0, le=86_400)


@internal_router.post("/lookup/result")
async def lookup_result(body: ResultIn, request: Request, principal: ConnectorPrincipal = _connector) -> dict:
    """Ответ на выданную заявку. Чужая, неизвестная или уже снятая по таймауту — 404."""
    lookup: lookup_mod.TgLookup = request.app.state.tg_lookup
    try:
        lookup.result(
            principal.account_id,
            body.id,
            outcome=body.outcome,
            payload=body.payload,
            reason=body.reason,
            retry_after_s=body.retry_after_s,
            now_ms=_now_ms(request),
        )
    except lookup_mod.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    return {"ok": True}
