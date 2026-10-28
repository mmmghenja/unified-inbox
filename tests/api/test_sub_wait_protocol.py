"""Ф14-6b п.7 (живой дефект 28.09): ожидание подписки на проект в режиме `messages` без метки протокола
клиента (`X-Hub-Sub-Wait`, старый `hub sub wait`) — 409 `client_outdated` ДО выдачи, сигнал остаётся
`pending`; режим `cards` без метки — как прежде; ожидание бесед метку не требует (ревью Ф14-6)."""

# ruff: noqa: F811 — фикстуры `procs`, `world` взяты из соседнего модуля и принимаются аргументами тестов

from .test_transcript_hold import (  # noqa: F401
    _chan_signals,
    _h,
    _ingest,
    _project_sub,
    _project_wait,
    _session,
    procs,
    world,
)


async def test_old_client_messages_mode_409_signal_stays_pending(client, db, world, procs, clock):
    sub_id = await _project_sub(client, world)
    _ingest(db, world, clock, "1", voice=False, text="привет")
    r = await _project_wait(client, world, sub_id, timeout_s=1, headers={})
    assert r.status_code == 409 and r.json()["detail"] == "client_outdated"
    r = await _project_wait(client, world, sub_id, timeout_s=1, headers={"X-Hub-Sub-Wait": "1"})
    assert r.status_code == 409
    assert _chan_signals(db) == [("pending",)]
    r = await _project_wait(client, world, sub_id, timeout_s=1)
    assert r.status_code == 200 and len(r.json()["signals"]) == 1


async def test_old_client_cards_mode_still_works(client, db, world, procs, clock):
    r = await client.post("/api/v1/subs", json={"session": _session(world)}, headers=_h(world["tok"]))
    sub_id = r.json()["sub"]["id"]
    r = await _project_wait(client, world, sub_id, timeout_s=1, headers={})
    assert r.status_code == 200 and r.json()["signals"] == []


async def test_old_client_chan_wait_not_refused(client, db, world, procs, clock):
    """Ревью Ф14-6: ожидание бесед метку не требует — все его клиенты (Ф14-5+) сигналы бесед понимают."""
    body = {"session": _session(world, full=False), "waiter": "0123456789abcdef", "timeout_s": 1}
    r = await client.post("/api/v1/chan-subs/wait", json=body, headers=_h(world["tok"]))
    assert r.status_code != 409
