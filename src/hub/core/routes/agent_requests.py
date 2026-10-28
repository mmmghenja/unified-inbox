"""Ручки заявок агентов (Ф41-5б, docs/architecture.md).

`POST /api/v1/agent-requests` — только bearer со скоупом `draft` (интерфейсу — 403); `GET
/api/v1/agent-requests` — интерфейсу все, токену — свои; `POST /api/v1/agent-requests/{id}/approve|decline`
— только UI-сессия (`ui_only`: bearer -> 403 с аудитом `ui_only_denied`), CSRF — middleware. Маршруты не
пишут SQL: `core/agent_requests.py`.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from .. import agent_requests as ar_mod
from .. import card_route as card_route_mod
from .. import decisions as decisions_mod
from .. import hub_sessions
from .. import owner_queue as zq
from ..security import Principal, principal_dependency
from .sessions import HeldEngine, _audit, _held, _waiters, _waiting
from .write import SendSessionIn, _deny_bearer, _now_ms, ui_only

public_router = APIRouter(tags=["agent-requests"])

_draft = Depends(principal_dependency("draft"))
_read = Depends(principal_dependency("read"))

KEY_MAX = 128


def _http(exc: ar_mod.AgentRequestError) -> HTTPException:
    return HTTPException(exc.status, exc.detail)


class AgentRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: StrictStr = Field(max_length=32)
    project_id: StrictInt = Field(ge=1, le=2**53)
    reason: StrictStr | None = Field(default=None, max_length=ar_mod.REASON_MAX)
    # перенос
    channel_id: StrictInt | None = Field(default=None, ge=1, le=2**62)
    # напарник
    role: StrictStr | None = Field(default=None, max_length=200)
    task: StrictStr | None = Field(default=None, max_length=ar_mod.TASK_MAX)
    engine: StrictStr | None = Field(default=None, max_length=32)
    model: StrictStr | None = Field(default=None, max_length=ar_mod.FIELD_MAX)
    effort: StrictStr | None = Field(default=None, max_length=ar_mod.FIELD_MAX)
    mode: StrictStr | None = Field(default=None, max_length=16)
    # Ф50-7: папка и файл роли напарника — пути от папки ПРОЕКТА (форму проверяет ядро, отказ — 422).
    subdir: StrictStr | None = Field(default=None, max_length=1200)
    role_file: StrictStr | None = Field(default=None, max_length=1200)
    # Сессия, опознанная MCP (тройка), и ключ запуска сессии хаба (П1) — отдельным полем.
    session: SendSessionIn | None = None
    session_key: StrictStr | None = Field(default=None, max_length=KEY_MAX)


@public_router.post("/agent-requests")
async def post_agent_request(
    body: AgentRequestIn, request: Request, response: Response, principal: Principal = _draft
) -> dict:
    """Заявка сессии. 201 — новая, 200 — уже открытая или прошедшая сразу (`auto: true`)."""
    if principal.is_ui:
        await _deny_bearer(request, principal)
    now_ms = _now_ms(request)
    audit = _audit(request, principal)
    waiters = _waiters(request)
    fields = body.model_dump(exclude={"kind", "project_id", "reason", "session", "session_key"})

    def _fn(conn):
        done = ar_mod.create(
            conn,
            principal=principal,
            kind=body.kind,
            project_id=body.project_id,
            body=fields,
            reason=body.reason,
            session=body.session,
            session_key=body.session_key,
            now_ms=now_ms,
            audit=audit,
            waiting=_waiting(request),
        )
        if done.created and done.request.get("status") == "pending":
            # Ф66-1 (П9): карточка владельца — «сразу да» по таблице маршрута; ответ сессии — как до клика.
            # Заведённая ответом сессия — в реестр после коммита, как у клика.
            auto = card_route_mod.auto_answer(
                conn, f"agent_request:{done.request['id']}", now_ms=now_ms, waiting=_waiting(request)
            )
            if auto.session is not None:
                done = done._replace(session=auto.session)
        row = None
        if done.session is not None:
            row = hub_sessions.session_json(conn, done.session, now_ms=now_ms, waiters=waiters)
        return done, row

    try:
        done, _row = await request.app.state.db.write(_fn)
    except ar_mod.AgentRequestError as exc:
        raise _http(exc) from exc
    if done.session is not None:
        _held(request).add(done.session.id)  # после коммита — как `POST /sessions`
    response.status_code = 201 if done.created and not done.request["auto"] else 200
    return done.request


@public_router.get("/agent-requests")
async def get_agent_requests(
    request: Request,
    status: Literal["pending", "approved", "declined"] | None = Query(None),
    limit: int = Query(ar_mod.LIST_MAX, ge=1, le=ar_mod.LIST_MAX),
    principal: Principal = _read,
) -> dict:
    now_ms = _now_ms(request)
    with request.app.state.db.reader() as conn:
        return {"requests": ar_mod.list_requests(conn, principal, now_ms=now_ms, status=status, limit=limit)}


class ApproveIn(BaseModel):
    """Тело «Завести»: поля меняют поля заявки на напарника (у переноса тело пустое)."""

    model_config = ConfigDict(extra="forbid")

    role: StrictStr | None = Field(default=None, max_length=200)
    engine: HeldEngine | None = None
    model: StrictStr | None = Field(default=None, max_length=ar_mod.FIELD_MAX)
    effort: StrictStr | None = Field(default=None, max_length=ar_mod.FIELD_MAX)
    mode: Literal["manual", "auto"] | None = None
    want_sub: dict | None = None
    autosend_ok: StrictBool | None = None
    # Ф50-7 (П2): папка и файл роли — «ключ есть»: значение (и пустая строка, и `null` — корень / без
    # файла) заменяет значение заявки; ключа нет — как в заявке.
    subdir: StrictStr | None = Field(default=None, max_length=1200)
    role_file: StrictStr | None = Field(default=None, max_length=1200)


class DeclineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: StrictStr | None = Field(default=None, max_length=ar_mod.NOTE_MAX)


async def _decide(request: Request, principal: Principal, request_no: int, action: str, **kw) -> dict:
    now_ms = _now_ms(request)
    audit = _audit(request, principal)
    waiters = _waiters(request)

    def _fn(conn):
        zq.take_from_agent(conn, f"agent_request:{request_no}", now_ms=now_ms)  # Ф65-1 Ж1
        decided = ar_mod.decide(
            conn, request_no, action, now_ms=now_ms, audit=audit, waiting=_waiting(request), **kw
        )
        decisions_mod.record(  # Ф76-1 (П5)
            conn, f"agent_request:{request_no}", decisions_mod.BY_UI, action, kw.get("note"), now_ms=now_ms
        )
        row = None
        if decided.session is not None:
            row = hub_sessions.session_json(conn, decided.session, now_ms=now_ms, waiters=waiters, ui=True)
        return decided, row

    try:
        decided, row = await request.app.state.db.write(_fn)
    except ar_mod.AgentRequestError as exc:
        raise _http(exc) from exc
    if decided.session is not None:
        _held(request).add(decided.session.id)
    answer: dict = {"request": decided.request}
    if row is not None:
        answer["session"] = row
    return answer


@public_router.post("/agent-requests/{request_no}/approve")
async def approve_agent_request(
    request_no: int, request: Request, body: ApproveIn | None = None, principal: Principal = ui_only
) -> dict:
    fields = body.model_dump(exclude_unset=True, exclude_none=True) if body is not None else {}
    for key in ("subdir", "role_file"):  # П2: у этих двух и `null` — заданное значение
        if body is not None and key in body.model_fields_set:
            fields[key] = getattr(body, key)
    return await _decide(request, principal, request_no, "approve", fields=fields)


@public_router.post("/agent-requests/{request_no}/decline")
async def decline_agent_request(
    request_no: int, request: Request, body: DeclineIn | None = None, principal: Principal = ui_only
) -> dict:
    return await _decide(request, principal, request_no, "decline", note=body.note if body else None)
