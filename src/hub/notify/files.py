"""Файлы спутника уведомлений: `<config_dir>/notify.token` (0600, bearer ровно со скоупом `notify`) и
`notify.token.json` (`{token_id}` без секрета: повторный `hub notify setup` отзывает прежний)."""

from pathlib import Path

from hub.agent.files import atomic_write, read_json, read_secret, write_secret

__all__ = ["atomic_write", "read_json", "read_secret", "write_secret"]

TOKEN_FILE = "notify.token"  # noqa: S105 — имя файла, не секрет
TOKEN_META_FILE = "notify.token.json"  # noqa: S105 — имя файла, не секрет
TOKEN_NAME = "notify:companion"  # noqa: S105 — имя токена в ядре, не секрет
TOKEN_SCOPES = ("notify",)
DEFAULT_URL = "http://127.0.0.1:8787/api/v1"


def token_path(config_dir: Path) -> Path:
    return config_dir / TOKEN_FILE


def token_meta_path(config_dir: Path) -> Path:
    return config_dir / TOKEN_META_FILE


def read_token(config_dir: Path) -> str | None:
    return read_secret(token_path(config_dir))
