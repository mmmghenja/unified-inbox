"""Ф50-4 (docs/architecture.md): что хост сообщает ядру для
состояния роли — источник идущего хода (`turn_origin`) и отметку «сигнал подан, ждёт конца инструмента»
(`mid_queued_ts`): ставится, когда строка сигнала встала в очередь движка, снимается, когда движок её начал,
выбросил или срок зазора вышел.

Ядро — в процессе, `claude` — подставной с очередью ввода; помощники — из test_f50_mid_turn.py."""

from hub.work import held as held_mod

from .test_f50_mid_turn import LONG, _busy, _start
from .test_held_host import _say, _turn_done, _users
from .test_held_incoming import _core_rec, _peer


def _queued(env, sid):
    return _core_rec(env, sid).mid_queued_ts


async def test_core_knows_queued_line_until_engine_takes_it_at_tool_end(env):
    tool = {"tool": "Bash", "input": {"command": "sleep 2"}, "hold": 2.0}
    host, sid, pid, _ = await _start(env, [[tool, {"text": "готово"}]])
    await _busy(env, host, sid)
    rec = _core_rec(env, sid)
    assert (rec.state, rec.turn_origin, rec.mid_queued_ts) == ("running", "say", None)
    assert rec.turn_ts is not None
    await _peer(env, pid)
    await env.until(host, lambda: _queued(env, sid) is not None, what="ядро знает: строка в очереди движка")
    rec = _core_rec(env, sid)
    assert rec.state == "running" and host.held.runners[sid].mid_reported is True
    # Движок взял строку на границе инструмента — отметка снята ещё до конца хода или вместе с ним.
    await env.until(host, lambda: _queued(env, sid) is None, what="строка начата — отметка снята")
    await _turn_done(env, host, sid)
    rec = _core_rec(env, sid)
    assert (rec.state, rec.turn_ts, rec.turn_origin, rec.mid_queued_ts) == ("idle", None, None, None)
    assert host.held.runners[sid].mid_reported is False


async def test_turn_started_by_queued_line_is_recorded_as_signal_turn(env):
    host, sid, pid, _ = await _start(env, [LONG, [{"sleep": 1.0}, {"text": "прочитала"}]])
    await _busy(env, host, sid)
    runner = host.held.runners[sid]
    await _peer(env, pid)
    await env.until(host, lambda: _queued(env, sid) is not None, what="отметка в ядре")
    assert (await env.ui("POST", f"/sessions/{sid}/interrupt", {})).status_code == 200
    await env.until(host, lambda: runner.turn_by_signal and runner.busy, what="ход по строке сигнала")
    await env.until(host, lambda: _core_rec(env, sid).turn_origin == "signal", what="источник хода в ядре")
    assert _queued(env, sid) is None  # движок начал строку
    await _turn_done(env, host, sid)
    assert _core_rec(env, sid).turn_origin is None


async def test_mark_is_cleared_when_urgent_say_drops_the_queued_line(env):
    host, sid, pid, _ = await _start(env, [LONG, [{"text": "срочное принято"}], [{"text": "проверила"}]])
    await _busy(env, host, sid)
    await _peer(env, pid)
    await env.until(host, lambda: _queued(env, sid) is not None, what="отметка в ядре")
    await _say(env, sid, "срочно", priority="now")
    await env.until(host, lambda: _queued(env, sid) is None, what="строка выброшена — отметка снята")
    await env.until(host, lambda: len(_users(env)) == 4, what="реплика, затем «проверь входящее»")
    await _turn_done(env, host, sid)
    assert _queued(env, sid) is None


async def test_mark_stays_through_the_gap_and_is_cleared_by_its_timeout(env, monkeypatch):
    monkeypatch.setattr(held_mod, "MID_GAP_S", 1.5)
    host, sid, pid, _ = await _start(env, [[{"sleep": 0.8}, {"text": "первый"}]], stall=True)
    await _busy(env, host, sid)
    runner = host.held.runners[sid]
    await _peer(env, pid)
    await env.until(host, lambda: _queued(env, sid) is not None, what="отметка в ядре")
    await env.until(host, lambda: runner.mid_gap is not None, what="зазор: ход кончился, строка не начата")
    assert _queued(env, sid) is not None  # ход кончился, а строка ещё в очереди движка
    await env.until(host, lambda: len(_users(env)) == 3, what="срок вышел — «проверь входящее»")
    await env.until(host, lambda: _queued(env, sid) is None, what="отметка снята по сроку зазора")
