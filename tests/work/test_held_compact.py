"""Ф41-4б (docs/architecture.md): сжатие контекста
постоянной сессии в хосте, база сессии и пороги от неё, «работа сверх подписки», обход паузы «Поднять».

Ядро — в процессе, `claude` — подставной (`fake_claude.py`: `/compact` — не ход, отвечает как настоящий)."""

import json

from .test_held_host import _events, _rec, _running, _say, _session, _state, _turn_done, _users
from .test_held_incoming import ROLE, _core_rec, _peer, _subscribe, _text, _touch

MODEL_USAGE = {"claude-test": {"contextWindow": 1_000_000}}


def _use(context: int, cost: float | None = None) -> dict:
    """Шаг сценария: расход хода — контекст последнего вызова модели."""
    extra: dict = {"usage": {"iterations": [{"input_tokens": context}]}, "modelUsage": MODEL_USAGE}
    if cost is not None:
        extra["total_cost_usd"] = cost
    return {"result_extra": extra}


def _compacts(env) -> int:
    return len(env._jsonl("compacts.jsonl"))


def _kinds(events: list[dict]) -> list[str]:
    return [e["kind"] for e in events]


async def _compacted(env, host, sid) -> None:
    runner = host.held.runners[sid]
    await env.until(host, lambda: _compacts(env) >= 1 and runner.compacting is None, what="сжатие закончено")
    await env.until(host, _state(env, sid, "idle"), what="idle после сжатия")


# --- сжатие по размеру --------------------------------------------------------------------------------------


async def test_compaction_at_turn_end_is_not_a_turn(env):
    env.scenario(
        turns=[[{"text": "раз"}, _use(160_000, 0.5)], [{"text": "два"}, _use(60_000, 1.0)]],
        compact={"pre_tokens": 161_000, "total_cost_usd": 0.75},
        init_model="claude-test",
    )
    host = await env.started(held_compact_min_growth=0, held_compact_tokens=150_000)
    sid, _pid, _workdir = await _session(env)
    await _running(env, host, sid)
    await _compacted(env, host, sid)
    events = await _events(env, sid)
    # Сжатие — не ход: один `turn_end` (хода), событие `compact`, сводка в ленту не идёт.
    assert _kinds(events).count("turn_end") == 1 and _kinds(events).count("compact") == 1
    [note] = [e for e in events if e["kind"] == "compact"]
    assert note["summary"] == "контекст сжат: было 160 тыс." and note["brief"] == {
        "type": "compact",
        "before": 160_000,
    }
    assert "СВОДКА-СЖАТИЯ" not in json.dumps(events, ensure_ascii=False)
    assert _text(_users(env)[-1]) == "/compact"
    rec = await _rec(env, sid)
    # П4в: размер неизвестен до следующего хода; предел и цена сжатия остаются в расходе.
    assert rec["usage"]["context_tokens"] is None and rec["usage"]["context_limit"] == 1_000_000
    assert rec["usage"]["cost_usd"] == 0.75 and rec["usage"]["base_tokens"] == 160_000
    last_turn = _core_rec(env, sid).last_turn_ts
    # П4б: второе сжатие сразу не запускается (хода с вызовом модели ещё не было).
    for _ in range(4):
        await env.cycle(host)
    assert _compacts(env) == 1 and _core_rec(env, sid).last_turn_ts == last_turn
    # Первый ход за сжатием — новая база (П17) и новый размер.
    await _say(env, sid, "ещё")
    await env.until(host, lambda: len(_users(env)) == 3, what="второй ход")
    await _turn_done(env, host, sid)
    rec = await _rec(env, sid)
    assert rec["usage"]["context_tokens"] == 60_000 and rec["usage"]["base_tokens"] == 60_000
    assert _compacts(env) == 1


async def test_threshold_from_base_no_compaction_after_every_turn(env):
    """П17: база 160 тыс. выше порога 150 тыс. — сжатие не идёт после каждого хода, пока контекст не вырос на
    `held_compact_min_growth`."""
    env.scenario(
        turns=[
            [{"text": "раз"}, _use(160_000)],
            [{"text": "два"}, _use(170_000)],
            [{"text": "три"}, _use(201_000)],
        ],
        init_model="claude-test",
    )
    host = await env.started(held_compact_tokens=150_000)
    sid, _pid, _workdir = await _session(env)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    for _ in range(3):
        await env.cycle(host)
    assert _compacts(env) == 0 and (await _rec(env, sid))["usage"]["base_tokens"] == 160_000
    await _say(env, sid, "ещё")
    await env.until(host, lambda: len(_users(env)) == 2, what="второй ход")
    await _turn_done(env, host, sid)
    for _ in range(3):
        await env.cycle(host)
    rec = await _rec(env, sid)
    assert _compacts(env) == 0 and rec["usage"]["context_tokens"] == 170_000
    assert rec["usage"]["base_tokens"] == 160_000  # база — от первого хода, дальше не двигается
    await _say(env, sid, "и ещё")
    await env.until(host, lambda: _compacts(env) == 1, what="выросла на 40 тыс. — сжатие")
    await _compacted(env, host, sid)


async def test_threshold_caps_at_three_quarters_of_limit(env):
    host = await env.started()
    held = host.held
    usage = {"context_tokens": 0, "context_limit": 100_000}
    assert held._compact_threshold({"engine": "claude", "usage": usage}) == 75_000
    assert held._compact_threshold({"engine": "claude", "usage": {}}) == 200_000  # Ф48-2 (D): умолчание
    assert held._compact_threshold({"engine": "opencode", "usage": {}}) == 200_000
    # П20: порог от базы — не выше 85 % предела контекста.
    assert held._compact_threshold({"engine": "claude", "usage": usage, "base_tokens": 90_000}) == 85_000
    haiku = {"context_tokens": 0, "context_limit": 200_000}
    big = {"engine": "claude", "usage": haiku, "base_tokens": 139_000}
    assert held._compact_threshold(big) == 170_000  # не 179 тыс.
    assert held._compact_threshold({**big, "base_tokens": 82_000}) == 150_000  # как раньше
    assert held._compact_threshold({"engine": "claude", "usage": {}, "base_tokens": 139_000}) == 200_000
    assert held._compact_threshold({"engine": "claude", "usage": {}, "base_tokens": 170_000}) == 210_000
    cold = {**big, "cache_expires_ts": None}
    assert held._heavy_cold({**cold, "context_tokens": 170_001}) is True  # «тяжёлая» — тем же потолком
    assert held._heavy_cold({**cold, "context_tokens": 170_000}) is False
    host.config["held_context_cap_share"] = 0.5
    assert held._compact_threshold(big) == 100_000
    assert held._compact_threshold({"engine": "claude", "usage": {}, "base_tokens": 20_000}) == 200_000


async def test_compaction_goes_before_the_next_queued_say(env):
    """Ф48-2 (C): ход окончен, контекст не ниже порога — сжатие идёт ПЕРЕД следующей репликой очереди (раньше
    очередь шла первой, и сессия Initech 09.10.2026 доросла до 232 тыс.)."""
    env.scenario(
        turns=[[{"sleep": 0.4}, {"text": "раз"}, _use(160_000)], [{"text": "два"}, _use(60_000)]],
        init_model="claude-test",
    )
    host = await env.started(held_compact_min_growth=0, held_compact_tokens=150_000)
    sid, _pid, _workdir = await _session(env)
    await _running(env, host, sid)
    await _say(env, sid, "вторая")
    await env.until(host, lambda: len(_users(env)) == 3, what="сжатие и вторая реплика")
    texts = [_text(m) for m in _users(env)]
    assert texts == ["привет", "/compact", "вторая"]
    await _turn_done(env, host, sid)
    assert _compacts(env) == 1


async def test_compaction_cancelled_by_host_interrupt_is_not_a_failure(env, monkeypatch):
    """Срочная реплика или «Прервать» во время сжатия: Claude Code отменяет сжатие — это не сбой (сессия
    Initech 09.10.2026: «сжатие контекста не удалось: failed» после срочной реплики)."""
    from hub.work.held import HeldRunner

    real = HeldRunner.compact

    async def compact(self, **kwargs):
        self.interrupt_sent = self.host.monotonic()  # хост прервал сжатие, едва оно началось
        return await real(self, **kwargs)

    monkeypatch.setattr(HeldRunner, "compact", compact)
    env.scenario(turns=[[{"text": "раз"}, _use(160_000)]], compact={"fail": True}, init_model="claude-test")
    host = await env.started(held_compact_min_growth=0, held_compact_tokens=150_000)
    sid, _pid, _workdir = await _session(env)
    await _running(env, host, sid)
    await _compacted(env, host, sid)
    events = await _events(env, sid)
    assert not [e for e in events if e["kind"] == "warn" and "сжатие" in e["summary"]]
    assert [e["summary"] for e in events if e["kind"] == "interrupt"] == [
        "сжатие контекста прервано — повторится после следующего хода"
    ]
    assert host.held.runners[sid].interrupt_sent is None
    assert (await _rec(env, sid))["state"] == "idle"


async def test_own_turn_of_engine_before_queued_compaction_is_not_its_failure(env):
    """10.10: `/compact` попал в зазор, когда движок сам начал ход; конец этого хода сжатие не закрывает,
    его текст виден в ленте, сжатие удаётся."""
    env.scenario(
        turns=[[{"text": "раз"}, _use(160_000, 0.5)]],
        compact={"pre_tokens": 161_000, "own_turn_first": True},
        init_model="claude-test",
    )
    host = await env.started(held_compact_min_growth=0, held_compact_tokens=150_000)
    sid, _pid, _workdir = await _session(env)
    await _running(env, host, sid)
    await _compacted(env, host, sid)
    events = await _events(env, sid)
    kinds = _kinds(events)
    assert kinds.count("compact") == 1
    assert not [e for e in events if e["kind"] == "warn" and "сжатие" in e["summary"]]
    assert kinds.count("turn_end") == 2  # ход владельца и свой ход движка
    assert "СВОЙ-ХОД движка" in json.dumps(events, ensure_ascii=False)
    assert "СВОДКА-СЖАТИЯ" not in json.dumps(events, ensure_ascii=False)


async def test_failed_compaction_warns_and_does_not_repeat(env):
    env.scenario(turns=[[{"text": "раз"}, _use(160_000)]], compact={"fail": True}, init_model="claude-test")
    host = await env.started(held_compact_min_growth=0, held_compact_tokens=150_000)
    sid, _pid, _workdir = await _session(env)
    await _running(env, host, sid)
    await _compacted(env, host, sid)
    events = await _events(env, sid)
    assert "compact" not in _kinds(events) and _kinds(events).count("turn_end") == 1
    assert [e["summary"] for e in events if e["kind"] == "warn" and "сжатие" in e["summary"]] == [
        "сжатие контекста не удалось: failed"
    ]
    rec = await _rec(env, sid)
    assert rec["state"] == "idle" and rec["usage"]["context_tokens"] == 160_000  # размер не стёрт
    for _ in range(4):
        await env.cycle(host)
    assert _compacts(env) == 1  # повтор — не раньше следующего хода с вызовом модели


async def test_compaction_without_confirmation_times_out(env):
    env.scenario(
        turns=[[{"text": "раз"}, _use(160_000)], [{"text": "жива"}, _use(161_000)]],
        compact={"hang": True},
        init_model="claude-test",
    )
    host = await env.started(
        held_compact_min_growth=0, held_compact_wait_s=0.4, held_compact_tokens=150_000
    )
    sid, _pid, _workdir = await _session(env)
    await _running(env, host, sid)
    runner = host.held.runners[sid]
    await env.until(host, lambda: _compacts(env) == 1 and runner.compacting is None, what="срок вышел")
    await env.until(host, _state(env, sid, "idle"), what="idle")
    events = await _events(env, sid)
    assert any(e["kind"] == "warn" and "не подтверждено за 0.4 с" in e["summary"] for e in events)
    # Сессия работает дальше: следующая реплика подаётся.
    await _say(env, sid, "ты тут?")
    await env.until(host, lambda: len(_users(env)) >= 3, what="реплика после срока")
    await _turn_done(env, host, sid)
    assert "жива" in [e["summary"] for e in await _events(env, sid) if e["kind"] == "text"]


async def test_compact_by_request_ignores_threshold_and_clears_flag(env):
    env.scenario(
        turns=[[{"text": "раз"}, _use(20_000)]], compact={"pre_tokens": 21_000}, init_model="claude-test"
    )
    host = await env.started()
    sid, _pid, _workdir = await _session(env)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    for _ in range(3):
        await env.cycle(host)
    assert _compacts(env) == 0  # до порога далеко
    r = await env.ui("POST", f"/sessions/{sid}/compact", {})
    assert r.status_code == 200, r.text
    await _compacted(env, host, sid)
    assert _core_rec(env, sid).compact_requested_ts is None
    [note] = [e for e in await _events(env, sid) if e["kind"] == "compact"]
    assert note["brief"] == {"type": "compact", "before": 20_000}
    # Спящей — отказ: сжимать нечем.
    await env.ui("POST", f"/sessions/{sid}/sleep", {})
    await env.until(host, _state(env, sid, "asleep"), what="усыплена")
    r = await env.ui("POST", f"/sessions/{sid}/compact", {})
    assert r.status_code == 409


# --- П17: «тяжёлая» от базы ---------------------------------------------------------------------------------


async def test_session_with_big_base_is_woken_by_signal(env):
    """База 160 тыс., контекст 170 тыс., кэш остыл: по абсолютному порогу 150 тыс. она «тяжёлая», по базе —
    нет, и сигнал её поднимает."""
    env.scenario(turns=[[{"text": "проснулась"}]])
    host = await env.started()
    sid, pid, workdir = await _session(env, role=ROLE)
    await _subscribe(env, sid, workdir)
    usage = {"context_tokens": 170_000, "context_limit": None, "cost_usd": None, "updated_ts": 1}
    _touch(env, sid, usage_json=json.dumps({**usage, "base_tokens": 160_000}))
    await _peer(env, pid)
    await env.until(host, lambda: len(_users(env)) == 1, what="подъём по сигналу")
    await _turn_done(env, host, sid)
    assert (await _rec(env, sid))["wait_reason"] is None
    held = host.held
    cold = {"engine": "claude", "cache_expires_ts": None}
    assert held._heavy_cold({**cold, "context_tokens": 170_000}) is True  # без базы — абсолютный порог
    assert held._heavy_cold({**cold, "context_tokens": 170_000, "base_tokens": 160_000}) is False
    assert held._heavy_cold({**cold, "context_tokens": 200_001, "base_tokens": 160_000}) is True


# --- П18: работа сверх подписки -----------------------------------------------------------------------------


async def test_overage_holds_says_and_signals_like_pause(env):
    env.scenario(turns=[[{"text": "первый"}], [{"text": "после"}], [{"text": "сигнал"}]])
    host = await env.started()
    sid, pid, workdir = await _session(env, role=ROLE)
    await _subscribe(env, sid, workdir)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    # Настоящий путь: событие лимита движка с признаком переплаты (в том же процессе хоста).
    host.on_rate_limit({"status": "allowed", "isUsingOverage": True})
    assert "pauses" not in host.state and host.state["overage"]["until"] > host.held.now_ms()
    await _peer(env, pid)
    await _say(env, sid, "ты тут?")
    for _ in range(4):
        await env.cycle(host)
    rec = await _rec(env, sid)
    assert len(_users(env)) == 1 and rec["wait_reason"] == "paused" and rec["sub"]["pending"] == 1
    notes = [
        e["summary"] for e in await _events(env, sid) if e["kind"] == "status" and "подождёт" in e["summary"]
    ]
    assert len(notes) == 1 and notes[0].startswith("работа сверх подписки до ")
    # Событие без переплаты снимает её — реплика, затем сигнал.
    host.on_rate_limit({"status": "allowed"})
    await env.until(host, lambda: len(_users(env)) == 3, what="реплика и сигнал поданы")
    await _turn_done(env, host, sid)
    assert _text(_users(env)[1]) == "ты тут?" and _text(_users(env)[2]).startswith("Сигнал хаба")
    assert (await _rec(env, sid))["wait_reason"] is None


async def test_real_rate_limit_event_from_session_stream_pauses_held(env):
    """Пауза, выставленная настоящим `rate_limit_event` из потока процесса сессии, держит реплику."""
    reset = 4_000_000_000
    event = {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": reset}}
    env.scenario(turns=[[{"emit": event}, {"text": "первый"}], [{"text": "второй"}]])
    host = await env.started()
    sid, _pid, _workdir = await _session(env)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    assert host.state["pauses"]["claude"]["until"] > host.held.now_ms()
    await _say(env, sid, "ещё")
    for _ in range(4):
        await env.cycle(host)
    assert len(_users(env)) == 1 and (await _rec(env, sid))["wait_reason"] == "paused"


# --- П19: обход паузы «Поднять» -----------------------------------------------------------------------------


async def test_wake_bypasses_pause_for_current_queue_only(env):
    env.scenario(turns=[[{"text": "раз-два"}], [{"text": "сигнал"}], [{"text": "поздняя"}]])
    host = await env.started()
    sid, pid, workdir = await _session(env, role=ROLE)
    await _subscribe(env, sid, workdir)
    host.on_rate_limit({"status": "rejected"})
    await _say(env, sid, "первая")
    await _say(env, sid, "вторая")
    await _peer(env, pid)
    for _ in range(4):
        await env.cycle(host)
    assert env.calls() == [] and (await _rec(env, sid))["wait_reason"] == "paused"
    r = await env.ui("POST", f"/sessions/{sid}/wake", {})
    assert r.status_code == 200, r.text
    # Вся нынешняя очередь реплик и готовый сигнал — несмотря на паузу.
    # Ф55-1 (2.1): обе реплики — одним вводом.
    await env.until(host, lambda: len(_users(env)) == 2, what="две реплики и сигнал")
    await _turn_done(env, host, sid)
    texts = [_text(m) for m in _users(env)]
    assert texts[0] == "Реплика 1 из 2:\nпервая\n\nРеплика 2 из 2:\nвторая"
    assert texts[1].startswith("Сигнал хаба")
    assert _core_rec(env, sid).wake_requested_ts is None
    # Следующая реплика снова ждёт паузу.
    await _say(env, sid, "третья")
    for _ in range(4):
        await env.cycle(host)
    rec = await _rec(env, sid)
    assert len(_users(env)) == 2 and rec["wait_reason"] == "paused" and rec["queued_says"] == 1
    assert host.held.bypass_says == {} and host.held.bypass_signals == set()
    # Нет ни реплик, ни сигналов — «поднимать не на что».
    host.state["pauses"]["claude"]["until"] = 0
    await env.until(host, lambda: len(_users(env)) == 3, what="пауза кончилась")
    await _turn_done(env, host, sid)
    r = await env.ui("POST", f"/sessions/{sid}/wake", {})
    assert r.status_code == 409 and r.json()["detail"] == "поднимать не на что"
