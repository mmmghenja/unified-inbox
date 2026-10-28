"""Ф41-4б (docs/architecture.md): ядро — просьба
«Сжать», расход после сжатия (`context_tokens: null`), база сессии `base_tokens`, «Поднять» на паузе.

Хост поддельный: bearer `work_host` зовёт ручки ядра напрямую."""

from . import test_hub_sessions as base
from .test_f41_incoming import API, PROC, _host, _session
from .test_hub_sessions import ROLE, _h, _mk, _now

procs = base.procs
ui = base.ui
world = base.world


async def _turn(client, w, hub_id, body, *, expect=200):
    return await _host(client, w, f"sessions/{hub_id}/turn", body, expect=expect)


async def test_compact_request_set_cleared_and_refused(client, ui, db, clock, world):
    w = world
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    path = f"{API}/sessions/{hub_id}/compact"
    # Спящая: процесса нет — сжимать нечем.
    r = await ui.post(path)
    assert r.status_code == 409 and "спит" in r.json()["detail"]
    assert (await client.post(path, json={}, headers=_h(w["host"]))).status_code == 403  # только владелец
    await _host(client, w, f"sessions/{hub_id}/state", {"state": "idle", **PROC})
    r = await ui.post(path)
    assert r.status_code == 200 and r.json()["session"]["id"] == hub_id
    rows = {row["id"]: row for row in (await _host(client, w, "sessions"))["items"]}
    asked = rows[hub_id]["compact_requested_ts"]
    assert asked == _now(clock)
    # Повтор просьбу не двигает; хост снимает её началом сжатия.
    clock.advance_ms(5000)
    assert (await ui.post(path)).status_code == 200
    rows = {row["id"]: row for row in (await _host(client, w, "sessions"))["items"]}
    assert rows[hub_id]["compact_requested_ts"] == asked
    row = await _turn(client, w, hub_id, {"state": "running", "clear": ["compact"]})
    assert row["session"]["compact_requested_ts"] is None
    # Переход в сон просьбу снимает сам (2.5).
    await _turn(client, w, hub_id, {"state": "idle"})
    assert (await ui.post(path)).status_code == 200
    row = await _turn(client, w, hub_id, {"state": "asleep"})
    assert row["session"]["compact_requested_ts"] is None
    assert (await ui.post(f"{API}/sessions/{hub_id}/close")).status_code == 200
    assert (await ui.post(path)).status_code == 409


async def test_usage_null_context_after_compaction_and_base_tokens(client, ui, db, clock, world):
    w = world
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    await _host(client, w, f"sessions/{hub_id}/state", {"state": "idle", **PROC})
    # Первый ход разговора: хост присылает базу (П17).
    usage = {"context_tokens": 160_000, "context_limit": 1_000_000, "turn_cost_usd": 0.5}
    usage["base_tokens"] = 160_000
    row = (await _turn(client, w, hub_id, {"state": "idle", "last_turn_ts": 1, "usage": usage}))["session"]
    assert row["usage"]["base_tokens"] == 160_000 and row["base_tokens"] == 160_000
    assert row["context_tokens"] == 160_000
    # Следующий ход без `base_tokens` — база прежняя.
    usage = {"context_tokens": 175_000, "context_limit": 1_000_000, "turn_cost_usd": 0.25}
    row = (await _turn(client, w, hub_id, {"state": "idle", "last_turn_ts": 2, "usage": usage}))["session"]
    assert row["usage"]["base_tokens"] == 160_000 and row["usage"]["context_tokens"] == 175_000
    # Сжатие: размер неизвестен (`null`), предел и база остаются, цена сжатия — в сумму.
    usage = {"context_tokens": None, "context_limit": None, "turn_cost_usd": 0.25}
    row = (await _turn(client, w, hub_id, {"state": "idle", "usage": usage}))["session"]
    assert row["usage"]["context_tokens"] is None and row["context_tokens"] is None
    assert row["usage"]["context_limit"] == 1_000_000 and row["usage"]["cost_usd"] == 1.0
    assert row["base_tokens"] == 160_000
    public = (await _session(ui, hub_id))["usage"]
    assert public["context_tokens"] is None and public["base_tokens"] == 160_000
    # Первый ход за сжатием — новая база.
    usage = {"context_tokens": 90_000, "context_limit": 1_000_000, "base_tokens": 90_000}
    row = (await _turn(client, w, hub_id, {"state": "idle", "last_turn_ts": 3, "usage": usage}))["session"]
    assert row["base_tokens"] == 90_000 and row["usage"]["context_tokens"] == 90_000
    # Негодное — 422; ключ `context_tokens` обязателен (и `null` — значение).
    bads = ({"context_limit": 5}, {"context_tokens": 1, "base_tokens": -1}, {"context_tokens": "много"})
    for bad in bads:
        await _turn(client, w, hub_id, {"state": "idle", "usage": bad}, expect=422)


async def test_usage_without_base_keeps_old_shape(client, ui, db, world):
    """База не измерена — ключа `base_tokens` в расходе нет (прежняя форма 41-3)."""
    w = world
    hub_id = (await _mk(ui, w))["id"]
    await _host(client, w, f"sessions/{hub_id}/state", {"state": "idle", **PROC})
    usage = {"context_tokens": 10, "context_limit": 100}
    row = (await _turn(client, w, hub_id, {"state": "idle", "usage": usage}))["session"]
    assert set(row["usage"]) == {"context_tokens", "context_limit", "cost_usd", "updated_ts"}
    assert row["base_tokens"] is None


async def test_wake_accepted_for_paused_session_with_queue_only(client, ui, db, world):
    """П19: «Поднять» принимается и у сессии с причиной `paused`; нет ни реплик, ни сигналов — 409."""
    w = world
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    await _turn(client, w, hub_id, {"state": "asleep", "wait_reason": "paused"})
    path = f"{API}/sessions/{hub_id}/wake"
    assert (await ui.post(path)).status_code == 200  # 10.10: спящую «Поднять» поднимает всегда
    assert (await ui.post(f"{API}/sessions/{hub_id}/say", {"text": "привет"})).status_code == 200
    r = await ui.post(path)
    assert r.status_code == 200 and r.json()["session"]["wait_reason"] == "paused"
    rows = {row["id"]: row for row in (await _host(client, w, "sessions"))["items"]}
    assert rows[hub_id]["wake_requested_ts"] is not None
