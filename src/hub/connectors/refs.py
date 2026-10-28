"""Извлечение `refs` из текста сообщения — общий модуль для ВСЕХ коннекторов (план, раздел 2: `refs_json`).
Спецификация: `docs/architecture.md`.

Ключи задач (`DEV-123`, `PRD-45`, …) и URL — из текста и из `link`-сущностей (`entities.ts`: `type=link`,
`url`). Без дублей, в порядке появления в тексте (URL из одних только entities — следом, в порядке аргумента),
не больше `MAX_REFS`.
"""

import re

MAX_REFS = 20

# `[A-Z][A-Z0-9]{1,9}-\d{1,6}` — граница слова с обеих сторон, чтобы не резать "ABC-123" из "XABC-1234".
_TASK_KEY = r"\b[A-Z][A-Z0-9]{1,9}-\d{1,6}\b"
_URL = r"https?://[^\s<>\"']+"
_COMBINED_RE = re.compile(f"(?P<key>{_TASK_KEY})|(?P<url>{_URL})")


def extract_refs(text: str | None, *, link_urls: list[str] | None = None) -> list[str]:
    """`text` сканируется ОДНИМ проходом (ключи и URL в порядке появления вперемешку); `link_urls` —
    URL из `link`-сущностей (`TextUrl`/`Url`), добавляются следом, тоже по порядку, тоже без дублей."""
    seen: set[str] = set()
    out: list[str] = []

    def _add(value: str) -> None:
        if value not in seen:
            seen.add(value)
            out.append(value)

    if text:
        for m in _COMBINED_RE.finditer(text):
            _add(m.group("key") or m.group("url"))

    for url in link_urls or []:
        _add(url)

    return out[:MAX_REFS]
