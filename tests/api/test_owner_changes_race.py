"""Ревью Ф56, круг 2 (3): отпечаток «что клиент уже видел» снимается ПОСЛЕ проверки `has_changes` отдельным
чтением. Запись писателя (свой поток) между ними не должна попасть в отпечаток: снимок на другой отметке
`seq` отдаётся сразу, а не по сроку `wait`. Окно — миллисекунды; здесь оно раскрыто подменой `has_changes`
(запись сразу после её «нет»)."""
import time

from api import test_hub_sessions as base
from api.test_f41_autopilot_5b import _auto
from api.test_f56_queue import _queue
from api.test_owner_turn import ASK, _Turns
from hub.core import held_work
from hub.core import owner_queue as zq

procs = base.procs
ui = base.ui
world = base.world


async def test_card_written_between_check_and_fingerprint_is_returned_at_once(
    client, ui, db, clock, world, monkeypatch
):
    w = world
    sid = await _auto(client, ui, w, mode="manual")
    turns = _Turns(client, w, sid)
    await turns.start()
    before = await _queue(client, ui=ui)
    assert before["items"] == []
    real = zq.has_changes
    fired = []

    def racing(conn, since):
        ready = real(conn, since)
        if not ready and not fired:
            fired.append(1)
            body = {"state": "idle", "last_turn_ts": 5_000, "summary": "ход окончен", "answer": ASK}
            db.write_sync(lambda c: held_work.turn(c, sid, body, now_ms=int(clock() * 1000)))
        return ready

    monkeypatch.setattr(zq, "has_changes", racing)
    began = time.monotonic()
    r = await ui.get(f"/api/v1/owner_channel/changes?since={before['seq']}&wait=3")
    took = time.monotonic() - began
    assert r.status_code == 200 and fired
    assert [c["kind"] for c in r.json()["items"]] == ["turn"]
    assert took < 1.5, f"новая карточка отдана через {took:.2f} с — только по сроку wait"
