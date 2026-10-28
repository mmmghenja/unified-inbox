"""Ручки правки и «удалить у всех» своих сообщений (Ф14-8, docs/architecture.md и поправки
П1, П5).

Публичные — только UI-сессия (`ui_only`: bearer -> 403 с аудитом `ui_only_denied`), CSRF — middleware:
`POST /api/v1/messages/{id}/edit`, `POST /api/v1/messages/revoke`, `DELETE /api/v1/messages/{id}/op`.
(`DELETE /api/v1/messages/{id}` — «удалить у себя» Ф8-4, здесь не трогается.) Внутренние (сокет коннектора,
копия `routes/reactions.py`): `GET /msg-ops/wait`, `POST /msg-ops/lease`, `POST /msg-ops/{message_id}/report`.
Маршруты не пишут SQL: чтение и запись — `core/msg_ops.py`. Аудит — без текста сообщения.
"""

import asyncio
import contextlib
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from .. import connector_auth, queries
from .. import msg_ops as msg_ops_mod
from ..audit import write_audit
from ..connector_auth import ConnectorPrincipal
from ..security import Principal
from .write import ui_only

public_router = APIRouter(tags=["msg-ops"])
internal_router = APIRouter(tags=["internal-msg-ops"])

_connector = Depends(connector_auth.connector_dependency())

_MAX_WAIT_S = 30.0
# Нижняя граница одного ожидания до ближайшего срока (как `routes/reactions.py`).
_MIN_DUE_WAIT_S = 0.05
_MAX_RETRY_AFTER_MS = 86_400_000
# Потолок тела правки до проверки лимита источника (лимит — в ядре, 422 `too_long`): защита от огромного тела.
_MAX_TEXT_LEN = 20_000


def _now_ms(request: Request) -> int:
    return int(request.app.state.clock() * 1000)


def _client_name(request: Request) -> str | None:
    name = request.headers.get("x-hub-client")
    return None if name is None else name[:64]


def _conflict(exc: Exception) -> HTTPException:
    """409/422/404 по классу ошибки очереди (одно место для трёх ручек)."""
    if isinstance(exc, queries.NotFound | msg_ops_mod.NotFound):
        return HTTPException(404, "not found")
    if isinstance(exc, msg_ops_mod.Unsupported):
        return HTTPException(409, "unsupported")
    if isinstance(exc, msg_ops_mod.NotEditable):
        return HTTPException(409, "not_editable")
    if isinstance(exc, msg_ops_mod.DeletePending):
        return HTTPException(409, "delete_pending")
    if isinstance(exc, msg_ops_mod.InFlight):
        return HTTPException(409, "in_flight")
    if isinstance(exc, msg_ops_mod.InvalidText):
        if exc.code == "too_long":
            return HTTPException(422, f"too_long: не больше {exc.limit} символов")
        return HTTPException(422, "empty")
    if isinstance(exc, msg_ops_mod.InvalidBatch):
        return HTTPException(422, f"invalid: {exc}")
    raise exc


_ERRORS = (queries.NotFound, msg_ops_mod.MsgOpError)


# --- публичные ручки (A3) ---------------------------------------------------------------------------------


class EditIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: StrictStr = Field(max_length=_MAX_TEXT_LEN)


@public_router.post("/messages/{message_id}/edit")
async def edit_message(
    message_id: int, body: EditIn, request: Request, response: Response, principal: Principal = ui_only
) -> dict:
    """Ответ `{"status": "queued"|"unchanged", "op": …}`. 404 — не видно или скрыто; 409 `not_editable` /
    `unsupported` / `delete_pending`; 422 `empty` / `too_long` (с лимитом в сообщении). Аудит `msg_edit`
    (id, длина текста)."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = uuid.uuid4().hex
    response.headers["X-Request-Id"] = request_id
    client_name = _client_name(request)

    def _fn(conn):
        queries.message_visible(conn, principal, message_id)
        result = msg_ops_mod.enqueue_edit(conn, message_id, body.text, now_ms)
        if result.status == "queued":
            write_audit(
                conn,
                now_ms=now_ms,
                actor=principal.kind,
                api_token_id=principal.token_id,
                client_name=client_name,
                request_id=request_id,
                action="msg_edit",
                result="queued",
                channel_id=result.channel_id,
                detail={"message_id": message_id, "len": len(body.text)},
            )
        return {"status": result.status, "op": msg_ops_mod.op_of(conn, message_id)}

    try:
        return await db.write(_fn)
    except _ERRORS as exc:
        raise _conflict(exc) from exc


class RevokeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_ids: list[StrictInt] = Field(min_length=1, max_length=msg_ops_mod.MAX_REVOKE_IDS)


@public_router.post("/messages/revoke")
async def revoke_messages(
    body: RevokeIn, request: Request, response: Response, principal: Principal = ui_only
) -> dict:
    """«Удалить у всех»: 1..10 id одной беседы (альбом), всё-или-ничего. Ответ `{"status": "queued", "ops":
    {id: op}}`. Коды — как у правки, плюс 422 — разные беседы / пустой список / больше 10. Аудит
    `msg_revoke` (ids)."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = uuid.uuid4().hex
    response.headers["X-Request-Id"] = request_id
    client_name = _client_name(request)

    def _fn(conn):
        for message_id in body.message_ids:
            queries.message_visible(conn, principal, message_id)
        channel_id = msg_ops_mod.enqueue_delete(conn, body.message_ids, now_ms)
        ids = list(dict.fromkeys(body.message_ids))
        write_audit(
            conn,
            now_ms=now_ms,
            actor=principal.kind,
            api_token_id=principal.token_id,
            client_name=client_name,
            request_id=request_id,
            action="msg_revoke",
            result="queued",
            channel_id=channel_id,
            detail={"message_ids": ids},
        )
        return {"status": "queued", "ops": {str(mid): msg_ops_mod.op_of(conn, mid) for mid in ids}}

    try:
        return await db.write(_fn)
    except _ERRORS as exc:
        raise _conflict(exc) from exc


@public_router.delete("/messages/{message_id}/op", status_code=204)
async def cancel_op(message_id: int, request: Request, principal: Principal = ui_only) -> Response:
    """«Отменить» / «Скрыть»: 204; 404 — строки нет (или сообщение не видно); 409 `in_flight` — задание у
    коннектора. Аудит `msg_op_cancel` (id)."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = uuid.uuid4().hex
    client_name = _client_name(request)

    def _fn(conn):
        queries.message_visible(conn, principal, message_id)
        channel_id = msg_ops_mod.cancel(conn, message_id, now_ms)
        write_audit(
            conn,
            now_ms=now_ms,
            actor=principal.kind,
            api_token_id=principal.token_id,
            client_name=client_name,
            request_id=request_id,
            action="msg_op_cancel",
            result="ok",
            channel_id=channel_id,
            detail={"message_id": message_id},
        )

    try:
        await db.write(_fn)
    except _ERRORS as exc:
        raise _conflict(exc) from exc
    return Response(status_code=204, headers={"X-Request-Id": request_id})


# --- внутренние ручки (сокет коннектора) -------------------------------------------------------------------


def _state(request: Request, principal: ConnectorPrincipal) -> tuple[bool, int | None, int]:
    db = request.app.state.db
    now_ms = _now_ms(request)
    with db.reader() as conn:
        ready, due = msg_ops_mod.wait_state(conn, account_id=principal.account_id, now_ms=now_ms)
    return ready, due, now_ms


@internal_router.get("/msg-ops/wait")
async def msg_ops_wait(
    request: Request,
    timeout_s: float = Query(25.0, ge=0),
    principal: ConnectorPrincipal = _connector,
) -> dict:
    """Long-poll, как `/reactions/wait`: «готово» = `lease` выдал бы хоть что-то своему аккаунту; ожидание
    ограничено ещё и временем до ближайшего срока строки."""
    notifier = request.app.state.notifier
    loop = asyncio.get_running_loop()
    if notifier.loop is None:
        notifier.attach(loop)
    timeout_s = min(timeout_s, _MAX_WAIT_S)
    deadline = loop.time() + timeout_s

    gen = notifier.gen
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(timeout_s):
            while True:
                ready, due_ms, now_ms = _state(request, principal)
                if ready:
                    return {"ready": True}
                wait_s = deadline - loop.time()
                if wait_s <= 0:
                    break
                if due_ms is not None:
                    wait_s = min(wait_s, max((due_ms - now_ms) / 1000, _MIN_DUE_WAIT_S))
                gen = await notifier.wait(gen, wait_s)
                if notifier.closed:
                    break  # Ф31-1 П5: ядро останавливается — пусто, ничего не выдавая
    return {"ready": False}


class MsgOpsLeaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int | None = Field(default=None, ge=1)


@internal_router.post("/msg-ops/lease")
async def msg_ops_lease(
    body: MsgOpsLeaseIn, request: Request, principal: ConnectorPrincipal = _connector
) -> list[dict]:
    """Задания `{message_id, gen, lease_gen, op, text (edit), channel: {ext_id, kind, parent_ext_id},
    message_ext_id, thread_ext_id, message_kind, has_files}`, по умолчанию по одному."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    limit = body.limit if body.limit is not None else 1
    return await db.write(
        lambda conn: msg_ops_mod.lease(conn, account_id=principal.account_id, now_ms=now_ms, limit=limit)
    )


class MsgOpReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_gen: int
    outcome: Literal["done", "retry", "fail"]
    code: Literal["expired", "forbidden", "not_found", "too_long", "other"] | None = None
    detail: str | None = Field(default=None, max_length=1000)
    retry_after_ms: int | None = Field(default=None, ge=0, le=_MAX_RETRY_AFTER_MS)


@internal_router.post("/msg-ops/{message_id}/report")
async def msg_ops_report(
    message_id: int, body: MsgOpReportIn, request: Request, principal: ConnectorPrincipal = _connector
) -> dict:
    """Чужое сообщение/нет строки — 404, устаревший `lease_gen` — 409; `fail` без `code` — 422. Неудача
    (`failed`/`gave_up`) — аудит `msg_op_failed` (id, код)."""
    if body.outcome == "fail" and body.code is None:
        raise HTTPException(422, "code required")
    db = request.app.state.db
    now_ms = _now_ms(request)

    def _fn(conn):
        result = msg_ops_mod.report(
            conn,
            account_id=principal.account_id,
            message_id=message_id,
            lease_gen=body.lease_gen,
            outcome=body.outcome,
            now_ms=now_ms,
            code=body.code,
            detail=body.detail,
            retry_after_ms=body.retry_after_ms,
        )
        if result in ("failed", "gave_up"):
            op = msg_ops_mod.op_of(conn, message_id) or {}
            write_audit(
                conn,
                now_ms=now_ms,
                actor="connector",
                api_token_id=None,
                client_name=None,
                request_id=uuid.uuid4().hex,
                action="msg_op_failed",
                result=result,
                detail={"message_id": message_id, "code": (op.get("error") or {}).get("code")},
            )
        return result

    try:
        result = await db.write(_fn)
    except msg_ops_mod.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    except msg_ops_mod.StaleLease as exc:
        raise HTTPException(409, "stale lease") from exc
    return {"ok": True, "result": result}
