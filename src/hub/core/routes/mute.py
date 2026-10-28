"""Ф99-2 (docs/architecture.md, П5–П7): `POST /channels/{id}/mute` — подписанная сессия глушит
беседу или снимает свою заглушку. Маршрут SQL не пишет: проверки и запись — `core/mute.py` внутри
`Db.write()`."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .. import mute as mute_mod
from .. import queries
from ..security import Principal, principal_dependency
from .write import SendSessionIn

router = APIRouter(tags=["mute"])

_authed = Depends(principal_dependency())


class MuteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    on: StrictBool = True
    # Длину (3..200 после обрезки пробелов) проверяет `mute.note_form` — отказ словами, а не схемой.
    note: str = Field(max_length=2000)
    # Сессия вызова (pid + `starttime` + разговор), как у `hub_handled`; без неё — 403.
    session: SendSessionIn | None = None


@router.post("/channels/{channel_id}/mute")
async def post_channel_mute(
    channel_id: int, body: MuteIn, request: Request, response: Response, principal: Principal = _authed
) -> dict:
    """403 — не та сессия (UI, нет `session`, процесс мёртв, нет живой подписки с ролью на проект беседы
    или на проект, слышащий её «Неразобранное»); 404 — беседа не видна; 409 — ручной режим владельца или снять
    заглушку владельца; 422 — архив, заготовка, причина; 429 — предел в час на подписку. Отказ ничего не
    пишет."""
    if principal.is_ui:
        raise HTTPException(403, "заглушка беседы: только для сессий (владелец глушит кнопкой в шапке беседы)")
    db = request.app.state.db
    now_ms = int(request.app.state.clock() * 1000)
    request_id = uuid.uuid4().hex
    response.headers["X-Request-Id"] = request_id
    client = request.headers.get("x-hub-client")
    client_name = None if client is None else client[:64]
    sub_waiters = getattr(request.app.state, "sub_waiters", None)

    def _fn(conn):
        project_id = queries.channel_visible(conn, principal, channel_id)
        name_row = conn.execute(
            "SELECT name FROM main.api_token WHERE id = ?", (principal.token_id,)
        ).fetchone()
        return mute_mod.mute(
            conn,
            principal=principal,
            token_name=name_row[0] if name_row and isinstance(name_row[0], str) else None,
            session=body.session,
            channel_id=channel_id,
            project_id=project_id,
            on=body.on,
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
    except mute_mod.MuteError as exc:
        raise HTTPException(exc.status, exc.detail) from exc
