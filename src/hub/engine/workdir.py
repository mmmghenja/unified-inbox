"""Безопасность рабочей папки проекта. Спека 7c, поправка П4 (В3).

`hub project set --workdir` и сессии хаба отказывают, если папка —
`/`, `$HOME`, предок или потомок `~/.config`, `~/.local/share`, `~/.claude`, `~/.ssh`, `~/.gnupg` или
каталогов хаба (`config_dir`, `data_dir`). Причина: под `--restricted` deny-правила из пользовательских
настроек Claude владельца не действуют, а файловые инструменты сессии видят всю рабочую папку.

Только stdlib: модуль импортирует CLI.
"""

import os
from pathlib import Path

HOME_SENSITIVE = (".config", ".local/share", ".claude", ".ssh", ".gnupg")


def _within(child: str, parent: str) -> bool:
    """`child` лежит внутри `parent` (или совпадает). Оба — после realpath."""
    if child == parent:
        return True
    return child.startswith(parent.rstrip("/") + "/")


def unsafe_reason(workdir: str | None, *, home: Path, config_dir: Path, data_dir: Path) -> str | None:
    """Почему папку нельзя отдавать сессии разборщика; `None` — можно. Существование не проверяется (это
    отдельная причина у вызывающего)."""
    if not isinstance(workdir, str) or not workdir:
        return "папка не задана"
    if not os.path.isabs(workdir) or "\x00" in workdir:
        return "путь не абсолютный"
    real = os.path.realpath(workdir)
    if real == "/":
        return "корень файловой системы"
    home_real = os.path.realpath(home)
    if real == home_real:
        return "домашний каталог целиком"
    forbidden = [home / sub for sub in HOME_SENSITIVE] + [config_dir, data_dir]
    for path in forbidden:
        other = os.path.realpath(path)
        if _within(real, other) or _within(other, real):
            return f"пересекается с {path}"
    return None
