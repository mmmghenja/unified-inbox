"""Кто распознаёт кусок диктовки и интервью (Ф79-1, docs/architecture.md).

1. Свой whisper на ноутбуке (`SpeechBroker`, срок `LOCAL_TIMEOUT_S`) — есть текст, хоть пустой (тишина), —
   ответ;
2. иначе ключ SpeechKit есть — `recognize_lpcm16k`, как до Ф79 (суточный предел — только на этом пути:
   он про деньги SpeechKit; проверку передаёт ручка — `take_day`);
3. иначе — «распознавать некому» (`unavailable`).

Журнал пишут ручки (одна строка на кусок с путём `whisper`/`speechkit`); текст и звук не пишутся никуда.
"""

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from .config import SpeechConfig
from .local import broker_of
from .recognize import recognize_lpcm16k

LOCAL_TIMEOUT_S = 6.0
# Языки, которые знает движок хаба (`transcribe/engine.py`, `LANGS`); прочие — сразу SpeechKit.
WHISPER_LANGS = frozenset({"ru", "en"})

Path = Literal["whisper", "speechkit"]
Code = Literal["unauthorized", "bad_audio", "timeout", "upstream", "unavailable", "daily"]


@dataclass(frozen=True)
class ChunkResult:
    ok: bool
    text: str = ""
    path: Path | None = None
    code: Code | None = None
    status: int | None = None  # ответ Яндекса — для журнала


def whisper_lang(lang: str | None) -> str | None:
    """`ru-RU` -> `ru`; язык, которого движок не знает, — `None` (кусок идёт сразу в SpeechKit)."""
    head = str(lang or "").split("-", 1)[0].strip().lower()
    return head if head in WHISPER_LANGS else None


def local_ready(app_state) -> bool:
    broker = broker_of(app_state)
    return broker is not None and broker.worker_alive()


async def recognize_chunk(
    app_state,
    pcm: bytes,
    config: SpeechConfig | None,
    lang: str,
    *,
    take_day: Callable[[], bool] | None = None,
    local_timeout_s: float = LOCAL_TIMEOUT_S,
) -> ChunkResult:
    """`lang` — в виде SpeechKit (`ru-RU`). `take_day` — суточный предел SpeechKit: `False` — исчерпан."""
    broker = broker_of(app_state)
    code = whisper_lang(lang)
    if broker is not None and code is not None:
        local = await broker.recognize(pcm, code, timeout_s=local_timeout_s)
        if local is not None:
            return ChunkResult(ok=True, text=local.text, path="whisper")
    if config is None:
        return ChunkResult(ok=False, code="unavailable")
    if take_day is not None and not take_day():
        return ChunkResult(ok=False, code="daily", path="speechkit")
    result = await recognize_lpcm16k(
        pcm,
        dataclasses.replace(config, lang=lang) if lang and lang != config.lang else config,
        transport=getattr(app_state, "speech_transport", None),
    )
    if result.ok:
        return ChunkResult(ok=True, text=result.text, path="speechkit", status=result.status)
    return ChunkResult(ok=False, path="speechkit", code=result.code, status=result.status)
