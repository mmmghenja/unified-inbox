"""Ф77-1 (docs/architecture.md): версия хаба на этой машине — короткий коммит рабочего
дерева, из которого импортирован пакет (`git -C <каталог пакета> rev-parse --short=8 HEAD`). Одна функция на
ядро и клиентов (MCP-сервер сессии): ядро пишет её в строку своей машины, клиент — в поле `machine` подписки и
пульса. Таймаут 2 с, кэш на процесс; нет git, не репозиторий, сбой — `None`."""

import functools
import re
import subprocess
from pathlib import Path

TIMEOUT_S = 2.0
COMMIT_RE = re.compile(r"^[0-9a-f]{7,12}$")


def _run(cwd: Path) -> str | None:
    try:
        done = subprocess.run(  # noqa: S603 — аргументы заданы кодом
            ["git", "-C", str(cwd), "rev-parse", "--short=8", "HEAD"],  # noqa: S607 — git из PATH
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if done.returncode != 0:
        return None
    text = (done.stdout or "").strip().lower()
    return text if COMMIT_RE.fullmatch(text) else None


@functools.cache
def commit() -> str | None:
    """Короткий коммит хаба на этой машине или `None`."""
    return _run(Path(__file__).resolve().parent)
