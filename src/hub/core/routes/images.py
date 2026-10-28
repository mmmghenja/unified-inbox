"""Картинки по запросу сессии через Alibaba qwen-image (Ф91, docs/architecture.md, Р1–Р6 и
решения головной 24.10: ядро картинки не хранит, скоуп — `attach`).

Доступ — кука интерфейса (CSRF у POST — middleware) или bearer со скоупом `attach` (профиль `all` и сессии
хаба: право класть файлы в черновики); прочий bearer — 403 с аудитом `ui_only_denied`.

- `GET /images/enabled` -> `{enabled, daily: {project, total}}` — ключ задан (`hub auth alibaba`);
- `POST /images` `{prompt, size?, n?, negative?, model?, fast?, extend?, project_id?, role?, title?,
  subtitle?, watermark?, watermark_style?, watermark_opacity?}` -> ядро зовёт
  DashScope, СРАЗУ скачивает картинки (ссылки живут 24 ч) и отдаёт их в ответе base64:
  `{images: [{data, mime, width, height, size}], model, n, cost_usd, left: {project, total}}`. Ядро картинки
  не хранит — в рабочую папку проекта их пишет MCP (`hub_image`). С `title` поверх каждой картинки — заголовок
  (`images/overlay.py`: затемнение снизу, до 2 строк, подпись, синяя черта), с `watermark` — водяной знак
  (`corner` | `diagonal` | `tile`); ни того ни другого — картинка как есть.

Проверки по порядку: тело (422 с текстом: размер, модель, пустой промпт, n 1…4); нет ключа — 503; чужой
проект — 404; пределы в сутки (Р4, `core/images.py`) — 429 с остатком; дальше вызов: ключ не принят — 502,
предел запросов Alibaba — 429, отказ провайдера (модерация, параметры) — 422, нет денег на счёте — 502,
таймаут — 504, прочее — 502. Текст отказа — `detail`, без ключа и ссылок.

Журнал — одна строка на вызов: проект, n, модель, код, время; промпта, ключа и ссылок в журнале нет.
"""

import asyncio
import base64
import logging
import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from ...images import config as image_config
from ...images import dashscope
from ...images import overlay as overlay_mod
from .. import images as images_mod
from .. import queries
from ..security import Principal
from .uploads import ui_or_attach

router = APIRouter(tags=["images"])

_logger = logging.getLogger(__name__)

ROLE_MAX = 64
_MIME = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}
_STATUS = {
    "key": 502,
    "rate": 429,
    "balance": 502,
    "rejected": 422,
    "timeout": 504,
    "upstream": 502,
    "too_big": 502,
}


class ImageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str
    size: str | None = None
    n: int = 1
    negative: str | None = None
    model: str | None = None
    fast: bool = False
    extend: bool = False
    title: str | None = None
    subtitle: str | None = None
    watermark: str | None = None
    watermark_style: str = "corner"
    watermark_opacity: float | None = None
    project_id: int | None = None
    role: str | None = None


def _config_dir(request: Request):
    """Каталог конфига: `app.state.config_dir`, если его поставили (тесты, стенд), иначе каталог хаба."""
    configured = getattr(request.app.state, "config_dir", None)
    if configured is not None:
        return configured
    from ... import paths  # здесь: ядро не тянет `paths` на уровне модуля

    return paths.config_dir()


def _now_ms(request: Request) -> int:
    return int(request.app.state.clock() * 1000)


def _client_name(request: Request) -> str | None:
    name = request.headers.get("x-hub-client")
    return None if name is None else name[:64]


def _check(body: ImageIn) -> tuple[str, int, int, str, str | None]:
    """Тело -> `(промпт, W, H, модель, negative)`; не годится — 422 с текстом."""
    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(422, "prompt: опиши картинку — пусто")
    if len(prompt) > dashscope.PROMPT_MAX_CHARS:
        raise HTTPException(422, f"prompt: не длиннее {dashscope.PROMPT_MAX_CHARS} символов")
    if not 1 <= body.n <= dashscope.MAX_N:
        raise HTTPException(422, f"n: от 1 до {dashscope.MAX_N}")
    negative = (body.negative or "").strip() or None
    if negative is not None and len(negative) > dashscope.NEGATIVE_MAX_CHARS:
        raise HTTPException(422, f"negative: не длиннее {dashscope.NEGATIVE_MAX_CHARS} символов")
    try:
        width, height = dashscope.parse_size(body.size)
        model = dashscope.model_of(body.model, fast=body.fast)
    except dashscope.BadParams as exc:
        raise HTTPException(422, str(exc)) from exc
    for name, value, limit in (
        ("title", body.title, overlay_mod.TITLE_MAX),
        ("subtitle", body.subtitle, overlay_mod.SUBTITLE_MAX),
        ("watermark", body.watermark, overlay_mod.WATERMARK_MAX),
    ):
        if value is not None and len(" ".join(value.split())) > limit:
            raise HTTPException(422, f"{name}: не длиннее {limit} символов")
    if body.watermark_style not in overlay_mod.WATERMARK_STYLES:
        raise HTTPException(422, f"watermark_style: {', '.join(overlay_mod.WATERMARK_STYLES)}")
    opacity = body.watermark_opacity
    if opacity is not None and not overlay_mod.OPACITY_MIN <= opacity <= overlay_mod.OPACITY_MAX:
        raise HTTPException(
            422, f"watermark_opacity: от {overlay_mod.OPACITY_MIN} до {overlay_mod.OPACITY_MAX}"
        )
    return prompt, width, height, model, negative


def _overlay(images, body: ImageIn) -> tuple[list[tuple[bytes, str, int | None, int | None]], bool]:
    """Заголовок и водяной знак поверх каждой картинки (Ф91, дополнения 24.10). Ни того ни другого — как
    есть. Наложение не удалось — картинка уходит без него (за неё уже заплачено), второй элемент — `True`."""
    title = " ".join((body.title or "").split())
    mark = " ".join((body.watermark or "").split())
    out: list[tuple[bytes, str, int | None, int | None]] = []
    failed = False
    for image in images:
        if not title and not mark:
            out.append((image.data, image.detected, image.width, image.height))
            continue
        try:
            data = overlay_mod.render(
                image.data,
                title=title,
                subtitle=body.subtitle,
                watermark=mark,
                watermark_style=body.watermark_style,
                watermark_opacity=body.watermark_opacity,
            )
        except overlay_mod.OverlayError:
            failed = True
            out.append((image.data, image.detected, image.width, image.height))
            continue
        size = dashscope.png_size(data)
        out.append((data, "png", size[0] if size else None, size[1] if size else None))
    return out, failed


def _project(request: Request, principal: Principal, project_id: int | None) -> int:
    """Проект счёта: названный (виден принципалу, иначе 404) или единственный проект токена; иначе — 0."""
    if project_id is None:
        ids = principal.project_ids
        return next(iter(ids)) if ids is not None and len(ids) == 1 else images_mod.NO_PROJECT
    if project_id <= 0:
        raise HTTPException(404, "not found")
    with request.app.state.db.reader() as conn:
        if not queries.project_visible(conn, principal, project_id):
            raise HTTPException(404, "not found")
    return project_id


@router.get("/images/enabled")
async def get_images_enabled(request: Request, principal: Principal = ui_or_attach) -> dict:  # noqa: ARG001
    config = image_config.load(_config_dir(request))
    if config is None:
        return {"enabled": False}
    return {"enabled": True, "daily": {"project": config.daily_project, "total": config.daily_total}}


@router.post("/images")
async def post_images(body: ImageIn, request: Request, principal: Principal = ui_or_attach) -> dict:
    started = time.monotonic()
    prompt, width, height, model, negative = _check(body)
    config = image_config.load(_config_dir(request))
    if config is None:
        raise HTTPException(503, "картинки не настроены: ключ Alibaba вводит владелец — hub auth alibaba")
    project_id = _project(request, principal, body.project_id)
    role = " ".join((body.role or "").split())[:ROLE_MAX] or None
    db = request.app.state.db
    request_id = uuid.uuid4().hex
    who = {
        "actor": principal.kind,
        "api_token_id": principal.token_id,
        "client_name": _client_name(request),
        "request_id": request_id,
    }
    detail = {"role": role, "model": model, "size": f"{width}*{height}", "prompt_len": len(prompt)}
    if (body.title or "").strip():
        detail["title"] = True
    if (body.watermark or "").strip():
        detail["watermark"] = body.watermark_style
    now_ms = _now_ms(request)

    def _reserve(conn):
        return images_mod.reserve(
            conn,
            project_id=project_id,
            n=body.n,
            daily_project=config.daily_project,
            daily_total=config.daily_total,
            now_ms=now_ms,
            detail=detail,
            **who,
        )

    try:
        left_project, left_total = await db.write(_reserve)
    except images_mod.LimitExceeded as exc:
        raise HTTPException(429, str(exc)) from exc

    result = await dashscope.generate(
        config,
        prompt=prompt,
        width=width,
        height=height,
        n=body.n,
        model=model,
        negative=negative,
        extend=body.extend,
        transport=getattr(request.app.state, "image_transport", None),
    )
    # Р4 (ревью 24.10): после 2xx со ссылками деньги списаны — расход считается по полученным ссылкам
    # (`paid`), даже если скачать не вышло; в предел возвращается только то, что Alibaba не сделал.
    ok = result.ok and len(result.images) > 0
    paid = result.paid
    got = len(result.images)
    cost = dashscope.cost_usd(model, paid) if paid else 0.0
    finish_detail = {**detail, "cost_usd": cost}
    if result.missed:
        finish_detail["missed"] = result.missed
    if result.code:
        finish_detail["code"] = result.code

    def _finish(conn):
        if paid:
            images_mod.finish(
                conn,
                ok=True,
                project_id=project_id,
                n=paid,
                now_ms=_now_ms(request),
                detail=finish_detail,
                **who,
            )
        if paid < body.n:
            # Не сделано (вызов не прошёл или пришло меньше заказанного) — денег не стоило, возвращается.
            images_mod.finish(
                conn,
                ok=False,
                project_id=project_id,
                n=body.n - paid,
                now_ms=_now_ms(request),
                detail={**detail, "code": "short" if paid else result.code},
                **who,
            )

    await db.write(_finish)
    answered = result.status if result.status is not None else "нет"
    _logger.info(
        "картинки: проект %d, n=%d, %s, %s, оплачено %d, скачано %d, %d мс",
        project_id,
        body.n,
        model,
        "ok" if ok and not result.missed else f"{result.code} (ответ {answered})",
        paid,
        got,
        int((time.monotonic() - started) * 1000),
    )
    if not ok:
        raise HTTPException(_STATUS.get(result.code or "", 502), result.reason or "сбой Alibaba")
    ready, overlay_failed = await asyncio.to_thread(_overlay, result.images, body)
    if overlay_failed:
        _logger.warning("картинки: заголовок не наложился, картинка отдана без него")
    answer = {
        "images": [
            {
                "data": base64.b64encode(data).decode("ascii"),
                "mime": _MIME.get(detected, "application/octet-stream"),
                "width": w,
                "height": h,
                "size": len(data),
            }
            for data, detected, w, h in ready
        ],
        "model": model,
        "n": got,
        "cost_usd": cost,
        "left": {"project": left_project + (body.n - paid), "total": left_total + (body.n - paid)},
    }
    if result.missed:
        answer["missed"] = result.missed
        answer["note"] = result.reason
    if overlay_failed:
        answer["overlay_failed"] = True
    return answer
