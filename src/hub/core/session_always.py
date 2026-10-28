"""«Всегда в этой сессии» — набор сессии хаба в ядре (Ф99-7.2, `docs/architecture.md`).

`hub_session.always_json` — `[{"key", "ts"}]` (миграция `0119_main.sql`). Ключ строит `hub.work.always.key_of`
по самой карточке — у карточек без кнопки (кнопки этапа 1, чужие хуки, правила проекта, сбой проверки,
вопросы, инструменты хаба…) ключа нет, и ядро отказывает 422 независимо от интерфейса. Ставит набор только
решение карточки куки интерфейса (`held_work.decide(always=True)` — его зовёт лишь `/session-requests/{id}/
decide`), снимает — только кука (`POST /sessions/{id}/always/remove`). Агент владельца и «Сразу да» его не ставят;
передача дел не переносит (новая сессия создаётся с пустым набором). Изменение двигает `seq` сессии — хост
видит набор в записи (`always`) и применяет на месте."""

import json

from hub.work import always as always_mod

from . import hub_sessions
from .audit import write_audit
from .hub_sessions import SessionError


def items_of(raw: str | None) -> list[dict]:
    """Набор из столбца: только годные ключи; битый столбец — пусто."""
    try:
        value = json.loads(raw) if raw else []
    except ValueError, TypeError:
        return []
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if isinstance(item, dict) and always_mod.valid(item.get("key")):
            ts = item.get("ts")
            result.append({"key": item["key"], "ts": ts if isinstance(ts, int) else None})
    return result


def card_key(request: dict) -> str | None:
    """Ключ «всегда» карточки (строка `held_request` с разобранным `input`); `None` — кнопки нет."""
    return always_mod.key_of(
        kind=request.get("kind"),
        rule=request.get("rule"),
        tool=request.get("tool"),
        tool_input=request.get("input"),
        truncated=bool(request.get("input_truncated")),
    )


def add(conn, rec, key: str, *, now_ms: int) -> None:
    """Ключ — в набор сессии (уже есть — ничего). Полный набор — 409."""
    if rec.closed_ts is not None:
        raise SessionError(409, "сессия закрыта")
    items = items_of(rec.always_json)
    if any(item["key"] == key for item in items):
        return
    if len(items) >= always_mod.MAX:
        raise SessionError(
            409, f"«Всегда» у сессии не больше {always_mod.MAX} — сними лишние в «Разрешениях»"
        )
    items.append({"key": key, "ts": now_ms})
    hub_sessions.touch(conn, rec.id, now_ms, always_json=json.dumps(items, ensure_ascii=False))


def remove(conn, rec, key: str, *, now_ms: int, audit: dict) -> bool:
    """Снять ключ. Нет такого — 404. Аудит `hub_session_always_off`."""
    items = items_of(rec.always_json)
    left = [item for item in items if item["key"] != key]
    if len(left) == len(items):
        raise SessionError(404, "такого «всегда» у сессии нет")
    hub_sessions.touch(
        conn, rec.id, now_ms, always_json=json.dumps(left, ensure_ascii=False) if left else None
    )
    detail = {"hub_session_id": rec.id, "key_kind": key.split(":", 1)[0]}
    write_audit(conn, now_ms=now_ms, action="hub_session_always_off", result="ok", detail=detail, **audit)
    return True
