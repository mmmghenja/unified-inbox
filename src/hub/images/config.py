"""Ключ и настройки Alibaba Model Studio (Ф91 Р1).

`<config_dir>/alibaba/` (0700; сессиям закрыт гардом `hub_dirs`, как весь каталог конфига хаба):
- `api_key` (0600) — ключ DashScope; пишет только `hub auth alibaba`, значение никуда не печатается;
- `config.json` (необязателен, правит владелец руками) — `base_url` (по умолчанию `dashscope-intl`, Сингапур; у
  новых ключей — `https://{WorkspaceId}.ap-southeast-1.maas.aliyuncs.com`), `proxy` (`socks5://…`; нет или
  `"none"` — напрямую), `image_daily` — `{"project": 60, "total": 200}` (Р4).

Ядро читает оба файла при каждом запросе (дёшево): после `hub auth alibaba` рестарт не нужен. Машинного файла
нет: ключ общий для главной машины.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..core.connector_auth import write_secret_file

DIR_NAME = "alibaba"
KEY_FILE = "api_key"
CONFIG_FILE = "config.json"
DEFAULT_BASE_URL = "https://dashscope-intl.aliyuncs.com"
DEFAULT_DAILY_PROJECT = 60
DEFAULT_DAILY_TOTAL = 200


@dataclass(frozen=True)
class ImageConfig:
    api_key: str = field(repr=False)  # ключ не попадает в журнал даже при печати объекта
    base_url: str = DEFAULT_BASE_URL
    proxy: str | None = field(default=None, repr=False)
    daily_project: int = DEFAULT_DAILY_PROJECT
    daily_total: int = DEFAULT_DAILY_TOTAL


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


def _positive(value: object, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return default


def _base_url(value: object) -> str:
    """Только https (ключ уходит заголовком); иначе — адрес по умолчанию."""
    text = str(value or "").strip().rstrip("/")
    return text if text.startswith("https://") else DEFAULT_BASE_URL


def load(config_dir: Path) -> ImageConfig | None:
    """Настройки или `None`, если ключ не задан (картинки выключены)."""
    key = read_key(config_dir)
    if not key:
        return None
    settings = read_settings(config_dir)
    daily = settings.get("image_daily")
    daily = daily if isinstance(daily, dict) else {}
    return ImageConfig(
        api_key=key,
        base_url=_base_url(settings.get("base_url")),
        proxy=_proxy(settings.get("proxy")),
        daily_project=_positive(daily.get("project"), DEFAULT_DAILY_PROJECT),
        daily_total=_positive(daily.get("total"), DEFAULT_DAILY_TOTAL),
    )


def save(config_dir: Path, *, api_key: str) -> None:
    """Ключ — в `api_key` (0600, во временный файл и `os.replace`: читатель видит либо прежний ключ, либо
    новый). `config.json` не трогается."""
    directory = service_dir(config_dir)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_mode & 0o077:
        directory.chmod(0o700)
    target = key_path(config_dir)
    tmp = target.with_name(f".{KEY_FILE}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    write_secret_file(tmp, api_key)
    try:
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
