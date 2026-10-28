"""`POST /api/v1/uploads?channel_id=N` — загрузка файла для отправки (Ф10-4,
docs/architecture.md и поправки П1, П12). Только UI-сессия (bearer -> 403 с аудитом
`ui_only_denied`), CSRF — middleware.

Тело — сырые байты потоком (тот же приём, что `PUT /internal/v1/media/{id}/content`): обязательный
`Content-Length` (без него — 411, больше 50 МиБ — 413 без чтения тела), чанки во временный файл склада
аккаунта канала, sha256 и сигнатура по ходу, `fsync`; мало места — 507. Затем в потоке-писателе — дедуп,
`rename` и строка `upload` (`uploads.commit`). Тип — только по сигнатуре байтов, `Content-Type` клиента
игнорируется; имя — из `X-Filename` (urlencoded UTF-8), очищенное. В журнал — без имён файлов.

Ф14-3 (`docs/architecture.md`, A3 и П4, П5): загружать может и bearer со скоупом
`attach` (сессия Claude кладёт файл в черновик); bearer без него — 403 с аудитом `ui_only_denied`. Bearer'у
вдобавок: беседа трекера (`no_reply`) — 422, квота токена (30 файлов / 100 МиБ за сутки по аудиту `upload`) —
до чтения тела и ещё раз в транзакции писателя (429). `POST /uploads/from-media` — пересылка вложения хаба
(`media:<id>`) загрузкой другой беседы: тот же аккаунт — без копии, другой — копия во временный файл склада
целевого аккаунта вне писателя, затем обычный `uploads.commit`.

Ф14-4 (`docs/architecture.md`): `?voice=1` — голосовое из UI (bearer — 422): не больше
20 МиБ (413 до чтения тела), только webm/ogg по сигнатуре (иначе 422, временный файл удаляется); вид
`voice` — в строке загрузки (`upload.kind`), mime `audio/webm`/`audio/ogg`, имя `voice.<ext>`, если
`X-Filename` пуст.
"""

import asyncio
import hashlib
import logging
import os
import uuid
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict
from starlette.requests import ClientDisconnect

from .. import media as media_mod
from .. import outbox as outbox_mod
from .. import queries
from .. import uploads as uploads_mod
from ..audit import write_audit
from ..security import Principal, principal_dependency
from .write import _deny_bearer

_logger = logging.getLogger(__name__)

router = APIRouter(tags=["uploads"])

_HEAD_BYTES = 64
_COPY_CHUNK = 1024 * 1024


class _LengthMismatch(Exception):
    pass


_authed = Depends(principal_dependency())


async def _ui_or_attach(request: Request, principal: Principal = _authed) -> Principal:
    """Ф14-3 A2: UI-сессия или bearer со скоупом `attach`; прочий bearer — 403 с аудитом `ui_only_denied`."""
    if not principal.is_ui and not principal.has_scope("attach"):
        await _deny_bearer(request, principal)
    return principal


ui_or_attach = Depends(_ui_or_attach)


def _now_ms(request: Request) -> int:
    return int(request.app.state.clock() * 1000)


def _early_quota(conn, principal: Principal, size: int, now_ms: int) -> None:
    """Ф14-3 A2 + П4: быстрый отказ по квоте до приёма тела; окончательная проверка — в писателе."""
    try:
        uploads_mod.check_quota(conn, api_token_id=principal.token_id, size=size, now_ms=now_ms)
    except uploads_mod.QuotaExceeded as exc:
        raise HTTPException(429, str(exc)) from exc


def _target(conn, principal: Principal, channel_id: int, size: int, now_ms: int) -> int:
    """Читатель: проверки целевой беседы до приёма байт — видимость (`queries.NotFound`), источник принимает
    файлы, bearer — не беседа трекера и квота токена (Ф14-3 A2), почта — не больше 17 МиБ. Вернёт
    `account_id` беседы."""
    queries.channel_visible(conn, principal, channel_id)
    target = uploads_mod.channel_target(conn, channel_id)
    if target is None:
        raise queries.NotFound(f"канал {channel_id} не найден")
    account_id, source = target
    if source not in uploads_mod.SOURCES:
        raise HTTPException(422, "файлы в эту беседу не отправляются")
    if not principal.is_ui and outbox_mod.channel_no_reply(uploads_mod.channel_meta(conn, channel_id)):
        raise HTTPException(422, "в эту беседу файлы не отправляются")
    if source == "mail" and size > uploads_mod.MAIL_MAX_BYTES:
        # Ф11-3b A7 + П6: вложения письма — не больше 17 МиБ суммой; больший файл не поместится никогда.
        raise HTTPException(413, "письмо: вложения не больше 17 МиБ")
    if not principal.is_ui:
        _early_quota(conn, principal, size, now_ms)
    return account_id


@router.post("/uploads")
async def post_upload(
    request: Request,
    channel_id: int = Query(...),
    voice: bool = Query(False),
    principal: Principal = ui_or_attach,
) -> dict:
    db = request.app.state.db
    if voice and not principal.is_ui:
        # Ф14-4 A1: голосовых от сессий нет — только запись из UI.
        raise HTTPException(422, "голосовое — только из интерфейса")
    raw_length = request.headers.get("content-length")
    if raw_length is None or not raw_length.isdigit():
        raise HTTPException(411, "content-length required")
    declared = int(raw_length)
    if declared > uploads_mod.MAX_UPLOAD_BYTES:
        raise HTTPException(413, "файл больше 50 МиБ")
    if voice and declared > uploads_mod.VOICE_MAX_BYTES:
        raise HTTPException(413, "голосовое больше 20 МиБ")
    if declared == 0:
        raise HTTPException(422, "пустой файл")

    try:
        with db.reader() as conn:
            account_id = _target(conn, principal, channel_id, declared, _now_ms(request))
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc

    if not media_mod.disk_ok(db.data_dir):
        _logger.warning("upload: мало места на диске")
        raise HTTPException(507, "insufficient storage")

    name = uploads_mod.clean_name(unquote(request.headers.get("x-filename") or "", errors="replace"))

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
        raise HTTPException(400, "length mismatch") from exc
    except ClientDisconnect as exc:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(400, "client disconnected") from exc
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    sha256 = hasher.hexdigest()
    detected = media_mod.detect(head)
    kind: str | None = None
    if voice:
        # Ф14-4 A1: голосовое — только запись webm/ogg; вид `voice` пишется в строку загрузки.
        if not uploads_mod.voice_ok(detected):
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(422, "голосовое — только запись webm/ogg")
        kind = uploads_mod.VOICE_KIND
        if not (request.headers.get("x-filename") or "").strip():
            name = uploads_mod.voice_name(detected)
    upload_id = uploads_mod.new_id()
    now_ms = int(request.app.state.clock() * 1000)
    request_id = uuid.uuid4().hex

    def _fn(conn):
        if not principal.is_ui:
            # Ф14-3 П4: окончательная проверка квоты — в транзакции писателя (параллельные загрузки).
            uploads_mod.check_quota(conn, api_token_id=principal.token_id, size=received, now_ms=now_ms)
        uploads_mod.commit(
            conn,
            upload_id=upload_id,
            channel_id=channel_id,
            account_id=account_id,
            tmp_path=tmp_path,
            sha256=sha256,
            size=received,
            detected=detected,
            name=name,
            now_ms=now_ms,
            kind=kind,
        )
        write_audit(
            conn,
            now_ms=now_ms,
            actor=principal.kind,
            api_token_id=principal.token_id,
            client_name=None,
            request_id=request_id,
            action="upload",
            result="ok",
            channel_id=channel_id,
            detail={"size": received, "detected": detected},
        )

    try:
        await db.write(_fn)
    except uploads_mod.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    except uploads_mod.QuotaExceeded as exc:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(429, str(exc)) from exc
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return {
        "upload_id": upload_id,
        "size": received,
        "mime": uploads_mod.mime_of(detected, kind),
        "name": name,
        "kind": kind or uploads_mod.kind_of(detected, received),
    }


# --- Ф14-3 A3 + П5: пересылка вложения хаба (`media:<id>`) -------------------------------------------------


class FromMediaIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: int
    media_id: int


def _copy_to_tmp(src: Path, target_dir: Path) -> tuple[Path, str, int, bytes]:
    """Поток (не писатель): копия файла склада во временный файл склада целевого аккаунта — `fsync`, sha256
    по ходу. Вернёт `(tmp_path, sha256, size, head)`."""
    tmp_path = target_dir / f"{media_mod.TMP_PREFIX}-{uuid.uuid4().hex}"
    hasher = hashlib.sha256()
    head = b""
    size = 0
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
            while chunk := inp.read(_COPY_CHUNK):
                if len(head) < _HEAD_BYTES:
                    head += chunk[: _HEAD_BYTES - len(head)]
                hasher.update(chunk)
                out.write(chunk)
                size += len(chunk)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path, hasher.hexdigest(), size, head


def _read_head(path: Path) -> bytes:
    with open(path, "rb") as f:
        return f.read(_HEAD_BYTES)


async def _prepare(db, found: uploads_mod.ForwardSource, account_id: int):
    """Ф14-3 П5: другой аккаунт — копия вне писателя (`asyncio.to_thread`); тот же — без копии. Вернёт
    `(tmp_path | None, sha256, size, detected)`."""
    src = media_mod.file_path(db.data_dir, found.account_id, found.sha256)
    if found.account_id == account_id:
        detected = found.detected or media_mod.detect(await asyncio.to_thread(_read_head, src))
        return None, found.sha256, found.size, detected
    media_mod.ensure_dir(media_mod.media_root(db.data_dir))
    target_dir = media_mod.account_dir(db.data_dir, account_id)
    media_mod.ensure_dir(target_dir)
    tmp_path, sha256, size, head = await asyncio.to_thread(_copy_to_tmp, src, target_dir)
    return tmp_path, sha256, size, found.detected or media_mod.detect(head)


@router.post("/uploads/from-media")
async def post_upload_from_media(
    body: FromMediaIn, request: Request, principal: Principal = ui_or_attach
) -> dict:
    """Ф14-3 A3 + П5: вложение хаба, видимое принципалу, — загрузкой целевой беседы. Целевая беседа — как у
    `POST /uploads` (видимость, источник, трекер, почта 17 МиБ, квота bearer); вложение — видимость
    `GET /media/{id}` и не удалено у источника (иначе 404), не скачано — 422. Имя — имя вложения. Аудит
    `upload` `{size, detected, from_media: true}` — входит в квоту."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    try:
        with db.reader() as conn:
            found = uploads_mod.forward_source(conn, principal, body.media_id)
            if found.size > uploads_mod.MAX_UPLOAD_BYTES:
                raise HTTPException(413, "файл больше 50 МиБ")
            if found.size == 0:
                raise HTTPException(422, "пустой файл")
            account_id = _target(conn, principal, body.channel_id, found.size, now_ms)
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    except uploads_mod.NotReady as exc:
        raise HTTPException(422, str(exc)) from exc

    if not media_mod.disk_ok(db.data_dir):
        _logger.warning("upload: мало места на диске")
        raise HTTPException(507, "insufficient storage")

    try:
        tmp_path, sha256, size, detected = await _prepare(db, found, account_id)
    except OSError as exc:
        raise HTTPException(422, "вложение ещё не скачано") from exc
    name = uploads_mod.clean_name(found.name)
    upload_id = uploads_mod.new_id()
    request_id = uuid.uuid4().hex

    def _fn(conn):
        # П5: вложение всё ещё видно и готово (его могли «удалить у себя» между чтением и записью).
        again = uploads_mod.forward_source(conn, principal, body.media_id)
        if again.sha256 != found.sha256:
            raise queries.NotFound(f"медиа {body.media_id} сменилось")
        if not principal.is_ui:
            uploads_mod.check_quota(conn, api_token_id=principal.token_id, size=size, now_ms=now_ms)
        if tmp_path is None:
            uploads_mod.commit_existing(
                conn,
                upload_id=upload_id,
                channel_id=body.channel_id,
                account_id=account_id,
                sha256=sha256,
                size=size,
                detected=detected,
                name=name,
                now_ms=now_ms,
            )
        else:
            uploads_mod.commit(
                conn,
                upload_id=upload_id,
                channel_id=body.channel_id,
                account_id=account_id,
                tmp_path=tmp_path,
                sha256=sha256,
                size=size,
                detected=detected,
                name=name,
                now_ms=now_ms,
            )
        write_audit(
            conn,
            now_ms=now_ms,
            actor=principal.kind,
            api_token_id=principal.token_id,
            client_name=None,
            request_id=request_id,
            action="upload",
            result="ok",
            channel_id=body.channel_id,
            detail={"size": size, "detected": detected, "from_media": True},
        )

    def _drop_tmp() -> None:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    try:
        await db.write(_fn)
    except (queries.NotFound, uploads_mod.NotFound) as exc:
        _drop_tmp()
        raise HTTPException(404, "not found") from exc
    except uploads_mod.NotReady as exc:
        _drop_tmp()
        raise HTTPException(422, str(exc)) from exc
    except uploads_mod.QuotaExceeded as exc:
        _drop_tmp()
        raise HTTPException(429, str(exc)) from exc
    except BaseException:
        _drop_tmp()
        raise
    return {
        "upload_id": upload_id,
        "size": size,
        "mime": uploads_mod.mime_of(detected),
        "name": name,
        "kind": uploads_mod.kind_of(detected, size),
    }
