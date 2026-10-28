"""Ф86-1 (docs/architecture.md.1, П3, П4): признак `held_say.typed` — реплику набрал сам
владелец. Окно сессии — 1; ответ по карточке кукой — 1, агентом владельца — 0; передача дел переносит признак исходной
реплики, а сам текст передачи — 0.

Мир — test_hub_sessions.py, карточка `turn` — помощники test_owner_turn.py; хост поддельный."""

from hub.core import held, held_work

from . import test_hub_sessions as base
from .test_f41_autopilot_5b import _auto
from .test_f41_handoff import _handoff
from .test_f56_queue import _agent, _answer, _channel_on
from .test_owner_turn import ASK, _say, _turn_rows, _Turns

procs = base.procs
ui = base.ui
world = base.world


def _typed(db, say_id) -> int:
    with db.reader() as conn:
        return conn.execute("SELECT typed FROM main.held_say WHERE id = ?", (say_id,)).fetchone()[0]


async def test_window_say_and_turn_answers(client, ui, db, world):
    w = world
    agent, _ = _agent(db)
    sid = await _auto(client, ui, w, mode="manual")
    assert _typed(db, await _say(ui, sid, "из окна")) == 1
    turns = _Turns(client, w, sid)
    await turns.start()
    await turns.end(ASK)
    await _answer(client, f"turn:{sid}:1", {"action": "reply", "text": "кукой"}, ui=ui)
    await turns.start()
    await turns.end(ASK)
    await _channel_on(ui, w, sid)
    await _answer(client, f"turn:{sid}:2", {"action": "reply", "text": "агентом"}, token=agent)
    rows = _turn_rows(db)
    assert [_typed(db, row[3]["say_id"]) for row in rows] == [1, 0]


async def test_handoff_keeps_typed_of_moved_says(client, ui, db, clock, world):
    w = world
    old = (await base._mk(ui, w))["id"]  # noqa: SLF001
    await _say(ui, old, "набрал владелец")

    def _agent_say(conn):
        return held_work.say(
            conn,
            old,
            text="ответ агента",
            priority="next",
            now_ms=base._now(clock),  # noqa: SLF001
            audit={"actor": "bearer", "api_token_id": None, "client_name": None, "request_id": "t"},
            source="agent",
        )

    db.write_sync(_agent_say)
    new = (await _handoff(ui, old))["session"]["id"]
    with db.reader() as conn:
        says = held_work.queued_says(conn, held.get(conn, new))
    by_text = {s["text"]: s["typed"] for s in says if s["text"] in ("набрал владелец", "ответ агента")}
    assert by_text == {"набрал владелец": True, "ответ агента": False}
    (handoff_text,) = [s for s in says if s["text"] not in by_text]
    assert handoff_text["typed"] is False
