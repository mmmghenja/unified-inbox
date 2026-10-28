"""Ручки операций над группами WhatsApp (Ф62-6, docs/architecture.md и поправки П5, П8).

Публичные: `POST /api/v1/group-ops` — bearer со скоупом `draft` (запреты `check_attach`), заявка сессии
(`pending`; `link` — сразу; сессия хаба в авто при снятом `wa_group` — сразу, `auto`); `POST
/api/v1/group-ops/ui` — только кука интерфейса (заявка владельца, сразу `approved`); `GET /api/v1/group-ops` и
`GET /api/v1/group-ops/{id}` — скоуп `read`, видимые проекты; `POST /api/v1/group-ops/{id}/approve|decline` —
ТОЛЬКО кука интерфейса (`ui_only`, П8; агент владельца — через `POST /owner_channel/{ref}/answer`). Внутренние (сокет
коннектора, форма — как `/leave/*`): `GET /group-op/wait`, `POST /group-op/lease`, `POST /group-op/report`.
Маршруты не пишут SQL: `core/group_ops.py`.
"""

import asyncio
import contextlib
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from .. import autopilot as autopilot_mod
from .. import card_route as card_route_mod
from .. import connector_auth
from .. import decisions as decisions_mod
from .. import group_ops as group_ops_mod
from .. import owner_queue as zq
from ..connector_auth import ConnectorPrincipal
from ..security import Principal, principal_dependency
from .subs import SessionIn
from .write import _client_name, _new_request_id, _now_ms, ui_only

public_router = APIRouter(tags=["group-ops"])
internal_router = APIRouter(tags=["internal-group-ops"])

_draft = Depends(principal_dependency("draft"))
_read = Depends(principal_dependency("read"))
_connector = Depends(connector_auth.connector_dependency())

_MAX_WAIT_S = 30.0
_MIN_DUE_WAIT_S = 0.05
_MAX_RETRY_AFTER_MS = 86_400_000
_ID_MAX = 2**53

Kind = Literal["create", "add", "remove", "link", "link_reset"]
Participant = StrictStr | StrictInt


def _http(exc: group_ops_mod.GroupOpError) -> HTTPException:
    status = {
        group_ops_mod.Invalid: 422,
        group_ops_mod.Denied: 403,
        group_ops_mod.NotFound: 404,
        group_ops_mod.Conflict: 409,
        group_ops_mod.TooMany: 429,
        group_ops_mod.StaleLease: 409,
    }.get(type(exc), 422)
    return HTTPException(status, str(exc))


# --- публичные ----------------------------------------------------------------------------------------------


class GroupOpIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Kind
    project_id: StrictInt | None = Field(default=None, ge=1, le=_ID_MAX)
    account_id: StrictInt | None = Field(default=None, ge=1, le=_ID_MAX)
    channel_id: StrictInt | None = Field(default=None, ge=1, le=_ID_MAX)
    title: StrictStr | None = Field(default=None, max_length=200)
    participants: list[Participant] | None = Field(default=None, max_length=group_ops_mod.MEMBERS_MAX)
    reason: StrictStr | None = Field(default=None, max_length=group_ops_mod.REASON_MAX)
    # Сессия (тройка) и ключ запуска сессии хаба — выключатель автопилота `wa_group` и автор для сигнала.
    session: SessionIn | None = None
    session_key: StrictStr | None = Field(default=None, max_length=128)


@public_router.post("/group-ops", status_code=201)
async def post_group_op(
    body: GroupOpIn, request: Request, response: Response, principal: Principal = _draft
) -> dict:
    """Заявка сессии: 201 с заявкой. 403 — запреты `check_attach`; 404 — проект или беседа не видны; 422 —
    поля, не WhatsApp, беседа без проекта, название длиннее 25; 429 — лимиты проекта."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    client_name = _client_name(request)
    session = body.session.model_dump() if body.session is not None else None

    def _fn(conn):
        op = group_ops_mod.create_session(
            conn,
            principal=principal,
            kind=body.kind,
            now_ms=now_ms,
            project_id=body.project_id,
            account_id=body.account_id,
            channel_id=body.channel_id,
            title=body.title,
            participants=body.participants,
            reason=body.reason,
            client_name=client_name,
            request_id=request_id,
            session=session,
        )
        # Сессия хаба в авто при снятом `wa_group` — тут же тем же кодом, что кнопка владельца.
        free = (
            autopilot_mod.session_free(
                conn, autopilot_mod.ident(body.session, body.session_key), group_ops_mod.SWITCH, op.project_id
            )
            if op.status == "pending"
            else None
        )
        if free is not None:
            op = group_ops_mod.decide(
                conn,
                op.id,
                "approve",
                now_ms=now_ms,
                api_token_id=principal.token_id,
                client_name=client_name,
                audit_request_id=request_id,
                auto_session=free.id,
            )
        if op.status == "pending":
            # Ф66-1 (П9): карточка владельца — «сразу да» по таблице маршрута; ответ сессии — как до клика.
            card_route_mod.auto_answer(conn, f"group_op:{op.id}", now_ms=now_ms)
        return group_ops_mod.op_dict(op)

    try:
        return await db.write(_fn)
    except group_ops_mod.GroupOpError as exc:
        raise _http(exc) from exc


class GroupOpUiIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Kind
    project_id: StrictInt | None = Field(default=None, ge=1, le=_ID_MAX)
    account_id: StrictInt | None = Field(default=None, ge=1, le=_ID_MAX)
    channel_id: StrictInt | None = Field(default=None, ge=1, le=_ID_MAX)
    title: StrictStr | None = Field(default=None, max_length=200)
    participants: list[Participant] | None = Field(default=None, max_length=group_ops_mod.MEMBERS_MAX)


@public_router.post("/group-ops/ui", status_code=201)
async def post_group_op_ui(
    body: GroupOpUiIn, request: Request, response: Response, principal: Principal = ui_only
) -> dict:
    """Заявка владельца из интерфейса — сразу `approved`. 404 — нет беседы/проекта; 422 — поля, не WhatsApp."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    client_name = _client_name(request)

    def _fn(conn):
        op = group_ops_mod.create_ui(
            conn,
            principal=principal,
            kind=body.kind,
            now_ms=now_ms,
            project_id=body.project_id,
            account_id=body.account_id,
            channel_id=body.channel_id,
            title=body.title,
            participants=body.participants,
            client_name=client_name,
            request_id=request_id,
        )
        return group_ops_mod.op_dict(op)

    try:
        return await db.write(_fn)
    except group_ops_mod.GroupOpError as exc:
        raise _http(exc) from exc


@public_router.get("/channels/{channel_id}/group-members")
async def get_group_members(
    channel_id: int,
    request: Request,
    response: Response,
    refresh: bool = Query(False),
    principal: Principal = _read,
) -> dict:
    """Состав группы WhatsApp — последний снимок `group_info` (`group_ops.members_view`): кука интерфейса —
    номер целиком в `phone`, bearer `draft` с видимостью беседы — только маска. `refresh=1` (только кука) —
    задание `members` коннектору без карточки; исход — заявка `refresh.op_id` (`/sync` `group_ops`, `GET
    /group-ops/{id}`), затем снова этот GET. 404 — нет беседы или не видна; 422 — не группа WhatsApp; 403 —
    `refresh` не из интерфейса."""
    db = request.app.state.db
    if not principal.is_ui and not principal.has_scope("draft"):  # кука интерфейса или bearer `draft`
        raise HTTPException(403, "forbidden")
    if channel_id <= 0 or channel_id > _ID_MAX:
        raise HTTPException(404, f"беседа #{channel_id} не найдена")
    try:
        if not refresh:
            with db.reader() as conn:
                return group_ops_mod.members_view(conn, principal, channel_id)
        if not principal.is_ui:
            raise HTTPException(403, "обновить состав группы может только владелец из интерфейса")
        now_ms = _now_ms(request)
        request_id = _new_request_id(response)
        client_name = _client_name(request)

        def _fn(conn):
            op = group_ops_mod.request_members(
                conn,
                principal=principal,
                channel_id=channel_id,
                now_ms=now_ms,
                client_name=client_name,
                request_id=request_id,
            )
            return group_ops_mod.members_view(conn, principal, channel_id, refresh_op=op)

        return await db.write(_fn)
    except group_ops_mod.GroupOpError as exc:
        raise _http(exc) from exc


@public_router.get("/group-ops")
async def get_group_ops(
    request: Request,
    project_id: int | None = Query(None, ge=0, le=_ID_MAX),
    channel_id: int | None = Query(None, ge=1, le=_ID_MAX),
    status: Literal["open", "pending", "approved", "done", "failed", "declined"] | None = Query(None),
    limit: int = Query(50, ge=1, le=group_ops_mod.LIST_MAX),
    pid: int | None = Query(None),
    proc_start: int | None = Query(None, ge=0),
    session_id: str | None = Query(None, max_length=128),
    principal: Principal = _read,
) -> list[dict]:
    """Заявки видимых проектов; по умолчанию — открытые и закрытые за 7 дней, новые первыми. Тройка
    (`pid`, `proc_start`, `session_id`) — только заявки этой сессии этим токеном (`hub_group_ops`)."""
    mine: dict[str, Any] | None = None
    if pid is not None or proc_start is not None or session_id is not None:
        if pid is None or proc_start is None or session_id is None:
            raise HTTPException(422, "сессия: нужны pid, proc_start и session_id вместе")
        mine = {"pid": pid, "proc_start": proc_start, "session_id": session_id}
    now_ms = _now_ms(request)
    with request.app.state.db.reader() as conn:
        return group_ops_mod.list_ops(
            conn,
            principal,
            now_ms=now_ms,
            project_id=project_id,
            channel_id=channel_id,
            status=status,
            mine=mine,
            limit=limit,
        )


@public_router.get("/group-ops/{op_id}")
async def get_group_op(op_id: int, request: Request, principal: Principal = _read) -> dict:
    """Одна заявка (интерфейс ждёт исход `link`/`create`); не видна — 404."""
    with request.app.state.db.reader() as conn:
        try:
            return group_ops_mod.op_dict(group_ops_mod.visible_op(conn, principal, op_id))
        except group_ops_mod.GroupOpError as exc:
            raise _http(exc) from exc


async def _decide(
    request: Request, response: Response, principal: Principal, op_id: int, action: str
) -> dict:
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    client_name = _client_name(request)

    def _fn(conn):
        zq.take_from_agent(
            conn, f"group_op:{op_id}", now_ms=now_ms
        )  # Ф65-1 Ж1: решал агент — владелец ответил сам
        op = group_ops_mod.decide(
            conn,
            op_id,
            action,
            now_ms=now_ms,
            api_token_id=principal.token_id,
            client_name=client_name,
            audit_request_id=request_id,
        )
        decisions_mod.record(conn, f"group_op:{op_id}", decisions_mod.BY_UI, action, now_ms=now_ms)  # Ф76-1
        return group_ops_mod.op_dict(op)

    try:
        return await db.write(_fn)
    except group_ops_mod.GroupOpError as exc:
        raise _http(exc) from exc


@public_router.post("/group-ops/{op_id}/approve")
async def post_group_op_approve(
    op_id: int, request: Request, response: Response, principal: Principal = ui_only
) -> dict:
    """«Да» — только владелец (bearer -> 403); только из `pending`, иначе 409."""
    return await _decide(request, response, principal, op_id, "approve")


@public_router.post("/group-ops/{op_id}/decline")
async def post_group_op_decline(
    op_id: int, request: Request, response: Response, principal: Principal = ui_only
) -> dict:
    """«Отказать» — только владелец; пока шаг ни разу не выдавался, иначе 409 «уже выполняю»."""
    return await _decide(request, response, principal, op_id, "decline")


# --- внутренние (сокет коннектора) -------------------------------------------------------------------------


def _state(request: Request, principal: ConnectorPrincipal) -> tuple[bool, int | None, int]:
    now_ms = _now_ms(request)
    with request.app.state.db.reader() as conn:
        ready, due = group_ops_mod.wait_state(conn, account_id=principal.account_id, now_ms=now_ms)
    return ready, due, now_ms


@internal_router.get("/group-op/wait")
async def group_op_wait(
    request: Request,
    timeout_s: float = Query(25.0, ge=0),
    principal: ConnectorPrincipal = _connector,
) -> dict:
    """Long-poll, как `/leave/wait`: «готово» = `lease` выдал бы задание своему аккаунту."""
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
                    break
    return {"ready": False}


class GroupOpLeaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int | None = Field(default=None, ge=1)


@internal_router.post("/group-op/lease")
async def group_op_lease(
    body: GroupOpLeaseIn, request: Request, principal: ConnectorPrincipal = _connector
) -> list[dict]:
    """Задания `{id, lease_gen, kind, ext_id, title, participants [{i, jid, phone?}], project_id,
    first_leased_ts, attempts, exclude? (только create — jid групп, которые сверке П1 не брать)}`,
    по одному."""
    now_ms = _now_ms(request)
    limit = body.limit if body.limit is not None else 1
    return await request.app.state.db.write(
        lambda conn: group_ops_mod.lease(conn, account_id=principal.account_id, now_ms=now_ms, limit=limit)
    )


class GroupOpReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: StrictInt
    lease_gen: StrictInt
    outcome: Literal["done", "retry", "drop"]
    retry_after_ms: int | None = Field(default=None, ge=0, le=_MAX_RETRY_AFTER_MS)
    postpone_ms: int | None = Field(default=None, ge=0, le=_MAX_RETRY_AFTER_MS)
    reason: str | None = Field(default=None, max_length=group_ops_mod.ERROR_MAX)
    result: dict | None = None


@internal_router.post("/group-op/report")
async def group_op_report(
    body: GroupOpReportIn, request: Request, principal: ConnectorPrincipal = _connector
) -> dict:
    """Чужой аккаунт/нет строки — 404, устаревший `lease_gen` или закрытый шаг — 409, кривой отчёт — 422."""
    now_ms = _now_ms(request)

    def _fn(conn):
        return group_ops_mod.report(
            conn,
            account_id=principal.account_id,
            op_id=body.id,
            lease_gen=body.lease_gen,
            outcome=body.outcome,
            now_ms=now_ms,
            retry_after_ms=body.retry_after_ms,
            postpone_ms=body.postpone_ms,
            reason=body.reason,
            result=body.result,
        )

    try:
        result = await request.app.state.db.write(_fn)
    except group_ops_mod.GroupOpError as exc:
        raise _http(exc) from exc
    return {"ok": True, "result": result}
