"""Ручки «Разобрано сессией» (Ф37-3, docs/architecture.md и C). Маршруты не пишут SQL:
проверки и запись — `core/handled.py` внутри `Db.write()`.

- `POST /channels/{id}/handled` — bearer подписанной сессии проекта с ролью (условия B1 — `handled.mark`).
- (ручки чтения и скрытия блока на главной убраны в Ф51-1 D.)
"""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictInt

from .. import handled as handled_mod
from .. import queries
from ..security import Principal, principal_dependency
from .write import SendSessionIn

router = APIRouter(tags=["handled"])

# message_id из hub_read или "last" — последнее чужое живое сообщение беседы (выбирает ядро в транзакции).
def _check_up_to(value):
    if value != "last" and not 1 <= value <= 2**62:
        raise ValueError("up_to_message_id: от 1 до 2**62")
    return value


_UpTo = Annotated[StrictInt | Literal["last"], AfterValidator(_check_up_to)]

_authed = Depends(principal_dependency())


def _now_ms(request: Request) -> int:
    return int(request.app.state.clock() * 1000)


def _client_name(request: Request) -> str | None:
    name = request.headers.get("x-hub-client")
    return None if name is None else name[:64]


class HandledIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    up_to_message_id: _UpTo
    # Длину (3..200 после обрезки пробелов) проверяет `handled.note_form` — отказ словами, а не схемой.
    note: str = Field(max_length=2000)
    # Сессия вызова (pid + `starttime` + разговор), как у черновика Ф37-1; без неё — 403.
    session: SendSessionIn | None = None


class HandledItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: StrictInt = Field(ge=1, le=2**62)
    up_to_message_id: _UpTo
    note: str = Field(max_length=2000)


HANDLED_BATCH_MAX = 50


class HandledBatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Ф66-3 D: до 50 бесед за вызов; больше — 422 схемой.
    items: list[HandledItemIn] = Field(min_length=1, max_length=HANDLED_BATCH_MAX)
    session: SendSessionIn | None = None


@router.post("/handled/batch")
async def post_handled_batch(
    body: HandledBatchIn, request: Request, response: Response, principal: Principal = _authed
) -> dict:
    """Ф66-3 D: пачка отметок «разобрано» одной транзакцией. Каждый элемент — тот же `handled.mark` с теми же
    проверками; исход по элементу: `{channel_id, ok: true, ...}` или `{channel_id, ok: false, error}` (ошибка
    одного остальных не отменяет — отказ `mark` ничего не пишет). Лимит часа общий: кончился посреди пачки —
    остальным `error: "rate_limited"`."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = uuid.uuid4().hex
    response.headers["X-Request-Id"] = request_id
    client_name = _client_name(request)
    sub_waiters = getattr(request.app.state, "sub_waiters", None)

    def _fn(conn):
        name_row = conn.execute(
            "SELECT name FROM main.api_token WHERE id = ?", (principal.token_id,)
        ).fetchone()
        token_name = name_row[0] if name_row and isinstance(name_row[0], str) else None
        results: list[dict] = []
        for item in body.items:
            try:
                project_id = queries.channel_visible(conn, principal, item.channel_id)
                done = handled_mod.mark(
                    conn,
                    principal=principal,
                    token_name=token_name,
                    session=body.session,
                    channel_id=item.channel_id,
                    project_id=project_id,
                    up_to_message_id=item.up_to_message_id,
                    note=item.note,
                    now_ms=now_ms,
                    waiting=sub_waiters.waiting if sub_waiters is not None else None,
                    client_name=client_name,
                    request_id=request_id,
                )
                results.append({"channel_id": item.channel_id, "ok": True, **done})
            except queries.NotFound:
                results.append({"channel_id": item.channel_id, "ok": False, "error": "not found"})
            except handled_mod.HandledError as exc:
                error = "rate_limited" if exc.status == 429 else exc.detail
                results.append({"channel_id": item.channel_id, "ok": False, "error": error})
        return results

    return {"results": await db.write(_fn)}


@router.post("/channels/{channel_id}/handled")
async def post_channel_handled(
    channel_id: int, body: HandledIn, request: Request, response: Response, principal: Principal = _authed
) -> dict:
    """B1–B4: подписанная сессия проекта с ролью отмечает беседу разобранной до сообщения. 403 — не та сессия
    (нет `session`, процесс мёртв, нет живой подписки на проект беседы с ролью, дежурная, оркестратор,
    UI-сессия) или отметка выключена в проекте (с командой для владельца); 404 — беседа не видна; 422 — архив,
    заготовка, чужое сообщение, пометка; 429 — лимит 60 в час на токен. Отказ ничего не пишет."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = uuid.uuid4().hex
    response.headers["X-Request-Id"] = request_id
    client_name = _client_name(request)
    sub_waiters = getattr(request.app.state, "sub_waiters", None)

    def _fn(conn):
        project_id = queries.channel_visible(conn, principal, channel_id)
        name_row = conn.execute(
            "SELECT name FROM main.api_token WHERE id = ?", (principal.token_id,)
        ).fetchone()
        return handled_mod.mark(
            conn,
            principal=principal,
            token_name=name_row[0] if name_row and isinstance(name_row[0], str) else None,
            session=body.session,
            channel_id=channel_id,
            project_id=project_id,
            up_to_message_id=body.up_to_message_id,
            note=body.note,
            now_ms=now_ms,
            waiting=sub_waiters.waiting if sub_waiters is not None else None,
            client_name=client_name,
            request_id=request_id,
        )

    try:
        return await db.write(_fn)
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    except handled_mod.HandledError as exc:
        raise HTTPException(exc.status, exc.detail) from exc
