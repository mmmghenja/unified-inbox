"""Ф41-4, правки по ревью (docs/architecture.md): выданный, но
не поданный сигнал; подъём ради подписки без места; обход паузы по «Поднять»; ход по сигналу, отклонённый
движком. Ядро — в процессе, `claude` — подставной."""

import pytest

from hub.agent.core_api import CoreUnavailable
from hub.work import held as held_mod

from .test_held_host import _alive, _events, _rec, _running, _say, _session, _turn_done, _users
from .test_held_incoming import ROLE, WANT, _core_rec, _no_debounce, _peer, _subscribe, _text, _touch


async def _apply(host) -> dict:
    data = await host.core.get("/work/host/wait", params=host.wait_params(0))
    await host.apply(data)
    return data


# --- 1: сигнал выдан ядром, до ввода сессии не дошёл --------------------------------------------------------


async def test_delivered_but_never_fed_signal_becomes_lost_at_foreign_turn(env):
    env.scenario(turns=[[{"text": "первый"}], [{"text": "второй"}], [{"text": "проверила"}]])
    host = await env.started()
    sid, pid, workdir = await _session(env, role=ROLE)
    await _subscribe(env, sid, workdir)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    _no_debounce(env)
    real_post = host.core.post

    async def post(path, **kw):
        result = await real_post(path, **kw)  # ядро выдачу записало
        if path.endswith(f"/sessions/{sid}/signals") and result.get("signals"):
            raise CoreUnavailable("ответ не дошёл")
        return result

    host.core.post = post
    await _peer(env, pid)
    await _apply(host)
    with pytest.raises(CoreUnavailable):
        await host.held.tick()
    host.core.post = real_post
    assert len(_users(env)) == 1 and _core_rec(env, sid).signal_turn_ts is not None

    # Реплика владельца — ход не по сигналу: признак не стирается концом хода, а становится «оборван», и после
    # хода сессия получает постоянную строку «проверь входящее».
    await _say(env, sid, "как дела")
    await env.until(host, lambda: len(_users(env)) == 3, what="реплика и строка об оборванном сигнале")
    await _turn_done(env, host, sid)
    texts = [_text(m) for m in _users(env)]
    assert texts[1:] == ["как дела", held_mod.SIGNAL_LOST_TEXT]
    rec = _core_rec(env, sid)
    assert rec.signal_turn_ts is None and rec.signal_lost_ts is None  # подано и снято началом того хода
    for _ in range(3):
        await env.cycle(host)
    assert len(_users(env)) == 3


# --- 2: подъём ради подписки без свободного места -----------------------------------------------------------


async def test_sub_wake_without_slot_writes_reason_once(env):
    env.scenario(turns=[[{"text": "ок"}]])
    host = await env.started(held_max_parallel=1, held_signal_wake_gap_s=0)
    project = env.project("Initech")
    a, _pid, _workdir = await _session(env, project=project)
    await _running(env, host, a)
    await _turn_done(env, host, a)
    b, _, _ = await _session(env, project=project, role=ROLE, want_sub=WANT)  # спит, `sub_stale`
    for _ in range(3):
        await env.cycle(host)
    assert _alive(host) == [a] and (await _rec(env, b))["wait_reason"] == "slot"
    turns = []
    real_post = host.core.post

    async def post(path, **kw):
        if path == f"/work/host/sessions/{b}/turn":
            turns.append(kw.get("json"))
        return await real_post(path, **kw)

    host.core.post = post
    seq = _core_rec(env, b).seq
    for _ in range(8):
        await host.tick()
        await _apply(host)
    assert turns == [] and _core_rec(env, b).seq == seq  # ни одной записи в ядро без изменения состояния
    assert (await _rec(env, b))["wait_reason"] == "slot"
    host.core.post = real_post
    # Место освободилось — сессия поднимается ради подписки, причина снимается.
    await env.ui("POST", f"/sessions/{a}/sleep")
    await env.until(host, lambda: b in _alive(host), what="подъём ради подписки")

    async def cleared():
        return (await _rec(env, b))["wait_reason"] is None

    await env.until(host, cleared, what="причина ожидания снята")


# --- 3: «Поднять» на паузе ---------------------------------------------------------------------------------


async def test_wake_bypass_covers_only_says_queued_at_the_press(env):
    env.scenario(turns=[[{"text": "раз"}], [{"text": "два"}]])
    host = await env.started()
    sid, _pid, _workdir = await _session(env)
    host.on_rate_limit({"status": "rejected"})
    first = await _say(env, sid, "первая")
    for _ in range(3):
        await env.cycle(host)
    assert env.calls() == [] and (await _rec(env, sid))["wait_reason"] == "paused"
    r = await env.ui("POST", f"/sessions/{sid}/wake", {})
    assert r.status_code == 200, r.text
    await _apply(host)
    await host.held.tick()
    assert host.held.bypass_says == {sid: {first}}
    await _say(env, sid, "написана после «Поднять»")
    await _apply(host)
    await host.held.tick()  # просьба ещё стоит (процесс поднимается) — обход не расширяется
    assert host.held.bypass_says == {sid: {first}}
    await env.until(host, lambda: len(_users(env)) >= 1, what="первая реплика подана")
    await _turn_done(env, host, sid)
    for _ in range(5):
        await env.cycle(host)
    assert [_text(m) for m in _users(env)] == ["первая"]
    rec = await _rec(env, sid)
    assert rec["wait_reason"] == "paused" and rec["queued_says"] == 1
    assert _core_rec(env, sid).wake_requested_ts is None


async def test_wake_request_with_nothing_left_is_cleared(env):
    """Просьба «Поднять» без реплик и готовых сигналов снимается: иначе она висит без срока и следующий
    сигнал поднял бы тяжёлую холодную сессию «по просьбе»."""
    host = await env.started()
    sid, _pid, _workdir = await _session(env)
    _touch(env, sid, wake_requested_ts=5)
    for _ in range(3):
        await env.cycle(host)
    rec = _core_rec(env, sid)
    assert rec.wake_requested_ts is None and rec.state == "asleep" and env.calls() == []
    seq = rec.seq
    for _ in range(3):
        await env.cycle(host)
    assert _core_rec(env, sid).seq == seq


# --- а: ход по сигналу отклонён движком ---------------------------------------------------------------------


async def test_signal_turn_rejected_by_engine_keeps_the_mark(env):
    env.scenario(turns=[[{"text": "первый"}], [{"reject": "лимит"}], [{"text": "проверила"}]])
    host = await env.started()
    sid, pid, workdir = await _session(env, role=ROLE)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    await _subscribe(env, sid, workdir)
    await _peer(env, pid)
    await env.until(host, lambda: len(_users(env)) == 2, what="сигнал во вводе процесса")
    await _turn_done(env, host, sid)
    rec = _core_rec(env, sid)
    assert rec.state == "idle" and rec.signal_turn_ts is not None and rec.signal_lost_ts is None
    ends = [e for e in await _events(env, sid) if e["kind"] == "turn_end"]
    assert [e["is_error"] for e in ends] == [False, True]
    assert host.held.runners[sid].signal_rejected is True

    # Следующая подача напоминает о входящем (те сигналы уже отмечены выданными); её обычный конец признак
    # снимает.
    _no_debounce(env)
    await _peer(env, pid, text="второе")
    await env.until(host, lambda: len(_users(env)) == 3, what="второй сигнал")
    await _turn_done(env, host, sid)
    assert held_mod.SIGNAL_FALLBACK_TEXT in _text(_users(env)[2])
    rec = _core_rec(env, sid)
    assert rec.signal_turn_ts is None and rec.signal_lost_ts is None
    assert host.held.runners[sid].signal_rejected is False


async def test_rejected_signal_turn_then_sleep_is_lost(env):
    env.scenario(turns=[[{"text": "первый"}], [{"reject": "лимит"}]])
    host = await env.started()
    sid, pid, workdir = await _session(env, role=ROLE)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    await _subscribe(env, sid, workdir)
    await _peer(env, pid)
    await env.until(host, lambda: len(_users(env)) == 2, what="сигнал во вводе процесса")
    await _turn_done(env, host, sid)
    assert _core_rec(env, sid).signal_turn_ts is not None
    host.on_rate_limit({"status": "rejected"})  # лимит: строку об оборванном сигнале подаст конец паузы
    await env.ui("POST", f"/sessions/{sid}/sleep")
    await env.until(host, lambda: sid not in host.held.runners, what="усыплена")
    rec = _core_rec(env, sid)
    assert rec.state == "asleep" and rec.signal_turn_ts is None and rec.signal_lost_ts is not None
