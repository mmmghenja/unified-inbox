"""Ф48-2 в хосте постоянных сессий (docs/architecture.md–D): прерванная
реплика возвращается в очередь и держит её до решения владельца; срочная реплика ждёт конца сжатия; сжатие по
размеру — перед следующей репликой очереди; свой порог и выключатель авто-сжатия сессии.

Ядро — в процессе, `claude` — подставной (`fake_claude.py`)."""

import asyncio

from hub.work import session
from hub.work.held import HeldRunner

from .test_held_bg import _cycles, _done, _feed, _started
from .test_held_compact import _compacted, _compacts, _use
from .test_held_host import (
    _controls,
    _events,
    _has_event,
    _interrupted,
    _rec,
    _running,
    _say,
    _session,
    _state,
    _turn_done,
    _users,
)
from .test_held_incoming import _text


async def _cut(env, host, sid) -> None:
    """«Прервать» идущий ход и дождаться, пока его реплика вернётся в очередь прерванной."""
    await env.until(host, _state(env, sid, "running"), what="ход идёт")
    r = await env.ui("POST", f"/sessions/{sid}/interrupt")
    assert r.status_code == 200, r.text
    await env.until(host, lambda: _interrupted(env, sid), what="реплика вернулась прерванной")
    await _turn_done(env, host, sid)


async def _patch(env, sid, body) -> dict:
    r = await env.ui("PATCH", f"/sessions/{sid}", body)
    assert r.status_code == 200, r.text
    return r.json()["session"]


# --- A: прерванная реплика ----------------------------------------------------------------------------------


async def test_interrupted_say_holds_the_queue_until_resend(env):
    env.scenario(turns=[[{"sleep": 30}], [{"text": "срочная принята"}], [{"text": "первая принята"}], []])
    host = await env.started()
    sid, _, _ = await _session(env)
    first = await _running(env, host, sid, "первая")
    await env.until(host, _state(env, sid, "running"), what="ход идёт")
    plain = await _say(env, sid, "следом")
    await _cut(env, host, sid)
    assert await _interrupted(env, sid) == [first]
    rec = await _rec(env, sid)
    assert (rec["state"], rec["queued_says"], rec["interrupted_says"]) == ("idle", 2, 1)
    assert any(
        e["kind"] == "say" and e["ref"] == str(first) and "прервана" in e["summary"]
        for e in await _events(env, sid)
    )
    # Очередь стоит: ни прерванная, ни обычная за ней сами не подаются.
    await _cycles(env, host, 8)
    assert [_text(m) for m in _users(env)] == ["первая"]

    # Срочная реплика прерванную не ждёт — подаётся; прерванная и обычная стоят дальше.
    urgent = await _say(env, sid, "срочно", "now")
    await env.until(host, lambda: len(_users(env)) == 2, what="срочная подана")
    await _turn_done(env, host, sid)
    await _cycles(env, host, 6)
    assert [m["uuid"] for m in _users(env)] == [session.held_say_uuid(first), session.held_say_uuid(urgent)]
    assert await _interrupted(env, sid) == [first] and (await _rec(env, sid))["queued_says"] == 2

    # «Отправить снова»: она идёт первой, как обычная следующая, под новым uuid; за ней — стоявшая обычная.
    r = await env.ui("POST", f"/sessions/{sid}/says/{first}/resend")
    assert r.status_code == 200, r.text
    # Ф55-1 (2.1): обе идут одним вводом — пачкой, под uuid повтора первой.
    await env.until(host, lambda: len(_users(env)) == 3, what="повтор и обычная поданы")
    await _turn_done(env, host, sid)
    users = _users(env)
    assert [_text(m) for m in users] == [
        "первая",
        "срочно",
        "Реплика 1 из 2:\nпервая\n\nРеплика 2 из 2:\nследом",
    ]
    assert users[2]["uuid"] not in (users[0]["uuid"], users[1]["uuid"], session.held_say_uuid(plain))
    rec = await _rec(env, sid)
    assert (rec["queued_says"], rec["interrupted_says"]) == (0, 0)
    assert "первая принята" in [e["summary"] for e in await _events(env, sid) if e["kind"] == "text"]


async def test_urgent_say_returns_the_cut_one_and_is_not_returned_itself(env):
    """Ход оборван срочной репликой: прерванной возвращается оборванная, не срочная; свой обычный конец хода
    срочную в очередь не возвращает."""
    env.scenario(turns=[[{"sleep": 30}], [{"text": "срочная принята"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    first = await _running(env, host, sid, "первая")
    await env.until(host, _state(env, sid, "running"), what="ход идёт")
    await _say(env, sid, "срочно", "now")
    await env.until(host, lambda: len(_users(env)) == 2, what="срочная подана")
    await _turn_done(env, host, sid)
    await _cycles(env, host, 4)
    assert await _interrupted(env, sid) == [first]
    assert (await _rec(env, sid))["queued_says"] == 1


async def test_interrupted_say_survives_sleep_and_does_not_wake(env):
    env.scenario(turns=[[{"sleep": 30}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    first = await _running(env, host, sid, "первая")
    await _cut(env, host, sid)
    r = await env.ui("POST", f"/sessions/{sid}/sleep", {})
    assert r.status_code == 200 and r.json()["dropped_says"] == 0
    await env.until(host, _state(env, sid, "asleep"), what="усыплена")
    env.scenario(turns=[[{"text": "снова принята"}]])
    await _cycles(env, host, 8)
    # Спящую прерванная реплика не поднимает и никуда не девается.
    assert len(env.calls()) == 1 and await _interrupted(env, sid) == [first]
    assert (await _rec(env, sid))["state"] == "asleep"
    # И перезапуск хоста её не подаёт.
    await asyncio.sleep(0.01)
    fresh = await env.started()
    await _cycles(env, fresh, 8)
    assert len(env.calls()) == 1 and await _interrupted(env, sid) == [first]
    # «Отправить снова» поднимает сессию и подаёт реплику — хотя прежний её uuid уже есть в разговоре.
    r = await env.ui("POST", f"/sessions/{sid}/says/{first}/resend")
    assert r.status_code == 200, r.text
    await env.until(fresh, lambda: len(env.calls()) == 2, what="подъём по повтору")
    await env.until(fresh, _has_event(env, sid, "text", "снова принята"), what="повтор отработан")
    assert (await _rec(env, sid))["queued_says"] == 0


# --- B: срочная реплика во время сжатия ---------------------------------------------------------------------


async def test_urgent_say_waits_for_the_end_of_compaction(env, monkeypatch):
    gate = asyncio.Event()
    real = HeldRunner._compact_done

    async def done(self, msg):
        await gate.wait()  # сжатие «идёт», пока тест не отпустит его конец
        await real(self, msg)

    monkeypatch.setattr(HeldRunner, "_compact_done", done)
    env.scenario(
        turns=[[{"text": "раз"}, _use(160_000)], [{"text": "срочная принята"}, _use(60_000)], []],
        compact={"pre_tokens": 161_000},
        init_model="claude-test",
    )
    host = await env.started(held_compact_min_growth=0, held_compact_tokens=150_000)
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    runner = host.held.runners[sid]
    await env.until(host, lambda: runner.compacting is not None, what="сжатие идёт")
    plain = await _say(env, sid, "обычная")
    urgent = await _say(env, sid, "срочно", "now")

    async def waits():
        return (await _rec(env, sid))["wait_reason"] == "compact"

    await env.until(host, waits, what="причина «ждёт конца сжатия»")
    await _cycles(env, host, 6)
    # Сжатие не прервано: `interrupt` движку не уходил, реплика не подана.
    assert _controls(env, "interrupt") == [] and runner.compacting is not None
    assert [_text(m) for m in _users(env)] == ["привет", "/compact"]

    gate.set()
    await env.until(host, lambda: len(_users(env)) == 3, what="срочная и обычная поданы")
    await _turn_done(env, host, sid)
    # После сжатия срочная идёт первой (Ф55-1: одной пачкой с обычной); сжатие состоялось, а не «прервано».
    batch = _users(env)[2]
    assert batch["uuid"] == session.held_say_uuid(urgent) != session.held_say_uuid(plain)
    assert _text(batch) == "Реплика 1 из 2:\nсрочно\n\nРеплика 2 из 2:\nобычная"
    events = await _events(env, sid)
    assert [e["kind"] for e in events].count("compact") == 1
    assert not [e for e in events if e["kind"] == "interrupt"]
    assert _controls(env, "interrupt") == [] and await _interrupted(env, sid) == []
    assert (await _rec(env, sid))["wait_reason"] is None


async def test_urgent_say_goes_after_hung_compaction_times_out(env):
    env.scenario(
        turns=[[{"text": "раз"}, _use(160_000)], [{"text": "срочная принята"}, _use(161_000)]],
        compact={"hang": True},
        init_model="claude-test",
    )
    host = await env.started(held_compact_min_growth=0, held_compact_wait_s=0.6, held_compact_tokens=150_000)
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    runner = host.held.runners[sid]
    await env.until(host, lambda: runner.compacting is not None, what="сжатие идёт")
    await _say(env, sid, "срочно", "now")
    await env.until(host, _has_event(env, sid, "text", "срочная принята"), what="срочная после срока сжатия")
    assert _controls(env, "interrupt") == []  # срок вышел сам — сжатие никто не прерывал


# --- C: сжатие между репликами очереди ----------------------------------------------------------------------


async def test_auto_compaction_conditions_with_a_queue(env):
    env.scenario(turns=[[{"text": "раз"}, _use(160_000)]], init_model="claude-test")
    host = await env.started(held_compact_min_growth=0, held_compact_tokens=150_000)
    sid, _, _ = await _session(env)
    await _patch(env, sid, {"compact_auto": False})  # чтобы хост не сжал сам, пока тест смотрит условия
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    await _cycles(env, host, 4)
    assert _compacts(env) == 0
    held = host.held
    runner = held.runners[sid]
    rec = {**held.records[sid], "compact_auto": True}
    assert held._compact_auto(rec, runner) is True
    # Реплика в очереди сжатию по размеру не мешает — оно идёт перед ней.
    held.says[sid] = {77: {"id": 77, "text": "следом", "priority": "next"}}
    try:
        assert held._compact_auto(rec, runner) is True
        assert held._compact_free(rec, runner) is False  # «Сжать» по просьбе — как раньше, после очереди
        # Только что поднятый процесс (хода ещё не было) сначала отвечает на то, ради чего поднят.
        runner.turn_seen = False
        assert held._compact_auto(rec, runner) is False
        runner.turn_seen = True
        # Открытая карточка и живые фоновые задачи — не сжимать, с очередью и без.
        runner.pending["card"] = {"core_id": 1}
        assert held._compact_auto(rec, runner) is False
        runner.pending.clear()
        await _feed(runner, _started("a1"))
        assert held._compact_auto(rec, runner) is False
        await _feed(runner, _done("a1"))
        assert held._compact_auto(rec, runner) is True
    finally:
        held.says.pop(sid, None)


async def test_no_compaction_before_queued_say_while_agent_runs(env):
    """Живой подагент: сжатия перед следующей репликой нет — реплика подаётся как есть."""
    env.scenario(
        turns=[
            [{"sleep": 0.4}, {"emit": _started("a1")}, {"text": "раз"}, _use(160_000)],
            [{"text": "два"}, _use(161_000)],
        ],
        compact={"pre_tokens": 161_000},
        init_model="claude-test",
    )
    host = await env.started(held_compact_min_growth=0, held_compact_tokens=150_000)
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    await _say(env, sid, "вторая")
    await env.until(host, lambda: len(_users(env)) == 2, what="вторая реплика")
    await _turn_done(env, host, sid)
    assert [_text(m) for m in _users(env)] == ["привет", "вторая"] and _compacts(env) == 0


# --- D: порог и выключатель сессии --------------------------------------------------------------------------


async def test_default_threshold_is_200k(env):
    env.scenario(turns=[[{"text": "раз"}, _use(160_000)]], init_model="claude-test")
    host = await env.started(held_compact_min_growth=0)
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)

    async def told():
        return (await _rec(env, sid))["compact_at"] == 200_000

    await env.until(host, told, what="действующий порог дошёл до ядра")
    await _cycles(env, host, 6)
    assert _compacts(env) == 0  # 160 тыс. ниже умолчания 200 тыс. (при прежних 150 тыс. сжалась бы)


async def test_session_threshold_and_switch(env):
    env.scenario(
        turns=[[{"text": "раз"}, _use(130_000)]], compact={"pre_tokens": 131_000}, init_model="claude-test"
    )
    host = await env.started(held_compact_min_growth=0)
    sid, _, _ = await _session(env)
    await _patch(env, sid, {"compact_tokens": 300_000})
    await _running(env, host, sid)
    await _turn_done(env, host, sid)

    def told(value):
        async def check():
            return (await _rec(env, sid))["compact_at"] == value

        return check

    await env.until(host, told(300_000), what="свой порог дошёл до ядра")
    await _cycles(env, host, 4)
    assert _compacts(env) == 0
    # Порог ниже контекста, но авто-сжатие выключено — не сжимает; действующий порог виден всё равно
    # (он «от базы сессии»: не ниже 130 тыс. первого хода).
    await _patch(env, sid, {"compact_tokens": 120_000, "compact_auto": False})
    await env.until(host, told(130_000), what="новый порог дошёл до ядра")
    await _cycles(env, host, 6)
    assert _compacts(env) == 0
    # «Сжать» по просьбе при выключенном авто работает.
    r = await env.ui("POST", f"/sessions/{sid}/compact", {})
    assert r.status_code == 200, r.text
    await _compacted(env, host, sid)
    assert _compacts(env) == 1


async def test_session_threshold_below_default_compacts(env):
    """Свой порог сессии ниже умолчания — сжатие идёт без перезапуска процесса, как только настройка дошла."""
    env.scenario(
        turns=[[{"text": "раз"}, _use(130_000)]], compact={"pre_tokens": 131_000}, init_model="claude-test"
    )
    host = await env.started(held_compact_min_growth=0)
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    await _cycles(env, host, 4)
    assert _compacts(env) == 0 and len(env.calls()) == 1
    await _patch(env, sid, {"compact_tokens": 120_000})
    await _compacted(env, host, sid)
    assert _compacts(env) == 1 and len(env.calls()) == 1



async def test_tick_does_not_feed_while_turn_end_is_being_written(env):
    """Приёмка Ф48-2 (C), найдено на стенде с настоящим claude: круг, попавший в зазор «ход окончен, но
    конец хода ещё пишется в ядро», видел старый размер контекста — сжатие не начиналось, и следующая
    реплика уходила раньше него. Пока конец хода пишется, круг сессию не трогает."""
    env.scenario(turns=[[{"text": "раз"}, _use(160_000)]], init_model="claude-test")
    host = await env.started(held_compact_min_growth=0, held_compact_tokens=150_000)
    sid, _, _ = await _session(env)
    await _patch(env, sid, {"compact_auto": False})
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    held = host.held
    runner = held.runners[sid]
    held.says[sid] = {77: {"id": 77, "text": "следом", "priority": "next"}}
    runner.turn_closing = True
    try:
        await held.tick()
        assert not runner.busy
        assert 77 in held.says[sid]
    finally:
        runner.turn_closing = False
        held.says.pop(sid, None)


async def test_compaction_before_third_say_when_two_are_queued_during_the_turn(env):
    """Приёмка Ф48-2 (C): порог перейдён вторым ходом — сжатие идёт перед третьей репликой. Ф55-1: три
    реплики, набранные сразу, ушли бы одной пачкой — здесь они приходят по одной, третья — во время хода
    (движок без событий очереди ввода: реплика ждёт конца хода)."""
    env.scenario(
        turns=[
            [{"text": "раз"}, _use(10_000)],
            [{"sleep": 0.6}, {"text": "два"}, _use(160_000)],
            [{"text": "три"}, _use(20_000)],
        ],
        init_model="claude-test",
    )
    host = await env.started(held_compact_min_growth=0, held_compact_tokens=150_000)
    sid, _, _ = await _session(env)
    await _say(env, sid, "первая")
    await env.until(host, lambda: len(_users(env)) == 1, what="первая подана")
    await _turn_done(env, host, sid)
    await _say(env, sid, "вторая")
    await env.until(host, lambda: len(_users(env)) == 2, what="вторая подана")
    await _say(env, sid, "третья")
    await env.until(host, lambda: len(_users(env)) == 4, what="сжатие и третья реплика")
    assert [_text(m) for m in _users(env)] == ["первая", "вторая", "/compact", "третья"]
