"""Один вызов SpeechKit v1 REST, короткое аудио (Ф59-1, раздел 1; образец — Umbrella
`src/lib/speechkit/recognize.ts`).

`POST <url>?lang=…&format=lpcm&sampleRateHertz=16000&topic=general&folderId=…`, заголовки
`Authorization: Api-Key <ключ>`, `x-data-logging-enabled: false`, `Content-Type: application/octet-stream`.
Таймаут 10 с на весь вызов. Повтор здесь — только когда соединение с Яндексом не установилось (запрос не ушёл,
оплаты не было): до двух раз с паузой 0,5 с в пределах того же таймаута. Остальные сбои повторяет интерфейс
(19.10: кусок держится в очереди, пока не дойдёт).

Сеть: `trust_env=False` (ключ не уходит в прокси окружения процесса); прокси — только из `config.json`
диктовки (`proxy`), по умолчанию напрямую — как у других внешних клиентов хаба (бот владельца, коннекторы).
Адрес — `HUB_SPEECHKIT_URL` (только для тестов и стенда), иначе настоящий.
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Literal

import httpx

from .config import SpeechConfig

DEFAULT_URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
URL_ENV = "HUB_SPEECHKIT_URL"
TIMEOUT_S = 10.0
SAMPLE_RATE = 16000
CONNECT_ATTEMPTS = 3
CONNECT_RETRY_PAUSE_S = 0.5

# httpx на INFO пишет адрес каждого запроса — с `folderId` в query; журнал диктовки — одна строка ручки
# (раздел 1 спеки). Порог поднимается при импорте, как в шлюзах Slack и Mattermost (`isEnabledFor`
# срабатывает раньше любого обработчика).
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

FailCode = Literal["unauthorized", "bad_audio", "timeout", "upstream"]


@dataclass(frozen=True)
class Recognized:
    ok: bool
    text: str = ""
    code: FailCode | None = None
    status: int | None = None  # код ответа Яндекса (для журнала и `--check`), `None` — ответа не было


def stt_url() -> str:
    return os.environ.get(URL_ENV) or DEFAULT_URL


def _fail(code: FailCode, status: int | None = None) -> Recognized:
    return Recognized(ok=False, code=code, status=status)


async def recognize_lpcm16k(
    pcm: bytes,
    config: SpeechConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout_s: float = TIMEOUT_S,
) -> Recognized:
    """LPCM 16 кГц Int16LE моно -> текст. Ошибки — кодом, не исключением; ключ и звук в исключения не
    попадают (только тип ошибки сети)."""
    params = {
        "lang": config.lang,
        "format": "lpcm",
        "sampleRateHertz": str(SAMPLE_RATE),
        "topic": "general",
        "folderId": config.folder_id,
    }
    headers = {
        "Authorization": f"Api-Key {config.api_key}",
        "x-data-logging-enabled": "false",
        "Content-Type": "application/octet-stream",
    }
    kwargs: dict = {"transport": transport} if transport is not None else {"proxy": config.proxy}
    try:
        async with asyncio.timeout(timeout_s):
            async with httpx.AsyncClient(timeout=timeout_s, trust_env=False, **kwargs) as client:
                for attempt in range(CONNECT_ATTEMPTS):
                    try:
                        response = await client.post(stt_url(), params=params, headers=headers, content=pcm)
                        break
                    except httpx.ConnectError, httpx.ConnectTimeout:
                        if attempt == CONNECT_ATTEMPTS - 1:
                            raise
                        await asyncio.sleep(CONNECT_RETRY_PAUSE_S)
    except TimeoutError, httpx.TimeoutException:
        return _fail("timeout")
    except httpx.HTTPError:
        return _fail("upstream")

    status = response.status_code
    if status in (401, 403):
        return _fail("unauthorized", status)
    if status == 400:
        return _fail("bad_audio", status)
    if not 200 <= status < 300:
        return _fail("upstream", status)
    try:
        data = response.json()
    except ValueError:
        return _fail("upstream", status)
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, str):
        return _fail("upstream", status)
    return Recognized(ok=True, text=result, status=status)
