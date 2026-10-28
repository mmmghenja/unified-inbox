"""Ф41-4, правки по ревью (ядро): отметка выданного сигнала `signal_turn_ts` — ход, начатый не по сигналу;
конец хода, отклонённого движком (`keep_signal`); сон по просьбе владельца при выданном, но не поданном сигнале."""

from hub.core import held

from . import test_hub_sessions as base
from .test_f41_incoming import API, PROC, _host, _idle, _peer, _signals
from .test_hub_sessions import CODER, MIN, ROLE, B, _hsub, _mk, _now, _sub

procs = base.procs
ui = base.ui
world = base.world


async def test_signal_mark_survives_foreign_turn_rejected_turn_and_owner_sleep(client, ui, db, clock, world):  # noqa: F811
    w = world
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    await _hsub(client, w, hub_id, mode="direct")
    await _sub(client, w, B, cwd=w["dir4"], role=CODER)

    def _rec():
        with db.reader() as conn:
            return held.get(conn, hub_id)

    async def _turn(**body):
        return await _host(client, w, f"sessions/{hub_id}/turn", body)

    async def _deliver(text):
        clock.advance_ms(MIN)
        await _peer(client, w, text=text)
        await _idle(client, w, hub_id)
        assert len((await _signals(client, w, hub_id))["signals"]) == 1
        assert _rec().signal_turn_ts == _now(clock)

    # (1) Сигналы выданы, а ход начат не по сигналу (ответ выдачи до хоста не дошёл, владелец написал реплику):
    # признак становится «оборван» началом хода — конец хода его уже не сотрёт.
    await _deliver("первое")
    await _turn(state="running")
    rec = _rec()
    assert (rec.signal_turn_ts, rec.signal_lost_ts) == (None, _now(clock))
    row = await _turn(state="idle", last_turn_ts=_now(clock))
    assert row["session"]["signal_lost"] is True and _rec().signal_lost_ts is not None
    await _turn(state="running", origin="signal", clear=["signal_lost"])  # строка «проверь входящее»
    await _turn(state="idle", last_turn_ts=_now(clock))
    rec = _rec()
    assert (rec.signal_turn_ts, rec.signal_lost_ts) == (None, None)

    # Повторный `running` посреди хода по сигналу (подтверждение, причина ожидания) — не начало хода.
    await _deliver("второе")
    await _turn(state="running", origin="signal")
    await _turn(state="running")
    await _host(client, w, f"sessions/{hub_id}/state", {"state": "running", **PROC})
    rec = _rec()
    assert rec.signal_turn_ts is not None and rec.signal_lost_ts is None

    # (а) Движок ход отклонил, не вызвав модель: конец хода с `keep_signal` признак не снимает.
    await _turn(state="idle", last_turn_ts=_now(clock), keep_signal=True)
    rec = _rec()
    assert rec.state == "idle" and rec.signal_turn_ts is not None and rec.signal_lost_ts is None

    # (в) «Усыпить» владельца при стоящем признаке и без идущего хода — сигнал так и не обработан: «оборван».
    assert (await ui.post(f"{API}/sessions/{hub_id}/sleep")).status_code == 200
    await _turn(state="asleep", clear=["sleep"])
    rec = _rec()
    assert (rec.state, rec.signal_turn_ts, rec.signal_lost_ts) == ("asleep", None, _now(clock))
    await _turn(state="asleep", clear=["signal_lost"])

    # То же — сигнал выдан, во ввод не записан, и владелец усыпил до подачи.
    await _deliver("третье")
    assert (await ui.post(f"{API}/sessions/{hub_id}/sleep")).status_code == 200
    await _turn(state="asleep", clear=["sleep"])
    assert _rec().signal_lost_ts == _now(clock)
    await _turn(state="asleep", clear=["signal_lost"])

    # А ход по сигналу, который владелец оборвал усыплением на ходу, — не «оборван» (как раньше).
    await _deliver("четвёртое")
    await _turn(state="running", origin="signal")
    assert (await ui.post(f"{API}/sessions/{hub_id}/sleep")).status_code == 200
    await _turn(state="asleep", clear=["sleep"])
    rec = _rec()
    assert (rec.signal_turn_ts, rec.signal_lost_ts) == (None, None)
