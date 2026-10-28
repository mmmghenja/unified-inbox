"""Ф73-1 (docs/architecture.md): ядро — «Перечитать MCP» у сессии хаба.

Ручка `POST /sessions/{id}/mcp-reload` — только кука интерфейса; закрытая — 409. Снятие — хост `turn` с
`clear: ["mcp_reload"]` и исполненным значением `mcp_reload_upto`: более новое нажатие остаётся.
Хост поддельный: bearer `work_host` зовёт ручки ядра напрямую."""

from . import test_hub_sessions as base
from .test_f41_incoming import API, PROC, _host, _session
from .test_hub_sessions import ROLE, _h, _mk, _now

procs = base.procs
ui = base.ui
world = base.world


async def _turn(client, w, hub_id, body, *, expect=200):
    return await _host(client, w, f"sessions/{hub_id}/turn", body, expect=expect)


def _done(state: str, upto: int) -> dict:
    return {"state": state, "clear": ["mcp_reload"], "mcp_reload_upto": upto}


def _host_row(rows: dict, hub_id: int) -> dict:
    return {row["id"]: row for row in rows["items"]}[hub_id]


async def test_mcp_reload_set_ui_only_and_closed_409(client, ui, db, clock, world):
    w = world
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    path = f"{API}/sessions/{hub_id}/mcp-reload"
    # Bearer любого скоупа, включая хост, — 403.
    assert (await client.post(path, json={}, headers=_h(w["host"]))).status_code == 403
    assert (await _session(ui, hub_id))["mcp_reload_ts"] is None
    # Спящая — можно (хост поднимет без хода).
    r = await ui.post(path)
    assert r.status_code == 200
    assert r.json()["session"]["id"] == hub_id and r.json()["session"]["mcp_reload_ts"] == _now(clock)
    host_row = _host_row(await _host(client, w, "sessions"), hub_id)
    assert host_row["mcp_reload_ts"] == _now(clock)
    audits = base._audits(db, "hub_session_mcp_reload")
    assert audits == [{"hub_session_id": hub_id, "state": "asleep"}]
    # Повторное нажатие — новое значение, строго новее (и в ту же миллисекунду).
    first = host_row["mcp_reload_ts"]
    second = (await ui.post(path)).json()["session"]["mcp_reload_ts"]
    assert second == first + 1
    clock.advance_ms(5000)
    third = (await ui.post(path)).json()["session"]["mcp_reload_ts"]
    assert third == _now(clock)
    assert (await ui.post(f"{API}/sessions/{hub_id}/close")).status_code == 200
    r = await ui.post(path)
    assert r.status_code == 409


async def test_mcp_reload_cleared_only_up_to_executed_value(client, ui, db, clock, world):
    w = world
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    path = f"{API}/sessions/{hub_id}/mcp-reload"
    await _host(client, w, f"sessions/{hub_id}/state", {"state": "idle", **PROC})
    asked = (await ui.post(path)).json()["session"]["mcp_reload_ts"]
    # Без исполненного значения — 422, запись не тронута.
    await _turn(client, w, hub_id, {"state": "idle", "clear": ["mcp_reload"]}, expect=422)
    assert _host_row(await _host(client, w, "sessions"), hub_id)["mcp_reload_ts"] == asked
    # Владелец нажал ещё раз, пока хост перечитывал под первое значение, — новое нажатие остаётся.
    clock.advance_ms(1000)
    again = (await ui.post(path)).json()["session"]["mcp_reload_ts"]
    assert again > asked
    row = await _turn(client, w, hub_id, _done("idle", asked))
    assert row["session"]["mcp_reload_ts"] == again
    # Исполнено новое — снято; сон и начало хода просьбу сами не снимают.
    row = await _turn(client, w, hub_id, _done("idle", again))
    assert row["session"]["mcp_reload_ts"] is None
    assert (await _session(ui, hub_id))["mcp_reload_ts"] is None
    asked = (await ui.post(path)).json()["session"]["mcp_reload_ts"]
    await _turn(client, w, hub_id, {"state": "running"})
    row = await _turn(client, w, hub_id, {"state": "asleep"})
    assert row["session"]["mcp_reload_ts"] == asked
    # Снятие уже снятого — без ошибки.
    await _turn(client, w, hub_id, _done("asleep", asked))
    row = await _turn(client, w, hub_id, _done("asleep", asked))
    assert row["session"]["mcp_reload_ts"] is None
