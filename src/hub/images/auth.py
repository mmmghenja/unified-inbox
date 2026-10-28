"""`hub auth alibaba [--from-env-file PATH] [--var NAME] [--force] [--check]` (Ф91 Р1; образец — `hub auth
speechkit`).

Запускает только владелец. Ключ вводится без эха (`getpass`) или берётся из dotenv-файла (переменная `--var`, по
умолчанию `DASHSCOPE_API_KEY`); значение никуда не печатается. Ядро не нужно: ключ читается из файла при
каждом запросе картинки.

`--check` — запрос с заведомо неверным размером: ключ проверяется, картинка не делается и не оплачивается.
"""

import asyncio
import getpass
from collections.abc import Callable
from pathlib import Path

import httpx

from ..speech.config import parse_env_file
from . import config as image_config
from .dashscope import probe

DEFAULT_VAR = "DASHSCOPE_API_KEY"
_YES = ("y", "yes", "д", "да")


def _from_env_file(path: Path, var: str, out: Callable[[str], None]) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        out(f"файл не читается: {path} ({type(exc).__name__}) — ничего не записано")
        return None
    key = parse_env_file(text).get(var, "").strip()
    if not key:
        out(f"в файле нет: {var} — ничего не записано")
        return None
    return key


def check(
    config_dir: Path, *, out: Callable[[str], None] = print, transport: httpx.AsyncBaseTransport | None = None
) -> int:
    config = image_config.load(config_dir)
    if config is None:
        out("ключ Alibaba не задан — hub auth alibaba")
        return 1
    result = asyncio.run(probe(config, transport=transport))
    if result.ok or result.code == "rejected":
        # 400 на заведомо неверный размер — ключ принят шлюзом; 2xx — тоже принят.
        out("ключ работает")
        return 0
    if result.code == "key":
        out(f"ключ не принят (HTTP {result.status}): не тот ключ или не тот регион (base_url в config.json)")
    elif result.code == "balance":
        out("ключ принят, но на счёте Model Studio нет денег")
    elif result.code == "rate":
        out("ключ принят, Alibaba просит подождать (предел запросов)")
    elif result.code == "timeout":
        out("Alibaba не ответил за 30 с")
    elif result.status is not None:
        out(f"сбой Alibaba (HTTP {result.status})")
    else:
        out("нет связи с Alibaba")
    return 1


def auth(
    *,
    config_dir: Path,
    env_file: Path | None = None,
    var: str = DEFAULT_VAR,
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

    if image_config.key_path(config_dir).exists() and not force:
        answer = (ask("Ключ Alibaba уже сохранён. Заменить? [y/N] ") or "").strip().lower()
        if answer not in _YES:
            out("оставлен прежний ключ")
            return check(config_dir, out=out, transport=transport) if run_check else 0

    if env_file is not None:
        key = _from_env_file(env_file, (var or DEFAULT_VAR).strip(), out)
    else:
        key = (ask_secret("Ключ API Alibaba Model Studio (ввод не показывается): ") or "").strip() or None
        if key is None:
            out("ключ не введён — ничего не записано")
    if key is None:
        return 1
    image_config.save(config_dir, api_key=key)
    out("ключ Alibaba сохранён")
    out(f"Файлы: {image_config.service_dir(config_dir)} (0700). Ядро подхватит без рестарта.")
    if run_check:
        return check(config_dir, out=out, transport=transport)
    out("Проверить ключ: hub auth alibaba --check")
    return 0
