"""Ф50-1 (docs/architecture.md): сигналы посреди хода на
уровне ядра — счёт срочных в разделе `sessions.signals`, выдача хосту при `running` (`mid`) без отметки хода
по сигналу, признак `mid_lost` в ручке хода.

Хост поддельный; помощники — из test_f41_incoming.py и test_hub_sessions.py."""

from hub.core import held

from . import test_hub_sessions as base
from .test_f41_incoming import API, _host, _idle, _peer, _signals, _wait_host
from .test_hub_sessions import CODER, MIN, ROLE, B, _h, _hsub, _mk, _now, _peer_to_sub, _sess, _sub

procs = base.procs
ui = base.ui
world = base.world


async def _hub(client, ui, w) -> int:
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    await _hsub(client, w, hub_id, mode="direct")
    await _sub(client, w, B, cwd=w["dir4"], role=CODER)  # отправитель
    return hub_id


async def _urgent_peer(client, w, text="блокер") -> int:
    r = await client.post(
        f"{API}/peer-msgs",
        json={"text": text, "to": {"project_id": w["p3"], "role": ROLE}, "session": _sess(B), "urgent": True},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 201, r.text
    return r.json()["msg"]["id"]


def _rec(db, hub_id):
    with db.reader() as conn:
        return held.get(conn, hub_id)


async def _running(client, w, hub_id):
    await _idle(client, w, hub_id)
    await _host(client, w, f"sessions/{hub_id}/turn", {"state": "running"})


async def _mid(client, w, hub_id):
    return await _host(client, w, f"sessions/{hub_id}/signals", {"mid": True})


# --- счёт срочных -------------------------------------------------------------------------------------------


async def test_urgent_counts_agent_urgent_and_message_of_owner(client, ui, db, world):
    w = world
    hub_id = await _hub(client, ui, w)
    await _peer(client, w)  # обычное сообщение агента — не срочное
    assert (await _wait_host(client, w, known={}))["sessions"]["signals"] == [
        {"id": hub_id, "ready": 1, "urgent": 0, "next_ms": None}
    ]
    await _urgent_peer(client, w)
    assert (await _wait_host(client, w, known={}))["sessions"]["signals"] == [
        {"id": hub_id, "ready": 2, "urgent": 1, "next_ms": None}
    ]
    sub_id = (await ui.get(f"{API}/sessions/{hub_id}")).json()["session"]["sub_id"]
    await _peer_to_sub(ui, w, sub_id)  # сообщение владельца агенту
    assert (await _wait_host(client, w, known={}))["sessions"]["signals"] == [
        {"id": hub_id, "ready": 3, "urgent": 2, "next_ms": None}
    ]
    # Выдача забирает всё — срочного больше нет.
    await _idle(client, w, hub_id)
    assert len((await _signals(client, w, hub_id))["signals"]) == 3
    assert (await _wait_host(client, w, known={}))["sessions"]["signals"] == []


# --- выдача посреди хода ------------------------------------------------------------------------------------


async def test_mid_delivers_only_when_running_and_leaves_no_signal_turn(client, ui, db, clock, world):
    w = world
    hub_id = await _hub(client, ui, w)
    first = await _urgent_peer(client, w)
    # Свободной сессии выдача с `mid` ничего не отдаёт и не отмечает: обычная выдача после неё — полная.
    await _idle(client, w, hub_id)
    assert (await _mid(client, w, hub_id))["signals"] == []
    assert [s["msg_id"] for s in (await _signals(client, w, hub_id))["signals"]] == [first]
    assert _rec(db, hub_id).signal_turn_ts == _now(clock)
    await _host(client, w, f"sessions/{hub_id}/turn", {"state": "running", "origin": "signal"})
    await _host(client, w, f"sessions/{hub_id}/turn", {"state": "idle", "last_turn_ts": _now(clock)})

    # Ход идёт: обычная выдача пуста (как раньше), с `mid` — всё готовое, обычное и срочное разом (П6).
    clock.advance_ms(MIN)
    await _running(client, w, hub_id)
    plain = await _peer(client, w, text="не к спеху")
    hot = await _urgent_peer(client, w, text="второй блокер")
    assert (await _signals(client, w, hub_id))["signals"] == []
    got = await _mid(client, w, hub_id)
    assert sorted(s["msg_id"] for s in got["signals"]) == [plain, hot]
    rec = _rec(db, hub_id)
    # Идущий ход не стал ходом по сигналу, состояние не тронуто.
    assert (rec.state, rec.signal_turn_ts, rec.signal_lost_ts) == ("running", None, None)
    assert (await _mid(client, w, hub_id))["signals"] == []  # повтора нет
    # Обычный конец хода: признака потери нет — строку сессия прочла в ходе.
    await _host(client, w, f"sessions/{hub_id}/turn", {"state": "idle", "last_turn_ts": _now(clock)})
    assert _rec(db, hub_id).signal_lost_ts is None


async def test_mid_waits_for_say_of_owner_and_sleep_request(client, ui, db, clock, world):
    w = world
    hub_id = await _hub(client, ui, w)
    await _running(client, w, hub_id)
    msg = await _urgent_peer(client, w)
    # Реплика владельца в очереди важнее сигнала — и посреди хода тоже.
    r = await ui.post(f"{API}/sessions/{hub_id}/say", {"text": "сначала это"})
    assert r.status_code == 200, r.text
    assert (await _mid(client, w, hub_id))["signals"] == []
    await _host(client, w, f"session-says/{r.json()['id']}/delivered", {})
    # Просьба «усыпить» — сигнал усыпляемой не уходит.
    assert (await ui.post(f"{API}/sessions/{hub_id}/sleep")).status_code == 200
    assert (await _mid(client, w, hub_id))["signals"] == []
    await _host(client, w, f"sessions/{hub_id}/turn", {"state": "asleep", "clear": ["sleep"]})
    await _running(client, w, hub_id)
    assert [s["msg_id"] for s in (await _mid(client, w, hub_id))["signals"]] == [msg]


# --- `mid_lost` ---------------------------------------------------------------------------------------------


async def test_mid_lost_sets_signal_lost_at_any_state(client, ui, db, clock, world):
    w = world
    hub_id = await _hub(client, ui, w)

    async def _host_row():
        return {row["id"]: row for row in (await _host(client, w, "sessions"))["items"]}[hub_id]

    # Ход оборван, строка не начата: конец хода в `idle` с признаком — «проверь входящее».
    await _running(client, w, hub_id)
    await _urgent_peer(client, w)
    assert len((await _mid(client, w, hub_id))["signals"]) == 1
    await _host(client, w, f"sessions/{hub_id}/turn", {"state": "idle", "last_turn_ts": _now(clock)})
    assert _rec(db, hub_id).signal_lost_ts is None  # без признака ядро о потере не знает (П3)
    await _host(
        client, w, f"sessions/{hub_id}/turn", {"state": "idle", "last_turn_ts": _now(clock), "mid_lost": True}
    )
    assert _rec(db, hub_id).signal_lost_ts == _now(clock) and (await _host_row())["signal_lost"] is True
    # Хост подаёт постоянную строку и снимает признак началом хода.
    fed = {"state": "running", "origin": "signal", "clear": ["signal_lost"]}
    await _host(client, w, f"sessions/{hub_id}/turn", fed)
    assert _rec(db, hub_id).signal_lost_ts is None
    await _host(client, w, f"sessions/{hub_id}/turn", {"state": "idle", "last_turn_ts": _now(clock)})

    # Усыпил сам владелец посреди хода — потеря строки, поданной посреди хода, всё равно названа.
    clock.advance_ms(MIN)
    await _running(client, w, hub_id)
    await _urgent_peer(client, w, text="ещё")
    assert len((await _mid(client, w, hub_id))["signals"]) == 1
    assert (await ui.post(f"{API}/sessions/{hub_id}/sleep")).status_code == 200
    slept = {"state": "asleep", "clear": ["sleep"], "mid_lost": True}
    await _host(client, w, f"sessions/{hub_id}/turn", slept)
    rec = _rec(db, hub_id)
    assert (rec.state, rec.signal_lost_ts) == ("asleep", _now(clock))

    # Признак сильнее снятия тем же вызовом; без `mid_lost` снятие работает как раньше.
    clock.advance_ms(MIN)
    await _host(
        client, w, f"sessions/{hub_id}/turn", {"state": "asleep", "clear": ["signal_lost"], "mid_lost": True}
    )
    assert _rec(db, hub_id).signal_lost_ts == _now(clock)
    await _host(client, w, f"sessions/{hub_id}/turn", {"state": "asleep", "clear": ["signal_lost"]})
    assert _rec(db, hub_id).signal_lost_ts is None
