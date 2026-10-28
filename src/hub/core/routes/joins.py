"""Ручки заявок на вступление в группы и каналы Telegram (Ф17-1, docs/architecture.md и
поправки П2, П3, П6, П7).

Публичные: `POST /api/v1/joins` — только bearer со скоупом `draft` (запреты `check_attach`: не UI-сессия, не
дежурная, не оркестратор, не `background`); `GET /api/v1/joins` — скоуп `read`, только видимые проекты;
`POST /api/v1/joins/{id}/approve|decline` — только UI-сессия (`ui_only`: bearer -> 403 с аудитом
`ui_only_denied`), CSRF — middleware. Внутренние (сокет коннектора, копия `routes/read_ack.py` по форме):
`GET /join/wait`, `POST /join/lease`, `POST /join/report`. Маршруты не пишут SQL: `core/join.py`.
"""

import asyncio
import contextlib
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from .. import autopilot as autopilot_mod
from .. import connector_auth
from .. import decisions as decisions_mod
from .. import join as join_mod
from .. import owner_queue as zq
from ..connector_auth import ConnectorPrincipal
from ..security import Principal, principal_dependency
from .subs import SessionIn
from .write import _client_name, _new_request_id, _now_ms, ui_only

public_router = APIRouter(tags=["joins"])
internal_router = APIRouter(tags=["internal-joins"])

_draft = Depends(principal_dependency("draft"))
_read = Depends(principal_dependency("read"))
_connector = Depends(connector_auth.connector_dependency())

_MAX_WAIT_S = 30.0
# Нижняя граница одного ожидания до ближайшего срока (как `routes/read_ack.py`).
_MIN_DUE_WAIT_S = 0.05
_MAX_RETRY_AFTER_MS = 86_400_000


def _http(exc: join_mod.JoinError) -> HTTPException:
    status = {
        join_mod.Invalid: 422,
        join_mod.Denied: 403,
        join_mod.NotFound: 404,
        join_mod.Conflict: 409,
        join_mod.TooMany: 429,
        join_mod.StaleLease: 409,
    }.get(type(exc), 422)
    return HTTPException(status, str(exc))


# --- публичные (A4) ---------------------------------------------------------------------------------------


class JoinIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: StrictInt = Field(ge=-(2**53), le=2**53)
    target: StrictStr = Field(max_length=join_mod.TARGET_INPUT_MAX)
    reason: StrictStr | None = Field(default=None, max_length=join_mod.REASON_MAX)
    account_id: StrictInt | None = Field(default=None, ge=1, le=2**53)
    # П7: сессия, опознанная MCP (pid + `starttime` + разговор), — только если опознана; без неё заявка
    # обычная (через владельца).
    session: SessionIn | None = None
    # Ф41-5б П1: ключ запуска сессии хаба — только для `autopilot.session_free`; не сохраняется.
    session_key: StrictStr | None = Field(default=None, max_length=128)


@public_router.post("/joins")
async def post_join(
    body: JoinIn, request: Request, response: Response, principal: Principal = _draft
) -> dict:
    """A4: заявка сессии. 201 — новая, 200 — уже открытая той же цели этого проекта (идемпотентно). 403 —
    запреты `check_attach`; 404 — проект не виден; 409 — цель просит другой проект / владелец отказал за 7 дней;
    422 — цель или аккаунт (несколько аккаунтов Telegram без `account_id` — со списком id); 429 — лимиты."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    client_name = _client_name(request)
    session = body.session.model_dump() if body.session is not None else None

    def _fn(conn):
        # Ф41-5б (5.5): сессии хаба в авто при снятом `tg_join` заявка примется сама после предпросмотра.
        free = autopilot_mod.session_free(
            conn, autopilot_mod.ident(session, body.session_key), "tg_join", body.project_id
        )
        req, created = join_mod.create(
            conn,
            principal=principal,
            project_id=body.project_id,
            target=body.target,
            reason=body.reason,
            session=session,
            now_ms=now_ms,
            account_id=body.account_id,
            client_name=client_name,
            request_id=request_id,
            autopilot_session=free.id if free is not None else None,
        )
        return join_mod.request_dict(req), created

    try:
        item, created = await db.write(_fn)
    except join_mod.JoinError as exc:
        raise _http(exc) from exc
    response.status_code = 201 if created else 200
    return item


@public_router.get("/joins")
async def get_joins(
    request: Request,
    project_id: int | None = Query(None, ge=1, le=2**53),
    status: Literal[
        "open", "checking", "pending", "approved", "joined", "requested", "already", "failed", "declined"
    ]
    | None = Query(None),
    limit: int = Query(50, ge=1, le=join_mod.LIST_MAX),
    principal: Principal = _read,
) -> list[dict]:
    """A4: заявки видимых проектов; по умолчанию — открытые и закрытые за 7 дней, новые первыми."""
    now_ms = _now_ms(request)
    with request.app.state.db.reader() as conn:
        return join_mod.list_requests(
            conn, principal, now_ms=now_ms, project_id=project_id, status=status, limit=limit
        )


async def _decide(
    request: Request, response: Response, principal: Principal, join_id: int, action: str
) -> dict:
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    client_name = _client_name(request)

    def _fn(conn):
        zq.take_from_agent(conn, f"join:{join_id}", now_ms=now_ms)  # Ф65-1 Ж1
        req = join_mod.decide(
            conn,
            join_id,
            action,
            now_ms=now_ms,
            api_token_id=principal.token_id,
            client_name=client_name,
            audit_request_id=request_id,
        )
        decisions_mod.record(conn, f"join:{join_id}", decisions_mod.BY_UI, action, now_ms=now_ms)  # Ф76-1
        return join_mod.request_dict(req)

    try:
        return await db.write(_fn)
    except join_mod.JoinError as exc:
        raise _http(exc) from exc


@public_router.post("/joins/{join_id}/approve")
async def post_join_approve(
    join_id: int, request: Request, response: Response, principal: Principal = ui_only
) -> dict:
    """Решение 3: «Вступить» — только владелец (bearer -> 403); только из `pending`, иначе 409."""
    return await _decide(request, response, principal, join_id, "approve")


@public_router.post("/joins/{join_id}/decline")
async def post_join_decline(
    join_id: int, request: Request, response: Response, principal: Principal = ui_only
) -> dict:
    """«Отказать» — только владелец; пока шаг «вступить» ни разу не выдавался (П2), иначе 409 «уже вступаю»."""
    return await _decide(request, response, principal, join_id, "decline")


# --- внутренние (сокет коннектора) -------------------------------------------------------------------------


def _state(request: Request, principal: ConnectorPrincipal) -> tuple[bool, int | None, int]:
    db = request.app.state.db
    now_ms = _now_ms(request)
    with db.reader() as conn:
        ready, due = join_mod.wait_state(conn, account_id=principal.account_id, now_ms=now_ms)
    return ready, due, now_ms


@internal_router.get("/join/wait")
async def join_wait(
    request: Request,
    timeout_s: float = Query(25.0, ge=0),
    principal: ConnectorPrincipal = _connector,
) -> dict:
    """Long-poll, как `/read-ack/wait`: «готово» = `lease` выдал бы шаг своему аккаунту; ожидание ограничено
    ещё и временем до ближайшего срока (темп аккаунта, пауза FloodWait)."""
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


class JoinLeaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int | None = Field(default=None, ge=1)


@internal_router.post("/join/lease")
async def join_lease(
    body: JoinLeaseIn, request: Request, principal: ConnectorPrincipal = _connector
) -> list[dict]:
    """Задания `{id, lease_gen, step: peek|join, target_kind, target, ext_id, project_id}`, по умолчанию по
    одному."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    limit = body.limit if body.limit is not None else 1
    return await db.write(
        lambda conn: join_mod.lease(conn, account_id=principal.account_id, now_ms=now_ms, limit=limit)
    )


class JoinPeekIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str | None = None
    kind: str | None = None
    participants: int | None = None
    about: str | None = None
    request_needed: bool = False
    member: bool = False
    forum: bool = False
    ext_id: str | None = None


class JoinReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: StrictInt
    lease_gen: StrictInt
    outcome: Literal["done", "retry", "drop"]
    retry_after_ms: int | None = Field(default=None, ge=0, le=_MAX_RETRY_AFTER_MS)
    reason: str | None = Field(default=None, max_length=join_mod.ERROR_MAX)
    # `done` шага `join`: исход и беседа (если известна).
    result: Literal["joined", "already", "requested"] | None = None
    ext_id: str | None = Field(default=None, max_length=join_mod.EXT_ID_MAX)
    title: str | None = Field(default=None, max_length=4096)
    # `done` шага `peek`: предпросмотр.
    peek: JoinPeekIn | None = None


@internal_router.post("/join/report")
async def join_report(
    body: JoinReportIn, request: Request, principal: ConnectorPrincipal = _connector
) -> dict:
    """Чужой аккаунт/нет строки — 404, устаревший `lease_gen` или закрытый шаг — 409, кривой отчёт — 422."""
    db = request.app.state.db
    now_ms = _now_ms(request)

    def _fn(conn):
        return join_mod.report(
            conn,
            account_id=principal.account_id,
            request_id=body.id,
            lease_gen=body.lease_gen,
            outcome=body.outcome,
            now_ms=now_ms,
            retry_after_ms=body.retry_after_ms,
            reason=body.reason,
            result=body.result,
            peek=body.peek.model_dump() if body.peek is not None else None,
            ext_id=body.ext_id,
            title=body.title,
        )

    try:
        result = await db.write(_fn)
    except join_mod.JoinError as exc:
        raise _http(exc) from exc
    return {"ok": True, "result": result}
