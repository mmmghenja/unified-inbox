"""Ручки помощника на интервью (Ф60-1, docs/architecture.md).
Маршруты не пишут SQL: записи — `core/interview.py` внутри `Db.write()`, память ядра (таймер `live`, живость
службы, кадры потока) — `core/interview_runtime.py` после коммита.

Кто что может:
- Владелец (кука интерфейса + CSRF у POST; bearer любого скоупа — 403 и аудит `ui_only_denied`, токен `listen` —
  403): начать, «Подскажи», пауза, «Продолжить», «Закончить», читать текущее и строки;
- служба записи `hub-listen` — bearer ровно `{listen}` (П7, `principal_dependency(listen_ok=True)`): опрос
  текущего, кусок звука, предупреждение, «Подскажи» глобальной клавишей. Кука и прочие bearer — 403;
- внутренний сокет (`hub listen status`) — `GET /admin/listen`.

Звук и текст строк в журнал не пишутся: одна строка `интервью: кусок N байт, M мс, код` (debug)."""

import logging
import time
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from starlette.requests import ClientDisconnect

from ...speech import config as speech_config
from ...speech.limits import DictateLimits
from ...speech.route import local_ready, recognize_chunk
from .. import connector_auth
from .. import interview as interview_mod
from ..hub_sessions import SessionError
from ..interview_runtime import InterviewRuntime
from ..security import LISTEN_SCOPE, Principal, principal_dependency
from .dictate import BYTES_PER_SEC, _config_dir
from .write import ui_only

public_router = APIRouter(tags=["interviews"])
internal_router = APIRouter(tags=["interviews-internal"])

_logger = logging.getLogger("hub.interview")

_listen = Depends(principal_dependency(LISTEN_SCOPE, listen_ok=True))

_ID_MAX = 2**53
_MS_MAX = 2**53
CHUNK_MAX_BODY = 512 * 1024
CHUNK_MIN_BYTES = int(0.3 * BYTES_PER_SEC)  # 9 600
CHUNK_MAX_BYTES = 15 * BYTES_PER_SEC  # 480 000
CHUNK_SPAN_MAX_MS = 60_000  # `end_ms - start_ms` — с запасом над 15 с
# 2.3: свой экземпляр пределов — диктовка и интервью не съедают лимиты друг друга.
PER_MINUTE = 120
DAILY = 4000


def _now_ms(request: Request) -> int:
    return int(request.app.state.clock() * 1000)


def _audit(request: Request, principal: Principal) -> dict:
    name = request.headers.get("x-hub-client")
    return {
        "actor": principal.kind,
        "api_token_id": principal.token_id,
        "client_name": None if name is None else name[:64],
        "request_id": uuid.uuid4().hex,
    }


def _runtime(request: Request) -> InterviewRuntime:
    runtime = getattr(request.app.state, "interviews", None)
    if runtime is None:
        raise HTTPException(503, "interview_unavailable")
    return runtime


def _limits(request: Request) -> DictateLimits:
    limits = getattr(request.app.state, "interview_limits", None)
    if limits is None:
        limits = DictateLimits(per_minute=PER_MINUTE)
        request.app.state.interview_limits = limits
    return limits


def _check_id(interview_id: int) -> None:
    if not 1 <= interview_id <= _ID_MAX:
        raise HTTPException(404, "not found")


async def _write(request: Request, fn):
    try:
        return await request.app.state.db.write(fn)
    except SessionError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


def _state(request: Request, row: dict) -> dict:
    """Запись интервью для ответа (читатель после коммита)."""
    with request.app.state.db.reader() as conn:
        return interview_mod.interview_dict(conn, row)


def _feed_reply(interview: dict, result: interview_mod.FeedResult) -> dict:
    return {
        "interview": interview,
        "queued": result.queued,
        "says": result.say_ids,
        "lines": result.lines,
    }


# --- владелец (кука интерфейса) ---------------------------------------------------------------------------------


class StartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: StrictInt = Field(ge=1, le=_ID_MAX)
    mode: Literal["live", "button"]
    them_lang: Literal["ru-RU", "en-US"]


@public_router.post("/interviews")
async def post_interview(body: StartIn, request: Request, principal: Principal = ui_only) -> dict:
    """Начать интервью в сессии хаба. Сессии нет — 404; закрыта или уже идёт интервью — 409."""
    now_ms = _now_ms(request)
    audit = _audit(request, principal)
    row = await _write(
        request,
        lambda conn: interview_mod.start(
            conn,
            session_id=body.session_id,
            mode=body.mode,
            them_lang=body.them_lang,
            now_ms=now_ms,
            audit=audit,
        ),
    )
    data = _state(request, row)
    _runtime(request).publish_state(data, now_ms)
    return {"interview": data}


@public_router.get("/interviews/current")
async def get_interview_current(request: Request, principal: Principal = ui_only) -> dict:  # noqa: ARG001
    """Открытое (`active|paused`) интервью с последними 200 строками и живостью службы записи."""
    now_ms = _now_ms(request)
    runtime = _runtime(request)
    with request.app.state.db.reader() as conn:
        row = interview_mod.current_row(conn)
        data = interview_mod.interview_dict(conn, row) if row is not None else None
        lines = interview_mod.last_lines(conn, row["id"]) if row is not None else []
    return {"interview": data, "lines": lines, **runtime.listener_fields(now_ms)}


@public_router.get("/interviews/{interview_id}/lines")
async def get_interview_lines(
    interview_id: int,
    request: Request,
    after: int = Query(0, ge=0, le=2**62),
    limit: int = Query(interview_mod.LINES_PAGE_MAX, ge=1, le=interview_mod.LINES_PAGE_MAX),
    principal: Principal = ui_only,  # noqa: ARG001
) -> dict:
    """Строки с `id > after` по возрастанию `id` (перечитывание после обрыва потока или `resync`)."""
    _check_id(interview_id)
    try:
        with request.app.state.db.reader() as conn:
            return {"lines": interview_mod.lines_after(conn, interview_id, after, limit)}
    except SessionError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


async def _hint(request: Request, principal: Principal, interview_id: int) -> dict:
    now_ms = _now_ms(request)
    audit = _audit(request, principal)
    runtime = _runtime(request)
    row, result = await _write(
        request, lambda conn: interview_mod.hint(conn, interview_id, now_ms=now_ms, audit=audit)
    )
    runtime.fed(interview_id, now_ms, result)
    data = _state(request, row)
    runtime.publish_state(data, now_ms)
    return _feed_reply(data, result)


@public_router.post("/interviews/{interview_id}/hint")
async def post_interview_hint(interview_id: int, request: Request, principal: Principal = ui_only) -> dict:
    """«Подскажи» (П3: подача `next`; при ждущей реплике без новых строк — `queued: true`, новой нет)."""
    _check_id(interview_id)
    return await _hint(request, principal, interview_id)


@public_router.post("/interviews/{interview_id}/pause")
async def post_interview_pause(interview_id: int, request: Request, principal: Principal = ui_only) -> dict:
    _check_id(interview_id)
    now_ms = _now_ms(request)
    audit = _audit(request, principal)
    runtime = _runtime(request)
    row = await _write(
        request, lambda conn: interview_mod.pause(conn, interview_id, now_ms=now_ms, audit=audit)
    )
    runtime.stop(interview_id)
    data = _state(request, row)
    runtime.publish_state(data, now_ms)
    return {"interview": data}


@public_router.post("/interviews/{interview_id}/resume")
async def post_interview_resume(interview_id: int, request: Request, principal: Principal = ui_only) -> dict:
    _check_id(interview_id)
    now_ms = _now_ms(request)
    audit = _audit(request, principal)
    row = await _write(
        request, lambda conn: interview_mod.resume(conn, interview_id, now_ms=now_ms, audit=audit)
    )
    data = _state(request, row)
    _runtime(request).publish_state(data, now_ms)
    return {"interview": data}


@public_router.post("/interviews/{interview_id}/end")
async def post_interview_end(interview_id: int, request: Request, principal: Principal = ui_only) -> dict:
    """«Закончить»: оставшиеся строки и «звонок окончен» — сессии (без единой строки — без подачи)."""
    _check_id(interview_id)
    now_ms = _now_ms(request)
    audit = _audit(request, principal)
    runtime = _runtime(request)
    row, result = await _write(
        request, lambda conn: interview_mod.end(conn, interview_id, now_ms=now_ms, audit=audit)
    )
    runtime.stop(interview_id)
    data = _state(request, row)
    runtime.publish_state(data, now_ms)
    return _feed_reply(data, result)


# --- служба записи (bearer ровно `listen`) ------------------------------------------------------------------


@public_router.get("/listen/current")
async def get_listen_current(request: Request, principal: Principal = _listen) -> dict:  # noqa: ARG001
    """Опрос службы (раз в 2 с): открытое интервью `{id, status, them_lang}` или `null`. Время опроса —
    живость службы (`listener_seen_ms` у `GET /interviews/current`)."""
    now_ms = _now_ms(request)
    runtime = _runtime(request)
    with request.app.state.db.reader() as conn:
        row = interview_mod.current_row(conn)
    if runtime.seen(now_ms) and row is not None:
        await runtime.publish_state_of(row["id"])  # служба ожила — панели снять «служба не запущена»
    return {"interview": interview_mod.listen_dict(row)}


def _chunk_log(size: int, started: float, status: int, path: str | None = None) -> None:
    _logger.debug(
        "интервью: кусок %d байт, %s, %d мс, %d",
        size,
        path or "—",
        int((time.monotonic() - started) * 1000),
        status,
    )


@public_router.post("/listen/{interview_id}/chunk")
async def post_listen_chunk(
    interview_id: int,
    request: Request,
    who: Literal["me", "them"] = Query(),
    start_ms: int = Query(ge=0, le=_MS_MAX),
    end_ms: int = Query(ge=0, le=_MS_MAX),
    principal: Principal = _listen,  # noqa: ARG001
) -> dict:
    """Кусок звука LPCM 16 кГц Int16LE моно 0,3–15 с (до 512 КБ) -> строка расшифровки или `null` (пусто).
    Проверки по порядку: нет интервью — 404; не `active` — 409 `interview_not_active`; `end_ms < start_ms`
    или разница больше 60 с — 422; нет ни ключа SpeechKit, ни быстрого работника whisper — 503
    `speechkit_off`; нет Content-Length — 411; 0 — 400 `bad_audio`; больше 512 КБ — 413; нечётная длина или
    длительность вне 0,3–15 с — 400 `bad_audio`; 120 кусков в минуту — 429 `listen_rate`. Дальше Ф79-1
    (`speech.route.recognize_chunk`): whisper ответил — строка; иначе ключа нет — 503 `listen_unavailable`;
    4000 в сутки (только путь SpeechKit) — 429 `listen_daily`; SpeechKit: ключ не принят — 502 `listen_key`,
    плохой звук — 400 `bad_audio`, таймаут — 504 `listen_timeout`, прочее — 502 `listen_upstream`; пока
    кусок распознавался, интервью встало или кончилось — 409 `interview_not_active`."""
    started = time.monotonic()
    _check_id(interview_id)
    try:
        with request.app.state.db.reader() as conn:
            row = interview_mod.check_active(conn, interview_id)
    except SessionError as exc:
        raise HTTPException(exc.status, "interview_not_active" if exc.status == 409 else exc.detail) from exc
    if end_ms < start_ms or end_ms - start_ms > CHUNK_SPAN_MAX_MS:
        raise HTTPException(422, "bad_span")
    config = speech_config.load(_config_dir(request))
    if config is None and not local_ready(request.app.state):
        raise HTTPException(503, "speechkit_off")

    raw_length = request.headers.get("content-length")
    if raw_length is None or not raw_length.isdigit():
        raise HTTPException(411, "content-length required")
    declared = int(raw_length)
    if declared == 0:
        raise HTTPException(400, "bad_audio")
    if declared > CHUNK_MAX_BODY:
        raise HTTPException(413, "chunk_too_big")
    if declared % 2 or not CHUNK_MIN_BYTES <= declared <= CHUNK_MAX_BYTES:
        raise HTTPException(400, "bad_audio")

    now_ms = _now_ms(request)
    limits = _limits(request)
    if not limits.take_minute(now_ms):
        _chunk_log(declared, started, 429)
        raise HTTPException(429, "listen_rate")
    try:
        body = await request.body()
    except ClientDisconnect as exc:
        raise HTTPException(400, "bad_audio") from exc
    if len(body) != declared:
        raise HTTPException(400, "bad_audio")

    lang = interview_mod.ME_LANG if who == "me" else row["them_lang"]
    result = await recognize_chunk(
        request.app.state, body, config, lang, take_day=lambda: limits.take_day(now_ms, DAILY)
    )
    if not result.ok:
        status, detail = {
            "unavailable": (503, "listen_unavailable"),
            "daily": (429, "listen_daily"),
            "unauthorized": (502, "listen_key"),
            "bad_audio": (400, "bad_audio"),
            "timeout": (504, "listen_timeout"),
        }.get(result.code or "", (502, "listen_upstream"))
        _chunk_log(declared, started, status, result.path)
        raise HTTPException(status, detail)

    now_ms = _now_ms(request)

    def _fn(conn):
        line = interview_mod.add_line(
            conn, interview_id, who=who, start_ms=start_ms, end_ms=end_ms, text=result.text, now_ms=now_ms
        )
        return line, interview_mod._get(conn, interview_id)  # noqa: SLF001

    try:
        line, fresh = await request.app.state.db.write(_fn)
    except SessionError as exc:
        _chunk_log(declared, started, exc.status, result.path)
        raise HTTPException(exc.status, "interview_not_active" if exc.status == 409 else exc.detail) from exc
    _chunk_log(declared, started, 200, result.path)
    if line is None:
        return {"line": None}
    _runtime(request).on_line(fresh, line, now_ms)
    return {
        "line": {key: value for key, value in line.items() if key != "text"} | {"chars": len(line["text"])}
    }


class WarnIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(default="", max_length=interview_mod.WARN_MAX)


@public_router.post("/listen/{interview_id}/warn")
async def post_listen_warn(
    interview_id: int, body: WarnIn, request: Request, principal: Principal = _listen
) -> dict:
    """П12: строка службы в панель (≤200; пустая — снять). Нет интервью — 404, окончено — 409."""
    _check_id(interview_id)
    now_ms = _now_ms(request)
    audit = _audit(request, principal)
    row = await _write(
        request,
        lambda conn: interview_mod.set_warn(conn, interview_id, body.text, now_ms=now_ms, audit=audit),
    )
    data = _state(request, row)
    _runtime(request).publish_state(data, now_ms)
    return {"warn": data["warn"]}


@public_router.post("/listen/hint")
async def post_listen_hint(request: Request, principal: Principal = _listen) -> dict:
    """«Подскажи» глобальной клавишей (`hub listen hint`): нет `active` интервью — 404 `no_interview`."""
    with request.app.state.db.reader() as conn:
        row = interview_mod.active_row(conn)
    if row is None:
        raise HTTPException(404, "no_interview")
    reply = await _hint(request, principal, row["id"])
    return {
        "interview_id": row["id"],
        "queued": reply["queued"],
        "says": len(reply["says"]),
        "lines": reply["lines"],
    }


# --- внутренний сокет ---------------------------------------------------------------------------------------


@internal_router.get("/admin/listen", dependencies=[Depends(connector_auth.admin_dependency())])
async def get_admin_listen(request: Request) -> dict:
    """`hub listen status`: открытое интервью (без строк) и живость службы записи."""
    runtime = getattr(request.app.state, "interviews", None)
    now_ms = _now_ms(request)
    with request.app.state.db.reader() as conn:
        row = interview_mod.current_row(conn)
        data = interview_mod.interview_dict(conn, row) if row is not None else None
    listener = (
        runtime.listener_fields(now_ms)
        if runtime is not None
        else {"listener_seen_ms": None, "listener_alive": False}
    )
    return {"interview": data, **listener, "now_ms": now_ms}
