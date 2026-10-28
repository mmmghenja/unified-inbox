"""Файлы службы расшифровки (Ф14-6, docs/architecture.md).

- `<config_dir>/transcribe.json` — настройки (не секрет): интерпретатор движка, каталоги модели, устройство,
  выгрузка по простою. Нет файла или он битый — умолчания.
- `<config_dir>/transcribe/` (0700): `token` — bearer ядра `transcribe:worker` (0600), `token.json` —
  `{token_id, host}` без секрета (повторный `hub transcribe setup` отзывает прежний — только выпущенный на
  этой машине, Ф79-1: служба стоит на обеих машинах, у каждой свой токен). `transcribe.json` и `transcribe/`
  — машинные файлы (scripts/hub-machine-files.sh): переключение главной их не возит.
- `<data_dir>/transcribe/` (0700): `tmp/` — файлы текущего задания (0600, удаляются после него и на старте),
  `state.json` — движок жив/нет, последние ошибки (для `hub transcribe status`). Юнит пишет ТОЛЬКО сюда
  (`ReadWritePaths=%h/.local/share/hub/transcribe`).

Запись — атомарно (те же функции, что у hub-agent). Значение токена никуда не печатается.
"""

import json
import logging
import os
from pathlib import Path

from hub.agent.files import atomic_write, private_dir, read_json, read_secret, write_secret

_logger = logging.getLogger(__name__)

__all__ = ["atomic_write", "private_dir", "read_json", "read_secret", "write_secret"]

DIR_NAME = "transcribe"
CONFIG_FILE = "transcribe.json"
TOKEN_FILE = "token"  # noqa: S105 — имя файла, не секрет
TOKEN_META_FILE = "token.json"  # noqa: S105 — имя файла, не секрет
STATE_FILE = "state.json"
TMP_DIR = "tmp"
TOKEN_NAME = "transcribe:worker"  # noqa: S105 — имя токена в ядре, не секрет
TOKEN_SCOPES = ("read", "transcribe")

_DICTATION = "~/.local/share/dictation_tool"
DEFAULT_CONFIG: dict = {
    # Решение 2: питон и модели диктовки, код — хаба (`engine.py`).
    "engine_python": f"{_DICTATION}/venv/bin/python3",
    "model_dirs": [f"{_DICTATION}/models/large-v3-turbo", f"{_DICTATION}/models/medium"],
    "compute_type": "int8_float16",
    "device": "cuda",
    # Решение 3: движок живёт, пока есть задания; столько секунд без заданий — закрыть (видеопамять свободна).
    "idle_unload_s": 300,
    "ffmpeg": "ffmpeg",
    # Ф79-1 (docs/architecture.md): `device == "cuda"` — быстрый работник:
    # голосовые первым и куски диктовки/интервью вторым движком (`dictate`); его модель выгружается после
    # `dictate_idle_unload_s` без кусков. На cpu диктовка не берётся.
    "dictate": True,
    "dictate_idle_unload_s": 1800,
}


def is_fast(config: dict) -> bool:
    return str(config.get("device") or "") == "cuda"


def config_path(config_dir: Path) -> Path:
    return config_dir / CONFIG_FILE


def config_service_dir(config_dir: Path) -> Path:
    return config_dir / DIR_NAME


def token_path(config_dir: Path) -> Path:
    return config_service_dir(config_dir) / TOKEN_FILE


def token_meta_path(config_dir: Path) -> Path:
    return config_service_dir(config_dir) / TOKEN_META_FILE


def data_service_dir(data_dir: Path) -> Path:
    return data_dir / DIR_NAME


def tmp_dir(data_dir: Path) -> Path:
    return data_service_dir(data_dir) / TMP_DIR


def state_path(data_dir: Path) -> Path:
    return data_service_dir(data_dir) / STATE_FILE


def load_config(config_dir: Path) -> dict:
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    path = config_path(config_dir)
    if not path.exists():
        return config
    stored = read_json(path)
    if stored is None:
        _logger.warning("transcribe: transcribe.json не читается — работаю на умолчаниях")
        return config
    config.update({k: v for k, v in stored.items() if k in DEFAULT_CONFIG})
    return config


def write_config(config_dir: Path, config: dict) -> None:
    private_dir(config_dir)
    atomic_write(config_path(config_dir), json.dumps(config, ensure_ascii=False, indent=2) + "\n")


def expand(path: str) -> str:
    return os.path.expanduser(str(path))


def load_state(data_dir: Path) -> dict | None:
    return read_json(state_path(data_dir))


def save_state(data_dir: Path, state: dict) -> None:
    private_dir(data_service_dir(data_dir))
    atomic_write(state_path(data_dir), json.dumps(state, ensure_ascii=False, indent=1) + "\n")


def clean_tmp(data_dir: Path) -> Path:
    """П10: на старте службы — пустой личный `tmp` (0700); ссылки не разыменовываются."""
    private_dir(data_service_dir(data_dir))
    path = private_dir(tmp_dir(data_dir))
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            for sub in child.iterdir():
                sub.unlink(missing_ok=True)
            child.rmdir()
        else:
            child.unlink(missing_ok=True)
    return path
