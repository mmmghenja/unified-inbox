"""Ручки сводки дня и слов владельца из соседних сессий (Ф86-1, docs/architecture.md.4–C1.6,
П6). Маршруты не пишут SQL — `core/digest.py`.

- `GET /digest/sources`, `GET|PUT /digest/{project_id}/{day}`, `POST /digest/{project_id}/off` — только
  bearer ровно `digest` (кука и прочий bearer — 403);
- `GET /projects/{id}/digest?day=` — кука или bearer `read` своих проектов (`digest` — 403);
- `GET /work/host/sessions/{id}/feed-extras`, `POST …/feed-extras/ack` — только хост (`work_host`).

Внутренний сокет: `GET /admin/digest?project_id=|cwd=&day=&max=` — `hub digest show`."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from hub import digest_text

from .. import connector_auth
from .. import digest as digest_mod
from ..security import DIGEST_SCOPE, Principal, principal_dependency
from .work import _host_dep
from .write import _client_name, _now_ms, read_or_agent

public_router = APIRouter(tags=["digest"])
internal_router = APIRouter(tags=["digest-admin"])

_client = Depends(principal_dependency(DIGEST_SCOPE, digest_ok=True))
_read = Depends(principal_dependency("read"))
_admin = Depends(connector_auth.admin_dependency())

_ID_MAX = 2**53
_TS_MAX = 2**62
_DAY = r"^\d{4}-\d{2}-\d{2}$"


def _audit(request: Request, principal: Principal, response: Response) -> dict:
    request_id = uuid.uuid4().hex
    response.headers["X-Request-Id"] = request_id
    return {
        "actor": principal.kind,
        "api_token_id": principal.token_id,
        "client_name": _client_name(request),
        "request_id": request_id,
    }


def _http(exc: digest_mod.DigestError) -> HTTPException:
    return HTTPException(exc.status, exc.detail)


# --- клиент сводки (скоуп `digest`) ------------------------------------------------------------------------


@public_router.get("/digest/sources")
async def get_digest_sources(
    request: Request,
    since_ts: int = Query(ge=0, le=_TS_MAX),
    until_ts: int | None = Query(None, ge=0, le=_TS_MAX),
    principal: Principal = _client,
) -> dict:
    """Источники сводки: `{now_ts, day, projects: [{id, name, store, workdir, digest_after_compact, sessions,
    says, truncated}]}`. Окно `since_ts..until_ts` (мс; `until_ts` по умолчанию — сейчас) не длиннее 48 ч,
    иначе 422. `says` — реплики владельца (`who: owner_channel`) и ответы его агента (`who: agent`), маскированные."""
    now_ms = _now_ms(request)
    with request.app.state.db.reader() as conn:
        try:
            return digest_mod.sources(
                conn, since_ts=since_ts, until_ts=now_ms if until_ts is None else until_ts, now_ms=now_ms
            )
        except digest_mod.DigestError as exc:
            raise _http(exc) from exc


@public_router.get("/digest/{project_id}/{day}")
async def get_digest(
    request: Request,
    project_id: int = Path(ge=1, le=_ID_MAX),
    day: str = Path(pattern=_DAY),
    principal: Principal = _client,
) -> dict:
    """Строка сводки `{project_id, day, text, model, upto_ts, created_ts, updated_ts, at}` или 404."""
    with request.app.state.db.reader() as conn:
        row = digest_mod.get(conn, project_id, day)
    if row is None:
        raise HTTPException(404, "not found")
    return row


class DigestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Точные пределы (не пусто, до 6 000) проверяет `digest.put` — отказ уходит русским текстом.
    text: StrictStr = Field(max_length=digest_mod.TEXT_MAX * 4)
    model: StrictStr | None = Field(default=None, max_length=digest_mod.MODEL_MAX * 4)
    upto_ts: StrictInt | None = Field(default=None, ge=0, le=_TS_MAX)


@public_router.put("/digest/{project_id}/{day}")
async def put_digest(
    body: DigestIn,
    request: Request,
    response: Response,
    project_id: int = Path(ge=1, le=_ID_MAX),
    day: str = Path(pattern=_DAY),
    principal: Principal = _client,
) -> dict:
    """Upsert сводки. День — только сегодня или вчера по времени хаба; текст непустой, до 6 000 — иначе 422; нет
    проекта — 404. Теги `<hub_digest`/`<hub_siblings` в тексте обезвреживаются."""
    now_ms = _now_ms(request)
    audit = _audit(request, principal, response)
    try:
        return await request.app.state.db.write(
            lambda conn: digest_mod.put(
                conn,
                project_id,
                day,
                text=body.text,
                model=body.model,
                upto_ts=body.upto_ts,
                now_ms=now_ms,
                audit=audit,
            )
        )
    except digest_mod.DigestError as exc:
        raise _http(exc) from exc


class OffIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: StrictStr = Field(max_length=digest_mod.REASON_MAX * 4)


@public_router.post("/digest/{project_id}/off")
async def post_digest_off(
    body: OffIn,
    request: Request,
    response: Response,
    project_id: int = Path(ge=1, le=_ID_MAX),
    principal: Principal = _client,
) -> dict:
    """Выключить «сводку после сжатия» у проекта: `{project_id, digest_after_compact: false, changed}`.
    Включает обратно только владелец. Аудит `digest_off`; при изменении — уведомление владельца («Сводка дня
    выключена у проекта N: причина»). Нет проекта — 404, пустая причина — 422."""
    now_ms = _now_ms(request)
    audit = _audit(request, principal, response)

    def _fn(conn):
        changed = digest_mod.turn_off(conn, project_id, reason=body.reason, now_ms=now_ms, audit=audit)
        return changed, digest_mod.off_notice(conn, project_id, body.reason)

    try:
        changed, (title, text) = await request.app.state.db.write(_fn)
    except digest_mod.DigestError as exc:
        raise _http(exc) from exc
    notifier = getattr(request.app.state, "notifier", None)
    ring = getattr(notifier, "ring", None)
    if changed and ring is not None:
        ring.add(title, text, "normal")
    return {"project_id": project_id, digest_mod.DIGEST_KEY: False, "changed": changed}


# --- чтение сессиями (C1.5) --------------------------------------------------------------------------------


@public_router.get("/projects/{project_id}/digest")
async def get_project_digest(
    request: Request,
    project_id: int = Path(ge=1, le=_ID_MAX),
    day: str | None = Query(None, pattern=_DAY),
    principal: Principal = read_or_agent  # Ф88-2: и агент владельца,
) -> dict:
    """Сводка проекта за день (по умолчанию — сегодня по времени хаба): строка, как у `GET /digest/…`, или 404
    (нет сводки, нет проекта или проект вне токена)."""
    if principal.project_ids is not None and project_id not in principal.project_ids:
        raise HTTPException(404, "not found")
    now_ms = _now_ms(request)
    with request.app.state.db.reader() as conn:
        row = digest_mod.get(conn, project_id, day or digest_mod.today(now_ms))
    if row is None:
        raise HTTPException(404, "not found")
    return row


# --- хост сессий хаба (П6) ---------------------------------------------------------------------------------


@public_router.get("/work/host/sessions/{session_id}/feed-extras")
async def get_feed_extras(
    request: Request,
    session_id: int = Path(ge=1, le=_ID_MAX),
    digest: int = Query(0, ge=0, le=1),
    principal: Principal = _host_dep,
) -> dict:
    """Что подать перед репликой: `{project_id, project, digest: {text, at, day, updated_ts} | null,
    digest_on, siblings: [{id, ts, role, session_id, text}], siblings_upto, siblings_on}`. Сводка — только
    при `digest=1`. Сессии нет — 404."""
    now_ms = _now_ms(request)
    with request.app.state.db.reader() as conn:
        try:
            return digest_mod.feed_extras(conn, session_id, want_digest=bool(digest), now_ms=now_ms)
        except digest_mod.DigestError as exc:
            raise _http(exc) from exc


class AckIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    siblings_upto: StrictInt = Field(ge=0, le=_TS_MAX)


@public_router.post("/work/host/sessions/{session_id}/feed-extras/ack")
async def post_feed_extras_ack(
    body: AckIn,
    request: Request,
    session_id: int = Path(ge=1, le=_ID_MAX),
    principal: Principal = _host_dep,
) -> dict:
    """Подача с блоками прошла: курсор соседей сессии — вперёд (назад не двигается). `{siblings_ts}`."""
    try:
        cursor = await request.app.state.db.write(
            lambda conn: digest_mod.ack(conn, session_id, body.siblings_upto)
        )
    except digest_mod.DigestError as exc:
        raise _http(exc) from exc
    return {"siblings_ts": cursor}


# --- `hub digest show` (C1.6) ------------------------------------------------------------------------------


@internal_router.get("/admin/digest", dependencies=[_admin])
async def get_admin_digest(
    request: Request,
    project_id: int | None = Query(None, ge=1, le=_ID_MAX),
    cwd: str | None = Query(None, min_length=1, max_length=4096),
    day: str | None = Query(None, pattern=_DAY),
    max_chars: int = Query(9500, alias="max", ge=200, le=100_000),
) -> dict:
    """`{project_id, project, day, digest: строка | null, block: готовый `<hub_digest>` | null}`. Проект —
    `project_id` или по `cwd` (самое длинное совпадение рабочей папки); не нашёлся — `project_id: null`.
    `block` не длиннее `max` символов (текст сводки укорачивается)."""
    if (project_id is None) == (cwd is None):
        raise HTTPException(422, "нужно одно из: project_id, cwd")
    now_ms = _now_ms(request)
    day = day or digest_mod.today(now_ms)
    with request.app.state.db.reader() as conn:
        pid = project_id if project_id is not None else digest_mod.project_by_cwd(conn, cwd or "")
        row = digest_mod.get(conn, pid, day) if pid is not None else None
        found = conn.execute("SELECT name FROM main.project WHERE id = ?", (pid,)).fetchone() if pid else None
    name = (found[0] if found else None) or (f"#{pid}" if pid else None)
    block = None
    if row is not None:
        block = digest_text.digest_block(row["text"], at=row["at"], project=name or "")
        over = len(block) - max_chars
        if over > 0:
            text = digest_text.clip(row["text"], max(0, len(row["text"]) - over))
            block = digest_text.digest_block(text, at=row["at"], project=name or "")
    return {"project_id": pid, "project": name, "day": day, "digest": row, "block": block}
