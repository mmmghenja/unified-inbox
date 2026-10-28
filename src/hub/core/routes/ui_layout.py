"""Раскладка доски «Главная» (Ф41-6, раздел 1): `GET/PUT /ui/layout`. Только кука интерфейса (`ui_only`),
с телефона тоже. Хранение и нормализация — `core/ui_layout.py`.
"""

from fastapi import APIRouter, HTTPException, Request

from .. import ui_layout as layout_mod
from ..security import Principal
from .write import ui_only

public_router = APIRouter(tags=["ui-layout"])


@public_router.get("/ui/layout")
async def get_ui_layout(request: Request, principal: Principal = ui_only) -> dict:
    with request.app.state.db.reader() as conn:
        return {"layout": layout_mod.get_layout(conn)}


@public_router.put("/ui/layout")
async def put_ui_layout(body: dict, request: Request, principal: Principal = ui_only) -> dict:
    try:
        layout = layout_mod.normalize(body.get("layout"))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    saved = await request.app.state.db.write(lambda conn: layout_mod.save_layout(conn, layout))
    return {"layout": saved}
