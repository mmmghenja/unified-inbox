"""Ручки заявок на правило раскладки (Ф38-2, docs/architecture.md).

`POST /api/v1/rule-requests` — только bearer со скоупом `draft` (запреты `check_attach`: не UI-сессия, не
дежурная, не оркестратор, не `background`); `GET /api/v1/rule-requests` — скоуп `read`, только видимые
проекты; `POST /api/v1/rule-requests/{id}/approve|decline` — только UI-сессия (`ui_only`: bearer -> 403 с
аудитом `ui_only_denied`), CSRF — middleware. Маршруты не пишут SQL: `core/rule_requests.py`.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from .. import autopilot as autopilot_mod
from .. import card_route as card_route_mod
from .. import decisions as decisions_mod
from .. import owner_queue as zq
from .. import rule_requests as rr_mod
from ..security import Principal, principal_dependency
from .subs import SessionIn
from .write import _client_name, _new_request_id, _now_ms, ui_only

public_router = APIRouter(tags=["rule-requests"])

_draft = Depends(principal_dependency("draft"))
_read = Depends(principal_dependency("read"))


def _http(exc: rr_mod.RuleRequestError) -> HTTPException:
    status = {
        rr_mod.Invalid: 422,
        rr_mod.Denied: 403,
        rr_mod.NotFound: 404,
        rr_mod.Conflict: 409,
        rr_mod.TooMany: 429,
    }.get(type(exc), 422)
    return HTTPException(status, str(exc))


class RuleRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: StrictInt = Field(ge=-(2**53), le=2**53)
    account_id: StrictInt = Field(ge=1, le=2**53)
    kind: StrictStr = Field(max_length=32)
    # `folder` — номер папки Telegram, `domain`/`label` — строка, `all` — не задаётся.
    value: StrictInt | StrictStr | None = None
    reason: StrictStr | None = Field(default=None, max_length=rr_mod.REASON_MAX)
    # Решение 7: сессия, опознанная MCP (pid + `starttime` + разговор), — по ней ядро находит имя линии
    # подписки (`who`); без неё заявка обычная, «кто просит» пуст.
    session: SessionIn | None = None
    # Ф41-5б П1: ключ запуска сессии хаба — только для `autopilot.session_free`; не сохраняется.
    session_key: StrictStr | None = Field(default=None, max_length=128)


@public_router.post("/rule-requests")
async def post_rule_request(
    body: RuleRequestIn, request: Request, response: Response, principal: Principal = _draft
) -> dict:
    """Заявка сессии на правило раскладки. 201 — новая, 200 — уже открытая с тем же условием в тот же проект.
    403 — запреты `check_attach`; 404 — проект не виден или нет аккаунта; 409 — такое правило уже есть /
    условие уже ведёт в другой проект / владелец отказал за 7 дней; 422 — вид или значение; 429 — лимиты."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    client_name = _client_name(request)
    session = body.session.model_dump() if body.session is not None else None

    def _fn(conn):
        item, created = rr_mod.create(
            conn,
            principal=principal,
            project_id=body.project_id,
            account_id=body.account_id,
            kind=body.kind,
            value=body.value,
            reason=body.reason,
            session=session,
            now_ms=now_ms,
            client_name=client_name,
            request_id=request_id,
        )
        # Ф41-5б (5.5, П6): сессии хаба в авто правило заводится сразу — только при снятых и `map_rule`,
        # и `move_channel` (правило перекладывает существующие беседы и всё будущее входящее).
        free = None
        if created and item["status"] == "pending":
            free = autopilot_mod.session_free(
                conn, autopilot_mod.ident(session, body.session_key), "map_rule", body.project_id
            )
            if free is not None and not autopilot_mod.switch_free(conn, "move_channel", body.project_id):
                free = None
        if free is not None:
            try:
                decided = rr_mod.decide(
                    conn,
                    item["id"],
                    "approve",
                    now_ms=now_ms,
                    api_token_id=principal.token_id,
                    client_name=client_name,
                    audit_request_id=request_id,
                    auto_session=free.id,
                )
            except rr_mod.Conflict:
                pass  # правило не собралось — заявка остаётся ждать владельца
            else:
                # Ответ просителю — в его видимости (названия бесед и папок — только `full`).
                req = rr_mod.get(conn, decided["id"])
                item = rr_mod.request_dict(conn, req, full=rr_mod.full_view(principal))
        if created and item["status"] == "pending":
            # Ф66-1 (П9): карточка владельца — «сразу да» по таблице маршрута; ответ сессии — как до клика.
            card_route_mod.auto_answer(conn, f"rule:{item['id']}", now_ms=now_ms)
        return item, created

    try:
        item, created = await db.write(_fn)
    except rr_mod.RuleRequestError as exc:
        raise _http(exc) from exc
    response.status_code = 201 if created else 200
    return item


@public_router.get("/rule-requests")
async def get_rule_requests(
    request: Request,
    project_id: int | None = Query(None, ge=1, le=2**53),
    status: Literal["open", "pending", "approved", "declined"] | None = Query(None),
    limit: int = Query(rr_mod.LIST_MAX, ge=1, le=rr_mod.LIST_MAX),
    principal: Principal = _read,
) -> list[dict]:
    """Заявки видимых проектов; по умолчанию — открытые и решённые за 7 дней, свежие первыми."""
    now_ms = _now_ms(request)
    with request.app.state.db.reader() as conn:
        return rr_mod.list_requests(
            conn, principal, now_ms=now_ms, project_id=project_id, status=status, limit=limit
        )


async def _decide(
    request: Request, response: Response, principal: Principal, request_no: int, action: str
) -> dict:
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    client_name = _client_name(request)

    def _fn(conn):
        zq.take_from_agent(conn, f"rule:{request_no}", now_ms=now_ms)  # Ф65-1 Ж1
        decided = rr_mod.decide(
            conn,
            request_no,
            action,
            now_ms=now_ms,
            api_token_id=principal.token_id,
            client_name=client_name,
            audit_request_id=request_id,
        )
        decisions_mod.record(conn, f"rule:{request_no}", decisions_mod.BY_UI, action, now_ms=now_ms)  # Ф76-1
        return decided

    try:
        return await db.write(_fn)
    except rr_mod.RuleRequestError as exc:
        raise _http(exc) from exc


@public_router.post("/rule-requests/{request_no}/approve")
async def post_rule_request_approve(
    request_no: int, request: Request, response: Response, principal: Principal = ui_only
) -> dict:
    """Решение 1: «Завести» — только владелец (bearer -> 403); только из `pending`, иначе 409. Условие больше не
    собирается (папку удалили, аккаунта нет) — 409 с текстом, заявка остаётся `pending`."""
    return await _decide(request, response, principal, request_no, "approve")


@public_router.post("/rule-requests/{request_no}/decline")
async def post_rule_request_decline(
    request_no: int, request: Request, response: Response, principal: Principal = ui_only
) -> dict:
    """«Отклонить» — только владелец; только из `pending`, иначе 409."""
    return await _decide(request, response, principal, request_no, "decline")
