"""Ручки расшифровки голосовых (Ф14-6, docs/architecture.md, 14-6a, раздел C и П7, П8, П10).

- Служба `hub transcribe run` — bearer со скоупом `transcribe` (он выдаётся только background-токену на все
  проекты вместе с `read`, `security.create_api_token`): `POST /transcripts/lease` (долгий опрос, как
  `GET /media/wait`, без записи на каждое пробуждение), `POST /transcripts/{id}/extend` (срок лиза по
  настоящей длительности, П8), `POST /transcripts/{id}/report`. Файл служба берёт обычной
  `GET /media/{id}` (`read`).
- UI: `POST /media/{id}/transcribe` — кнопка «Расшифровать» / «Расшифровать заново»; только UI-сессия (bearer
  -> 403 и аудит `ui_only_denied`), CSRF — middleware.

Маршруты не пишут SQL: чтение и запись — `transcripts` внутри `db.reader()`/`Db.write()`. Журнал — только
`media_id` и класс ошибки, тексты расшифровок — никогда.
"""

import asyncio
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from .. import media as media_mod
from .. import queries
from .. import transcripts as transcripts_mod
from ..audit import write_audit
from ..security import Principal, principal_dependency
from .write import ui_only

public_router = APIRouter(tags=["transcripts"])

_transcribe_scope = principal_dependency("transcribe")

LEASE_WAIT_MAX_S = 25.0
# Опрос БД читателем между пробуждениями: пауза `not_before_ts` и просрочка лиза коммитом не будятся.
_POLL_S = 5.0


async def _worker(request: Request) -> Principal:
    """Скоуп `transcribe` — только bearer службы (UI-сессии он не выдаётся; проверка — на всякий случай)."""
    principal = await _transcribe_scope(request)
    if principal.is_ui:
        raise HTTPException(403, "forbidden")
    return principal


_worker_dep = Depends(_worker)


def _now_ms(request: Request) -> int:
    return int(request.app.state.clock() * 1000)


def _raise_for(exc: transcripts_mod.TranscriptError) -> None:
    if isinstance(exc, transcripts_mod.NotFound):
        raise HTTPException(404, "not found") from exc
    if isinstance(exc, transcripts_mod.StaleLease):
        raise HTTPException(409, "stale lease") from exc
    if isinstance(exc, transcripts_mod.Busy):
        raise HTTPException(409, "transcribe_busy") from exc
    if isinstance(exc, transcripts_mod.NotReady):
        raise HTTPException(409, "media_not_ready") from exc
    if isinstance(exc, transcripts_mod.NotTranscribable):
        raise HTTPException(422, "not transcribable") from exc
    raise HTTPException(409, "conflict") from exc


# --- служба -----------------------------------------------------------------------------------------------


class LeaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wait_s: float = 0.0
    # Ф79-1, раздел C: быстрый работник (ноутбук, cuda). Старый клиент поле не шлёт — медленный.
    fast: bool = False


async def _try_lease(request: Request, *, fast: bool = False) -> dict | None:
    """Сначала читатель: пустая транзакция записи на каждое пробуждение будила бы всех ожидающих (коммит ->
    `notifier.notify`) — горячий цикл. Писатель — только когда выдать есть что. Ф79-1: медленному при живом
    быстром — только задания, ждущие дольше `SLOW_AFTER_MS` (граница — заново на каждом пробуждении)."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    before = transcripts_mod.queued_before_for(fast=fast, now_ms=now_ms)
    with db.reader() as conn:
        if not transcripts_mod.has_leasable(conn, now_ms=now_ms, queued_before=before):
            return None
    return await db.write(lambda conn: transcripts_mod.lease(conn, now_ms=now_ms, queued_before=before))


@public_router.post("/transcripts/lease")
async def transcripts_lease(body: LeaseIn, request: Request, _principal: Principal = _worker_dep):
    """200 `{media_id, message_id, lease_gen, kind, mime, detected, duration_s, size, manual, max_s}` или 204.
    Долгий опрос до `wait_s` (≤ 25 с): поколение `Notifier` снимается ДО чтения БД."""
    notifier = request.app.state.notifier
    if notifier.loop is None:
        notifier.attach(asyncio.get_running_loop())
    wait_s = max(0.0, min(float(body.wait_s), LEASE_WAIT_MAX_S))
    deadline = time.monotonic() + wait_s
    gen = notifier.gen
    # 14-6b: служба жива — потребители ждут расшифровку (метка в памяти, без записи в БД).
    transcripts_mod.mark_worker_seen()
    if body.fast:
        transcripts_mod.mark_fast_seen()
    job = await _try_lease(request, fast=body.fast)
    if job is not None:
        return job
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            # Конец опроса метку НЕ ставит: ушедшая служба (оборванное соединение) не продлевает себе жизнь.
            return Response(status_code=204)
        gen = await notifier.wait(gen, min(left, _POLL_S))
        if notifier.closed:
            # Ф31-1 П5: ядро останавливается — пусто, лиз не берётся.
            return Response(status_code=204)
        job = await _try_lease(request, fast=body.fast)
        if job is not None:
            transcripts_mod.mark_worker_seen()
            if body.fast:
                transcripts_mod.mark_fast_seen()
            return job


class ExtendIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_gen: int
    duration_s: float


@public_router.post("/transcripts/{media_id}/extend")
async def transcripts_extend(
    media_id: int, body: ExtendIn, request: Request, _principal: Principal = _worker_dep
) -> dict:
    """П8: длительность стала известна из wav — срок лиза от неё (`max(120 с, 3 × длительность)`)."""
    if not 0 <= body.duration_s <= 24 * 3600:
        raise HTTPException(422, "invalid duration")
    transcripts_mod.mark_worker_seen()  # ревью Ф14-6: служба на связи — жива
    db = request.app.state.db
    now_ms = _now_ms(request)
    try:
        expires = await db.write(
            lambda conn: transcripts_mod.extend(
                conn, media_id=media_id, lease_gen=body.lease_gen, duration_s=body.duration_s, now_ms=now_ms
            )
        )
    except transcripts_mod.TranscriptError as exc:
        _raise_for(exc)
    return {"lease_expires_ts": expires}


class ReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_gen: int
    outcome: str
    text: str | None = None
    lang: str | None = None
    lang_prob: float | None = None
    problem: str | None = None
    model: str | None = None
    device: str | None = None
    elapsed_ms: int | None = None
    error: str | None = None


@public_router.post("/transcripts/{media_id}/report")
async def transcripts_report(
    media_id: int, body: ReportIn, request: Request, _principal: Principal = _worker_dep
) -> dict:
    if body.outcome not in transcripts_mod.OUTCOMES:
        raise HTTPException(422, "invalid outcome")
    if body.lang is not None and body.lang not in transcripts_mod.LANGS:
        raise HTTPException(422, "invalid lang")
    transcripts_mod.mark_worker_seen()  # ревью Ф14-6: служба на связи — жива
    db = request.app.state.db
    now_ms = _now_ms(request)

    def _fn(conn):
        return transcripts_mod.report(
            conn,
            media_id=media_id,
            lease_gen=body.lease_gen,
            outcome=body.outcome,
            now_ms=now_ms,
            text=body.text,
            lang=body.lang,
            lang_prob=body.lang_prob,
            problem=body.problem,
            model=body.model,
            device=body.device,
            elapsed_ms=body.elapsed_ms,
            error=body.error,
        )

    try:
        status = await db.write(_fn)
    except transcripts_mod.TranscriptError as exc:
        _raise_for(exc)
    return {"ok": True, "status": status}


# --- UI -------------------------------------------------------------------------------------------------


@public_router.post("/media/{media_id}/transcribe")
async def media_transcribe(media_id: int, request: Request, principal: Principal = ui_only) -> dict:
    """Решение 6: «Расшифровать» / «Расшифровать заново». Не `ready` — 409 `media_not_ready`; вид не
    голосовой — 422; идёт — 409 `transcribe_busy`. Возвращает обновлённый `media` сообщения."""
    db = request.app.state.db
    now_ms = _now_ms(request)
    request_id = uuid.uuid4().hex

    def _fn(conn):
        message_id = transcripts_mod.message_of(conn, media_id)
        if message_id is None:
            raise transcripts_mod.NotFound("media")
        channel_id = queries.message_visible(conn, principal, message_id)
        transcripts_mod.request(conn, media_id, now_ms=now_ms)
        write_audit(
            conn,
            now_ms=now_ms,
            actor=principal.kind,
            api_token_id=principal.token_id,
            client_name=None,
            request_id=request_id,
            action="media_transcribe",
            result="ok",
            channel_id=channel_id,
            detail={"message_id": message_id, "media_id": media_id},
        )
        return message_id, media_mod.message_media(conn, message_id)

    try:
        message_id, media = await db.write(_fn)
    except queries.NotFound as exc:
        raise HTTPException(404, "not found") from exc
    except transcripts_mod.TranscriptError as exc:
        _raise_for(exc)
    return {"message_id": message_id, "media": media}
