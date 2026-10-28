"""Ручки нажатий кнопок под сообщениями Telegram-ботов (Ф44-1c, docs/architecture.md; фактический контракт — раздел 11
спеки).

Публичные: `POST /api/v1/messages/{id}/buttons/press` — владелец (кука: строка сразу `approved`) или сессия
(bearer `draft` + `session`: карточка, без карточки — только два случая `presses._auto_way`);
`POST /api/v1/button-presses/{id}/approve|decline` — только UI-сессия (`ui_only`: bearer -> 403 с аудитом
`ui_only_denied`), в том числе с телефона (П6); `GET /api/v1/button-presses[/{id}]` — владельцу всё, bearer —
только нажатия своей тройки сессии (П8). Внутренние (сокет коннектора, по образцу `routes/joins.py`):
`GET /button-presses/wait`, `POST /button-presses/lease`, `POST /button-presses/{id}/report`.
Маршруты не пишут SQL: `core/presses.py`. Сырой callback кнопки отдаёт только `lease` — коннектору.
"""

import asyncio
import contextlib
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from .. import card_route as card_route_mod
from .. import connector_auth, queries
from .. import decisions as decisions_mod
from .. import owner_queue as zq
from .. import presses as presses_mod
from ..connector_auth import ConnectorPrincipal
from ..security import Principal, principal_dependency
from .write import SendSessionIn, _client_name, _new_request_id, _now_ms, touch_activity, ui_only

public_router = APIRouter(tags=["button-presses"])
internal_router = APIRouter(tags=["internal-button-presses"])

_authed = Depends(principal_dependency())
_read = Depends(principal_dependency("read"))
_connector = Depends(connector_auth.connector_dependency())

_ID_MAX = 2**53
_MAX_WAIT_S = 30.0
_MIN_DUE_WAIT_S = 0.05
_MAX_RETRY_AFTER_MS = 86_400_000


def _http(exc: presses_mod.PressError) -> HTTPException:
    status = {
        presses_mod.Invalid: 422,
        presses_mod.Denied: 403,
        presses_mod.NotFound: 404,
        presses_mod.Conflict: 409,
        presses_mod.StaleLease: 409,
    }.get(type(exc), 422)
    return HTTPException(status, str(exc))


# --- публичные --------------------------------------------------------------------------------------------


class PressIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Ряд и место кнопки — с 1, как в выдаче сессиям.
    row: StrictInt = Field(ge=1, le=100)
    col: StrictInt = Field(ge=1, le=100)
    # П4: текст кнопки, который видел нажимающий; ядро сверяет с текущим (409 `stale_button`).
    text: StrictStr = Field(max_length=presses_mod.TEXT_MAX)
    # П7: сессия — моделью (не словарём): условия «сразу» те же, что у черновика. Только bearer.
    session: SendSessionIn | None = None
    # Ключ запуска сессии хаба — только для `autopilot.session_free`; не сохраняется.
    session_key: StrictStr | None = Field(default=None, max_length=128)


@public_router.post("/messages/{message_id}/buttons/press")
async def post_press(
    body: PressIn,
    request: Request,
    response: Response,
    message_id: int = Path(ge=1, le=_ID_MAX),
    principal: Principal = _authed,
) -> dict:
    """Нажать кнопку. 201 — новая строка, 200 — уже живая строка этой кнопки (П2, `created: false`).
    Владельцу: `{press, created}` — строка `approved`. Сессии: `{press, created, sent}`; `sent: false` — карточка
    ждёт владельца; при исчерпанном пределе «сразу» — ещё `auto_denied: "limit"` и `auto_limit`.
    403 — нет скоупа `draft`, дежурная, оркестратор; 404 — сообщения нет или беседа не видна; 409
    `stale_button` — клавиатура изменилась; 422 — кнопка не нажимается, нет `session` у bearer."""
    if principal.is_ui and (body.session is not None or body.session_key is not None):
        raise HTTPException(422, "session: только для bearer-клиентов")
    if body.session_key is not None and body.session is None:
        raise HTTPException(422, "session_key: только вместе с session")
    if not principal.is_ui and not principal.has_scope("draft"):
        raise HTTPException(403, "forbidden")
    touch_activity(request, principal)
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    client_name = _client_name(request)
    sub_waiters = getattr(request.app.state, "sub_waiters", None)

    def _fn(conn):
        channel_id = presses_mod.message_channel(conn, message_id)
        project_id = queries.channel_visible(conn, principal, channel_id)
        made = presses_mod.create(
            conn,
            principal=principal,
            message_id=message_id,
            channel_id=channel_id,
            project_id=project_id,
            row=body.row,
            col=body.col,
            text=body.text,
            session=body.session,
            session_key=body.session_key,
            now_ms=now_ms,
            client_name=client_name,
            request_id=request_id,
            waiting=sub_waiters.waiting if sub_waiters is not None else None,
        )
        if not principal.is_ui and made.created and made.press.status == "pending":
            # Ф66-1 (П9): нажатие, которое ждёт владельца, — «сразу да» по таблице маршрута проекта. Ответ сессии —
            # как до клика владельца (исход придёт сигналом автору).
            card_route_mod.auto_answer(
                conn,
                f"press:{made.press.id}",
                now_ms=now_ms,
                waiting=sub_waiters.waiting if sub_waiters is not None else None,
            )
        triple = _triple_of(body.session)
        own = presses_mod.readable(made.press, principal, triple)
        return made, presses_mod.press_dict(conn, made.press, result=own)

    try:
        made, item = await db.write(_fn)
    except queries.NotFound as exc:
        raise HTTPException(404, queries.not_found_detail(exc)) from exc
    except presses_mod.PressError as exc:
        raise _http(exc) from exc
    response.status_code = 201 if made.created else 200
    out: dict = {"press": item, "created": made.created}
    if not principal.is_ui:
        out["sent"] = item["status"] != "pending"
        if made.denied is not None:
            out["auto_denied"] = made.denied
            out["auto_limit"] = made.limit_of
            if made.guard is not None:
                out["auto_guard"] = made.guard  # Ф63-2 (1.3): страховка беседы с ботом — категория
    return out


def _triple_of(session) -> tuple | None:
    return (session.pid, session.proc_start, session.session_id) if session is not None else None


def _query_triple(pid: int | None, proc_start: int | None, session_id: str | None) -> tuple | None:
    if pid is None or proc_start is None or session_id is None:
        return None
    return (pid, proc_start, session_id)


@public_router.get("/button-presses")
async def get_presses(
    request: Request,
    status: Literal["pending", "approved", "leased", "done", "failed", "unknown", "declined", "expired"]
    | None = Query(None),
    limit: int = Query(50, ge=1, le=presses_mod.LIST_MAX),
    pid: int | None = Query(None, ge=-(2**31 - 1), le=2**31 - 1),
    proc_start: int | None = Query(None, ge=0, le=2**62),
    session_id: str | None = Query(None, min_length=1, max_length=128),
    principal: Principal = _read,
) -> list[dict]:
    """Владельцу — все нажатия; bearer — только своей сессии (`pid`, `proc_start`, `session_id` в запросе) в
    видимых проектах, без тройки — пусто (П8). По умолчанию — живые и изменившиеся за сутки."""
    now_ms = _now_ms(request)
    triple = _query_triple(pid, proc_start, session_id)
    with request.app.state.db.reader() as conn:
        return presses_mod.list_presses(
            conn, principal, now_ms=now_ms, triple=triple, status=status, limit=limit
        )


@public_router.get("/button-presses/{press_id}")
async def get_press(
    request: Request,
    press_id: int = Path(ge=1, le=_ID_MAX),
    pid: int | None = Query(None, ge=-(2**31 - 1), le=2**31 - 1),
    proc_start: int | None = Query(None, ge=0, le=2**62),
    session_id: str | None = Query(None, min_length=1, max_length=128),
    principal: Principal = _read,
) -> dict:
    """Состояние и результат одного нажатия; чужое (не своя тройка, не видимый проект) — 404. Строку с
    истёкшим сроком это чтение закрывает само (`presses.sweep`): опрос видит правду и без коннектора."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    triple = _query_triple(pid, proc_start, session_id)

    def _read_one(conn):
        press = presses_mod.get(conn, press_id)
        if press is None or not presses_mod.readable(press, principal, triple):
            return None, False
        return presses_mod.press_dict(conn, press, result=True), presses_mod.overdue(press, now_ms)

    with db.reader() as conn:
        item, overdue = _read_one(conn)
    if item is not None and overdue:

        def _fn(conn):
            presses_mod.sweep(conn, now_ms)
            return _read_one(conn)[0]

        item = await db.write(_fn)
    if item is None:
        raise HTTPException(404, "not found")
    return item


async def _decide(
    request: Request, response: Response, principal: Principal, press_id: int, action: str
) -> dict:
    touch_activity(request, principal)
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    client_name = _client_name(request)

    def _fn(conn):
        zq.take_from_agent(conn, f"press:{press_id}", now_ms=now_ms)  # Ф65-1 Ж1
        press = presses_mod.decide(
            conn,
            press_id,
            action,
            now_ms=now_ms,
            api_token_id=principal.token_id,
            client_name=client_name,
            audit_request_id=request_id,
        )
        decisions_mod.record(conn, f"press:{press_id}", decisions_mod.BY_UI, action, now_ms=now_ms)  # Ф76-1
        return presses_mod.press_dict(conn, press, result=True)

    try:
        return await db.write(_fn)
    except presses_mod.PressError as exc:
        raise _http(exc) from exc


@public_router.post("/button-presses/{press_id}/approve")
async def post_press_approve(
    request: Request,
    response: Response,
    press_id: int = Path(ge=1, le=_ID_MAX),
    principal: Principal = ui_only,
) -> dict:
    """«Нажать» по карточке — только владелец (bearer -> 403); только из `pending`, иначе 409."""
    return await _decide(request, response, principal, press_id, "approve")


@public_router.post("/button-presses/{press_id}/decline")
async def post_press_decline(
    request: Request,
    response: Response,
    press_id: int = Path(ge=1, le=_ID_MAX),
    principal: Principal = ui_only,
) -> dict:
    """«Нет» по карточке — только владелец; только из `pending`, иначе 409."""
    return await _decide(request, response, principal, press_id, "decline")


# --- внутренние (сокет коннектора) ------------------------------------------------------------------------


def _state(request: Request, principal: ConnectorPrincipal) -> tuple[bool, int | None, int]:
    now_ms = _now_ms(request)
    with request.app.state.db.reader() as conn:
        ready, due = presses_mod.wait_state(conn, account_id=principal.account_id, now_ms=now_ms)
    return ready, due, now_ms


@internal_router.get("/button-presses/wait")
async def press_wait(
    request: Request,
    timeout_s: float = Query(25.0, ge=0),
    principal: ConnectorPrincipal = _connector,
) -> dict:
    """Long-poll, как `/join/wait`: «готово» = `lease` что-то сделает своему аккаунту (выдаст или закроет
    просроченное); ожидание ограничено ещё и временем до ближайшего срока."""
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
                    break  # ядро останавливается — пусто, ничего не выдавая
    return {"ready": False}


class PressLeaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int | None = Field(default=None, ge=1)


@internal_router.post("/button-presses/lease")
async def press_lease(
    body: PressLeaseIn, request: Request, principal: ConnectorPrincipal = _connector
) -> list[dict]:
    """Задания `{id, lease_gen, channel_ext_id, message_ext_id, data_b64}`, по умолчанию по одному."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    limit = body.limit if body.limit is not None else 1
    return await db.write(
        lambda conn: presses_mod.lease(conn, account_id=principal.account_id, now_ms=now_ms, limit=limit)
    )


class PressResultIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: str | None = None
    alert: bool = False
    url: str | None = None
    timeout: bool = False


class PressReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_gen: StrictInt
    outcome: Literal["done", "failed", "unknown", "retry"]
    retry_after_ms: int | None = Field(default=None, ge=0, le=_MAX_RETRY_AFTER_MS)
    reason: str | None = Field(default=None, max_length=200)
    result: PressResultIn | None = None


@internal_router.post("/button-presses/{press_id}/report")
async def press_report(
    body: PressReportIn,
    request: Request,
    press_id: int = Path(ge=1, le=_ID_MAX),
    principal: ConnectorPrincipal = _connector,
) -> dict:
    """Чужой аккаунт или нет строки — 404, устаревший `lease_gen` или закрытая строка — 409."""
    db = request.app.state.db
    now_ms = _now_ms(request)

    def _fn(conn):
        return presses_mod.report(
            conn,
            account_id=principal.account_id,
            press_id=press_id,
            lease_gen=body.lease_gen,
            outcome=body.outcome,
            now_ms=now_ms,
            result=body.result.model_dump() if body.result is not None else None,
            reason=body.reason,
            retry_after_ms=body.retry_after_ms,
        )

    try:
        status = await db.write(_fn)
    except presses_mod.PressError as exc:
        raise _http(exc) from exc
    return {"ok": True, "status": status}
