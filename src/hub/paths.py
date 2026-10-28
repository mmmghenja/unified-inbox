"""Каталоги данных и конфига. Оба — 0700 (план, раздел 7); создаются только здесь."""

import os
from pathlib import Path


def data_dir() -> Path:
    return _private(Path(os.environ.get("HUB_DATA_DIR") or Path.home() / ".local/share/hub"))


def config_dir() -> Path:
    return _private(config_dir_path())


def config_dir_path() -> Path:
    """Путь каталога конфига без создания — для проверок, которые только читают (К8 в `cli.main`)."""
    return Path(os.environ.get("HUB_CONFIG_DIR") or Path.home() / ".config/hub")


def _private(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_mode & 0o077:
        path.chmod(0o700)
    return path
