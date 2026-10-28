"""Ф71-1 (docs/architecture.md): ядро принимает движок `cursor` у сессии хаба —
создание, «Новая с передачей дел», заявка на напарника; неизвестный движок — 422. Помощники и «мир» — из
test_hub_sessions.py."""

from hub.core import agent_requests as ar_mod
from hub.core import handoff, held, held_work

from . import test_hub_sessions as base
from .test_hub_sessions import _mk

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"


async def test_create_cursor_session(ui, db, world):
    w = world
    row = await _mk(ui, w, engine="cursor", model="grok-4.7", effort="high", role="тестер")
    assert (row["engine"], row["model"], row["effort"]) == ("cursor", "grok-4.7", "high")
    with db.reader() as conn:
        assert held.get(conn, row["id"]).engine == "cursor"
    await _mk(ui, w, engine="cursor-agent", expect=422)


async def test_handoff_to_cursor_and_back(ui, db, world):
    w = world
    old = (await _mk(ui, w, model="opus", role="тестер"))["id"]
    r = await ui.post(f"{API}/sessions/{old}/handoff", {"engine": "cursor", "model": "grok-4.7"})
    assert r.status_code == 200, r.text
    new = r.json()["session"]
    assert (new["engine"], new["model"]) == ("cursor", "grok-4.7")
    with db.reader() as conn:
        says = [s["text"] for s in held_work.queued_says(conn, held.get(conn, new["id"]))]
    assert "движок Claude Code" in says[0]
    r = await ui.post(f"{API}/sessions/{new['id']}/handoff", {"engine": "claude"})
    assert r.status_code == 200, r.text
    with db.reader() as conn:
        says = [s["text"] for s in held_work.queued_says(conn, held.get(conn, r.json()["session"]["id"]))]
    assert "движок Cursor" in says[0]
    assert handoff.ENGINE_NAMES["cursor"] == "Cursor"


def test_agent_request_engine_cursor_is_accepted():
    """Проверка поля `engine` заявки — общая с созданием сессии (`held.ENGINES`)."""
    assert "cursor" in held.ENGINES
    assert ar_mod.held_mod.ENGINES is held.ENGINES
