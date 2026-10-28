"""Ключ и настройки SpeechKit (Ф59-1, раздел 1 «Ключи»).

`<config_dir>/speechkit/` (0700; сессиям закрыт гардом `hub_dirs`, как весь каталог конфига хаба):
- `api_key` (0600) — ключ API Yandex Cloud; пишет только `hub auth speechkit`, значение никуда не печатается;
- `config.json` — `folder_id`, `lang` (`ru-RU`), `daily_chunks` (2000), необязательный `proxy`
  (`socks5://…`; нет или `"none"` — напрямую).

Ядро читает оба файла при каждом запросе (дёшево): после `hub auth speechkit` рестарт не нужен.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

from ..core.connector_auth import write_secret_file

DIR_NAME = "speechkit"
KEY_FILE = "api_key"
CONFIG_FILE = "config.json"
DEFAULT_LANG = "ru-RU"
DEFAULT_DAILY_CHUNKS = 2000


@dataclass(frozen=True)
class SpeechConfig:
    api_key: str
    folder_id: str
    lang: str = DEFAULT_LANG
    daily_chunks: int = DEFAULT_DAILY_CHUNKS
    proxy: str | None = None


def service_dir(config_dir: Path) -> Path:
    return config_dir / DIR_NAME


def key_path(config_dir: Path) -> Path:
    return service_dir(config_dir) / KEY_FILE


def config_path(config_dir: Path) -> Path:
    return service_dir(config_dir) / CONFIG_FILE


def read_key(config_dir: Path) -> str | None:
    """Ключ из файла или `None` (нет файла, нечитаем, пуст)."""
    try:
        value = key_path(config_dir).read_text(encoding="utf-8").strip()
    except OSError, UnicodeDecodeError:
        return None
    return value or None


def read_settings(config_dir: Path) -> dict:
    """`config.json` как словарь; нет файла или битый — пустой словарь."""
    try:
        value = json.loads(config_path(config_dir).read_text(encoding="utf-8"))
    except OSError, UnicodeDecodeError, ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _proxy(value: object) -> str | None:
    text = str(value or "").strip()
    return None if text.lower() in ("", "none") else text


def _daily(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return DEFAULT_DAILY_CHUNKS


def load(config_dir: Path) -> SpeechConfig | None:
    """Настройки диктовки или `None`, если ключ или каталог не заданы (диктовка выключена)."""
    key = read_key(config_dir)
    settings = read_settings(config_dir)
    folder = str(settings.get("folder_id") or "").strip()
    if not key or not folder:
        return None
    lang = str(settings.get("lang") or "").strip() or DEFAULT_LANG
    return SpeechConfig(
        api_key=key,
        folder_id=folder,
        lang=lang,
        daily_chunks=_daily(settings.get("daily_chunks")),
        proxy=_proxy(settings.get("proxy")),
    )


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_mode & 0o077:
        path.chmod(0o700)


def save(config_dir: Path, *, api_key: str, folder_id: str) -> None:
    """Ключ — в `api_key` (0600, через `write_secret_file` во временный файл и `os.replace`: читатель видит
    либо прежний ключ, либо новый); `folder_id` — в `config.json` поверх прежних настроек (`lang`,
    `daily_chunks`, `proxy` сохраняются)."""
    directory = service_dir(config_dir)
    _private_dir(directory)
    target = key_path(config_dir)
    tmp = target.with_name(f".{KEY_FILE}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    write_secret_file(tmp, api_key)
    try:
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    settings = read_settings(config_dir)
    settings["folder_id"] = folder_id
    settings.setdefault("lang", DEFAULT_LANG)
    settings.setdefault("daily_chunks", DEFAULT_DAILY_CHUNKS)
    cfg_tmp = config_path(config_dir).with_name(f".{CONFIG_FILE}.{os.getpid()}.tmp")
    fd = os.open(cfg_tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
        os.chmod(cfg_tmp, 0o600)
        os.replace(cfg_tmp, config_path(config_dir))
    except BaseException:
        cfg_tmp.unlink(missing_ok=True)
        raise


def parse_env_file(text: str) -> dict[str, str]:
    """Строки `KEY=value` dotenv-файла: пустые и `#…` пропускаются, `export ` в начале снимается, кавычки
    вокруг значения снимаются; у значения без кавычек хвост ` #…` — комментарий."""
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            hash_at = value.find(" #")
            if hash_at >= 0:
                value = value[:hash_at].rstrip()
        if key:
            result[key] = value
    return result
