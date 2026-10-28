"""Ручки медиа (Ф8-5, docs/architecture.md, A2–A3 и поправки П3–П9, П12, П14). Два роутера:

- `public_router` (`/api/v1`): `GET /media/{id}` — отдача файла (скоуп `read` + `project_scope` через
  сообщение -> канал), `POST /messages/{id}/media/{idx}/fetch` — «Загрузить» (только UI-сессия,
  CSRF — middleware).
- `internal_router` (`/internal/v1`, секрет коннектора): `GET /media/wait`, `POST /media/lease`,
  `PUT /media/{id}/content`, `POST /media/{id}/report` — очередь загрузки по образцу outbox.

Маршруты не пишут SQL: чтение — `queries`, запись — `media` внутри `Db.write()`. Журнал — только `media_id` и
класс ошибки (без имён файлов, URL и текстов).
"""

import asyncio
import contextlib
import hashlib
import logging
import os
import stat
import unicodedata
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from starlette.requests import ClientDisconnect

from .. import connector_auth, queries
from .. import media as media_mod
from ..audit import write_audit
from ..connector_auth import ConnectorPrincipal
from ..security import Principal, principal_dependency
from .write import _client_name, ui_only

_logger = logging.getLogger(__name__)

public_router = APIRouter(tags=["media"])
internal_router = APIRouter(tags=["internal-media"])

_read = Depends(principal_dependency("read"))
_connector = Depends(connector_auth.connector_dependency())

_MAX_WAIT_S = 30.0
_HEAD_BYTES = 64

# A3: тип ответа — ТОЛЬКО по сигнатуре байтов (`detected`). Инлайн — растровые картинки, видео и аудио;
# всё остальное (pdf, SVG, HTML, неизвестное) — `application/octet-stream` скачиванием.
_INLINE_TYPES = {
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
    "mp4": "video/mp4",
    "webm": "video/webm",
    "ogg": "audio/ogg",
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
}
_MEDIA_CSP = "default-src 'none'; sandbox; frame-ancestors 'none'"
_MEDIA_CACHE = "private, max-age=86400"
_MAX_FILENAME = 200


def _now_ms(request: Request) -> int:
    return int(request.app.state.clock() * 1000)


def safe_filename(name: str | None, media_id: int) -> str:
    """Имя для `Content-Disposition`: без путей и управляющих символов (включая невидимые Cf — RTL-подмена
    расширения), не пустое."""
    raw = name or ""
    raw = raw.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(ch for ch in raw if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Zl", "Zp"))
    cleaned = cleaned.strip().strip(".").strip()
    if not cleaned:
        cleaned = f"file-{media_id}"
    return cleaned[:_MAX_FILENAME]


def content_disposition(name: str | None, media_id: int) -> str:
    """П9: `attachment; filename="<ASCII-запасное>"; filename*=UTF-8''<имя>`."""
    filename = safe_filename(name, media_id)
    ascii_name = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\;' else "_" for ch in filename)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


def media_headers(detected: str | None, name: str | None, media_id: int) -> tuple[str, dict[str, str]]:
    """`(Content-Type, заголовки)` ответа `GET /media/{id}` (A3). `mime` и имя из источника на тип не
    влияют."""
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": _MEDIA_CSP,
        "Cache-Control": _MEDIA_CACHE,
    }
    content_type = _INLINE_TYPES.get(detected or "")
    if content_type is not None:
        headers["Content-Disposition"] = "inline"
        return content_type, headers
    headers["Content-Disposition"] = content_disposition(name, media_id)
    return "application/octet-stream", headers


# --- публичные ------------------------------------------------------------------------------------------


@public_router.get("/media/{media_id}")
async def get_media(media_id: int, request: Request, principal: Principal = _read):
    db = request.app.state.db
    try:
        with db.reader() as conn:
            info = queries.media_file(conn, principal, media_id)
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc

    path = media_mod.file_path(db.data_dir, info.account_id, info.sha256)
    try:
        st = await asyncio.to_thread(os.stat, path)
    except FileNotFoundError:
        st = None
    if st is None or not stat.S_ISREG(st.st_mode):
        # П5: `ready`, а файла нет (бэкап медиа не содержит) — строка в `deferred`, снова «Загрузить».
        await db.write(lambda conn: media_mod.mark_missing(conn, media_id=media_id))
        _logger.warning("media %s: файла нет на диске", media_id)
        raise HTTPException(404, "not found")

    content_type, headers = media_headers(info.detected, info.name, media_id)
    # П9: FileResponse — Range (перемотка видео/аудио), файл потоком.
    return FileResponse(path, media_type=content_type, headers=headers, stat_result=st)


@public_router.get("/media/{media_id}/meta")
async def get_media_meta(media_id: int, request: Request, principal: Principal = _read) -> dict:
    """Ф22-1 B1: описание вложения для `hub_download` (любой статус). Чужое, скрытое, нет — 404."""
    db = request.app.state.db
    try:
        with db.reader() as conn:
            return queries.media_meta(conn, principal, media_id)
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc


@public_router.post("/media/{media_id}/fetch")
async def fetch_media_for_session(media_id: int, request: Request, principal: Principal = _read) -> dict:
    """Ф22-1 B2 + П3: сессия просит загрузить вложение (скоуп `read`, предикат проекта — как у `meta`).
    Отсрочку источника не обходит (`media.request_fetch_for_session`). Аудит `media_fetch` — с токеном и
    `media_id`, без имени файла. Ответ — `meta` после запроса. UI-ручка клика (`/messages/…/fetch`) — своя.
    Ф31-1 решение 5 + П12: повтор `failed` чаще раза в 10 мин — строка не меняется, в ответе
    `retry_after_ts`."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = uuid.uuid4().hex
    client_name = _client_name(request)

    def _fn(conn):
        meta = queries.media_meta(conn, principal, media_id)
        paused_until: int | None = None
        try:
            changed = media_mod.request_fetch_for_session(conn, media_id=media_id, now_ms=now_ms)
        except media_mod.RefetchPaused as exc:
            changed, paused_until = False, exc.until_ms
        write_audit(
            conn,
            now_ms=now_ms,
            actor=principal.kind,
            api_token_id=principal.token_id,
            client_name=client_name,
            request_id=request_id,
            action="media_fetch",
            result="ok" if changed else ("paused" if paused_until is not None else "unchanged"),
            channel_id=meta["channel_id"],
            detail={"media_id": media_id, "status": meta["status"]},
        )
        if paused_until is not None:
            # Ф31-1 П12: срок «повтори после» — полем ответа (`hub_download` пишет его сессии).
            return {**meta, "retry_after_ts": paused_until}
        return queries.media_meta(conn, principal, media_id) if changed else meta

    try:
        return await db.write(_fn)
    except (queries.NotFound, media_mod.NotFound) as exc:
        raise HTTPException(404, "not found") from exc


@public_router.post("/messages/{message_id}/media/{idx}/fetch")
async def fetch_media(message_id: int, idx: int, request: Request, principal: Principal = ui_only) -> dict:
    """A2.3: «Загрузить» — только UI-сессия (bearer -> 403 с аудитом `ui_only_denied`), CSRF — middleware.
    Возвращает обновлённый `media` сообщения (П8)."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = uuid.uuid4().hex

    def _fn(conn):
        channel_id = queries.message_visible(conn, principal, message_id)
        media = media_mod.request_fetch(conn, message_id=message_id, idx=idx, now_ms=now_ms)
        write_audit(
            conn,
            now_ms=now_ms,
            actor=principal.kind,
            api_token_id=principal.token_id,
            client_name=None,
            request_id=request_id,
            action="media_fetch",
            result="ok",
            channel_id=channel_id,
            detail={"message_id": message_id, "idx": idx},
        )
        return media

    try:
        media = await db.write(_fn)
    except (queries.NotFound, media_mod.NotFound) as exc:
        raise HTTPException(404, "not found") from exc
    except media_mod.Conflict as exc:
        raise HTTPException(409, "not fetchable") from exc
    return {"message_id": message_id, "media": media}


# --- внутренние (коннектор) --------------------------------------------------------------------------------


def _raise_for(exc: media_mod.MediaError) -> None:
    if isinstance(exc, media_mod.NotFound):
        raise HTTPException(404, "not found") from exc
    if isinstance(exc, media_mod.Forbidden):
        raise HTTPException(403, "forbidden") from exc
    if isinstance(exc, media_mod.StaleLease):
        raise HTTPException(409, "stale lease") from exc
    raise HTTPException(409, "conflict") from exc


async def _leasable_now(request: Request, principal: ConnectorPrincipal) -> bool:
    db = request.app.state.db
    now_ms = _now_ms(request)
    with db.reader() as conn:
        return media_mod.has_leasable(conn, account_id=principal.account_id, now_ms=now_ms)


@internal_router.get("/media/wait")
async def media_wait(
    request: Request,
    timeout_s: float = Query(25.0, ge=0),
    principal: ConnectorPrincipal = _connector,
) -> dict:
    """Long-poll, как `/outbox/wait`: «готово» = `lease` выдал бы хоть что-то своему аккаунту. Поколение
    `Notifier` снимается ДО чтения БД — уведомление между проверкой и ожиданием не теряется."""
    notifier = request.app.state.notifier
    if notifier.loop is None:
        notifier.attach(asyncio.get_running_loop())
    timeout_s = min(timeout_s, _MAX_WAIT_S)

    gen = notifier.gen
    if await _leasable_now(request, principal):
        return {"ready": True}
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(timeout_s):
            while True:
                gen = await notifier.wait(gen, timeout_s)
                if notifier.closed:
                    break  # Ф31-1 П5: ядро останавливается — пусто, ничего не выдавая
                if await _leasable_now(request, principal):
                    return {"ready": True}
    return {"ready": False}


class MediaLeaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int | None = None


@internal_router.post("/media/lease")
async def media_lease(
    body: MediaLeaseIn, request: Request, principal: ConnectorPrincipal = _connector
) -> list[dict]:
    """П3: задания `{media_id, lease_gen, max_bytes, message: {channel_ext_id, ext_id, thread_ext_id}, idx,
    kind, mime, size, name, src_id, ref}`."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    limit = body.limit if body.limit is not None else 1
    return await db.write(
        lambda conn: media_mod.lease(conn, account_id=principal.account_id, now_ms=now_ms, limit=limit)
    )


class _LengthMismatch(Exception):
    pass


@internal_router.put("/media/{media_id}/content")
async def media_put_content(
    media_id: int,
    request: Request,
    lease_gen: int = Query(...),
    principal: ConnectorPrincipal = _connector,
) -> dict:
    """A2.4 + П4/П5/П6/П12: тело потоком с обязательным `Content-Length` (без него — 411); длиннее потолка
    задания — 413 и `deferred`/`too_big` без чтения тела; свободно меньше 5 ГиБ — 507 и строка как после
    `temporary`; тело другой длины или обрыв — 400, строка остаётся `fetching`, временный файл удалён (повтор
    с тем же `lease_gen` разрешён). Затем `fsync` и в потоке-писателе — дедуп, `rename`, `ready`."""
    db = request.app.state.db
    account_id = principal.account_id
    raw_length = request.headers.get("content-length")
    if raw_length is None or not raw_length.isdigit():
        raise HTTPException(411, "content-length required")
    declared = int(raw_length)

    try:
        with db.reader() as conn:
            max_bytes = media_mod.check_lease(
                conn, media_id=media_id, account_id=account_id, lease_gen=lease_gen
            )
    except media_mod.MediaError as exc:
        _raise_for(exc)

    if declared > max_bytes:
        now_ms = _now_ms(request)
        try:
            await db.write(
                lambda conn: media_mod.reject_oversize(
                    conn,
                    media_id=media_id,
                    account_id=account_id,
                    lease_gen=lease_gen,
                    size=declared,
                    now_ms=now_ms,
                )
            )
        except media_mod.MediaError as exc:
            _raise_for(exc)
        raise HTTPException(413, "too large")

    if not media_mod.disk_ok(db.data_dir):
        now_ms = _now_ms(request)
        try:
            await db.write(
                lambda conn: media_mod.reject_disk_full(
                    conn, media_id=media_id, account_id=account_id, lease_gen=lease_gen, now_ms=now_ms
                )
            )
        except media_mod.MediaError as exc:
            _raise_for(exc)
        _logger.warning("media %s: мало места на диске", media_id)
        raise HTTPException(507, "insufficient storage")

    media_mod.ensure_dir(media_mod.media_root(db.data_dir))
    target_dir = media_mod.account_dir(db.data_dir, account_id)
    media_mod.ensure_dir(target_dir)
    tmp_path = target_dir / f"{media_mod.TMP_PREFIX}-{uuid.uuid4().hex}"
    hasher = hashlib.sha256()
    head = b""
    received = 0
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            async for chunk in request.stream():
                if not chunk:
                    continue
                received += len(chunk)
                if received > declared:
                    raise _LengthMismatch
                if len(head) < _HEAD_BYTES:
                    head += chunk[: _HEAD_BYTES - len(head)]
                hasher.update(chunk)
                f.write(chunk)
            if received != declared:
                raise _LengthMismatch
            f.flush()
            await asyncio.to_thread(os.fsync, f.fileno())
    except _LengthMismatch as exc:
        tmp_path.unlink(missing_ok=True)
        _logger.warning("media %s: длина тела не совпала с Content-Length", media_id)
        raise HTTPException(400, "length mismatch") from exc
    except ClientDisconnect as exc:
        tmp_path.unlink(missing_ok=True)
        _logger.warning("media %s: коннектор оборвал загрузку", media_id)
        raise HTTPException(400, "client disconnected") from exc
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    sha256 = hasher.hexdigest()
    detected = media_mod.detect(head)
    now_ms = _now_ms(request)

    def _fn(conn):
        media_mod.commit_upload(
            conn,
            media_id=media_id,
            account_id=account_id,
            lease_gen=lease_gen,
            tmp_path=tmp_path,
            sha256=sha256,
            size=received,
            detected=detected,
            now_ms=now_ms,
        )

    try:
        await db.write(_fn)
    except media_mod.MediaError as exc:
        _raise_for(exc)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return {"ok": True, "size": received, "detected": detected}


class MediaReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_gen: int
    outcome: str
    error: str | None = None
    retry_after_s: int | None = None
    size: int | None = None  # при `too_big` — размер у источника (П6)


@internal_router.post("/media/{media_id}/report")
async def media_report(
    media_id: int, body: MediaReportIn, request: Request, principal: ConnectorPrincipal = _connector
) -> dict:
    if body.outcome not in media_mod.REPORT_OUTCOMES:
        raise HTTPException(422, "invalid outcome")
    db = request.app.state.db
    now_ms = _now_ms(request)

    def _fn(conn):
        media_mod.report(
            conn,
            media_id=media_id,
            account_id=principal.account_id,
            lease_gen=body.lease_gen,
            outcome=body.outcome,
            error=body.error,
            retry_after_s=body.retry_after_s,
            now_ms=now_ms,
            size=body.size,
        )

    try:
        await db.write(_fn)
    except media_mod.MediaError as exc:
        _raise_for(exc)
    return {"ok": True}
