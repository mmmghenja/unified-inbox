"""Диктовка (Ф59-1, docs/architecture.md; Ф79-1 — свой whisper первым,
docs/architecture.md).

Только UI-сессия (`ui_only`: bearer, в том числе `human`/`term`, -> 403 и аудит `ui_only_denied`), CSRF у
POST — middleware. На удалённом слушателе тело до 1 МиБ проходит общим пределом `middleware.py` (кусок 25 с =
800 КБ), отдельного пути не нужно.

- `GET /dictate` -> `{enabled}` — ключ и каталог заданы (`hub auth speechkit`) или жив быстрый работник
  whisper (Ф79-1);
- `POST /dictate` — тело LPCM 16 кГц Int16LE моно -> `{text}`. Проверки по порядку: нет ни ключа, ни
  работника -> 404 `dictate_off`; нет Content-Length -> 411; 0 -> 400 `bad_audio`; > 1 МиБ -> 413; нечётная
  длина или длительность вне 0,3–30 с -> 400 `bad_audio`; 60 кусков в минуту (на все пути) -> 429
  `dictate_rate`; дальше `speech.route.recognize_chunk`: whisper ответил -> `{text}` (тишина — `""`); иначе
  ключа нет -> 503 `dictate_unavailable`; суточный потолок (только путь SpeechKit) -> 429 `dictate_daily`;
  SpeechKit: ключ не принят -> 502 `dictate_key`, плохой звук -> 400 `bad_audio`, таймаут -> 504
  `dictate_timeout`, прочее -> 502 `dictate_upstream`.

Звук и текст не сохраняются и не пишутся в журнал: одна строка `диктовка: N байт, путь, M мс, код` (debug;
сбой SpeechKit — info с кодом ответа Яндекса, чтобы по журналу было видно, где рвётся).
"""

import logging
import time

from fastapi import APIRouter, HTTPException, Request
from starlette.requests import ClientDisconnect

from ...speech import config as speech_config
from ...speech.limits import DictateLimits
from ...speech.route import local_ready, recognize_chunk
from ..security import Principal
from .write import ui_only

public_router = APIRouter(tags=["dictate"])

_logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 1024 * 1024
BYTES_PER_SEC = 32_000  # 16 000 Гц × 2 байта, моно
MIN_BYTES = int(0.3 * BYTES_PER_SEC)  # 9 600
MAX_BYTES = 30 * BYTES_PER_SEC  # 960 000


def _config_dir(request: Request):
    """Каталог конфига: `app.state.config_dir`, если его поставили (тесты, стенд), иначе каталог хаба."""
    configured = getattr(request.app.state, "config_dir", None)
    if configured is not None:
        return configured
    from ... import paths  # здесь: ядро не тянет `paths` на уровне модуля

    return paths.config_dir()


def _limits(request: Request) -> DictateLimits:
    limits = getattr(request.app.state, "dictate_limits", None)
    if limits is None:
        limits = DictateLimits()
        request.app.state.dictate_limits = limits
    return limits


def _log(size: int, started: float, status: int, path: str | None = None) -> None:
    _logger.debug(
        "диктовка: %d байт, %s, %d мс, %d",
        size,
        path or "—",
        int((time.monotonic() - started) * 1000),
        status,
    )


@public_router.get("/dictate")
async def get_dictate(request: Request, principal: Principal = ui_only) -> dict:  # noqa: ARG001
    enabled = speech_config.load(_config_dir(request)) is not None or local_ready(request.app.state)
    return {"enabled": enabled}


@public_router.post("/dictate")
async def post_dictate(request: Request, principal: Principal = ui_only) -> dict:  # noqa: ARG001
    return await recognize_body(request)


async def recognize_body(request: Request) -> dict:
    """Общая часть `POST /dictate` и `POST /owner_channel/bot-dictate`: проверки, пределы, распознавание (допуск
    вызывающий проверил до этого)."""
    started = time.monotonic()
    config = speech_config.load(_config_dir(request))
    if config is None and not local_ready(request.app.state):
        raise HTTPException(404, "dictate_off")

    raw_length = request.headers.get("content-length")
    if raw_length is None or not raw_length.isdigit():
        raise HTTPException(411, "content-length required")
    declared = int(raw_length)
    if declared == 0:
        raise HTTPException(400, "bad_audio")
    if declared > MAX_BODY_BYTES:
        raise HTTPException(413, "dictate_too_big")
    if declared % 2 or not MIN_BYTES <= declared <= MAX_BYTES:
        raise HTTPException(400, "bad_audio")

    now_ms = int(request.app.state.clock() * 1000)
    limits = _limits(request)
    if not limits.take_minute(now_ms):
        _log(declared, started, 429)
        raise HTTPException(429, "dictate_rate")

    try:
        body = await request.body()
    except ClientDisconnect as exc:
        raise HTTPException(400, "bad_audio") from exc
    if len(body) != declared:
        raise HTTPException(400, "bad_audio")

    result = await recognize_chunk(
        request.app.state,
        body,
        config,
        config.lang if config is not None else speech_config.DEFAULT_LANG,
        take_day=None if config is None else (lambda: limits.take_day(now_ms, config.daily_chunks)),
    )
    if result.ok:
        _log(declared, started, 200, result.path)
        return {"text": result.text}
    if result.code == "unavailable":
        _log(declared, started, 503, result.path)
        raise HTTPException(503, "dictate_unavailable")
    if result.code == "daily":
        _log(declared, started, 429, result.path)
        raise HTTPException(429, "dictate_daily")
    status, detail = {
        "unauthorized": (502, "dictate_key"),
        "bad_audio": (400, "bad_audio"),
        "timeout": (504, "dictate_timeout"),
    }.get(result.code or "", (502, "dictate_upstream"))
    _logger.info(
        "диктовка: сбой SpeechKit %s (ответ %s), %d байт, %d мс",
        result.code,
        result.status if result.status is not None else "нет",
        declared,
        int((time.monotonic() - started) * 1000),
    )
    raise HTTPException(status, detail)
