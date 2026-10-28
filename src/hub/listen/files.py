"""Файлы службы записи интервью (Ф60-1, П7; образец — `hub transcribe`).

- `<config_dir>/listen/` (0700): `token` — bearer ядра `listen:recorder` ровно со скоупом `listen` (0600),
  `token.json` — `{token_id}` без секрета (повторный `hub listen setup` отзывает прежний).
- `<data_dir>/listen/` (0700) — каталог данных службы (состояние для `status`, если понадобится части B;
  юнит может писать только сюда). Звук на диск не пишется нигде.

Значение токена никуда не печатается."""

from pathlib import Path

from hub.agent.files import atomic_write, private_dir, read_json, read_secret, write_secret

__all__ = ["atomic_write", "private_dir", "read_json", "read_secret", "write_secret"]

DIR_NAME = "listen"
TOKEN_FILE = "token"  # noqa: S105 — имя файла, не секрет
TOKEN_META_FILE = "token.json"  # noqa: S105 — имя файла, не секрет
TOKEN_NAME = "listen:recorder"  # noqa: S105 — имя токена в ядре, не секрет
TOKEN_SCOPES = ("listen",)
DEFAULT_URL = "http://127.0.0.1:8787/api/v1"


def config_service_dir(config_dir: Path) -> Path:
    return config_dir / DIR_NAME


def token_path(config_dir: Path) -> Path:
    return config_service_dir(config_dir) / TOKEN_FILE


def token_meta_path(config_dir: Path) -> Path:
    return config_service_dir(config_dir) / TOKEN_META_FILE


def data_service_dir(data_dir: Path) -> Path:
    return data_dir / DIR_NAME


def read_token(config_dir: Path) -> str | None:
    return read_secret(token_path(config_dir))
