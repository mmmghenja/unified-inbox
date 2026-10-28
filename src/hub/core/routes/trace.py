"""Ручка `hub trace` (Ф97-1, docs/architecture.md): внутренняя, только чтение, админ-сокет."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .. import connector_auth
from .. import trace as trace_mod

internal_router = APIRouter(tags=["trace-internal"])

_admin = Depends(connector_auth.admin_dependency())


@internal_router.get("/admin/trace")
async def get_trace(
    request: Request,
    ref: str | None = Query(None, min_length=1, max_length=200),
    around_ms: int | None = Query(None, ge=0),
    min: int = Query(15, ge=1, le=24 * 60),  # noqa: A002 — имя параметра из спеки
    project_id: int | None = Query(None, ge=1),
    _: None = _admin,
) -> dict:
    if (ref is None) == (around_ms is None):
        raise HTTPException(422, "нужен ровно один из параметров: ref или around_ms")
    with request.app.state.db.reader() as conn:
        if ref is not None:
            body = trace_mod.by_ref(conn, ref)
            if body is None:
                raise HTTPException(404, "по ref ничего нет")
            return body
        return trace_mod.around(conn, around_ms, min, project_id)
