"""Читаемая лента для `hub trace` (Ф97-1): ответ ядра -> строки `ЧЧ:ММ:СС  источник  кто  текст` по времени.

Время — местное, машины, где запущена команда. Тексты целиком, переводы строк — с отступом."""

import time

_INDENT = " " * 4


def _clock(ts_ms: int) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts_ms / 1000))


def _day(ts_ms: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts_ms / 1000))


def _indent(text: str) -> str:
    return text.replace("\n", "\n" + _INDENT)


def _payload_lines(payload: dict) -> str:
    """Ключи поручения построчно; списки — по элементу на строку."""
    lines: list[str] = []
    for key, value in payload.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"{_INDENT}- {v}" for v in value)
        elif isinstance(value, dict):
            lines.append(f"{key}:")
            lines.extend(f"{_INDENT}{k}: {v}" for k, v in value.items())
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


def _entries(body: dict) -> list[tuple[int, int, str, str, str]]:
    out: list[tuple[int, int, str, str, str]] = []  # (ts, порядок, источник, кто, текст)
    n = 0

    def add(ts, source, who, text) -> None:
        nonlocal n
        if ts:
            n += 1
            out.append((ts, n, source, who, text))

    for h in body.get("human_request", []):
        who = f"human:{h['id']} {h['kind']}"
        parts = [h.get("need") or ""]
        for key in ("site", "url", "doing"):
            if h.get(key):
                parts.append(f"{key}: {h[key]}")
        add(h["created_ts"], "заявка", who, "\n".join(parts))
        if h.get("decided_ts"):
            note = f"\n{h['decided_note']}" if h.get("decided_note") else ""
            add(h["decided_ts"], "заявка", who, f"решено: {h['status']} ({h.get('decided_by') or '?'}){note}")
    for i in body.get("items", []):
        is_errand = i["kind"] == "errand"
        source = "поручение" if is_errand else "карточка"
        who = f"{i['ref']} {i['kind']}" + (f" проект {i['project_id']}" if i["project_id"] else "")
        add(i["created_ts"], source, who, _payload_lines(i["payload"]))
        if i.get("closed_ts"):
            add(i["closed_ts"], source, i["ref"], f"закрыто ({i.get('closed_by') or '?'})")
    for t in body.get("talk", []):
        ref = f" [{t['ref']}]" if t.get("ref") else ""
        files = "".join(f"\n[файл: {f['name']}, {f['mime']}, {f['size']} Б]" for f in t.get("files", []))
        if t["author"] == "agent":
            source = "вопрос агента" if t.get("ask") else "агент→владелец"
            who = f"агент{ref}"
        else:
            source = "владелец→агент"
            who = ("владелец, из бота" if t.get("via") == "bot" else "владелец") + ref
        add(t["ts"], source, who, (t["text"] or "") + files)
    for s in body.get("held_say", []):
        add(s["created_ts"], f"сессия #{s['session_id']}: реплика владельца", s["status"], s["text"])
    for e in body.get("held_event", []):
        who = (
            e["kind"] + (f" {e['tool']}" if e.get("tool") else "") + (" ОШИБКА" if e.get("is_error") else "")
        )
        add(e["ts"], f"сессия #{e['session_id']}: событие", who, e["summary"])
    out.sort(key=lambda r: (r[0], r[1]))
    return out


def render(body: dict) -> str:
    lines: list[str] = []
    if body.get("refs"):
        lines.append("связанные ref: " + ", ".join(body["refs"]))
    day = None
    for ts, _, source, who, text in _entries(body):
        if _day(ts) != day:
            day = _day(ts)
            lines.append(f"--- {day}")
        lines.append(f"{_clock(ts)}  {source}  {who}  {_indent(text)}".rstrip())
    if body.get("truncated"):
        lines.append("ОБРЕЗАНО (предел 500 строк): " + ", ".join(body["truncated"]))
    return "\n".join(lines)
