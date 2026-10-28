"""Ф23-3: в выдаче MCP (`hub_read`/`hub_thread`/`hub_message` — все через `format.message_lines`) пост
канала со счётчиком — строка рядом с реакциями «просмотры: 1 234, пересылки: 5 (на 15:59)»; время —
локальное; нет `views` — строки нет."""

import json
from datetime import datetime

from hub.mcp_server import format as fmt

SNAP = datetime(2026, 10, 1, 15, 59).timestamp()


def _msg(**over) -> dict:
    base = {"id": 7, "ext_id": "9980", "ts": 1_790_000_000_000, "is_me": False, "text": "Пост"}
    base["sender"] = None
    base.update(over)
    return base


def test_views_line_after_reactions(monkeypatch):
    # «сегодня» — день снятия (без даты в скобках); иначе тест зависел от дня запуска
    monkeypatch.setattr(fmt.time, "time", lambda: SNAP + 600)
    lines = fmt.message_lines(
        _msg(
            reactions_json=json.dumps([{"emoji": "👍", "count": 3}]),
            views={"views": 1234, "forwards": 5, "ts": int(SNAP * 1000)},
        )
    )
    assert lines[-2] == "    реакции: 👍×3"
    assert lines[-1] == "    просмотры: 1 234, пересылки: 5 (на 15:59)"


def test_views_text_variants():
    now = SNAP + 600
    assert fmt.views_text({"views": 7, "forwards": None, "ts": None}, now) == "просмотры: 7"
    assert fmt.views_text({"views": 1_234_567, "forwards": 0, "ts": None}, now) == (
        "просмотры: 1 234 567, пересылки: 0"
    )
    # снято не сегодня — с датой
    old = int((SNAP - 2 * 86400) * 1000)
    assert fmt.views_text({"views": 7, "forwards": None, "ts": old}, now) == "просмотры: 7 (на 29.09 15:59)"


def test_no_views_no_line():
    for value in (None, {}, {"views": "5"}, {"views": True}, "x"):
        lines = fmt.message_lines(_msg(views=value))
        assert not any("просмотры" in line for line in lines), value
