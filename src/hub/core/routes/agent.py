"""Ручки карточек дежурного агента `/api/v1/agent/items`. Спека `docs/architecture.md`, A2–A3 и
поправки (П2, П5, П10/К3, мелочи). Маршруты не пишут SQL: видимость и чтение — `queries.py`, запись —
`agent_items.py`, в одной транзакции `Db.write()` вместе с проверкой видимости.

- `POST /agent/items` — скоуп `triage` (разборщик; `background` и `interactive`). Аудита нет: это поток
  записей агента, `api_token_id` стоит на самой строке. При разделении карточки (Ф11-2 A2) в ответе
  `closed_item_id`/`closed_status`; `status` — статус итоговой карточки (Ф11-9 H4).
- `POST /agent/items/close-answered` — скоуп `triage` (Ф11-2 A3): закрыть ОТВЕЧЕННУЮ открытую карточку беседы.
- `GET /agent/items` — скоуп `read`.
- `POST /agent/items/{id}/status` — UI-сессия или bearer `interactive` (скоуп `read`); `background` -> 403:
  разборщик свои карточки не закрывает.
- `POST /agent/items/channel-status` — то же по беседе: все её открытые карточки одним вызовом (Ф37-2 B1).

Журнал запусков Claude `GET /api/v1/agent/runs` (Ф8-6, `docs/architecture.md`) — скоуп `read`;
запись запусков дежурной убрана в Ф57-2 (строки пишут сессии хаба).
"""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .. import agent_items, queries, subs
from .. import handled as handled_mod
from ..security import Principal, principal_dependency
from .write import SendSessionIn

router = APIRouter(tags=["agent"])

_triage = Depends(principal_dependency("triage"))
_read = Depends(principal_dependency("read"))


def _now_ms(request: Request) -> int:
    return int(request.app.state.clock() * 1000)


class AgentItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: int
    message_ids: list[int] = Field(min_length=1, max_length=agent_items.MAX_MESSAGE_IDS)
    label: Literal["urgent", "reply", "fyi", "noise"]
    summary: str = Field(min_length=1, max_length=1000)
    action: str | None = Field(default=None, max_length=500)
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=64)


@router.post("/agent/items")
async def post_agent_item(body: AgentItemIn, request: Request, principal: Principal = _triage) -> dict:
    db = request.app.state.db
    now_ms = _now_ms(request)

    def _fn(conn):
        queries.channel_visible(conn, principal, body.channel_id)
        result = agent_items.upsert(
            conn,
            channel_id=body.channel_id,
            message_ids=body.message_ids,
            label=body.label,
            summary=body.summary,
            action=body.action,
            provider=body.provider,
            model=body.model,
            api_token_id=principal.token_id,
            now_ms=now_ms,
        )
        if not result.noop:
            # Ф13-1 B3: сигнал подписке проекта — в той же транзакции, по итоговой строке карточки.
            subs.signal_for_item(conn, result.item_id, now_ms=now_ms)
        return result

    try:
        result = await db.write(_fn)
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    except agent_items.AgentItemError as exc:
        raise HTTPException(422, str(exc)) from exc
    # Ф11-9 H4: `status` итоговой карточки — новая, рождённая уже отвеченной, сразу `done`.
    body_out: dict = {
        "id": result.item_id,
        "created": result.created,
        "noop": result.noop,
        "status": result.status,
    }
    if result.closed_item_id is not None:
        # Ф11-2 A2: открытая карточка закрыта при разделении (отвечена -> `done`, прочитана -> `seen`).
        body_out["closed_item_id"] = result.closed_item_id
        body_out["closed_status"] = result.closed_status
    return body_out


class CloseAnsweredIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: int


@router.post("/agent/items/close-answered")
async def post_agent_items_close_answered(
    body: CloseAnsweredIn, request: Request, principal: Principal = _triage
) -> dict:
    """Ф11-2 A3: разборщик увидел моё отправленное сообщение — ядро само проверяет «отвечено» открытой
    карточки беседы в той же транзакции и закрывает её (`done`). Фоновый токен закрыть карточку по своему
    усмотрению по-прежнему не может. Без аудита — поток записей агента, как `POST /agent/items`."""
    db = request.app.state.db

    def _fn(conn):
        queries.channel_visible(conn, principal, body.channel_id)
        return agent_items.close_answered(conn, body.channel_id)

    try:
        closed = await db.write(_fn)
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    return {"closed": closed}


@router.get("/agent/items")
async def get_agent_items(
    request: Request,
    status: str = "new",
    project: int | None = None,
    label: str | None = None,
    channel: int | None = None,
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = _read,
) -> list[dict]:
    if status not in ("any", queries.AGENT_ITEM_OPEN) and status not in queries.AGENT_ITEM_STATUSES:
        raise HTTPException(422, "invalid status")
    if label is not None and label not in queries.AGENT_ITEM_LABELS:
        raise HTTPException(422, "invalid label")
    db = request.app.state.db
    try:
        with db.reader() as conn:
            return queries.list_agent_items(
                conn,
                principal,
                status=status,
                project_id=project,
                label=label,
                channel_id=channel,
                limit=limit,
            )
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc


class AgentItemStatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["seen", "done", "dismissed"]
    # Сессия вызова: закрытие карточки сессией с ролью заодно снимает беседу с «Нужна реакция».
    session: SendSessionIn | None = None


def _handled_by_close(request: Request, conn, principal, session, channel_id, item_ids, status) -> int | None:
    """Закрытые (`done`/`dismissed`) сессией карточки -> `handled.mark_closed_cards`; id сообщения."""
    if status == "seen" or not item_ids:
        return None
    sub_waiters = getattr(request.app.state, "sub_waiters", None)
    result = handled_mod.mark_closed_cards(
        conn,
        principal=principal,
        session=session,
        channel_id=channel_id,
        item_ids=item_ids,
        now_ms=_now_ms(request),
        waiting=sub_waiters.waiting if sub_waiters is not None else None,
        client_name=_client_name(request),
        request_id=uuid.uuid4().hex,
    )
    return None if result is None else result["message_id"]


@router.post("/agent/items/{item_id}/status")
async def post_agent_item_status(
    item_id: int, body: AgentItemStatusIn, request: Request, principal: Principal = _read
) -> dict:
    if principal.kind == "background":
        raise HTTPException(403, "forbidden: background token cannot close agent items")
    db = request.app.state.db

    def _fn(conn):
        channel_id = queries.agent_item_channel(conn, principal, item_id)
        status = agent_items.set_status(conn, item_id, status=body.status)
        handled = _handled_by_close(request, conn, principal, body.session, channel_id, [item_id], status)
        return status, channel_id, handled

    try:
        status, channel_id, handled = await db.write(_fn)
    except (queries.NotFound, agent_items.NotFound) as exc:
        raise HTTPException(404, "not found") from exc
    except agent_items.InvalidTransition as exc:
        raise HTTPException(409, "invalid transition") from exc
    return {"id": item_id, "status": status, "channel_id": channel_id, "handled_up_to": handled}


class AgentChannelStatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: int
    status: Literal["seen", "done", "dismissed"]
    session: SendSessionIn | None = None


@router.post("/agent/items/channel-status")
async def post_agent_items_channel_status(
    body: AgentChannelStatusIn, request: Request, principal: Principal = _read
) -> dict:
    """Ф37-2 B1: отметить ВСЕ открытые карточки беседы одним вызовом (сигнал «лично мне» номеров карточек не
    несёт). Те же права, что у отметки по номеру: UI-сессия или bearer `interactive`, `background` -> 403;
    беседа обязана быть видна токену (чужой проект — 404). Ответ — номера изменённых карточек."""
    if principal.kind == "background":
        raise HTTPException(403, "forbidden: background token cannot close agent items")
    db = request.app.state.db

    def _fn(conn):
        queries.channel_visible(conn, principal, body.channel_id)
        ids = agent_items.set_channel_status(conn, body.channel_id, status=body.status)
        handled = _handled_by_close(request, conn, principal, body.session, body.channel_id, ids, body.status)
        return ids, handled

    try:
        ids, handled = await db.write(_fn)
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    return {"channel_id": body.channel_id, "status": body.status, "ids": ids, "handled_up_to": handled}


# --- Журнал запусков Claude (Ф8-6 A1) --------------------------------------------------------------------


def _client_name(request: Request) -> str | None:
    name = request.headers.get("x-hub-client")
    return None if name is None else name[:64]


@router.get("/agent/runs")
async def get_agent_runs(
    request: Request,
    since: int | None = Query(None, ge=0),
    project: int | None = None,
    limit: int = Query(100, ge=1, le=500),
    principal: Principal = _read,
) -> list[dict]:
    db = request.app.state.db
    try:
        with db.reader() as conn:
            return queries.list_agent_runs(conn, principal, since=since, project_id=project, limit=limit)
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc
