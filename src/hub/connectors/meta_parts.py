"""Ф14-2 A6 (П4, `docs/architecture.md`): справочник аккаунта (`tg_contacts`, `slack_people`)
в `account_meta` порциями — у ядра потолок 64 КиБ на ключ, справочник бывает больше.

Ключ `<name>` — заголовок `{"parts": N, "count": M}`, порции `<name>:0` … `<name>:N-1` — списки записей,
каждая сериализованная (как в ядре: `json.dumps(ensure_ascii=False, sort_keys=True)`, UTF-8) не больше
`PART_MAX_BYTES`. События — СНАЧАЛА порции, ПОТОМ заголовок: ядро применяет пачку по порядку, и читатель
(`queries.account_meta_list`) не увидит заголовок, ссылающийся на ещё не дошедшие порции. Лишние старые
порции (прошлая публикация была длиннее) ядро не читает.
"""

import json

PART_MAX_BYTES = 40 * 1024


def _size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def split_parts(items: list[dict], max_bytes: int = PART_MAX_BYTES) -> list[list[dict]]:
    """Записи по порциям: каждая порция-список в сериализованном виде не больше `max_bytes`. Запись, которая
    одна не помещается (не бывает у имён и id), пропускается."""
    parts: list[list[dict]] = []
    current: list[dict] = []
    current_size = 2  # `[]`
    for item in items:
        item_size = _size(item)
        if item_size + 2 > max_bytes:
            continue
        extra = item_size + (2 if current else 0)  # разделитель `, `
        if current and current_size + extra > max_bytes:
            parts.append(current)
            current, current_size = [], 2
            extra = item_size
        current.append(item)
        current_size += extra
    if current:
        parts.append(current)
    return parts


def meta_events(name: str, items: list[dict], max_bytes: int = PART_MAX_BYTES) -> list[dict]:
    """События `account_meta` справочника `name`: порции по порядку, последним — заголовок."""
    parts = split_parts(items, max_bytes)
    events = [
        {"type": "account_meta", "key": f"{name}:{index}", "value": part} for index, part in enumerate(parts)
    ]
    count = sum(len(part) for part in parts)
    events.append({"type": "account_meta", "key": name, "value": {"parts": len(parts), "count": count}})
    return events
