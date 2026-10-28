"""Ф92-1 (A): итог агента владельца виден — пометка `brief.by = agent` у события `say` и `agent_reply` в
`GET /owner_channel/closed`. Мир и поддельный хост — как в test_owner_turn.py."""

from . import test_hub_sessions as base
from .test_f41_autopilot_5b import _auto
from .test_f56_queue import _agent, _answer, _channel_on
from .test_owner_turn import API, ASK, _say, _turn_rows, _Turns

procs = base.procs
ui = base.ui
world = base.world


async def _events(ui, sid) -> list[dict]:
    r = await ui.get(f"{API}/sessions/{sid}/events")
    assert r.status_code == 200, r.text
    body = r.json()
    return body["events"] if isinstance(body, dict) else body


async def test_agent_say_is_marked_in_feed_and_ui_say_is_not(client, ui, db, world):
    w = world
    agent, _ = _agent(db)
    sid = await _auto(client, ui, w, mode="manual")
    turns = _Turns(client, w, sid)
    await turns.start()
    await turns.end(ASK)
    await _channel_on(ui, w, sid)
    ref = f"turn:{sid}:1"
    await _answer(client, ref, {"action": "reply", "text": "дал доступ"}, token=agent)
    agent_say = _turn_rows(db)[0][3]["say_id"]
    ui_say = await _say(ui, sid, "от владельца")
    await turns.deliver(agent_say)
    await turns.deliver(ui_say)
    says = [e for e in await _events(ui, sid) if e["kind"] == "say"]
    by_text = {e["summary"]: e for e in says}
    a = by_text["владелец (next): дал доступ"]  # префикс не меняется: его разбирают передача дел и UI
    assert (a["brief"] or {}).get("by") == "agent"
    assert not (by_text["владелец (next): от владельца"]["brief"] or {}).get("by")


async def test_closed_carries_agent_reply_text_masked(client, ui, db, world):
    w = world
    agent, _ = _agent(db)
    sid = await _auto(client, ui, w, mode="manual")
    turns = _Turns(client, w, sid)
    await _channel_on(ui, w, sid)
    await turns.start()
    await turns.end(ASK)
    body = {"action": "reply", "text": "бери API_KEY=sk-live-0123456789"}
    await _answer(client, f"turn:{sid}:1", body, token=agent)
    await turns.start()
    await turns.end(ASK)
    await _answer(client, f"turn:{sid}:2", {"action": "ok"}, token=agent)
    await turns.start()
    await turns.end(ASK)
    await _answer(client, f"turn:{sid}:3", {"action": "ok"}, ui=ui)  # владелец сам — без `agent_reply`
    items = {i["ref"]: i for i in (await ui.get(f"{API}/owner_channel/closed")).json()["items"]}
    r1 = items[f"turn:{sid}:1"]["agent_reply"]
    assert r1["action"] == "reply" and "бери" in r1["text"]
    assert "sk-live" not in r1["text"]
    assert items[f"turn:{sid}:2"]["agent_reply"] == {"action": "ok", "text": None}
    assert items[f"turn:{sid}:3"]["agent_reply"] is None
