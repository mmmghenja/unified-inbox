"""Ф48-2 на уровне ядра, продолжение (docs/architecture.md и D):
прерванная реплика не задерживает сигналы хаба; «Новая с передачей дел» переносит настройку сжатия и
прерванные реплики. Помощники и «мир» — из test_hub_sessions.py (как у тестов 41-4 и 41-7); хост
поддельный."""

from hub.core import held, held_work

from . import test_hub_sessions as base
from .test_f41_handoff import _handoff, _patch, _say
from .test_f41_incoming import _host, _idle, _peer, _signals
from .test_hub_sessions import CODER, ROLE, B, _hsub, _mk, _sub

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"


async def _cut(client, w, hub_id, say_id) -> None:
    """Поддельный хост: реплика подана, ход по ней оборван — она вернулась в очередь прерванной."""
    await _host(client, w, f"session-says/{say_id}/delivered", {})
    await _host(client, w, f"sessions/{hub_id}/turn", {"state": "running"})
    out = await _host(client, w, f"sessions/{hub_id}/turn", {"state": "idle", "interrupted_say": say_id})
    assert out["interrupted_say"] == say_id


async def test_interrupted_say_does_not_hold_signals(client, ui, db, world):  # noqa: F811
    w = world
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    await _hsub(client, w, hub_id, mode="direct")
    await _sub(client, w, B, cwd=w["dir4"], role=CODER)
    await _idle(client, w, hub_id)
    first = await _say(ui, hub_id, "оборванная")
    await _cut(client, w, hub_id, first)
    await _say(ui, hub_id, "обычная за ней")  # хост её не подаст, пока владелец не решит про прерванную
    await _peer(client, w)
    body = await _signals(client, w, hub_id)
    assert len(body["signals"]) == 1  # очередь стоит на прерванной — сигнал агента идёт
    # Контроль: обычная реплика без прерванной перед ней сигнал задерживает, как раньше.
    await _host(client, w, f"sessions/{hub_id}/turn", {"state": "running", "origin": "signal"})
    await _host(client, w, f"sessions/{hub_id}/turn", {"state": "idle"})
    r = await ui.c.delete(f"{API}/sessions/{hub_id}/says/{first}", headers=ui.headers)
    assert r.status_code == 200, r.text
    await _peer(client, w, text="ещё одно")
    body = await _signals(client, w, hub_id)
    assert body["signals"] == [] and body["chan_signals"] == []


async def test_handoff_moves_compact_settings_and_interrupted_says(client, ui, db, world):  # noqa: F811
    w = world
    old = (await _mk(ui, w))["id"]
    await _patch(ui, old, {"compact_tokens": 120_000, "compact_auto": False})
    await _idle(client, w, old)
    first = await _say(ui, old, "оборванная")
    await _cut(client, w, old, first)
    await _say(ui, old, "обычная")
    with db.reader() as conn:
        was = held_work.queued_says(conn, held.get(conn, old))[0]["interrupted_ts"]

    new = (await _handoff(ui, old))["session"]
    assert (new["compact_tokens"], new["compact_auto"]) == (120_000, False)
    assert (new["queued_says"], new["interrupted_says"]) == (3, 1)
    with db.reader() as conn:
        says = held_work.queued_says(conn, held.get(conn, new["id"]))
        assert held_work.queued_says(conn, held.get(conn, old)) == []
        # Текст передачи дел подаётся, хотя прерванная держит несрочные: он идёт срочным.
        assert held_work.feedable_says(conn, held.get(conn, new["id"])) == 1
    assert [(s["status"], s["priority"]) for s in says] == [
        ("interrupted", "next"),
        ("queued", "now"),
        ("queued", "next"),
    ]
    assert (says[0]["text"], says[0]["interrupted_ts"], says[2]["text"]) == ("оборванная", was, "обычная")
    # Без прерванных текст передачи дел — обычной срочности, как раньше.
    again = (await _handoff(ui, (await _mk(ui, w))["id"]))["session"]
    with db.reader() as conn:
        (say,) = held_work.queued_says(conn, held.get(conn, again["id"]))
    assert (say["priority"], say["status"]) == ("next", "queued")
