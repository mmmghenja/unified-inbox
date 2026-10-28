"""Ф97-1 (docs/architecture.md): `hub trace` — полная переписка по карточке канала владельца.

Только чтение. Заявка, строки очереди (карточки и поручения агенту), реплики разговора «Мой агент»
с файлами, реплики владельца и события сессии хаба — одним ответом, чтобы расследовать случай, не лазая в
базу. Тела файлов, скриншотов и `held_event.detail_json` (там diff и вывод команд) не отдаются. SQL — только
здесь, маршрут тонкий; основная база подключена как `main.`, общая (held_*) — без префикса. Время —
миллисекунды, как в таблицах.
"""

import json

LIMIT = 500  # строк на раздел; больше — раздел обрезан, имя раздела — в `truncated`
_SESSION_BEFORE_MS = 5 * 60_000
_SESSION_AFTER_MS = 10 * 60_000

_ITEM_COLUMNS = (
    "id, ref, kind, project_id, session_id, created_ts, closed_ts, closed_by, hold_ts, payload_json"
)
_TALK_COLUMNS = "id, ts, author, text, ref, reply_to, ask, tell, via, closed_outcome, session_id, progress"
_HUMAN_COLUMNS = (
    "id, project_id, kind, site, url, doing, need, profile, tab, shot_size, shot_mime, expires_ts, status, "
    "decided_ts, decided_by, decided_note, author_project_id, author_role_key, author_role_no, created_ts"
)


def _rows(conn, sql: str, params=()) -> list[dict]:
    cur = conn.execute(sql, params)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def _cut(rows: list[dict], section: str, truncated: list[str]) -> list[dict]:
    if len(rows) > LIMIT:
        truncated.append(section)
        return rows[:LIMIT]
    return rows


def _marks(n: int) -> str:
    return ",".join("?" * n)


def _unpack(item: dict) -> dict:
    raw = item.pop("payload_json", None)
    try:
        payload = json.loads(raw) if raw else {}
    except ValueError:
        payload = {}
    item["payload"] = payload if isinstance(payload, dict) else {}
    return item


def _items_by_refs(conn, refs: list[str], limit: int) -> list[dict]:
    if not refs:
        return []
    sql = (
        f"SELECT {_ITEM_COLUMNS} FROM main.owner_item "  # noqa: S608
        f"WHERE ref IN ({_marks(len(refs))}) "
        f"OR (kind = 'errand' AND json_extract(payload_json, '$.target') IN ({_marks(len(refs))})) "
        "ORDER BY id LIMIT ?"
    )
    return [_unpack(r) for r in _rows(conn, sql, (*refs, *refs, limit))]


def _linked_refs(conn, ref: str) -> list[str]:
    """Сам ref + один шаг в обе стороны: у поручения — его `target`, у карточки — ref поручений на неё."""
    refs = {ref}
    for item in _items_by_refs(conn, [ref], LIMIT + 1):
        refs.add(item["ref"])
        if item["kind"] == "errand":
            target = item["payload"].get("target")
            if isinstance(target, str) and target:
                refs.add(target)
    return sorted(refs)


def _human_ids(refs: list[str]) -> list[int]:
    out = []
    for ref in refs:
        head, _, tail = ref.partition(":")
        if head == "human" and tail.isdigit():
            out.append(int(tail))
    return out


def _files(conn, talk_ids: list[int]) -> dict[int, list[dict]]:
    if not talk_ids:
        return {}
    rows = _rows(
        conn,
        f"SELECT talk_id, name, mime, size, purged_ts FROM main.talk_file "  # noqa: S608
        f"WHERE talk_id IN ({_marks(len(talk_ids))}) ORDER BY created_ts, id",
        talk_ids,
    )
    out: dict[int, list[dict]] = {}
    for row in rows:
        out.setdefault(row.pop("talk_id"), []).append(row)
    return out


def _attach_files(conn, talk: list[dict]) -> None:
    files = _files(conn, [t["id"] for t in talk])
    for t in talk:
        t["files"] = files.get(t["id"], [])


def _session_part(conn, sessions: list[int], lo: int, hi: int, truncated: list[str]) -> tuple[list, list]:
    """Реплики владельца и события сессий хаба в окне. `detail_json` событий не берём."""
    if not sessions:
        return [], []
    marks = _marks(len(sessions))
    say = _rows(
        conn,
        "SELECT id, session_id, text, priority, status, created_ts, delivered_ts, source, typed "  # noqa: S608
        f"FROM held_say WHERE session_id IN ({marks}) AND created_ts BETWEEN ? AND ? ORDER BY id LIMIT ?",
        (*sessions, lo, hi, LIMIT + 1),
    )
    events = _rows(
        conn,
        "SELECT id, session_id, ts, kind, tool, summary, ref, is_error "  # noqa: S608
        f"FROM held_event WHERE session_id IN ({marks}) AND ts BETWEEN ? AND ? ORDER BY id LIMIT ?",
        (*sessions, lo, hi, LIMIT + 1),
    )
    return _cut(say, "held_say", truncated), _cut(events, "held_event", truncated)


def by_ref(conn, ref: str) -> dict | None:
    """Всё по карточке или поручению; `None` — по ref нет ничего вообще."""
    truncated: list[str] = []
    refs = _linked_refs(conn, ref)
    items = _cut(_items_by_refs(conn, refs, LIMIT + 1), "items", truncated)
    human_ids = _human_ids(refs)
    human = (
        _rows(
            conn,
            f"SELECT {_HUMAN_COLUMNS} FROM main.human_request "  # noqa: S608
            f"WHERE id IN ({_marks(len(human_ids))}) ORDER BY id",
            human_ids,
        )
        if human_ids
        else []
    )
    base = _rows(
        conn,
        f"SELECT {_TALK_COLUMNS} FROM main.owner_talk "  # noqa: S608
        f"WHERE ref IN ({_marks(len(refs))}) ORDER BY id LIMIT ?",
        (*refs, LIMIT + 1),
    )
    base_ids = [t["id"] for t in base]
    replies = (
        _rows(
            conn,
            f"SELECT {_TALK_COLUMNS} FROM main.owner_talk "  # noqa: S608
            f"WHERE reply_to IN ({_marks(len(base_ids))})",
            base_ids,
        )
        if base_ids
        else []
    )
    seen = {t["id"] for t in base}
    talk = sorted(base + [r for r in replies if r["id"] not in seen], key=lambda t: t["id"])
    talk = _cut(talk, "talk", truncated)
    _attach_files(conn, talk)
    if not (items or human or talk):
        return None
    stamps = [t for i in items for t in (i["created_ts"], i["closed_ts"]) if t]
    stamps += [h["created_ts"] for h in human] + [h["decided_ts"] for h in human if h["decided_ts"]]
    stamps += [t["ts"] for t in talk]
    sessions = sorted({i["session_id"] for i in items if i["session_id"]})
    say, events = (
        _session_part(
            conn, sessions, min(stamps) - _SESSION_BEFORE_MS, max(stamps) + _SESSION_AFTER_MS, truncated
        )
        if stamps
        else ([], [])
    )
    return {
        "ref": ref,
        "refs": refs,
        "human_request": human,
        "items": items,
        "talk": talk,
        "held_say": say,
        "held_event": events,
        "truncated": truncated,
    }


def around(conn, around_ms: int, minutes: int, project_id: int | None = None) -> dict:
    """Всё по каналу владельца за окно ±minutes. С проектом: его карточки и заявки; реплики разговора — те, что по
    ref этих строк, и без ref («Общее»); события сессий не берём."""
    truncated: list[str] = []
    lo, hi = around_ms - minutes * 60_000, around_ms + minutes * 60_000
    proj = " AND project_id = ?" if project_id is not None else ""
    pp = (project_id,) if project_id is not None else ()
    items = [
        _unpack(r)
        for r in _rows(
            conn,
            f"SELECT {_ITEM_COLUMNS} FROM main.owner_item "  # noqa: S608
            f"WHERE ((created_ts BETWEEN ? AND ?) OR (closed_ts BETWEEN ? AND ?)){proj} ORDER BY id LIMIT ?",
            (lo, hi, lo, hi, *pp, LIMIT + 1),
        )
    ]
    human = _rows(
        conn,
        f"SELECT {_HUMAN_COLUMNS} FROM main.human_request "  # noqa: S608
        f"WHERE ((created_ts BETWEEN ? AND ?) OR (decided_ts BETWEEN ? AND ?)){proj} ORDER BY id LIMIT ?",
        (lo, hi, lo, hi, *pp, LIMIT + 1),
    )
    talk = _rows(
        conn,
        f"SELECT {_TALK_COLUMNS} FROM main.owner_talk WHERE ts BETWEEN ? AND ? ORDER BY id LIMIT ?",  # noqa: S608
        (lo, hi, LIMIT + 1),
    )
    if project_id is not None:
        project_refs = {i["ref"] for i in items} | {f"human:{h['id']}" for h in human}
        talk = [t for t in talk if t["ref"] is None or t["ref"] in project_refs]
    items = _cut(items, "items", truncated)
    human = _cut(human, "human_request", truncated)
    talk = _cut(talk, "talk", truncated)
    _attach_files(conn, talk)
    return {
        "around_ms": around_ms,
        "minutes": minutes,
        "project_id": project_id,
        "human_request": human,
        "items": items,
        "talk": talk,
        "held_say": [],
        "held_event": [],
        "truncated": truncated,
    }
