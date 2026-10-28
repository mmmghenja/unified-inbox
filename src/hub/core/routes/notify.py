"""Уведомления владельца через ядро (Ф33-2, docs/architecture.md).

- `POST /internal/v1/notify` (внутренний сокет, секрет админа): служба хаба кладёт уведомление в кольцо;
  ответ `listened` — слушали ли кольцо последние 3 минуты (нет — отправитель сам зовёт `notify-send`);
- `GET /api/v1/notify/wait?after=N&epoch=E&timeout_s=50` (bearer ровно со скоупом `notify`, `hub notify run`):
  long-poll. Без `after` — «с этого момента»: сразу отвечает текущим номером. `epoch` другой (ядро
  перезапускалось) или `after` больше текущего номера — отдаётся всё кольцо с начала, `reset: true`.
Текст уведомления в журнал ядра не пишется."""

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, StrictStr

from .. import connector_auth
from ..notify_ring import NotifyRing
from ..security import NOTIFY_SCOPE, Principal, principal_dependency

public_router = APIRouter(tags=["notify"])
internal_router = APIRouter(tags=["notify-internal"])

_notify = Depends(principal_dependency(NOTIFY_SCOPE, notify_ok=True))
_admin = Depends(connector_auth.admin_dependency())

MAX_WAIT_S = 55
TITLE_MAX = 200
TEXT_MAX = 2000


class NotifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: StrictStr
    text: StrictStr
    urgency: Literal["critical", "normal", "low"] = "critical"


def _ring(request: Request) -> NotifyRing:
    return request.app.state.notifier.ring


@internal_router.post("/notify", dependencies=[_admin])
async def post_notify(body: NotifyIn, request: Request) -> dict:
    item = _ring(request).add(body.title[:TITLE_MAX], body.text[:TEXT_MAX], body.urgency)
    return {"id": item["id"], "listened": _ring(request).listened()}


@public_router.get("/notify/wait")
async def get_notify_wait(
    request: Request,
    after: int | None = Query(None, ge=0),
    epoch: str | None = Query(None, max_length=32),
    timeout_s: int = Query(50, ge=0, le=MAX_WAIT_S),
    _principal: Principal = _notify,
) -> dict:
    ring = _ring(request)
    ring.touch()
    base = {"epoch": ring.epoch}
    if after is None:
        return {**base, "items": [], "last": ring.last, "reset": False}
    reset = (epoch is not None and epoch != ring.epoch) or after > ring.last
    if reset:
        return {**base, "items": ring.after(0), "last": ring.last, "reset": True}
    items = await ring.wait(after, timeout_s)
    return {**base, "items": items, "last": ring.last, "reset": False}
