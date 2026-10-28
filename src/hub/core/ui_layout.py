"""Раскладка доски «Главная» (Ф41-6, docs/architecture.md): нормализация и хранение.

Хранится одним JSON в ключе `ui_layout` таблицы `main.meta` (миграций нет).
"""

import json

KEY = "ui_layout"
MAX_BYTES = 16_384
MAX_ITEMS = 40
MAX_HIDDEN_LINES = 100
VIEWS = ("own", "projects", "project")


def _int(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _item(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    if kind == "orch":
        return {"kind": "orch"}
    if kind in ("session", "channel"):
        ident = _int(raw.get("id"))
        return {"kind": kind, "id": ident} if ident is not None and ident >= 1 else None
    if kind == "agent":
        key = raw.get("key")
        return {"kind": kind, "key": key} if isinstance(key, str) and 1 <= len(key) <= 200 else None
    return None


def normalize(obj) -> dict:
    """Приводит раскладку к канонической форме; негодное — `ValueError`."""
    if not isinstance(obj, dict):
        raise ValueError("layout must be an object")
    columns = obj.get("columns", 3)
    columns = 3 if isinstance(columns, bool) or not isinstance(columns, int | float) else int(columns)
    columns = max(1, min(5, columns))
    opened: list[dict] = []
    seen: set[tuple] = set()
    raw_open = obj.get("open")
    for raw in raw_open if isinstance(raw_open, list) else []:
        item = _item(raw)
        if item is None:
            continue
        ident = (item["kind"], item.get("id", item.get("key")))
        if ident in seen:
            continue
        seen.add(ident)
        opened.append(item)
        if len(opened) >= MAX_ITEMS:
            break
    hidden: list[int] = []
    raw_hidden = obj.get("hidden_projects")
    for raw in raw_hidden if isinstance(raw_hidden, list) else []:
        value = _int(raw)
        if value is not None and value >= 1 and value not in hidden:
            hidden.append(value)
            if len(hidden) >= MAX_ITEMS:
                break
    # Убранные из списка линии «не в сети»: ключ линии и `ended_ts` её последней подписки на момент клика —
    # подписалась и ушла снова (`ended_ts` новее) — линия опять видна.
    lines: list[dict] = []
    raw_lines = obj.get("hidden_lines")
    for raw in raw_lines if isinstance(raw_lines, list) else []:
        if not isinstance(raw, dict):
            continue
        key, ended = raw.get("key"), _int(raw.get("ended_ts"))
        if not isinstance(key, str) or not 1 <= len(key) <= 200 or ended is None or ended < 0:
            continue
        if any(line["key"] == key for line in lines):
            continue
        lines.append({"key": key, "ended_ts": ended})
    lines = lines[-MAX_HIDDEN_LINES:]
    # Ф47-1: вид «Главной» (свой набор / по проектам / один проект) и выбор сессии в столбце проекта.
    view = obj.get("view")
    view_project = _int(obj.get("view_project"))
    picks: dict[str, dict] = {}
    raw_picks = obj.get("picks")
    for raw_id, raw_item in raw_picks.items() if isinstance(raw_picks, dict) else []:
        project = None
        if isinstance(raw_id, str) and raw_id.isascii() and raw_id.isdigit():
            project = int(raw_id)
        item = _item(raw_item)
        if project is None or project < 1 or item is None or item["kind"] not in ("session", "agent"):
            continue
        picks[str(project)] = item
        if len(picks) >= MAX_ITEMS:
            break
    result = {
        "v": 1,
        "list_collapsed": obj.get("list_collapsed") is True,
        "columns": columns,
        "wait_open": obj.get("wait_open") is True,
        "open": opened,
        "hidden_projects": hidden,
        "hidden_lines": lines,
        "view": view if view in VIEWS else "own",
        "view_project": view_project if view_project is not None and view_project >= 1 else None,
        "picks": picks,
    }
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_BYTES:
        raise ValueError("layout too large")
    return result


def get_layout(conn) -> dict | None:
    row = conn.execute("SELECT value FROM main.meta WHERE key = ?", (KEY,)).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except ValueError:
        return None


def merge_hidden_lines(stored: dict | None, layout: dict) -> dict:
    """Убранные линии «не в сети» не теряются, когда раскладку сохраняет другая вкладка или телефон со старой
    копией (раскладка пишется целиком): ключи из хранимой раскладки добавляются, у общего ключа берётся более
    поздний `ended_ts`. Вернуть убранную линию можно только новым концом подписки — так и было."""
    old = stored.get("hidden_lines") if isinstance(stored, dict) else None
    if not isinstance(old, list) or not old:
        return layout
    kept: dict[str, int] = {}
    for raw in old:
        if not isinstance(raw, dict):
            continue
        key, ended = raw.get("key"), _int(raw.get("ended_ts"))
        if isinstance(key, str) and ended is not None and key not in kept:
            kept[key] = ended
    incoming = [line["key"] for line in layout["hidden_lines"]]
    merged = [{"key": key, "ended_ts": ended} for key, ended in kept.items() if key not in incoming]
    for line in layout["hidden_lines"]:
        merged.append({"key": line["key"], "ended_ts": max(line["ended_ts"], kept.get(line["key"], 0))})
    return {**layout, "hidden_lines": merged[-MAX_HIDDEN_LINES:]}


def save_layout(conn, layout: dict) -> dict:
    """PUT раскладки: слить убранные линии с хранимыми и записать; вернуть записанное."""
    merged = merge_hidden_lines(get_layout(conn), layout)
    set_layout(conn, merged)
    return merged


def set_layout(conn, layout: dict) -> None:
    conn.execute(
        "INSERT INTO main.meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (KEY, json.dumps(layout, ensure_ascii=False)),
    )
