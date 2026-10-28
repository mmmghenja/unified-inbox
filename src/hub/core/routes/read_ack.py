"""Ручки очереди «прочитано в источник» (Ф8-7, docs/architecture.md и поправки П2, П7) —
внутренний сокет, секрет коннектора, по образцу медиа (`routes/media.py`): `GET /read-ack/wait` (long-poll),
`POST /read-ack/lease`, `POST /read-ack/report`. Маршруты не пишут SQL: чтение и запись — `core/read_ack.py`.
"""

import asyncio
import contextlib
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .. import connector_auth
from .. import read_ack as read_ack_mod
from ..connector_auth import ConnectorPrincipal

internal_router = APIRouter(tags=["internal-read-ack"])

_connector = Depends(connector_auth.connector_dependency())

_MAX_WAIT_S = 30.0
# Нижняя граница одного ожидания до ближайшего срока: часы ядра и цикла событий — разные, не крутиться
# впустую.
_MIN_DUE_WAIT_S = 0.05
_MAX_RETRY_AFTER_MS = 86_400_000


def _now_ms(request: Request) -> int:
    return int(request.app.state.clock() * 1000)


def _state(request: Request, principal: ConnectorPrincipal) -> tuple[bool, int | None, int]:
    db = request.app.state.db
    now_ms = _now_ms(request)
    with db.reader() as conn:
        ready, due = read_ack_mod.wait_state(conn, account_id=principal.account_id, now_ms=now_ms)
    return ready, due, now_ms


@internal_router.get("/read-ack/wait")
async def read_ack_wait(
    request: Request,
    timeout_s: float = Query(25.0, ge=0),
    principal: ConnectorPrincipal = _connector,
) -> dict:
    """Long-poll: «готово» = `lease` выдал бы хоть что-то своему аккаунту. Поколение `Notifier` снимается ДО
    чтения БД (уведомление между проверкой и ожиданием не теряется); ожидание ограничено ещё и временем до
    ближайшего срока строки аккаунта — иначе отметка с задержкой 5 с ждала бы конца long-poll (П2: срок —
    только у строк, которые аккаунт вообще может получить)."""
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


class ReadAckLeaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int | None = Field(default=None, ge=1)


@internal_router.post("/read-ack/lease")
async def read_ack_lease(
    body: ReadAckLeaseIn, request: Request, principal: ConnectorPrincipal = _connector
) -> list[dict]:
    """A3 + П1/П7: задания `{channel_id, gen, lease_gen, channel: {ext_id, kind, parent_ext_id}, message:
    {ext_id, ts, ext_sort, thread_ext_id, is_me}}`, по умолчанию по одному."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    limit = body.limit if body.limit is not None else 1
    return await db.write(
        lambda conn: read_ack_mod.lease(conn, account_id=principal.account_id, now_ms=now_ms, limit=limit)
    )


class ReadAckReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: int
    lease_gen: int
    outcome: Literal["done", "retry", "drop"]
    retry_after_ms: int | None = Field(default=None, ge=0, le=_MAX_RETRY_AFTER_MS)


@internal_router.post("/read-ack/report")
async def read_ack_report(
    body: ReadAckReportIn, request: Request, principal: ConnectorPrincipal = _connector
) -> dict:
    """A4: чужой канал/нет строки — 404, устаревший `lease_gen` — 409."""
    db = request.app.state.db
    now_ms = _now_ms(request)

    def _fn(conn):
        return read_ack_mod.report(
            conn,
            account_id=principal.account_id,
            channel_id=body.channel_id,
            lease_gen=body.lease_gen,
            outcome=body.outcome,
            now_ms=now_ms,
            retry_after_ms=body.retry_after_ms,
        )

    try:
        result = await db.write(_fn)
    except read_ack_mod.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    except read_ack_mod.StaleLease as exc:
        raise HTTPException(409, "stale lease") from exc
    return {"ok": True, "result": result}
