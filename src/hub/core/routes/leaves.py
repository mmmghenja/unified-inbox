"""Ручки выхода из групп и каналов Telegram (Ф17-2, docs/architecture.md, A2–A4, A7 и поправки
П6–П8, П12).

Публичные: `POST /api/v1/leaves` — только bearer со скоупом `draft` (запреты `check_attach`: не UI-сессия, не
дежурная, не оркестратор, не `background`), заявка `pending`; `POST /api/v1/channels/{id}/leave` — только
UI-сессия (кнопка владельца в шапке беседы: сразу `approved`); `GET /api/v1/leaves` — скоуп `read`, только видимые
проекты; `POST /api/v1/leaves/{id}/approve|decline` — только UI-сессия (`ui_only`: bearer -> 403 с аудитом
`ui_only_denied`), CSRF — middleware. Внутренние (сокет коннектора, форма — как `/join/*`): `GET /leave/wait`,
`POST /leave/lease`, `POST /leave/report`. Маршруты не пишут SQL: `core/leave.py`.
"""

import asyncio
import contextlib
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from .. import autopilot as autopilot_mod
from .. import card_route as card_route_mod
from .. import connector_auth
from .. import decisions as decisions_mod
from .. import leave as leave_mod
from .. import owner_queue as zq
from ..connector_auth import ConnectorPrincipal
from ..security import Principal, principal_dependency
from .subs import SessionIn
from .write import _client_name, _new_request_id, _now_ms, ui_only

public_router = APIRouter(tags=["leaves"])
internal_router = APIRouter(tags=["internal-leaves"])

_draft = Depends(principal_dependency("draft"))
_read = Depends(principal_dependency("read"))
_connector = Depends(connector_auth.connector_dependency())

_MAX_WAIT_S = 30.0
# Нижняя граница одного ожидания до ближайшего срока (как `routes/joins.py`).
_MIN_DUE_WAIT_S = 0.05
_MAX_RETRY_AFTER_MS = 86_400_000


def _http(exc: leave_mod.LeaveError) -> HTTPException:
    status = {
        leave_mod.Invalid: 422,
        leave_mod.Denied: 403,
        leave_mod.NotFound: 404,
        leave_mod.Conflict: 409,
        leave_mod.TooMany: 429,
        leave_mod.StaleLease: 409,
    }.get(type(exc), 422)
    return HTTPException(status, str(exc))


# --- публичные ----------------------------------------------------------------------------------------------


class LeaveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: StrictInt = Field(ge=1, le=2**53)
    reason: StrictStr | None = Field(default=None, max_length=leave_mod.REASON_MAX)
    # Ф41-5б (5.5): сессия (тройка) и ключ запуска сессии хаба — выключатель автопилота `tg_leave`.
    session: SessionIn | None = None
    # Ф41-5б П1: ключ запуска сессии хаба — только для `autopilot.session_free`; не сохраняется.
    session_key: StrictStr | None = Field(default=None, max_length=128)


@public_router.post("/leaves")
async def post_leave(
    body: LeaveIn, request: Request, response: Response, principal: Principal = _draft
) -> dict:
    """A2 (сессия): 201 — новая заявка `pending`, 200 — уже открытая этой беседы от того же проекта. 403 —
    запреты `check_attach`; 404 — беседа не видна; 409 — уже вышли / выход просит другой проект / идёт
    вступление / владелец оставил беседу за 7 дней; 422 — не группа или канал Telegram, беседа без проекта,
    форум в другом проекте; 429 — лимиты."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    client_name = _client_name(request)

    def _fn(conn):
        req, created = leave_mod.create_session(
            conn,
            principal=principal,
            channel_id=body.channel_id,
            reason=body.reason,
            now_ms=now_ms,
            client_name=client_name,
            request_id=request_id,
            session=body.session.model_dump() if body.session is not None else None,
        )
        # Ф41-5б (5.5, П7): сессии хаба в авто при снятом `tg_leave` — тут же тем же кодом, что кнопка.
        free = (
            autopilot_mod.session_free(
                conn, autopilot_mod.ident(body.session, body.session_key), "tg_leave", req.project_id
            )
            if created and req.status == "pending"
            else None
        )
        if free is not None:
            try:
                req = leave_mod.decide(
                    conn,
                    req.id,
                    "approve",
                    now_ms=now_ms,
                    api_token_id=principal.token_id,
                    client_name=client_name,
                    audit_request_id=request_id,
                    auto_session=free.id,
                )
            except leave_mod.Conflict:
                pass  # идёт вступление по этой беседе — заявка остаётся ждать владельца
        if created and req.status == "pending":
            # Ф66-1 (П9): карточка владельца — «сразу да» по таблице маршрута; ответ сессии — как до клика.
            card_route_mod.auto_answer(conn, f"leave:{req.id}", now_ms=now_ms)
        return leave_mod.request_dict(req), created

    try:
        item, created = await db.write(_fn)
    except leave_mod.LeaveError as exc:
        raise _http(exc) from exc
    response.status_code = 201 if created else 200
    return item


@public_router.post("/channels/{channel_id}/leave")
async def post_channel_leave(
    channel_id: int, request: Request, response: Response, principal: Principal = ui_only
) -> dict:
    """A2 (владелец, кнопка в шапке беседы): заявка сразу `approved`; открытая `pending` — одобряется, `approved`
    — как есть. 200 с заявкой. 404 — нет беседы; 409 — уже вышли / идёт вступление; 422 — не группа или
    канал Telegram."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    client_name = _client_name(request)

    def _fn(conn):
        # Ф58-3: «Выйти» из шапки одобряет открытую заявку на выход — решал агент — его поручение закрыть.
        src = leave_mod._chan(conn, channel_id)  # noqa: SLF001 — та же беседа-цель, что у `create_ui`
        if src is not None:
            existing = leave_mod._open_for(conn, leave_mod._target(conn, src).id)  # noqa: SLF001
            if existing is not None and existing.status == "pending":
                zq.take_from_agent(conn, f"leave:{existing.id}", now_ms=now_ms)  # Ф65-1 Ж1
        else:
            existing = None
        req = leave_mod.create_ui(
            conn,
            principal=principal,
            channel_id=channel_id,
            now_ms=now_ms,
            client_name=client_name,
            request_id=request_id,
        )
        if existing is not None and existing.status == "pending":  # Ф76-1 (П5): «Выйти» одобрил заявку
            decisions_mod.record(conn, f"leave:{existing.id}", decisions_mod.BY_UI, "approve", now_ms=now_ms)
        return leave_mod.request_dict(req)

    try:
        return await db.write(_fn)
    except leave_mod.LeaveError as exc:
        raise _http(exc) from exc


@public_router.get("/leaves")
async def get_leaves(
    request: Request,
    project_id: int | None = Query(None, ge=1, le=2**53),
    channel_id: int | None = Query(None, ge=1, le=2**53),
    status: Literal["open", "pending", "approved", "left", "failed", "declined"] | None = Query(None),
    limit: int = Query(50, ge=1, le=leave_mod.LIST_MAX),
    principal: Principal = _read,
) -> list[dict]:
    """A7: заявки видимых проектов; по умолчанию — открытые и закрытые за 7 дней, новые первыми;
    `channel_id` (П12) — заявки беседы (у темы — её форума)."""
    now_ms = _now_ms(request)
    with request.app.state.db.reader() as conn:
        return leave_mod.list_requests(
            conn,
            principal,
            now_ms=now_ms,
            project_id=project_id,
            channel_id=channel_id,
            status=status,
            limit=limit,
        )


async def _decide(
    request: Request, response: Response, principal: Principal, leave_id: int, action: str
) -> dict:
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    client_name = _client_name(request)

    def _fn(conn):
        zq.take_from_agent(conn, f"leave:{leave_id}", now_ms=now_ms)  # Ф65-1 Ж1
        req = leave_mod.decide(
            conn,
            leave_id,
            action,
            now_ms=now_ms,
            api_token_id=principal.token_id,
            client_name=client_name,
            audit_request_id=request_id,
        )
        decisions_mod.record(conn, f"leave:{leave_id}", decisions_mod.BY_UI, action, now_ms=now_ms)  # Ф76-1
        return leave_mod.request_dict(req)

    try:
        return await db.write(_fn)
    except leave_mod.LeaveError as exc:
        raise _http(exc) from exc


@public_router.post("/leaves/{leave_id}/approve")
async def post_leave_approve(
    leave_id: int, request: Request, response: Response, principal: Principal = ui_only
) -> dict:
    """A3: «Выйти» — только владелец (bearer -> 403); только из `pending`, иначе 409."""
    return await _decide(request, response, principal, leave_id, "approve")


@public_router.post("/leaves/{leave_id}/decline")
async def post_leave_decline(
    leave_id: int, request: Request, response: Response, principal: Principal = ui_only
) -> dict:
    """A3: «Оставить» — только владелец; пока шаг ни разу не выдавался, иначе 409 «уже выхожу»."""
    return await _decide(request, response, principal, leave_id, "decline")


# --- внутренние (сокет коннектора) -------------------------------------------------------------------------


def _state(request: Request, principal: ConnectorPrincipal) -> tuple[bool, int | None, int]:
    db = request.app.state.db
    now_ms = _now_ms(request)
    with db.reader() as conn:
        ready, due = leave_mod.wait_state(conn, account_id=principal.account_id, now_ms=now_ms)
    return ready, due, now_ms


@internal_router.get("/leave/wait")
async def leave_wait(
    request: Request,
    timeout_s: float = Query(25.0, ge=0),
    principal: ConnectorPrincipal = _connector,
) -> dict:
    """Long-poll, как `/join/wait`: «готово» = `lease` выдал бы шаг своему аккаунту; ожидание ограничено ещё
    и временем до ближайшего срока (темп аккаунта, пауза FloodWait)."""
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


class LeaveLeaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int | None = Field(default=None, ge=1)


@internal_router.post("/leave/lease")
async def leave_lease(
    body: LeaveLeaseIn, request: Request, principal: ConnectorPrincipal = _connector
) -> list[dict]:
    """Задания `{id, lease_gen, step: "leave", ext_id, chat_kind, project_id}`, по умолчанию по одному."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    limit = body.limit if body.limit is not None else 1
    return await db.write(
        lambda conn: leave_mod.lease(conn, account_id=principal.account_id, now_ms=now_ms, limit=limit)
    )


class LeaveReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: StrictInt
    lease_gen: StrictInt
    outcome: Literal["done", "retry", "drop"]
    retry_after_ms: int | None = Field(default=None, ge=0, le=_MAX_RETRY_AFTER_MS)
    reason: str | None = Field(default=None, max_length=leave_mod.ERROR_MAX)
    result: Literal["left", "not_member"] | None = None


@internal_router.post("/leave/report")
async def leave_report(
    body: LeaveReportIn, request: Request, principal: ConnectorPrincipal = _connector
) -> dict:
    """Чужой аккаунт/нет строки — 404, устаревший `lease_gen` или закрытый шаг — 409, кривой отчёт — 422."""
    db = request.app.state.db
    now_ms = _now_ms(request)

    def _fn(conn):
        return leave_mod.report(
            conn,
            account_id=principal.account_id,
            request_id=body.id,
            lease_gen=body.lease_gen,
            outcome=body.outcome,
            now_ms=now_ms,
            retry_after_ms=body.retry_after_ms,
            reason=body.reason,
            result=body.result,
        )

    try:
        result = await db.write(_fn)
    except leave_mod.LeaveError as exc:
        raise _http(exc) from exc
    return {"ok": True, "result": result}
