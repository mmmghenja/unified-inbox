"""Ф50-1 + Ф50-2 (docs/architecture.md): сигналы посреди
хода в хосте — срочное занятой сессии сразу, обычное после порога и не чаще порога, запреты П5, зазор после
`result` (П4) и его срок, потеря строки (`mid_lost`) на всех путях обрыва, срочное спящей; строки журнала.

Ядро — в процессе, `claude` — подставной с очередью ввода (`"lifecycle": true`, как на пробах
`.runs/f50-probe`). Срочный сигнал — сообщение владельца агенту; обычный — сообщение агента другой линии."""

import asyncio
import json
import logging

from hub.core import held as held_core
from hub.work import held as held_mod
from hub.work import session

from .test_held_host import (
    _controls,
    _events,
    _interrupted,
    _rec,
    _running,
    _say,
    _session,
    _state,
    _turn_done,
    _users,
)
from .test_held_incoming import (
    ROLE,
    _core_rec,
    _no_debounce,
    _peer,
    _signal_events,
    _subscribe,
    _text,
    _token,
    _touch,
    _warns,
)

SENDER = "программист"
LONG = [{"sleep": 30}]
ORIGIN = {"Origin": "http://127.0.0.1:8787"}


async def _start(env, turns, *, stall=False, **config):
    """Сессия роли с подпиской и рядом — спящая линия-отправитель (от неё идут обычные сообщения)."""
    env.scenario(lifecycle=True, lifecycle_stall=stall, turns=turns)
    host = await env.started(**config)
    project = env.project("Initech")
    sid, pid, workdir = await _session(env, project=project, role=ROLE)
    await _subscribe(env, sid, workdir)
    return host, sid, pid, workdir


async def _sender(env, project) -> int:
    sender, _pid, workdir = await _session(env, project=project, role=SENDER)
    await _subscribe(env, sender, workdir)
    return sender


async def _agent_msg(env, pid, sender, *, role=ROLE, urgent=False, text="чужой текст агента") -> int:
    """Сообщение агента линии `role` от сессии-отправителя: без `urgent` — обычный сигнал."""
    proc, start, session_id = held_core.triple(sender)
    body = {
        "text": text,
        "to": {"project_id": pid, "role": role},
        "session": {"pid": proc, "proc_start": start, "session_id": session_id},
        "urgent": urgent,
    }
    headers = {"Authorization": f"Bearer {_token(env)}", **ORIGIN}
    r = await env.client.post("/api/v1/peer-msgs", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["msg"]["id"]


async def _busy(env, host, sid, text="долгое") -> int:
    say_id = await _running(env, host, sid, text)
    await env.until(host, lambda: host.held.runners[sid].busy, what="ход идёт")
    return say_id


def _texts(events: list[dict]) -> list[str]:
    return [e["summary"] for e in events if e["kind"] == "text"]


def _log(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name.startswith("hub.work")]


# --- срочное — сразу ----------------------------------------------------------------------------------------


async def test_urgent_signal_is_fed_mid_turn_and_read_inside_the_turn(env, caplog):
    caplog.set_level(logging.INFO, logger="hub.work")
    tool = {"tool": "Bash", "input": {"command": "sleep 2"}, "hold": 2.0}
    host, sid, pid, _ = await _start(env, [[tool, {"text": "готово"}]])
    say_id = await _busy(env, host, sid)
    runner = host.held.runners[sid]
    msg_id = await _peer(env, pid)
    await env.until(host, lambda: len(_users(env)) == 2, what="сигнал подан во время хода")
    # Подан занятой сессии, задолго до порога обычных сигналов (300 с); ход остался ходом по реплике.
    assert runner.busy and runner.turn_say_id == say_id and not runner.turn_by_signal
    assert runner.turn_origin is None and runner.signal_uuid is None
    rec = _core_rec(env, sid)
    assert (rec.state, rec.signal_turn_ts) == ("running", None)
    say_line, signal = _users(env)
    assert say_line["uuid"] == session.held_say_uuid(say_id) != signal["uuid"]
    assert "priority" not in signal and "synthetic" not in signal  # обычная строка: ход не прерывает
    text = _text(signal)
    assert text.startswith(held_mod.SIGNAL_MID_TEXT + "\n\nСигнал хаба по подписке")
    assert f"сообщение владельца из хаба #{msg_id}" in text
    assert "секретный текст" not in json.dumps(env.stdin(), ensure_ascii=False)
    await _turn_done(env, host, sid)
    # Строка вошла в идущий ход на границе инструмента: один ход, один итог, потери нет.
    assert runner.mid == {} and runner.mid_gap is None
    events = await _events(env, sid)
    [note] = _signal_events(events)
    assert note["summary"] == "сигнал хаба (во время хода): сообщений владельца 1"
    assert note["brief"]["from_user"] == 1
    assert [e["kind"] for e in events].count("turn_end") == 1 and _texts(events) == ["готово"]
    rec = _core_rec(env, sid)
    assert (rec.state, rec.signal_turn_ts, rec.signal_lost_ts) == ("idle", None, None)
    assert await _interrupted(env, sid) == [] and _warns(events) == []
    for _ in range(3):
        await env.cycle(host)
    assert len(_users(env)) == 2 and len(env.calls()) == 1
    # Ф50-2: журнал — начало и конец хода, подача сигнала; чужого текста нет.
    log = _log(caplog)
    assert f"work: сессия хаба {sid} — ход начат (реплика)" in log
    assert f"work: сессия хаба {sid} — сигнал подан во время хода: сообщений владельца 1; срочных 1" in log
    [end] = [line for line in log if f"сессия хаба {sid} — ход окончен" in line]
    assert end.endswith("шагов 1, готово")
    assert "секретный текст" not in "\n".join(log)


# --- обычное — после порога и не чаще порога ----------------------------------------------------------------


async def test_plain_signal_waits_for_threshold_and_is_not_fed_more_often(env):
    first_tool = {"tool": "Bash", "input": {"command": "a"}, "hold": 2.2}
    second_tool = {"tool": "Bash", "input": {"command": "b"}, "hold": 6.0}
    host, sid, pid, workdir = await _start(
        env, [[first_tool, second_tool, {"text": "готово"}]], held_midturn_after_s=1.5
    )
    sender = await _sender(env, (pid, workdir))
    await _busy(env, host, sid)
    runner = host.held.runners[sid]
    await _agent_msg(env, pid, sender)
    await env.cycle(host)
    await env.cycle(host)
    # Ход короче порога — обычный сигнал занятую сессию не тревожит (как раньше).
    assert len(_users(env)) == 1 and host.held.sig_ready == {sid: 1} and host.held.sig_urgent == {}
    await env.until(host, lambda: len(_users(env)) == 2, what="порог прошёл — подано")
    first_at = runner.mid_last
    assert first_at - runner.turn_started >= 1.5
    assert _text(_users(env)[1]).startswith(held_mod.SIGNAL_MID_TEXT)
    await env.until(host, lambda: runner.mid == {}, what="строка вошла в ход на границе инструмента")
    assert runner.busy
    # Второй обычный сигнал: с прошлой подачи посреди хода порог ещё не прошёл — ждёт.
    _no_debounce(env)
    await _agent_msg(env, pid, sender, text="второе")
    await env.until(host, lambda: host.held.sig_ready == {sid: 1}, what="второй сигнал готов")
    assert host.monotonic() - first_at < 1.5 and len(_users(env)) == 2
    assert host.held._mid_ok(runner, host.held.records[sid]) is False
    await env.until(host, lambda: len(_users(env)) == 3, what="порог от прошлой подачи прошёл")
    assert runner.mid_last - first_at >= 1.5 and runner.busy


async def test_urgent_ignores_threshold_of_plain_signals(env):
    host, sid, pid, workdir = await _start(env, [LONG], held_midturn_after_s=3600)
    sender = await _sender(env, (pid, workdir))
    await _busy(env, host, sid)
    plain = await _agent_msg(env, pid, sender)
    for _ in range(3):
        await env.cycle(host)
    assert len(_users(env)) == 1
    # Срочное забирает и накопленное обычное — одной подачей (П6).
    hot = await _agent_msg(env, pid, sender, urgent=True, text="блокер")
    await env.until(host, lambda: len(_users(env)) == 2, what="срочное подано")
    text = _text(_users(env)[1])
    assert f"#{plain}" in text and f"#{hot}" in text
    [note] = _signal_events(await _events(env, sid))
    assert note["summary"] == "сигнал хаба (во время хода): сообщений агентов 2"


# --- запреты П5 ---------------------------------------------------------------------------------------------


async def test_mid_feed_is_forbidden_in_every_p5_state(env, monkeypatch):
    host, sid, _pid, _ = await _start(env, [LONG])
    await _busy(env, host, sid)
    held = host.held
    runner = held.runners[sid]
    rec = held.records[sid]
    held.sig_ready, held.sig_urgent = {sid: 1}, {sid: 1}
    assert held._mid_ok(runner, rec) is True
    for name, value in (
        ("compacting", {"at": 0.0}),
        ("pending", {"r": {"core_id": 1}}),
        ("reason", "sleep"),
        ("interrupt_sent", 1.0),
        ("initialized", False),
        ("turn_closing", True),
        ("mid", {"u": "queued"}),
        ("mid_gap", 1.0),
        ("busy", False),
        ("engine", "opencode"),
        ("signal_uuid", "u"),  # обычный сигнал ещё стоит в очереди движка
    ):
        old = getattr(runner, name)
        setattr(runner, name, value)
        assert held._mid_ok(runner, rec) is False, name
        setattr(runner, name, old)
    assert held._mid_ok(runner, rec) is True
    held.sig_ready = {}
    assert held._mid_ok(runner, rec) is False  # готового нет

    # Остановка хоста, реплика владельца в очереди, просьба «усыпить», пауза лимита — отсеивает `_incoming`.
    fed: list[int] = []

    async def spy(runner, rec):
        fed.append(runner.sid)

    monkeypatch.setattr(held, "_feed_mid", spy)
    held.sig_ready = {sid: 1}
    await held._incoming(rec)
    assert fed == [sid]
    host.stopping = True
    await held._incoming(rec)
    host.stopping = False
    held.says[sid] = {99: {"id": 99, "priority": "next", "text": "реплика"}}
    await held._incoming(rec)
    held.says.pop(sid)
    await held._incoming({**rec, "sleep_requested_ts": 1})
    host.state["pauses"] = {"claude": {"until": held.now_ms() + 60_000}}
    await held._incoming(rec)
    host.state["pauses"] = {}
    assert fed == [sid]
    await held._incoming(rec)
    assert fed == [sid, sid]


# --- реплика владельца важнее: строка выбрасывается, потом «проверь входящее» (П3) -------------------------------


async def test_now_say_drops_queued_signal_and_goes_first_then_lost_line(env):
    host, sid, pid, _ = await _start(env, [LONG, [{"text": "срочное принято"}], [{"text": "проверила"}]])
    first = await _busy(env, host, sid)
    runner = host.held.runners[sid]
    await _peer(env, pid)
    await env.until(host, lambda: runner.mid and "queued" in runner.mid.values(), what="строка в очереди")
    urgent = await _say(env, sid, "срочно", priority="now")
    await env.until(host, lambda: len(_users(env)) == 4, what="реплика, затем «проверь входящее»")
    await _turn_done(env, host, sid)
    [interrupt] = _controls(env, "interrupt")
    assert interrupt["request"].get("cancel_queued") is True  # иначе движок начал бы ход по строке раньше
    lines = _users(env)
    assert [m["uuid"] for m in lines[:3:2]] == [session.held_say_uuid(i) for i in (first, urgent)]
    assert _text(lines[1]).startswith(held_mod.SIGNAL_MID_TEXT)
    assert _text(lines[3]) == held_mod.SIGNAL_LOST_TEXT
    events = await _events(env, sid)
    # Ход по реплике владельца — её собственный: текст ответа идёт раньше хода «проверь входящее».
    assert _texts(events) == ["срочное принято", "проверила"]
    assert await _interrupted(env, sid) == [first]  # учёт хода реплики не испорчен (Ф48-2)
    rec = _core_rec(env, sid)
    assert (rec.state, rec.signal_turn_ts, rec.signal_lost_ts) == ("idle", None, None)
    assert runner.mid == {} and not runner.mid_lost_due and len(env.calls()) == 1


# --- «Прервать» без реплики: движок начинает ход по строке — это ход по сигналу (П3, П4) --------------------


async def test_interrupt_button_then_engine_turn_by_queued_line_is_signal_turn(env, caplog):
    caplog.set_level(logging.INFO, logger="hub.work")
    host, sid, pid, _ = await _start(env, [LONG, [{"sleep": 1.0}, {"text": "прочитала"}]])
    first = await _busy(env, host, sid)
    runner = host.held.runners[sid]
    await _peer(env, pid)
    await env.until(host, lambda: "queued" in runner.mid.values(), what="строка в очереди")
    assert (await env.ui("POST", f"/sessions/{sid}/interrupt", {})).status_code == 200
    await env.until(host, lambda: _core_rec(env, sid).signal_turn_ts is not None, what="ход по сигналу")
    assert runner.busy and runner.turn_by_signal and runner.turn_say_id is None
    [interrupt] = _controls(env, "interrupt")
    assert "cancel_queued" not in interrupt["request"]
    await _turn_done(env, host, sid)
    events = await _events(env, sid)
    assert _texts(events) == ["прочитала"] and _warns(events) == []
    rec = _core_rec(env, sid)
    assert (rec.state, rec.signal_turn_ts, rec.signal_lost_ts) == ("idle", None, None)
    assert await _interrupted(env, sid) == [first]
    for _ in range(3):
        await env.cycle(host)
    assert len(_users(env)) == 2  # «проверь входящее» не нужно: строку сессия прочла своим ходом
    assert f"work: сессия хаба {sid} — ход начат (сигнал)" in _log(caplog)
    assert any(line.endswith("прервано") and "ход окончен" in line for line in _log(caplog))


# --- зазор после `result` (П4) ------------------------------------------------------------------------------


async def test_gap_after_result_keeps_session_busy_and_say_joins_the_signal_turn(env):
    turns = [[{"sleep": 1.2}, {"text": "первый"}], [{"sleep": 1.2}, {"text": "второй"}], [{"text": "третий"}]]
    host, sid, pid, _ = await _start(env, turns)
    first = await _busy(env, host, sid)
    runner = host.held.runners[sid]
    await _peer(env, pid)
    await env.until(host, lambda: len(_users(env)) == 2, what="сигнал подан во время хода")
    # Ф55-1 (2.2): реплика владельца занятой сессии — тоже строкой посреди хода, не дожидаясь его конца.
    second = await _say(env, sid, "два")
    await env.until(host, lambda: len(_users(env)) == 3, what="реплика подана во время хода")
    await env.until(host, lambda: _core_rec(env, sid).signal_turn_ts is not None, what="ход по строке")
    # Первый ход кончился без границы инструмента: строки остались в очереди движка, сессия не отпущена;
    # движок начал по ним один ход — ход по сигналу, реплика вошла в него же.
    assert runner.busy and runner.turn_by_signal and len(_users(env)) == 3
    await _turn_done(env, host, sid)
    for _ in range(3):
        await env.cycle(host)
    users = _users(env)
    assert users[0]["uuid"] == session.held_say_uuid(first)
    assert users[2]["uuid"] == session.held_say_mid_uuid(second, None, 1) and _text(users[2]) == "два"
    events = await _events(env, sid)
    assert _texts(events) == ["первый", "второй"] and _warns(events) == []
    assert [e["kind"] for e in events].count("turn_end") == 2 and len(_users(env)) == 3
    rec = _core_rec(env, sid)
    assert (rec.state, rec.signal_turn_ts, rec.signal_lost_ts) == ("idle", None, None)
    assert await _interrupted(env, sid) == [] and (await _rec(env, sid))["queued_says"] == 0


async def test_gap_timeout_frees_session_and_lost_line_follows(env, monkeypatch):
    monkeypatch.setattr(held_mod, "MID_GAP_S", 1.0)
    host, sid, pid, _ = await _start(env, [[{"sleep": 0.8}, {"text": "первый"}]], stall=True)
    await _busy(env, host, sid)
    runner = host.held.runners[sid]
    await _peer(env, pid)
    await env.until(host, lambda: len(_users(env)) == 2, what="сигнал подан во время хода")
    await env.until(host, lambda: runner.mid_gap is not None, what="зазор: ход кончился, строка не начата")
    # В зазоре сессия занята: вторая пачка сигналов и сон по простою не идут.
    assert runner.busy and host.held._mid_ok(runner, host.held.records[sid]) is False
    await env.until(host, lambda: len(_users(env)) == 3, what="срок вышел — «проверь входящее»")
    assert _text(_users(env)[2]) == held_mod.SIGNAL_LOST_TEXT
    assert runner.mid == {} and runner.mid_gap is None
    assert "сигнал, поданный во время хода, не прочитан — сессия проверит входящее" in _warns(
        await _events(env, sid)
    )


# --- потеря строки на остальных путях обрыва ----------------------------------------------------------------


async def test_sleep_with_queued_signal_drops_it_and_wakes_for_lost_line(env):
    host, sid, pid, _ = await _start(env, [LONG], held_signal_wake_gap_s=0)
    await _busy(env, host, sid)
    runner = host.held.runners[sid]
    await _peer(env, pid)
    await env.until(host, lambda: "queued" in runner.mid.values(), what="строка в очереди")
    assert (await env.ui("POST", f"/sessions/{sid}/sleep", {})).status_code == 200
    await env.until(host, lambda: len(env.calls()) == 2, what="уснула и поднята под «проверь входящее»")
    [interrupt] = _controls(env, "interrupt")
    assert interrupt["request"].get("cancel_queued") is True  # усыпляемой сессии ход по строке не нужен
    await env.until(host, lambda: len(_users(env)) == 3, what="строка «проверь входящее»")
    assert _text(_users(env)[2]) == held_mod.SIGNAL_LOST_TEXT
    # Новый процесс подставного claude читает сценарий с начала — его ход долгий; хватит того, что он начат.
    await env.until(host, lambda: _core_rec(env, sid).signal_lost_ts is None, what="потеря закрыта подачей")
    assert _core_rec(env, sid).signal_turn_ts is not None


async def test_process_exit_with_queued_signal_is_lost(env):
    host, sid, pid, _ = await _start(env, [[{"sleep": 1.5}, {"exit": 3}]], held_launch_retry_s=600)
    await _busy(env, host, sid)
    await _peer(env, pid)
    await env.until(host, lambda: len(_users(env)) == 2, what="сигнал подан во время хода")
    await env.until(host, _state(env, sid, "asleep"), what="процесс вышел")
    assert _core_rec(env, sid).signal_lost_ts is not None


async def test_line_not_written_is_reported_lost(env, monkeypatch):
    host, sid, pid, _ = await _start(env, [LONG])
    await _busy(env, host, sid)
    runner = host.held.runners[sid]

    async def broken(text, *, message_uuid):
        return False

    monkeypatch.setattr(runner, "send_signal_mid", broken)
    await _peer(env, pid)
    await env.until(host, lambda: _core_rec(env, sid).signal_lost_ts is not None, what="потеря названа ядру")
    assert held_mod.SIGNAL_NOT_SENT_TEXT in _warns(await _events(env, sid))
    assert len(_users(env)) == 1 and (await _rec(env, sid))["sub"]["pending"] == 0


# --- срочное спящей (П7) ------------------------------------------------------------------------------------


def _heavy(env, sid) -> None:
    usage = {"context_tokens": 5000, "context_limit": 200_000, "cost_usd": None, "updated_ts": 1}
    _touch(env, sid, usage_json=json.dumps(usage))


async def test_urgent_wakes_heavy_cold_session_and_plain_does_not(env, caplog):
    caplog.set_level(logging.INFO, logger="hub.work")
    host, sid, pid, workdir = await _start(env, [[{"text": "подняли"}]], held_wake_max_tokens=1000)
    sender = await _sender(env, (pid, workdir))
    _heavy(env, sid)
    await _agent_msg(env, pid, sender)
    for _ in range(5):
        await env.cycle(host)
    rec = await _rec(env, sid)
    assert env.calls() == [] and rec["wait_reason"] == "heavy"  # обычный сигнал — как раньше
    # Ф50-2: отказ в подъёме — в журнал один раз, а не каждым кругом.
    refusals = [line for line in _log(caplog) if "подъёма нет" in line]
    assert refusals == [
        f"work: сессия хаба {sid} — сигналы ждут, подъёма нет: тяжёлая и холодная (готово 1, срочных 0)"
    ]
    _no_debounce(env)
    await _agent_msg(env, pid, sender, urgent=True, text="блокер")
    await env.until(host, lambda: len(_users(env)) == 1, what="срочное подняло тяжёлую холодную")
    await _turn_done(env, host, sid)
    assert (await _rec(env, sid))["wait_reason"] is None and len(env.calls()) == 1


async def test_urgent_is_first_in_wake_tempo_but_does_not_bypass_it(env):
    env.scenario(lifecycle=True, turns=[[{"text": "ок"}]])
    host = await env.started(held_signal_wake_gap_s=600, held_cold_wake_gap_s=600)
    project = env.project("Initech")
    a, pid, workdir = await _session(env, project=project, role="первая")
    b, _, _ = await _session(env, project=project, role="вторая")
    c, _, _ = await _session(env, project=project, role="третья")
    for sid in (a, b, c):
        await _subscribe(env, sid, workdir)
    sender = await _sender(env, project)
    await _agent_msg(env, pid, sender, role="первая")
    await _agent_msg(env, pid, sender, role="вторая", urgent=True)
    await _agent_msg(env, pid, sender, role="третья", urgent=True)
    await env.until(host, lambda: len(env.calls()) == 1, what="первый подъём")
    for _ in range(4):
        await env.cycle(host)
    # Поднята сессия со срочным, хотя её номер старше; вторая срочная темп не обходит.
    [call] = env.calls()
    assert call["env"]["HUB_SESSION"] == str(b)
    assert sorted(host.held.sig_urgent) in ([c], [b, c]) and a in host.held.sig_ready


async def test_urgent_does_not_bypass_limit_pause(env):
    host, sid, pid, _ = await _start(env, [[{"text": "ок"}]])
    host.state["pauses"] = {"claude": {"until": host.held.now_ms() + 600_000}}
    await _peer(env, pid)
    for _ in range(5):
        await env.cycle(host)
    assert env.calls() == [] and host.held.sig_urgent == {sid: 1}
    assert (await _rec(env, sid))["wait_reason"] == "paused"


# --- ревью пачки Ф50: подача на конце хода и «Прервать» в зазоре --------------------------------------------


async def test_feed_does_not_overtake_the_end_of_turn_record(env, monkeypatch):
    """Подозрение ревью (2): выдача сигналов занятой сессии шла, а ход кончился и его конец ещё пишется в
    ядро (`turn_closing`). Подача хода по сигналу в этот миг обогнала бы запись `idle`: та легла бы поверх
    `running` и стёрла признак хода по сигналу. Подача ждёт конца записи."""
    turns = [[{"sleep": 1.0}, {"text": "первый"}], [{"sleep": 1.5}, {"text": "второй"}]]
    host, sid, pid, _ = await _start(env, turns)
    await _busy(env, host, sid)
    runner = host.held.runners[sid]
    taken, release_take = asyncio.Event(), asyncio.Event()
    flushing, release_flush = asyncio.Event(), asyncio.Event()
    real_take, real_flush = held_mod.HeldSessions._take, held_mod.HeldSessions.live_flush

    async def slow_take(self, session_id, *, mid):
        result = await real_take(self, session_id, mid=mid)
        if mid and not taken.is_set():
            taken.set()
            await release_take.wait()
        return result

    async def slow_flush(self, session_id):
        if not flushing.is_set():
            flushing.set()
            await release_flush.wait()
        await real_flush(self, session_id)

    monkeypatch.setattr(held_mod.HeldSessions, "_take", slow_take)
    monkeypatch.setattr(held_mod.HeldSessions, "live_flush", slow_flush)
    await _peer(env, pid)
    await host.apply(await host.core.get("/work/host/wait", params=host.wait_params(0)))
    tick = asyncio.create_task(host.tick())
    await asyncio.wait_for(taken.wait(), 10)  # сигналы выданы (ядро считает их поданными), ход ещё идёт
    await asyncio.wait_for(flushing.wait(), 10)  # ход кончился, его конец ещё не записан в ядро
    assert runner.turn_closing and not runner.busy and _core_rec(env, sid).state == "running"
    release_take.set()
    await asyncio.sleep(0.3)
    assert len(_users(env)) == 1 and not runner.busy  # в незаписанный конец хода ничего не подано
    release_flush.set()
    await asyncio.wait_for(tick, 10)
    await env.until(host, lambda: len(_users(env)) == 2, what="сигнал подан после конца хода")
    assert _text(_users(env)[1]).startswith("Сигнал хаба по подписке")
    for _ in range(3):
        await env.cycle(host)
    rec = _core_rec(env, sid)
    assert runner.busy and runner.turn_by_signal
    assert rec.state == "running" and rec.signal_turn_ts is not None  # признак хода по сигналу цел
    await _turn_done(env, host, sid)
    events = await _events(env, sid)
    assert _texts(events) == ["первый", "второй"] and _warns(events) == []
    rec = _core_rec(env, sid)
    assert (rec.state, rec.signal_turn_ts, rec.signal_lost_ts) == ("idle", None, None)
    for _ in range(3):
        await env.cycle(host)
    assert len(_users(env)) == 2  # «проверь входящее» не понадобилось


async def _in_gap_with_interrupt(env, monkeypatch, gap_s):
    """Зазор П4 (ход кончился, строка сигнала в очереди движка, хода по ней нет) и «Прервать» в нём."""
    monkeypatch.setattr(held_mod, "MID_GAP_S", gap_s)
    host, sid, pid, _ = await _start(env, [[{"sleep": 0.8}, {"text": "первый"}]], stall=True)
    await _busy(env, host, sid)
    runner = host.held.runners[sid]
    await _peer(env, pid)
    await env.until(host, lambda: len(_users(env)) == 2, what="сигнал подан во время хода")
    await env.until(
        host, lambda: runner.mid_gap is not None and runner.turn_by_signal, what="зазор: ждём ход по строке"
    )
    assert (await env.ui("POST", f"/sessions/{sid}/interrupt", {})).status_code == 200
    await env.until(host, lambda: runner.interrupt_sent is not None, what="хост послал interrupt")
    assert runner.interrupt_upto is not None and runner.mid_gap is not None
    return host, sid, runner


async def test_interrupt_in_the_gap_is_forgotten_when_the_gap_times_out(env, monkeypatch):
    """Подозрение ревью (3): «Прервать» пришлось на зазор, движок хода так и не начал — зазор закрыт по
    сроку. Посланный `interrupt` ни к какому ходу не относится: его след снят, иначе следующий ход был бы
    помечен «прервано», а подача посреди хода закрыта до его конца."""
    host, sid, runner = await _in_gap_with_interrupt(env, monkeypatch, 1.5)
    await env.until(host, lambda: runner.mid_gap is None, what="срок зазора вышел")
    assert runner.interrupt_sent is None and runner.interrupt_upto is None and not runner.busy
    assert runner._was_interrupted({}, host.monotonic()) is False  # noqa: SLF001
    await env.until(host, lambda: _core_rec(env, sid).interrupt_requested_ts is None, what="просьба снята")
    await env.until(host, lambda: len(_users(env)) == 3, what="«проверь входящее»")
    assert _text(_users(env)[2]) == held_mod.SIGNAL_LOST_TEXT
    assert runner.interrupt_sent is None and await _interrupted(env, sid) == []
    assert [e for e in await _events(env, sid) if e["kind"] == "interrupt"] == []


async def test_interrupt_in_the_gap_does_not_mark_the_turn_the_engine_then_starts(env, monkeypatch):
    """То же, но движок ход по строке начал: `interrupt`, посланный до начала хода, его не прерывал —
    конец этого хода не «прервано»; просьба владельца считается исполненной и снимется концом хода."""
    host, sid, runner = await _in_gap_with_interrupt(env, monkeypatch, 30.0)
    upto = runner.interrupt_upto
    [line] = list(runner.mid)
    await runner._mid_stage(line, "started")  # noqa: SLF001 — движок начал ход по строке из очереди
    assert runner.mid_gap is None and runner.busy and runner.turn_by_signal
    assert runner.interrupt_sent is None and runner.interrupt_upto == upto
    assert runner._was_interrupted({}, host.monotonic()) is False  # noqa: SLF001
    for _ in range(3):
        await env.cycle(host)
    assert len(_controls(env, "interrupt")) == 1  # второй раз хост ход не прерывает
