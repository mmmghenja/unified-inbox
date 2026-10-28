"""Правила сессий хаба — два слоя текста, их вклеивает в системный промпт сам хаб (Ф9-1a, раздел 1; спека
`docs/architecture.md`): и сессии разборщика 7c (W8), и рабочей сессии 9a (F7).

- общий слой — `~/.claude/hub-rules.md` (как вести себя с людьми от имени владельца);
- слой проекта — `<project.workdir>/.claude/hub.md` (люди, манера, скорость, поведение, где логи).

`<config_dir>/wake|work/rules*.md` больше не читаются. Нет файла, битая ссылка, не читается или длиннее 8000
символов (обрезан) — НЕ молча: вызывающий пишет строку в журнал запуска и показывает предупреждение в
`hub wake status` / `hub work status`; запуск идёт как обычно. Текст правил в журнал не попадает — только путь
и причина.

Только stdlib: модуль импортирует лёгкий CLI-путь.
"""

import os
from pathlib import Path
from typing import NamedTuple

RULES_MAX_CHARS = 8000
COMMON_HEADER = "Правила владельца для всех сессий хаба:"
PROJECT_HEADER = "Правила проекта:"


class RulesFile(NamedTuple):
    text: str | None  # `None` — файла нет, не читается или пуст
    warning: str | None  # причина для журнала и status; `None` — всё в порядке


def common_rules_path(home: Path) -> Path:
    return Path(home) / ".claude" / "hub-rules.md"


def project_rules_path(workdir: str | os.PathLike) -> Path:
    return Path(workdir) / ".claude" / "hub.md"


def read_rules_file(path: Path) -> RulesFile:
    if path.is_symlink() and not path.exists():
        return RulesFile(None, f"{path}: битая ссылка")
    if not path.exists():
        return RulesFile(None, f"{path}: нет файла")
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        return RulesFile(None, f"{path}: не читается ({exc.__class__.__name__})")
    if not text:
        return RulesFile(None, None)
    if len(text) > RULES_MAX_CHARS:
        return RulesFile(
            text[:RULES_MAX_CHARS], f"{path}: длиннее {RULES_MAX_CHARS} символов ({len(text)}) — обрезан"
        )
    return RulesFile(text, None)


class Rules(NamedTuple):
    blocks: list[str]  # готовые блоки промпта с заголовками
    warnings: list[str]


def load(*, home: Path, workdir: str | None) -> Rules:
    """Оба слоя: общий, затем проектный (он главнее про людей, манеру и сроки — стоит ниже)."""
    blocks: list[str] = []
    warnings: list[str] = []
    sources = [(COMMON_HEADER, common_rules_path(home))]
    if workdir:
        sources.append((PROJECT_HEADER, project_rules_path(workdir)))
    else:
        warnings.append("у проекта нет рабочей папки — правил проекта нет")
    for header, path in sources:
        loaded = read_rules_file(path)
        if loaded.warning:
            warnings.append(loaded.warning)
        if loaded.text:
            blocks.append(f"{header}\n{loaded.text}")
    return Rules(blocks, warnings)


def compose(base: str, *, home: Path, workdir: str | None) -> tuple[str, list[str]]:
    """Неизменный текст хаба + оба слоя правил. Возвращает (текст промпта, предупреждения)."""
    rules = load(home=home, workdir=workdir)
    return "\n\n".join([base, *rules.blocks]), rules.warnings
