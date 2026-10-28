"""Ручки терминала хаба (Ф56-5, docs/architecture.md).

Допуск: кука интерфейса (владелец, и с телефона; пишущие — с CSRF, как все) или ключ агента владельца — bearer ровно
`{term}` (К4, `principal_dependency(term_ok=True)`). Токенам `human`/`human_bot` и прочим bearer — 403.

| ручка | кука | ключ `term` |
| `GET /term` | все терминалы | только свои (К12) |
| `POST /term` | да, до 4 своих | да, до 2 своих |
| `DELETE /term/{id}` | да | 403 |
| `GET /term/stream` | да (один поток на вкладку, К2) | 403 |
| `POST /term/{id}/input`, `/resize` | да | 403 |
| `POST /term/{id}/run` | да (`force` — только кука) | только свои |
| `GET /term/{id}/screen` | да | только свои |
| `GET /term/runs` | да | 403 |

Внутренняя (`/internal/v1`, админ-сокет): `GET /admin/term` — `hub term list` (все терминалы с владельцем).

Чужой для ключа терминал — 404, как несуществующий. Маршруты не пишут SQL: журнал — `term/journal.py`."""

import base64
import binascii
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from ...term import journal
from ...term import stream as stream_mod
from ...term import text as text_mod
from ...term.bridge import TermGone
from ...term.hub import DEFAULT_COLS, DEFAULT_ROWS, TermBusy, TermHub, TermLimit
from ...term.tmux import OWNER_UI, TermInfo, TmuxError, owner_of_token, title_of
from .. import connector_auth
from ..security import TERM_SCOPE, Principal, principal_dependency
from .sync import _SSE_HEADERS, session_checker

_logger = logging.getLogger("hub.term")

public_router = APIRouter(tags=["term"])
internal_router = APIRouter(tags=["term-internal"])

INPUT_MAX = 64 * 1024
SCREEN_LINES_MAX = 2000
_ID_MAX = 10**9
_principal = Depends(principal_dependency(term_ok=True))


def _caller(principal: Principal = _principal) -> Principal:
    if principal.is_ui or principal.has_scope(TERM_SCOPE):
        return principal
    raise HTTPException(403, "forbidden")


def _ui_caller(principal: Principal = _principal) -> Principal:
    if principal.is_ui:
        return principal
    raise HTTPException(403, "forbidden")


_any = Depends(_caller)
_ui = Depends(_ui_caller)


def _hub(request: Request) -> TermHub:
    hub = getattr(request.app.state, "term", None)
    if hub is None:
        raise HTTPException(503, "term_unavailable")
    return hub


def _owner(principal: Principal) -> str:
    return OWNER_UI if principal.is_ui else owner_of_token(principal.token_id)


def _who(principal: Principal) -> str:
    return "ui" if principal.is_ui else owner_of_token(principal.token_id)


def _check_id(term_id: int) -> None:
    if not 1 <= term_id <= _ID_MAX:
        raise HTTPException(404, "term_not_found")


def _view(info: TermInfo, principal: Principal) -> dict:
    data = {
        "id": info.id,
        "title": title_of(info),
        "cols": info.cols,
        "rows": info.rows,
        "created_ts": info.created_ts,
        "alive": True,
        "command": info.command,
    }
    if principal.is_ui:
        data["owner"] = "ui" if info.owner == OWNER_UI else "agent"
    return data


async def _visible(request: Request, principal: Principal, term_id: int) -> TermInfo:
    """Терминал, который этому вызывающему виден: кука — любой, ключ — только свой (К12)."""
    _check_id(term_id)
    info = await _hub(request).info(term_id)
    if info is None or (not principal.is_ui and info.owner != _owner(principal)):
        raise HTTPException(404, "term_not_found")
    return info


@public_router.get("/term")
async def list_terms(request: Request, principal: Principal = _any) -> list[dict]:
    infos = await _hub(request).list()
    if not principal.is_ui:
        mine = _owner(principal)
        infos = [info for info in infos if info.owner == mine]
    return [_view(info, principal) for info in infos]


class CreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cols: StrictInt = Field(DEFAULT_COLS, ge=20, le=500)
    rows: StrictInt = Field(DEFAULT_ROWS, ge=5, le=200)


@public_router.post("/term")
async def create_term(request: Request, body: CreateIn | None = None, principal: Principal = _any) -> dict:
    hub = _hub(request)
    body = body or CreateIn()
    db = request.app.state.db

    async def _alloc() -> int:
        return await db.write(journal.next_term_id)

    try:
        info = await hub.create(owner=_owner(principal), alloc_id=_alloc, cols=body.cols, rows=body.rows)
    except TermLimit:
        raise HTTPException(409, "term_limit") from None
    except (TmuxError, TermGone) as exc:
        _logger.warning("терминал: создать не удалось: %s", exc)
        raise HTTPException(503, "term_failed") from None
    return _view(info, principal)


@public_router.delete("/term/{term_id}")
async def delete_term(term_id: int, request: Request, principal: Principal = _any) -> dict:
    """Кука — любой терминал; ключ агента — только свой (Ф84: аренда терминала на поручение)."""
    await _visible(request, principal, term_id)
    await _hub(request).kill(term_id)
    return {"id": term_id, "closed": True}


@public_router.get("/term/runs")
async def list_runs(
    request: Request, limit: int = Query(50, ge=1, le=journal.LIMIT_MAX), principal: Principal = _ui
) -> list[dict]:
    with request.app.state.db.reader() as conn:
        return journal.list_runs(conn, limit=limit)


@public_router.get("/term/stream")
async def term_stream(
    request: Request, pos: str | None = Query(None, max_length=1024), principal: Principal = _ui
):
    """К2: один поток на вкладку, кадры всех терминалов с `term_id`; `Last-Event-ID` важнее `pos` (К7).
    Закрывается при отзыве куки / `hub remote off` (`session_checker`) и при остановке ядра (К13)."""
    hub = _hub(request)
    raw = request.headers.get("last-event-id")
    try:
        positions = stream_mod.parse_positions(raw if raw is not None else pos)
    except stream_mod.BadPos:
        raise HTTPException(422, "invalid pos") from None
    if hub.full:
        raise HTTPException(429, "слишком много открытых потоков терминала — закрой лишние вкладки хаба")
    still_valid = session_checker(request, principal)

    async def _generate():
        async for frame in stream_mod.stream(hub, positions=positions, still_valid=still_valid):
            yield frame

    return StreamingResponse(_generate(), media_type="text/event-stream", headers=_SSE_HEADERS)


class InputIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_b64: StrictStr = Field(max_length=(INPUT_MAX * 4) // 3 + 4)


@public_router.post("/term/{term_id}/input")
async def term_input(term_id: int, body: InputIn, request: Request, principal: Principal = _any) -> dict:
    """Байты как есть (клавиши xterm). В журнал НЕ пишется — в нём пароли. Ключ агента — только в свой
    терминал (Ф84: ввод в запущенную программу; `/run` пишет журнал — для ответа на приглашение не он)."""
    try:
        data = base64.b64decode(body.data_b64, validate=True)
    except binascii.Error, ValueError:
        raise HTTPException(422, "invalid data_b64") from None
    if not data or len(data) > INPUT_MAX:
        raise HTTPException(422, "invalid data_b64")
    if principal.is_ui:
        _check_id(term_id)
    else:
        await _visible(request, principal, term_id)
    try:
        bridge = await _hub(request).bridge(term_id)
        await bridge.send_bytes(data)
    except TermGone:
        raise HTTPException(404, "term_not_found") from None
    except TmuxError:
        raise HTTPException(503, "term_failed") from None
    return {"ok": True}


class ResizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cols: StrictInt = Field(ge=20, le=500)
    rows: StrictInt = Field(ge=5, le=200)


@public_router.post("/term/{term_id}/resize")
async def term_resize(term_id: int, body: ResizeIn, request: Request, principal: Principal = _ui) -> dict:
    _check_id(term_id)
    try:
        bridge = await _hub(request).bridge(term_id)
        await bridge.resize(body.cols, body.rows)
    except TermGone:
        raise HTTPException(404, "term_not_found") from None
    except TmuxError:
        raise HTTPException(503, "term_failed") from None
    return {"cols": body.cols, "rows": body.rows}


class SourceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["ui_block", "agent"]
    ref: StrictStr | None = Field(None, max_length=journal.REF_MAX)


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: StrictStr = Field(max_length=text_mod.TEXT_MAX)
    source: SourceIn
    force: StrictBool = False


@public_router.post("/term/{term_id}/run")
async def term_run(term_id: int, body: RunIn, request: Request, principal: Principal = _any) -> dict:
    """К8: текст без управляющих символов (422 `control_chars`/`empty`), в панели оболочка и не alt-screen
    (иначе 409 `term_busy`; повтор с `force` — только кука, ключу — 403). Запись в журнал — после вставки."""
    reason = text_mod.check(body.text)
    if reason is not None:
        raise HTTPException(422, reason)
    if body.force and not principal.is_ui:
        raise HTTPException(403, "force_forbidden")
    await _visible(request, principal, term_id)
    try:
        await _hub(request).run(term_id, body.text, force=body.force)
    except TermBusy:
        raise HTTPException(409, "term_busy") from None
    except TermGone:
        raise HTTPException(404, "term_not_found") from None
    except TmuxError:
        raise HTTPException(503, "term_failed") from None
    now_ms = int(request.app.state.clock() * 1000)
    who = _who(principal)
    run_id = await request.app.state.db.write(
        lambda conn: journal.record_run(
            conn,
            now_ms=now_ms,
            term_id=term_id,
            who=who,
            source_kind=body.source.kind,
            source_ref=body.source.ref,
            text=body.text,
        )
    )
    return {"run_id": run_id, "term_id": term_id}


@public_router.get("/term/{term_id}/screen")
async def term_screen(
    term_id: int,
    request: Request,
    lines: int = Query(100, ge=1, le=SCREEN_LINES_MAX),
    principal: Principal = _any,
) -> dict:
    await _visible(request, principal, term_id)
    try:
        bridge = await _hub(request).bridge(term_id)
        screen = await bridge.screen(lines)
    except TermGone:
        raise HTTPException(404, "term_not_found") from None
    except TmuxError:
        raise HTTPException(503, "term_failed") from None
    return {"term_id": term_id, **screen}


@internal_router.get("/admin/term", dependencies=[Depends(connector_auth.admin_dependency())])
async def admin_list_terms(request: Request) -> list[dict]:
    infos = await _hub(request).list()
    return [
        {
            "id": info.id,
            "title": title_of(info),
            "cols": info.cols,
            "rows": info.rows,
            "created_ts": info.created_ts,
            "owner": info.owner,
        }
        for info in infos
    ]
