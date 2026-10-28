"""`POST /presence` и `GET /presence` — присутствие владельца в беседе. Спецификация:
docs/architecture.md и поправка П8. Хранилище — в памяти (`app.state.presence`), в БД ничего
не пишется и аудита нет (UI шлёт раз в 20 с). Видимость канала — через `queries.channel_visible`.

- `POST` — ТОЛЬКО UI-сессия (cookie + CSRF — middleware), bearer -> 403 (`ui_only`, с аудитом отказа).
- `GET` — скоуп `read`; проектный токен видит только каналы своих проектов.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .. import queries
from ..security import Principal, principal_dependency
from .write import ui_only

router = APIRouter(tags=["presence"])

_read = Depends(principal_dependency("read"))


def _now_ms(request: Request) -> int:
    return int(request.app.state.clock() * 1000)


class PresenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # П8: случайный id вкладки (UI создаёт при загрузке) — `null` снимает запись только этой вкладки.
    client_id: str = Field(min_length=1, max_length=64)
    channel_id: int | None = None
    thread_ext_id: str | None = Field(default=None, max_length=200)


@router.post("/presence")
async def post_presence(body: PresenceIn, request: Request, principal: Principal = ui_only) -> dict:
    db = request.app.state.db
    if body.channel_id is not None:
        try:
            with db.reader() as conn:
                queries.channel_visible(conn, principal, body.channel_id)
        except queries.NotFound as exc:
            raise HTTPException(404, "not found") from exc
    request.app.state.presence.set(
        token_id=principal.token_id,
        client_id=body.client_id,
        channel_id=body.channel_id,
        thread_ext_id=body.thread_ext_id if body.channel_id is not None else None,
        now_ms=_now_ms(request),
    )
    return {"ok": True}


@router.post("/presence/active")
async def post_presence_active(request: Request, principal: Principal = ui_only) -> dict:
    """Ф20-3 A1: UI шлёт раз в 60 с, пока окно видимо, в фокусе и было действие за 5 мин. Только UI-сессия
    (bearer -> 403 `ui_only`); в БД и аудит ничего не пишется."""
    del principal
    request.app.state.activity.touch(_now_ms(request))
    return {"ok": True}


@router.get("/presence")
async def get_presence(request: Request, principal: Principal = _read) -> dict:
    items = request.app.state.presence.items(_now_ms(request))
    if principal.project_ids is not None and items:
        db = request.app.state.db
        visible: dict[int, bool] = {}
        with db.reader() as conn:
            for channel_id, _thread in items:
                if channel_id in visible:
                    continue
                try:
                    queries.channel_visible(conn, principal, channel_id)
                    visible[channel_id] = True
                except queries.NotFound:
                    visible[channel_id] = False
        items = [item for item in items if visible[item[0]]]
    return {"items": [{"channel_id": cid, "thread_ext_id": thread} for cid, thread in items]}
