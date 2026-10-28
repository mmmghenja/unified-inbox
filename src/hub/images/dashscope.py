"""Один вызов генерации картинок DashScope (qwen-image) и скачивание результата сразу (Ф91 Р2; факты API —
раздел 2 плана).

`POST <base_url>/api/v1/services/aigc/multimodal-generation/generation`, заголовок `Authorization: Bearer
<ключ>`, тело `{"model", "input": {"messages": [{"role": "user", "content": [{"text": …}]}]}, "parameters":
{"size": "W*H", "n", "negative_prompt"?, "prompt_extend", "watermark": false}}`. Ответ —
`output.choices[].message.content[].image` (ссылки PNG, живут 24 ч); ошибка — `code`/`message`.

Только синхронно; повтора нет (каждый удачный вызов стоит денег). Таймаут генерации — `TIMEOUT_S` на весь
вызов; каждая картинка скачивается сразу же своим запросом без ключа (ссылка подписана), не дольше
`DOWNLOAD_TIMEOUT_S` и не больше `MAX_IMAGE_BYTES`; ответ API читается не больше `MAX_RESPONSE_BYTES`.
Скачивание — только `https` с хоста `*.aliyuncs.com` (или хоста адреса API): ответ, подсунутый кем-то, не
заставит ядро сходить во внутреннюю сеть.

Сеть: `trust_env=False` (ключ не уходит в прокси окружения процесса); прокси — только из `config.json`
(`proxy`). Адрес API — `HUB_DASHSCOPE_URL` (только тесты и стенд; хост этого адреса тоже разрешён для
скачивания), иначе `base_url` настроек.

Ошибки — кодом и коротким текстом для сессии (`Generated.reason`), не исключением; ключ, промпт и ссылки в
текст и журнал не попадают. Сообщение провайдера (`message`) передаётся сессии укороченным: в нём причина
отказа (модерация, неверный размер), ключа там нет.
"""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

import httpx

from .config import ImageConfig

GEN_PATH = "/api/v1/services/aigc/multimodal-generation/generation"
URL_ENV = "HUB_DASHSCOPE_URL"
DEFAULT_MODEL = "qwen-image-2.0-pro"
FAST_MODEL = "qwen-image-2.0"
# Цена за картинку, $ (Model Studio, Сингапур; проба Umbrella 24.10).
PRICE_USD = {DEFAULT_MODEL: 0.075, FAST_MODEL: 0.035}
TIMEOUT_S = 90.0  # генерация — до ~60 с
DOWNLOAD_TIMEOUT_S = 30.0
MIB = 1024 * 1024
MAX_IMAGE_BYTES = 20 * MIB
MAX_RESPONSE_BYTES = 256 * 1024
MAX_N = 4
PROMPT_MAX_CHARS = 2000  # у API — 1300 токенов; длиннее он откажет сам, здесь — грубый потолок
NEGATIVE_MAX_CHARS = 500
MIN_PIXELS = 512 * 512
MAX_PIXELS = 2048 * 2048
MIN_SIDE = 256
MAX_SIDE = 4096
# Соотношения сторон -> размер (пикселей не больше 2048²).
ASPECTS = {
    "16:9": (2688, 1536),
    "9:16": (1536, 2688),
    "1:1": (2048, 2048),
    "4:3": (2304, 1728),
    "3:4": (1728, 2304),
}
DEFAULT_ASPECT = "16:9"
ALLOWED_HOST_SUFFIX = ".aliyuncs.com"
REASON_MAX = 300

_SIZE_RE = re.compile(r"^\s*(\d{2,5})\s*[*xх×]\s*(\d{2,5})\s*$", re.IGNORECASE)
_logger = logging.getLogger(__name__)

# httpx на INFO пишет адрес каждого запроса (подписанные ссылки картинок) — порог поднимается, как у диктовки.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

FailCode = Literal["key", "rate", "balance", "rejected", "timeout", "upstream", "too_big"]


@dataclass(frozen=True)
class Image:
    data: bytes = field(repr=False)
    detected: str  # `png` | `jpeg` | `webp`
    width: int | None
    height: int | None


@dataclass(frozen=True)
class Generated:
    ok: bool
    images: tuple[Image, ...] = ()
    code: FailCode | None = None
    reason: str = ""  # текст для сессии (без ключа и ссылок)
    status: int | None = None  # HTTP-код ответа API (журнал, `--check`); `None` — ответа не было
    provider_code: str | None = None
    # Сколько картинок Alibaba сделал (2xx со ссылками) — за них уже списаны деньги, даже если скачать не
    # вышло. `images` — скачанные из них; `paid - len(images)` не скачались (`reason` — почему).
    paid: int = 0

    @property
    def missed(self) -> int:
        return max(self.paid - len(self.images), 0)


class BadParams(ValueError):
    """Параметры запроса не годятся — текст для сессии (422)."""


def parse_size(value: str | None) -> tuple[int, int]:
    """`16:9`/`1:1`/`9:16`/`4:3`/`3:4` или `W*H` (`x`, `×` тоже) -> `(W, H)`. Пикселей — от 512² до 2048²,
    сторона — 256…4096; иначе `BadParams`."""
    text = (value or DEFAULT_ASPECT).strip()
    if text in ASPECTS:
        return ASPECTS[text]
    match = _SIZE_RE.match(text)
    if match is None:
        raise BadParams(f"size: {', '.join(ASPECTS)} или W*H, получено {value!r}")
    width, height = int(match.group(1)), int(match.group(2))
    if not (MIN_SIDE <= width <= MAX_SIDE and MIN_SIDE <= height <= MAX_SIDE):
        raise BadParams(f"size: сторона от {MIN_SIDE} до {MAX_SIDE} пикселей")
    if not MIN_PIXELS <= width * height <= MAX_PIXELS:
        raise BadParams("size: площадь от 512×512 до 2048×2048 пикселей")
    return width, height


def model_of(model: str | None, *, fast: bool = False) -> str:
    if model is None or not str(model).strip():
        return FAST_MODEL if fast else DEFAULT_MODEL
    name = str(model).strip()
    if name not in PRICE_USD:
        raise BadParams(f"model: {DEFAULT_MODEL} или {FAST_MODEL}")
    return name


def cost_usd(model: str, n: int) -> float:
    return round(PRICE_USD[model] * n, 4)


def api_url(config: ImageConfig) -> str:
    return (os.environ.get(URL_ENV) or config.base_url).rstrip("/") + GEN_PATH


def body_of(
    *, prompt: str, width: int, height: int, n: int, model: str, negative: str | None, extend: bool
) -> dict:
    parameters: dict = {
        "size": f"{width}*{height}",
        "n": n,
        "prompt_extend": bool(extend),
        "watermark": False,
    }
    if negative:
        parameters["negative_prompt"] = negative
    return {
        "model": model,
        "input": {"messages": [{"role": "user", "content": [{"text": prompt}]}]},
        "parameters": parameters,
    }


def detect(head: bytes) -> str | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def png_size(data: bytes) -> tuple[int, int] | None:
    """Ширина и высота из заголовка IHDR PNG."""
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def _fail(
    code: FailCode, reason: str, status: int | None = None, provider_code: str | None = None
) -> Generated:
    return Generated(ok=False, code=code, reason=reason, status=status, provider_code=provider_code)


def _short(text: object) -> str:
    value = " ".join(str(text or "").split())
    return value[:REASON_MAX] + ("…" if len(value) > REASON_MAX else "")


def _error_of(status: int, data: object) -> Generated:
    code = str(data.get("code") or "") if isinstance(data, dict) else ""
    message = _short(data.get("message")) if isinstance(data, dict) else ""
    detail = f" ({code}: {message})" if code or message else ""
    if status in (401, 403) or code in ("InvalidApiKey", "AccessDenied") or code.startswith("Unauthorized"):
        return _fail(
            "key", "ключ Alibaba не принят — пусть владелец проверит hub auth alibaba --check", status, code
        )
    if status == 429 or code.startswith("Throttling"):
        return _fail("rate", f"Alibaba: превышен предел запросов, повтори позже{detail}", status, code)
    if code.startswith("Arrearage"):
        return _fail("balance", "Alibaba: на счёте Model Studio нет денег — скажи владельцу", status, code)
    if 400 <= status < 500:
        return _fail("rejected", f"Alibaba отказал в запросе{detail}", status, code)
    return _fail("upstream", f"сбой Alibaba (HTTP {status}){detail}", status, code)


async def _read_limited(response: httpx.Response, limit: int) -> bytes | None:
    """Тело ответа целиком, но не больше `limit` байт (`None` — больше)."""
    declared = response.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _image_urls(data: object) -> list[str]:
    output = data.get("output") if isinstance(data, dict) else None
    choices = output.get("choices") if isinstance(output, dict) else None
    urls: list[str] = []
    for choice in choices if isinstance(choices, list) else []:
        message = choice.get("message") if isinstance(choice, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        for part in content if isinstance(content, list) else []:
            if isinstance(part, dict) and isinstance(part.get("image"), str):
                urls.append(part["image"])
    return urls


def _allowed(url: str, api: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    api_parts = urlsplit(api)
    if host and host == (api_parts.hostname or "").lower() and parts.scheme == api_parts.scheme:
        return True  # стенд и тесты (`HUB_DASHSCOPE_URL`) — тот же хост
    return parts.scheme == "https" and host.endswith(ALLOWED_HOST_SUFFIX)


async def _download(client: httpx.AsyncClient, url: str) -> Image | Generated:
    try:
        async with asyncio.timeout(DOWNLOAD_TIMEOUT_S):
            async with client.stream("GET", url, timeout=DOWNLOAD_TIMEOUT_S) as response:
                if response.status_code != 200:
                    return _fail("upstream", f"картинка не скачалась (HTTP {response.status_code})")
                data = await _read_limited(response, MAX_IMAGE_BYTES)
    except TimeoutError, httpx.TimeoutException:
        return _fail("timeout", f"картинка не скачалась за {int(DOWNLOAD_TIMEOUT_S)} с")
    except httpx.HTTPError as exc:
        return _fail("upstream", f"картинка не скачалась ({type(exc).__name__})")
    if data is None:
        return _fail("too_big", f"картинка больше {MAX_IMAGE_BYTES // MIB} МиБ — не принята")
    kind = detect(data[:16])
    if kind is None:
        return _fail("upstream", "по ссылке пришла не картинка")
    size = png_size(data) if kind == "png" else None
    return Image(data=data, detected=kind, width=size[0] if size else None, height=size[1] if size else None)


async def generate(
    config: ImageConfig,
    *,
    prompt: str,
    width: int,
    height: int,
    n: int = 1,
    model: str = DEFAULT_MODEL,
    negative: str | None = None,
    extend: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout_s: float = TIMEOUT_S,
) -> Generated:
    """Сгенерировать `n` картинок и скачать их. Параметры проверены вызывающим (`parse_size`, `model_of`)."""
    url = api_url(config)
    headers = {"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"}
    body = body_of(
        prompt=prompt, width=width, height=height, n=n, model=model, negative=negative, extend=extend
    )
    kwargs: dict = {"transport": transport} if transport is not None else {"proxy": config.proxy}
    async with httpx.AsyncClient(
        timeout=timeout_s, trust_env=False, follow_redirects=False, **kwargs
    ) as client:
        try:
            async with asyncio.timeout(timeout_s):
                async with client.stream("POST", url, headers=headers, json=body) as response:
                    status = response.status_code
                    raw = await _read_limited(response, MAX_RESPONSE_BYTES)
        except TimeoutError, httpx.TimeoutException:
            return _fail("timeout", f"Alibaba не ответил за {int(timeout_s)} с — повтори позже")
        except httpx.HTTPError as exc:
            return _fail("upstream", f"нет связи с Alibaba ({type(exc).__name__})")
        if raw is None:
            return _fail("upstream", "ответ Alibaba слишком большой", status)
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if not 200 <= status < 300:
            return _error_of(status, data)
        if isinstance(data, dict) and data.get("code"):
            return _error_of(400, data)
        urls = _image_urls(data)
        if not urls:
            return _fail("upstream", "Alibaba ответил без картинок", status)
        # С этого места генерация оплачена: сбой скачивания одной картинки не отменяет остальные.
        links = urls[:n]
        images: list[Image] = []
        failure: Generated | None = None
        for link in links:
            if not _allowed(link, url):
                _logger.warning("картинки: ссылка с чужого хоста отброшена")
                failure = failure or _fail("upstream", "Alibaba вернул ссылку с чужого адреса")
                continue
            got = await _download(client, link)
            if isinstance(got, Generated):
                failure = failure or got
                continue
            images.append(got)
    if failure is None:
        return Generated(ok=True, images=tuple(images), status=status, paid=len(links))
    missed = len(links) - len(images)
    return Generated(
        ok=bool(images),
        images=tuple(images),
        code=failure.code,
        reason=f"не скачались {missed} из {len(links)} (оплачены): {failure.reason}",
        status=status,
        provider_code=failure.provider_code,
        paid=len(links),
    )


async def probe(config: ImageConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> Generated:
    """`hub auth alibaba --check`: запрос с заведомо неверным размером (`1*1`). Ключ проверяет шлюз до
    параметров: 400 `InvalidParameter` — ключ принят, картинка не делается и не оплачивается; 401/403 —
    нет."""
    url = api_url(config)
    headers = {"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"}
    body = body_of(prompt="check", width=1, height=1, n=1, model=FAST_MODEL, negative=None, extend=False)
    kwargs: dict = {"transport": transport} if transport is not None else {"proxy": config.proxy}
    try:
        async with httpx.AsyncClient(
            timeout=30.0, trust_env=False, follow_redirects=False, **kwargs
        ) as client:
            response = await client.post(url, headers=headers, json=body)
    except httpx.TimeoutException:
        return _fail("timeout", "Alibaba не ответил за 30 с")
    except httpx.HTTPError as exc:
        return _fail("upstream", f"нет связи с Alibaba ({type(exc).__name__})")
    try:
        data = response.json()
    except ValueError:
        data = None
    if 200 <= response.status_code < 300:
        return Generated(ok=True, status=response.status_code)
    return _error_of(response.status_code, data)
