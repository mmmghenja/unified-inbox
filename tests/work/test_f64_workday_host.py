"""Ф64-1 (docs/architecture.md; тесты раздела 7 и конца
раздела 9): рабочий день проекта в хосте сессий — замок на подъём по сигналу и ради подписки, мягкий сон
после хода и фоновых задач, реплика владельца и «Поднять», уведомления молчат.

Ядро — в процессе, `claude` — подставной (`fake_claude.py`). День закрывает и открывает владелец ручкой
интерфейса."""

import asyncio

from hub.work import held as held_mod

from .test_held_bg import _feed, _started
from .test_held_host import _alive, _events, _rec, _running, _say, _session, _state, _turn_done, _users
from .test_held_incoming import ROLE, WANT, _agent_peer, _core_rec, _peer, _subscribe, _text, _touch


async def _workday(env, pid) -> None:
    r = await env.ui("PATCH", f"/projects/{pid}", {"workday": True})
    assert r.status_code == 200, r.text


async def _day(env, pid, open_: bool) -> dict:
    r = await env.ui("POST", f"/projects/{pid}/day", {"open": open_})
    assert r.status_code == 200, r.text
    return r.json()


async def _cycles(env, host, n: int = 5) -> None:
    for _ in range(n):
        await env.cycle(host)


def _sleep_notes(events: list[dict]) -> list[str]:
    return [e["summary"] for e in events if e["kind"] == "status" and "день проекта закрыт" in e["summary"]]


async def _closed_session(env, host, *, signals: int = 0):
    """Подписанная спящая сессия проекта «по времени»; день закрыт; `signals` сообщений агента накоплено."""
    sid, pid, workdir = await _session(env, role=ROLE)
    await _subscribe(env, sid, workdir)
    await _workday(env, pid)
    await _day(env, pid, False)
    for _ in range(signals):
        await _agent_peer(env, pid, workdir)
    return sid, pid, workdir


# --- замок: сигнал и подписка не поднимают ------------------------------------------------------------------


async def test_signal_does_not_wake_in_closed_day_and_open_wakes_with_all(env):
    env.scenario(turns=[[{"text": "утро"}]])
    host = await env.started(held_signal_wake_gap_s=0)
    sid, pid, workdir = await _closed_session(env, host, signals=2)
    await _cycles(env, host, 6)
    rec = await _rec(env, sid)
    # Сигналы ждут в ядре, процесс не поднят, причина ожидания не пишется (не «тяжёлая», не «пауза»).
    assert env.calls() == [] and sid not in host.held.runners
    assert rec["sub"]["pending"] >= 1 and rec["wait_reason"] is None
    assert host.held.sig_ready.get(sid)  # хост о них знает — и всё равно не поднимает
    events = await _events(env, sid)
    assert _sleep_notes(events) == [held_mod.DAY_CLOSED_NOTE]  # одна строка на закрытие, не на каждый сигнал
    await _agent_peer(env, pid, workdir)
    await _cycles(env, host, 4)
    assert _sleep_notes(await _events(env, sid)) == [held_mod.DAY_CLOSED_NOTE]
    # «Начать день» — обычный подъём по сигналам, всё накопленное одной подачей.
    opened = await _day(env, pid, True)
    assert opened["open"] is True
    await env.until(host, lambda: len(_users(env)) == 1, what="подъём после «Начать день»")
    await _turn_done(env, host, sid)
    [line] = _users(env)
    assert _text(line).startswith("Сигнал хаба по подписке") and "сообщений агентов 3" in _text(line)
    assert (await _rec(env, sid))["sub"]["pending"] == 0


async def test_without_workday_signal_wakes_as_before(env):
    """Без «Закончить день» замка нет (проверка теста выше на зубы: та же сессия поднимается сразу)."""
    env.scenario(turns=[[{"text": "проснулась"}]])
    host = await env.started(held_signal_wake_gap_s=0)
    sid, pid, workdir = await _session(env, role=ROLE)
    await _subscribe(env, sid, workdir)
    await _workday(env, pid)  # проект «по времени», но день открыт
    await _agent_peer(env, pid, workdir)
    await env.until(host, lambda: len(_users(env)) == 1, what="подъём по сигналу")


async def test_signal_lost_without_wake_button_does_not_wake(env):
    env.scenario(turns=[[{"text": "продолжила"}]])
    host = await env.started(held_signal_wake_gap_s=0)
    sid, pid, _workdir = await _closed_session(env, host)
    _touch(env, sid, signal_lost_ts=1)
    await _cycles(env, host, 6)
    assert env.calls() == [] and _core_rec(env, sid).signal_lost_ts is not None
    # «Поднять» владельца снимает замок: строка «ход по сигналу был прерван» подаётся.
    r = await env.ui("POST", f"/sessions/{sid}/wake", {})
    assert r.status_code == 200, r.text
    await env.until(host, lambda: len(_users(env)) == 1, what="«Поднять» подняло")
    await _turn_done(env, host, sid)
    assert _text(_users(env)[0]) == held_mod.SIGNAL_LOST_TEXT
    assert _core_rec(env, sid).signal_lost_ts is None


async def test_wake_button_feeds_signals_and_process_lives(env):
    env.scenario(turns=[[{"text": "подняли"}]])
    host = await env.started(held_signal_wake_gap_s=0)
    sid, pid, _workdir = await _closed_session(env, host, signals=1)
    await _cycles(env, host, 4)
    assert env.calls() == []
    r = await env.ui("POST", f"/sessions/{sid}/wake", {})
    assert r.status_code == 200, r.text
    await env.until(host, lambda: len(_users(env)) == 1, what="«Поднять» подняло и подало")
    await _turn_done(env, host, sid)
    assert _text(_users(env)[0]).startswith("Сигнал хаба по подписке")
    # Поднята словом владельца после закрытия — мягкий сон её не трогает.
    await _cycles(env, host, 6)
    assert sid in _alive(host) and host.held.runners[sid].reason is None


async def test_no_sub_wake_in_closed_day(env, monkeypatch):
    host = await env.started(held_signal_wake_gap_s=0, held_sub_wait_s=30)
    sid, pid, _workdir = await _session(env, role=ROLE, want_sub=WANT)
    await _workday(env, pid)
    await _day(env, pid, False)
    launches = []
    real = host.held._launch
    monkeypatch.setattr(
        host.held, "_launch", lambda rec, *a, **kw: launches.append(rec["id"]) or real(rec, *a, **kw)
    )
    await _cycles(env, host, 6)
    assert launches == []  # даже запуск не начинается (а не начинается и отменяется)
    assert env.calls() == [] and (await _rec(env, sid))["sub_stale"] is True
    await _day(env, pid, True)
    await env.until(host, lambda: sid in _alive(host), what="подъём ради подписки после «Начать день»")
    assert host.held.runners[sid].wake == "sub"


# --- мягкий сон: ход не прерывается -------------------------------------------------------------------------


async def test_idle_process_sleeps_when_day_closes(env):
    env.scenario(turns=[[{"text": "первый"}]])
    host = await env.started()
    sid, pid, _workdir = await _session(env, role=ROLE)
    await _workday(env, pid)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    runner = host.held.runners[sid]
    await _day(env, pid, False)
    await env.until(host, _state(env, sid, "asleep"), what="мягкий сон")
    assert runner.reason == "workday" and not runner.cut
    events = await _events(env, sid)
    assert held_mod.SLEEP_NOTES["workday"] in [e["summary"] for e in events if e["kind"] == "status"]
    assert _core_rec(env, sid).signal_lost_ts is None  # хода не было — утром поднимать не на что


async def test_running_turn_finishes_then_sleeps_and_gets_no_signals(env):
    env.scenario(turns=[[{"text": "начала"}, {"sleep": 1.5}, {"text": "доделала"}]])
    host = await env.started(held_signal_wake_gap_s=0)
    sid, pid, workdir = await _session(env, role=ROLE)
    await _subscribe(env, sid, workdir)
    await _workday(env, pid)
    await _say(env, sid, "поработай")
    await env.until(host, _state(env, sid, "running"), what="ход идёт")
    runner = host.held.runners[sid]
    await _day(env, pid, False)
    await _agent_peer(env, pid, workdir)
    await _peer(
        env, pid
    )  # сообщение владельца агенту — срочный сигнал: посреди хода в открытый день подался бы сразу
    await _cycles(env, host, 4)
    assert runner.busy and runner.reason is None  # ход не прерван
    await env.until(host, _state(env, sid, "asleep"), what="уснула после хода", limit_s=20)
    assert runner.reason == "workday" and not runner.cut
    texts = [e["summary"] for e in await _events(env, sid) if e["kind"] == "text"]
    assert texts == ["начала", "доделала"]
    assert len(_users(env)) == 1  # сигнал не подан ни посреди хода, ни после
    assert (await _rec(env, sid))["sub"]["pending"] >= 1
    assert _core_rec(env, sid).signal_lost_ts is None


async def test_background_tasks_hold_the_soft_sleep(env):
    env.scenario(turns=[[{"text": "раз"}]])
    host = await env.started()
    sid, pid, _workdir = await _session(env, role=ROLE)
    await _workday(env, pid)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    runner = host.held.runners[sid]
    await _feed(runner, _started("agent-a"))
    await _day(env, pid, False)
    await _cycles(env, host, 6)
    assert runner.alive and runner.reason is None  # подагент работает — сессия не усыпляется
    await _feed(
        runner,
        {
            "type": "system",
            "subtype": "task_notification",
            "task_id": "agent-a",
            "run_id": "r1",
            "status": "completed",
            "summary": "готово",
        },
    )
    await env.until(host, _state(env, sid, "asleep"), what="уснула после фоновой задачи")
    assert runner.reason == "workday"


async def test_say_after_close_wakes_and_process_is_not_put_to_sleep(env):
    env.scenario(turns=[[{"text": "ответила"}]])
    host = await env.started(held_signal_wake_gap_s=0)
    sid, pid, workdir = await _closed_session(env, host, signals=1)
    await _running(env, host, sid, "срочно глянь")
    await _turn_done(env, host, sid)
    runner = host.held.runners[sid]
    await _cycles(env, host, 6)
    # Поднята репликой владельца после закрытия: живёт до обычного простоя, сигналов не получает.
    assert runner.alive and runner.reason is None
    assert len(_users(env)) == 1 and not _text(_users(env)[0]).startswith("Сигнал хаба")
    assert (await _rec(env, sid))["sub"]["pending"] >= 1


async def test_process_after_close_with_ready_signals_sleeps_by_idle(env):
    env.scenario(turns=[[{"text": "ответила"}]])
    host = await env.started(held_signal_wake_gap_s=0)
    sid, pid, workdir = await _closed_session(env, host, signals=1)
    await _running(env, host, sid, "глянь")
    await _turn_done(env, host, sid)
    held = host.held
    runner = held.runners[sid]
    await _cycles(env, host, 3)
    assert held.sig_ready.get(sid)  # сигнал готов, но простой его не учитывает (П1)
    host.config["held_idle_close_s"] = 0
    await env.until(host, lambda: sid not in held.runners, what="простой усыпил")
    assert runner.reason == "idle"
    assert len(_users(env)) == 1


async def test_say_before_close_runs_its_turn_and_sleeps_after(env):
    """Реплика до закрытия: ход доделывается, потом мягкий сон (слова владельца после закрытия не было)."""
    env.scenario(turns=[[{"sleep": 1.0}, {"text": "сделала"}]])
    host = await env.started()
    sid, pid, _workdir = await _session(env, role=ROLE)
    await _workday(env, pid)
    await _say(env, sid, "сделай")
    await env.until(host, _state(env, sid, "running"), what="ход идёт")
    await _day(env, pid, False)
    await env.until(host, _state(env, sid, "asleep"), what="уснула после хода", limit_s=20)
    assert [e["summary"] for e in await _events(env, sid) if e["kind"] == "text"] == ["сделала"]


# --- запуск, начатый до закрытия ----------------------------------------------------------------------------


async def test_signal_launch_cancelled_when_day_closes_during_launch(env, monkeypatch):
    host = await env.started(held_signal_wake_gap_s=0)
    sid, pid, workdir = await _session(env, role=ROLE)
    await _subscribe(env, sid, workdir)
    await _workday(env, pid)
    held = host.held
    real = held._launch_steps
    closed = asyncio.Event()

    async def steps(runner, *args, **kwargs):
        # День закрывается, пока запуск по сигналу идёт: свежая запись «для хоста» несёт уже `day_closed_ts`.
        await _day(env, pid, False)
        closed.set()
        return await real(runner, *args, **kwargs)

    monkeypatch.setattr(held, "_launch_steps", steps)
    await _agent_peer(env, pid, workdir)
    await env.until(host, closed.is_set, what="запуск начался")
    await _cycles(env, host, 6)
    assert env.calls() == [] and sid not in _alive(host)
    assert _users(env) == []


# --- П4: уведомления молчат ---------------------------------------------------------------------------------


async def test_card_in_closed_day_gives_no_notification(env):
    ask = {"tool": "Bash", "input": {"command": "git push origin main"}}
    env.scenario(turns=[[{"ask": ask}, {"text": "дальше"}]])
    host = await env.started()
    sid, pid, _workdir = await _session(env, role=ROLE)
    await _workday(env, pid)
    await _day(env, pid, False)
    await _running(env, host, sid, "запушь")

    async def pending():
        return (await env.ui("GET", f"/sessions/{sid}")).json()["requests"]

    await env.until(host, pending, what="карточка")
    await _cycles(env, host, 4)
    await asyncio.sleep(0.2)
    assert env.notes == []  # Ф66-1: возврата от делегата больше нет


async def test_card_in_open_day_notifies(env):
    """Зубы теста выше: при открытом дне карточка уведомляет."""
    ask = {"tool": "Bash", "input": {"command": "git push origin main"}}
    env.scenario(turns=[[{"ask": ask}, {"text": "дальше"}]])
    host = await env.started()
    sid, pid, _workdir = await _session(env, role=ROLE)
    await _workday(env, pid)
    await _running(env, host, sid, "запушь")

    async def pending():
        return (await env.ui("GET", f"/sessions/{sid}")).json()["requests"]

    await env.until(host, pending, what="карточка")
    await env.until(host, lambda: len(env.notes) == 1, what="уведомление")
