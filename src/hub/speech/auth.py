"""`hub auth speechkit [--from-env-file PATH] [--force] [--check]` (Ф59-1, раздел 1 «CLI»).

Запускает только владелец. Ключ вводится без эха (`getpass`) или берётся из dotenv-файла (`SPEECHKIT_API_KEY`,
каталог — `SPEECHKIT_FOLDER_ID`, пусто — `OCR_YANDEX_FOLDER_ID`); значения никуда не печатаются. Ядро не
нужно: ключ читается из файла при каждом запросе диктовки.

`--check` — пробный запрос к SpeechKit с 0,5 с тишины: «ключ работает» или причина.
"""

import asyncio
import getpass
from collections.abc import Callable
from pathlib import Path

import httpx

from . import config as speech_config
from .recognize import SAMPLE_RATE, recognize_lpcm16k

KEY_ENV = "SPEECHKIT_API_KEY"
FOLDER_ENV = "SPEECHKIT_FOLDER_ID"
FOLDER_FALLBACK_ENV = "OCR_YANDEX_FOLDER_ID"
_YES = ("y", "yes", "д", "да")
SILENCE = bytes(SAMPLE_RATE)  # 0,5 с тишины: 8 000 отсчётов × 2 байта


def _from_env_file(path: Path, out: Callable[[str], None]) -> tuple[str, str] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        out(f"файл не читается: {path} ({type(exc).__name__}) — ничего не записано")
        return None
    values = speech_config.parse_env_file(text)
    key = values.get(KEY_ENV, "").strip()
    folder = values.get(FOLDER_ENV, "").strip() or values.get(FOLDER_FALLBACK_ENV, "").strip()
    missing = []
    if not key:
        missing.append(KEY_ENV)
    if not folder:
        missing.append(f"{FOLDER_ENV} (или {FOLDER_FALLBACK_ENV})")
    if missing:
        out(f"в файле нет: {', '.join(missing)} — ничего не записано")
        return None
    return key, folder


def _interactive(
    config_dir: Path, ask_secret: Callable[[str], str], ask: Callable[[str], str], out: Callable[[str], None]
) -> tuple[str, str] | None:
    key = (ask_secret("Ключ API SpeechKit (ввод не показывается): ") or "").strip()
    if not key:
        out("ключ не введён — ничего не записано")
        return None
    before = str(speech_config.read_settings(config_dir).get("folder_id") or "").strip()
    hint = " (Enter — оставить прежний)" if before else ""
    folder = (ask(f"Каталог Yandex Cloud, folder_id{hint}: ") or "").strip() or before
    if not folder:
        out("каталог не введён — ничего не записано")
        return None
    return key, folder


def check(
    config_dir: Path, *, out: Callable[[str], None] = print, transport: httpx.AsyncBaseTransport | None = None
) -> int:
    """Пробный запрос с 0,5 с тишины (одна оплата минимального куска)."""
    config = speech_config.load(config_dir)
    if config is None:
        out("диктовка не настроена — hub auth speechkit")
        return 1
    result = asyncio.run(recognize_lpcm16k(SILENCE, config, transport=transport))
    if result.ok:
        out("ключ работает")
        return 0
    if result.code == "unauthorized":
        out(
            f"ключ не принят (HTTP {result.status}): у ключа нет роли ai.speechkit-stt.user "
            "или каталог не тот"
        )
    elif result.code == "bad_audio":
        out("SpeechKit не принял пробный звук (HTTP 400)")
    elif result.code == "timeout":
        out("SpeechKit не ответил за 10 с")
    elif result.status is not None:
        out(f"сбой SpeechKit (HTTP {result.status})")
    else:
        out("нет связи со SpeechKit")
    return 1


def auth(
    *,
    config_dir: Path,
    env_file: Path | None = None,
    force: bool = False,
    run_check: bool = False,
    ask_secret: Callable[[str], str] = getpass.getpass,
    ask: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """`--check` без `--from-env-file` — только проверка сохранённого ключа; иначе — запись (и проверка,
    если `--check`)."""
    if run_check and env_file is None:
        return check(config_dir, out=out, transport=transport)

    exists = speech_config.key_path(config_dir).exists()
    if exists and not force:
        answer = (ask("Ключ SpeechKit уже сохранён. Заменить? [y/N] ") or "").strip().lower()
        if answer not in _YES:
            out("оставлен прежний ключ")
            return check(config_dir, out=out, transport=transport) if run_check else 0

    got = (
        _from_env_file(env_file, out)
        if env_file is not None
        else _interactive(config_dir, ask_secret, ask, out)
    )
    if got is None:
        return 1
    key, folder = got
    speech_config.save(config_dir, api_key=key, folder_id=folder)
    out("ключ SpeechKit сохранён, каталог: задан")
    out(f"Файлы: {speech_config.service_dir(config_dir)} (0700). Ядро подхватит без рестарта.")
    if run_check:
        return check(config_dir, out=out, transport=transport)
    out("Проверить ключ: hub auth speechkit --check")
    return 0
