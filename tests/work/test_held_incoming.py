"""Ф41-4а (docs/architecture.md; тесты
раздела 6 «Хост»): входящее постоянной сессии в хосте — подача сигналов, правило подъёма спящей, темп, места,
подъём ради подписки, пауза лимита, оборванный ход по сигналу.

Ядро — в процессе, `claude` — подставной (`fake_claude.py`). MCP-сервера хаба в подставном процессе нет:
подписку сессии тест оформляет ручкой ядра её условной тройкой — тем же запросом, что шлёт сервер."""

import asyncio
import json
import time

from hub.core import held as held_core
from hub.core import hub_sessions, security
from hub.work import held as held_mod
from hub.work import session

from .test_held_host import (
    _alive,
    _events,
    _full,
    _rec,
    _running,
    _say,
    _session,
    _state,
    _turn_done,
    _users,
)

ROLE = "маркетолог"
WANT = {"mode": "direct"}


def _token(env) -> str:
    if getattr(env, "mcp_token", None) is None:
        created = env.db.write_sync(
            lambda conn: security.create_api_token(
                conn,
                name="mcp:all",
                kind="interactive",
                scopes=["read", "draft"],
                project_ids=None,
                expires_ts=None,
            )
        )
        env.mcp_token = created.token
    return env.mcp_token


async def _subscribe(env, sid, workdir, **extra) -> int:
    """То, что делает MCP-сервер хаба внутри сессии при старте (`Hub.auto_subscribe`)."""
    pid, start, session_id = held_core.triple(sid)
    body = {
        "session": {
            "pid": pid,
            "proc_start": start,
            "session_id": session_id,
            "cwd": str(workdir),
            "kind": "hub",
            "name": None,
            "name_source": None,
        },
        "mode": "direct",
        **extra,
    }
    headers = {"Authorization": f"Bearer {_token(env)}", "Origin": "http://127.0.0.1:8787"}
    r = await env.client.post("/api/v1/subs", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["sub"]["id"]


async def _peer(env, pid, role=ROLE, text="секретный текст сообщения") -> int:
    """Сообщение владельца агенту из интерфейса — сигнал `peer` линии роли."""
    r = await env.ui("POST", "/peer-msgs", {"text": text, "to": {"project_id": pid, "role": role}})
    assert r.status_code == 201, r.text
    return r.json()["msg"]["id"]


async def _agent_peer(env, pid, workdir, role=ROLE) -> int:
    """Сообщение агента соседней линии проекта — обычный (не срочный) сигнал `peer` линии роли.

    Сообщение владельца (`_peer`) с Ф50-1 срочное: тяжёлую холодную сессию оно поднимает само."""
    sender, _pid, _workdir = await _session(env, project=(pid, workdir), role="программист")
    await _subscribe(env, sender, workdir)
    proc, start, session_id = held_core.triple(sender)
    body = {
        "text": "чужой текст агента",
        "to": {"project_id": pid, "role": role},
        "session": {"pid": proc, "proc_start": start, "session_id": session_id},
    }
    headers = {"Authorization": f"Bearer {_token(env)}", "Origin": "http://127.0.0.1:8787"}
    r = await env.client.post("/api/v1/peer-msgs", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["msg"]["id"]


def _no_debounce(env) -> None:
    """Часы теста настоящие: окно дебаунса (5 с от прошлой выдачи) снимаем в базе."""
    env.db.write_sync(lambda conn: conn.execute("UPDATE main.session_sub SET last_delivery_ts = NULL"))


def _touch(env, sid, **sets) -> None:
    env.db.write_sync(lambda conn: hub_sessions.touch(conn, sid, int(time.time() * 1000), **sets))


def _text(line: dict) -> str:
    return line["message"]["content"][0]["text"]


def _signal_events(events: list[dict]) -> list[dict]:
    return [e for e in events if e["kind"] == "status" and (e.get("brief") or {}).get("type") == "signal"]


def _warns(events: list[dict]) -> list[str]:
    """Предупреждения ленты, кроме «у проекта нет файла общих правил» (в тестовом доме его нет)."""
    return [e["summary"] for e in events if e["kind"] == "warn" and "правила сессии" not in e["summary"]]


def _core_rec(env, sid) -> held_core.HubSession:
    with env.db.reader() as conn:
        return held_core.get(conn, sid)


# --- подача сигнала -----------------------------------------------------------------------------------------


async def test_signal_fed_to_idle_session_as_numbers_only(env):
    env.scenario(turns=[[{"text": "первый"}], [{"text": "прочитала"}]])
    host = await env.started()
    sid, pid, workdir = await _session(env, role=ROLE)
    say_id = await _running(env, host, sid)
    await _turn_done(env, host, sid)
    [call] = env.calls()
    assert call["env"]["HUB_SESSION_SUB_PROFILE"] == "all"  # П10: подписывает один профиль
    sub_id = await _subscribe(env, sid, workdir)
    msg_id = await _peer(env, pid)
    await env.until(host, lambda: len(_users(env)) == 2, what="сигнал во вводе процесса")
    await _turn_done(env, host, sid)
    say_line, signal = _users(env)
    # Свой корень uuid: с репликой владельца не пересекается; `synthetic` процессу Claude Code не подаётся.
    assert say_line["uuid"] == session.held_say_uuid(say_id) != signal["uuid"]
    assert "synthetic" not in signal and "priority" not in signal
    text = _text(signal)
    # Та же строка, что печатает `hub sub wait` (вариант без «перезапусти ожидание»): числа и номера.
    assert text.startswith(f"Сигнал хаба по подписке #{sub_id} (проект {pid}): новое — сообщений владельца 1.")
    assert f"сообщение владельца из хаба #{msg_id} — hub_agent_inbox()" in text
    assert "ожидание" not in text and "sub wait" not in text
    assert "секретный текст" not in json.dumps(env.stdin(), ensure_ascii=False)  # чужой текст во ввод не идёт
    events = await _events(env, sid)
    [note] = _signal_events(events)
    assert note["summary"] == "сигнал хаба: сообщений владельца 1"
    assert note["brief"] == {
        "type": "signal",
        "cards": 0,
        "messages": 0,
        "reminders": 0,
        "agents": 0,
        "from_user": 1,
        "drafts": 0,
        "presses": 0,
        "asks": 0,  # Ф49-1: карточки других сессий, отданные этой как делегату
        "humans": 0,  # Ф54-1: исходы заявок «нужен человек»
        "group_ops": 0,  # Ф62-6: исходы операций с группами WhatsApp
        "channels": 0,
    }
    assert [e["summary"] for e in events if e["kind"] == "text"] == ["первый", "прочитала"]
    rec = _core_rec(env, sid)
    assert rec.state == "idle" and rec.signal_turn_ts is None and rec.signal_lost_ts is None
    assert host.held.sig_ready == {} and len(env.calls()) == 1
    # Один раз: дальше подавать нечего.
    for _ in range(3):
        await env.cycle(host)
    assert len(_users(env)) == 2


async def test_signal_waits_for_turn_end_card_and_say(env):
    ask = {"tool": "Bash", "input": {"command": "git push origin main"}}
    env.scenario(turns=[[{"ask": ask}, {"text": "первый"}], [{"text": "второй"}], [{"text": "третий"}]])
    host = await env.started()
    sid, pid, workdir = await _session(env, role=ROLE)
    await _subscribe(env, sid, workdir)
    first = await _running(env, host, sid, "раз")

    async def pending():
        return (await _full(env, sid))["requests"]

    [request] = await env.until(host, pending, what="карточка")
    await _peer(env, pid)
    second = await _say(env, sid, "два")
    for _ in range(4):
        await env.cycle(host)
    # Ход идёт и висит карточка: сигнал ждёт в ядре, во вводе только первая реплика.
    assert [m["uuid"] for m in _users(env)] == [session.held_say_uuid(first)]
    assert host.held.sig_ready == {sid: 1} and (await _rec(env, sid))["sub"]["pending"] == 1
    r = await env.ui("POST", f"/session-requests/{request['id']}/decide", {"decision": "deny"})
    assert r.status_code == 200
    await env.until(host, lambda: len(_users(env)) == 3, what="реплика и сигнал поданы")
    await _turn_done(env, host, sid)
    lines = _users(env)
    # Реплика владельца всегда раньше сигнала; сигнал — после конца хода.
    assert [m["uuid"] for m in lines[:2]] == [session.held_say_uuid(first), session.held_say_uuid(second)]
    assert _text(lines[2]).startswith("Сигнал хаба по подписке")
    texts = [e["summary"] for e in await _events(env, sid) if e["kind"] == "text"]
    assert texts == ["первый", "второй", "третий"]


# --- подъём спящей ------------------------------------------------------------------------------------------


async def test_asleep_session_woken_by_signal(env):
    env.scenario(turns=[[{"text": "проснулась"}]])
    host = await env.started()
    sid, pid, workdir = await _session(env, role=ROLE)
    await _subscribe(env, sid, workdir)
    for _ in range(3):
        await env.cycle(host)
    assert env.calls() == [] and host.held.runners == {}  # подписка сама никого не поднимает
    await _peer(env, pid)
    await env.until(host, lambda: len(_users(env)) == 1, what="подъём по сигналу и подача")
    await _turn_done(env, host, sid)
    [line] = _users(env)
    assert _text(line).startswith("Сигнал хаба по подписке")
    assert len(env.calls()) == 1 and (await _rec(env, sid))["wait_reason"] is None
    kinds = [e["kind"] for e in await _events(env, sid)]
    assert kinds.index("start") < kinds.index("status") < kinds.index("turn_end")


async def test_heavy_and_cold_waits_for_wake_button(env):
    env.scenario(turns=[[{"text": "подняли"}]])
    host = await env.started(held_wake_max_tokens=1000)
    sid, pid, workdir = await _session(env, role=ROLE)
    await _subscribe(env, sid, workdir)
    usage = {"context_tokens": 5000, "context_limit": 200_000, "cost_usd": None, "updated_ts": 1}
    _touch(env, sid, usage_json=json.dumps(usage))
    await _agent_peer(env, pid, workdir)
    for _ in range(5):
        await env.cycle(host)
    # Тяжёлая (контекст больше порога) и холодная (срока кэша нет): по сигналу не поднимается.
    rec = await _rec(env, sid)
    assert env.calls() == [] and rec["state"] == "asleep" and rec["wait_reason"] == "heavy"
    assert rec["sub"]["pending"] == 1  # сигнал ждёт в ядре, выданным не отмечен
    for _ in range(3):
        await env.cycle(host)
    assert (await _rec(env, sid))["wait_reason"] == "heavy"  # причина не снимается, пока сигнал ждёт (П3а)
    r = await env.ui("POST", f"/sessions/{sid}/wake", {})
    assert r.status_code == 200, r.text
    await env.until(host, lambda: len(_users(env)) == 1, what="«Поднять» подняло")
    await _turn_done(env, host, sid)
    rec = await _rec(env, sid)
    assert rec["wait_reason"] is None and rec["sub"]["pending"] == 0
    assert _core_rec(env, sid).wake_requested_ts is None
    assert _text(_users(env)[0]).startswith("Сигнал хаба по подписке")


async def test_heavy_cold_rule(env):
    host = await env.started(held_wake_max_tokens=1000)
    held = host.held
    now = held.now_ms()
    cases = [
        ({"engine": "claude", "context_tokens": 5000, "cache_expires_ts": None}, True),
        ({"engine": "claude", "context_tokens": 5000, "cache_expires_ts": now - 1}, True),
        ({"engine": "claude", "context_tokens": 5000, "cache_expires_ts": now + 60_000}, False),  # тёплая
        ({"engine": "claude", "context_tokens": 900, "cache_expires_ts": None}, False),  # лёгкая
        ({"engine": "claude", "context_tokens": None, "cache_expires_ts": None}, False),  # размер неизвестен
        ({"engine": "opencode", "context_tokens": 5000, "cache_expires_ts": None}, False),  # срока кэша нет
    ]
    for rec, expected in cases:
        assert held._heavy_cold(rec) is expected, rec


async def test_say_wakes_heavy_session_always(env):
    env.scenario(turns=[[{"text": "на месте"}], [{"text": "прочитала"}]])
    host = await env.started(held_wake_max_tokens=1000)
    sid, pid, workdir = await _session(env, role=ROLE)
    await _subscribe(env, sid, workdir)
    _touch(env, sid, usage_json=json.dumps({"context_tokens": 5000, "context_limit": None}))
    await _agent_peer(env, pid, workdir)
    for _ in range(3):
        await env.cycle(host)
    assert (await _rec(env, sid))["wait_reason"] == "heavy"
    await _running(env, host, sid, "проверь входящее")
    await env.until(host, lambda: len(_users(env)) == 2, what="после реплики подан и сигнал")
    await _turn_done(env, host, sid)
    assert _text(_users(env)[0]) == "проверь входящее"
    assert _text(_users(env)[1]).startswith("Сигнал хаба по подписке")
    assert (await _rec(env, sid))["wait_reason"] is None


async def test_wake_tempo_one_at_a_time(env):
    env.scenario(turns=[[{"text": "ок"}]])
    host = await env.started(held_signal_wake_gap_s=600, held_cold_wake_gap_s=600)
    project = env.project("Initech")
    a, pid, workdir = await _session(env, project=project, role="первая")
    b, _, _ = await _session(env, project=project, role="вторая")
    for sid in (a, b):
        await _subscribe(env, sid, workdir)
    await _peer(env, pid, role="первая")
    await _peer(env, pid, role="вторая")
    await env.until(host, lambda: len(_alive(host)) == 1, what="первый подъём")
    await env.until(host, lambda: len(_users(env)) == 1, what="первой сигнал подан")
    for _ in range(4):
        await env.cycle(host)
    # Второй подъём по сигналу — не раньше интервала на весь хост.
    assert len(env.calls()) == 1 and len(_alive(host)) == 1
    waiting = b if _alive(host) == [a] else a
    assert (await _rec(env, waiting))["state"] == "asleep" and host.held.sig_ready == {waiting: 1}
    host.held.last_signal_wake = float("-inf")  # интервал прошёл
    await env.until(host, lambda: len(_users(env)) == 2, what="второй подъём")
    assert len(env.calls()) == 2


async def test_signal_does_not_evict_idle_session(env):
    env.scenario(turns=[[{"text": "ок"}]])
    host = await env.started(held_max_parallel=1, held_signal_wake_gap_s=0, held_cold_wake_gap_s=0)
    project = env.project("Initech")
    a, pid, workdir = await _session(env, project=project)
    b, _, _ = await _session(env, project=project, role=ROLE)
    await _subscribe(env, b, workdir)
    await _running(env, host, a)
    await _turn_done(env, host, a)
    await _peer(env, pid)
    for _ in range(5):
        await env.cycle(host)
    # Места нет: сигнал простаивающую сессию не вытесняет — ждёт места.
    assert _alive(host) == [a] and host.held.runners[a].reason is None
    assert (await _rec(env, b))["wait_reason"] == "slot" and len(env.calls()) == 1
    # Реплика владельца вытесняет (прежнее правило); после неё подаётся и ждавший сигнал.
    await _say(env, b, "теперь ты")
    await env.until(host, lambda: _alive(host) == [b], what="вытеснение ради реплики")
    await env.until(host, lambda: len(_users(env)) == 3, what="реплика и сигнал")
    assert (await _rec(env, a))["state"] == "asleep"


# --- пустая и непонятная выдача ---------------------------------------------------------------------------


async def test_empty_delivery_feeds_nothing_and_does_not_spin(env):
    env.scenario(turns=[[{"text": "первый"}]])
    host = await env.started()
    sid, _pid, workdir = await _session(env, role=ROLE)
    await _subscribe(env, sid, workdir)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    posts = []
    real_post = host.core.post

    async def post(path, **kw):
        posts.append(path)
        return await real_post(path, **kw)

    host.core.post = post
    held = host.held
    held.sig_ready[sid] = 1  # хост «знает» о готовом, а к выдаче всё отсеялось
    await held.tick()
    await held.tick()
    assert posts.count(f"/work/host/sessions/{sid}/signals") == 1  # второй круг выдачу не зовёт
    assert len(_users(env)) == 1 and _signal_events(await _events(env, sid)) == []
    assert held.sig_empty[sid][0] == 1


async def test_unknown_signal_form_gives_constant_line(env):
    """П13: ответ выдачи не прошёл проверки клиента ожидания — событие `warn` и постоянная строка сессии."""
    env.scenario(turns=[[{"text": "первый"}], [{"text": "проверила"}]])
    host = await env.started()
    sid, _pid, workdir = await _session(env, role=ROLE)
    sub_id = await _subscribe(env, sid, workdir)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    real_post = host.core.post

    async def post(path, **kw):
        if path.endswith("/signals"):
            return {
                "sub_id": sub_id,
                "project_id": 1,
                "signals": [{"kind": "новый вид", "text": "ЧУЖОЙ ТЕКСТ"}],
                "chan_signals": [{"sub_id": "x"}],
            }
        return await real_post(path, **kw)

    host.core.post = post
    host.held.sig_ready[sid] = 2
    await env.until(host, lambda: len(_users(env)) == 2, what="постоянная строка подана")
    assert _text(_users(env)[1]) == held_mod.SIGNAL_FALLBACK_TEXT
    assert "ЧУЖОЙ ТЕКСТ" not in json.dumps(env.stdin(), ensure_ascii=False)
    events = await _events(env, sid)
    assert any(e["kind"] == "warn" and "сигналы хаба не разобраны: 2" in e["summary"] for e in events)


# --- П5: ход по сигналу, оборванный выключением ------------------------------------------------------------


async def test_signal_lost_line_after_interrupted_signal_turn(env):
    env.scenario(turns=[[{"text": "проверила входящее"}]])
    host = await env.started()
    sid, _pid, _workdir = await _session(env, role=ROLE)
    # Прошлый хост умер посреди хода по сигналу: ядро поставило признак.
    _touch(env, sid, signal_lost_ts=1)
    await env.until(host, lambda: len(_users(env)) == 1, what="подъём и постоянная строка")
    await _turn_done(env, host, sid)
    [line] = _users(env)
    assert _text(line) == held_mod.SIGNAL_LOST_TEXT
    rec = _core_rec(env, sid)
    assert rec.signal_lost_ts is None and rec.signal_turn_ts is None  # признак снят, ход закончен обычно
    events = await _events(env, sid)
    assert any(e["kind"] == "status" and "ход по сигналу был прерван" in e["summary"] for e in events)
    for _ in range(3):
        await env.cycle(host)
    assert len(_users(env)) == 1  # один раз


async def test_stale_result_after_resume_keeps_signal_turn(env):
    """10.10 (исследователь Umbrella): после подъёма движок сам закрыл пустой ход (`num_turns: 0`), пока
    строка «ход по сигналу был прерван» стояла у него в очереди. Этот конец — не конец хода по сигналу:
    второй обрыв подряд снова оставляет признак, сессия не остаётся спать молча."""
    env.scenario(stale_turn=True, turns=[[{"text": "начала"}, {"sleep": 30}]])
    host = await env.started()
    sid, _pid, _workdir = await _session(env, role=ROLE)
    _touch(env, sid, signal_lost_ts=1)
    await env.until(host, lambda: len(_users(env)) == 1, what="подъём и постоянная строка")

    async def _began() -> bool:
        return any(e["kind"] == "text" and "начала" in e["summary"] for e in await _events(env, sid))

    for _ in range(200):
        await env.cycle(host)
        if await _began():
            break
        await asyncio.sleep(0.05)
    assert await _began()
    rec = _core_rec(env, sid)
    assert rec.state == "running" and rec.signal_turn_ts is not None and rec.signal_lost_ts is None
    await host.held.runners[sid].sleep("autopilot_hold")  # второй обрыв: владелец поставил ограничение
    await env.until(host, _state(env, sid, "asleep"), what="сессия усыплена")
    rec = _core_rec(env, sid)
    assert rec.signal_lost_ts is not None and rec.signal_turn_ts is None


async def test_autopilot_hold_cut_turn_resumes_with_line(env):
    """10.10: поставленное ограничение автопилота обрывает ход по РЕПЛИКЕ — сессия не остаётся спать с
    брошенной работой: признак ставится, после подъёма подаётся постоянная строка."""
    env.scenario(turns=[[{"sleep": 30}]])
    host = await env.started()
    sid, _pid, _workdir = await _session(env, role=ROLE)
    await _say(env, sid, "поработай")
    await env.until(host, _state(env, sid, "running"), what="ход по реплике идёт")
    env.scenario(turns=[[{"text": "продолжила"}]])  # новый процесс читает сценарий заново
    await host.held.runners[sid].sleep("autopilot_hold")
    await env.until(host, lambda: len(_users(env)) == 2, what="подъём и постоянная строка")
    assert _text(_users(env)[1]) == held_mod.SIGNAL_LOST_TEXT
    await _turn_done(env, host, sid)
    assert _core_rec(env, sid).signal_lost_ts is None


async def test_autopilot_hold_idle_session_sleeps_quietly(env):
    host = await env.started()
    sid, _pid, _workdir = await _session(env, role=ROLE)
    await _say(env, sid, "привет")
    await env.until(host, lambda: len(_users(env)) == 1, what="реплика подана")
    await _turn_done(env, host, sid)
    await host.held.runners[sid].sleep("autopilot_hold")
    await env.until(host, _state(env, sid, "asleep"), what="сессия усыплена")
    assert _core_rec(env, sid).signal_lost_ts is None  # хода не было — поднимать не на что


async def test_host_killed_during_signal_turn_marks_lost(env):
    """Сквозной: сигнал подан, хост пропал посреди хода — новый хост после старта подаёт постоянную строку."""
    env.scenario(turns=[[{"sleep": 30}]])
    host = await env.started()
    sid, pid, workdir = await _session(env, role=ROLE)
    await _subscribe(env, sid, workdir)
    await _peer(env, pid)
    await env.until(host, lambda: len(_users(env)) == 1, what="сигнал подан")
    await env.until(host, _state(env, sid, "running"), what="ход по сигналу идёт")
    assert _core_rec(env, sid).signal_turn_ts is not None
    # «Выключение»: процесс убит, прежний хост ничего не сообщил; новый хост стартует с `recover`.
    runner = host.held.runners[sid]
    runner.dead = True
    await runner.kill()
    host.held.runners.clear()
    env.scenario(turns=[[{"text": "проверила"}]])
    host2 = await env.started()
    rec = _core_rec(env, sid)
    assert rec.state == "asleep" and rec.signal_lost_ts is not None and rec.signal_turn_ts is None
    await env.until(host2, lambda: len(_users(env)) == 2, what="строка «ход по сигналу был прерван»")
    await _turn_done(env, host2, sid)
    # Ф89-1 (П12): первой подачей нового процесса — строка о перезапуске службы, затем постоянная строка.
    restart, lost = (b["text"] for b in _users(env)[1]["message"]["content"])
    assert restart.startswith("<hub_restart>") and "(перезапуск службы сессий)" in restart
    assert lost == held_mod.SIGNAL_LOST_TEXT
    assert _core_rec(env, sid).signal_lost_ts is None
    # Сигнал сообщения отмечен выданным один раз — второй раз его строка не подаётся.
    assert sum(1 for m in _users(env) if _text(m).startswith("Сигнал хаба по подписке")) == 1


# --- П1, 3.3: подъём ради подписки --------------------------------------------------------------------------


async def test_sub_wake_without_turn_and_sleep_when_subscribed(env):
    host = await env.started(held_signal_wake_gap_s=0, held_sub_wait_s=30)
    sid, _pid, workdir = await _session(env, role=ROLE, want_sub=WANT)
    assert (await _rec(env, sid))["sub_stale"] is True
    await env.until(host, lambda: sid in _alive(host), what="подъём ради подписки")
    assert host.held.runners[sid].wake == "sub"
    assert _users(env) == []  # процесс поднят БЕЗ хода
    await _subscribe(env, sid, workdir)  # MCP-сервер хаба внутри сессии подписался
    await env.until(host, _state(env, sid, "asleep"), what="процесс усыплён после подписки")
    await env.until(host, lambda: sid not in host.held.runners, what="раннер снят")
    rec = await _rec(env, sid)
    assert rec["sub_stale"] is False and rec["sub"] is not None and rec["failure"] is None
    events = await _events(env, sid)
    assert any(e["kind"] == "status" and "поднимался ради подписки" in e["summary"] for e in events)
    assert _warns(events) == [] and not any(e["kind"] == "error" for e in events)
    for _ in range(3):
        await env.cycle(host)
    assert len(env.calls()) == 1 and _users(env) == []


async def test_sub_wake_gives_up_with_warn_once_per_want_value(env):
    host = await env.started(held_signal_wake_gap_s=0, held_sub_wait_s=0.3)
    sid, _pid, _workdir = await _session(env, role=ROLE, want_sub=WANT)
    await env.until(host, lambda: sid in _alive(host), what="подъём ради подписки")
    await env.until(host, _state(env, sid, "asleep"), what="подписки нет — процесс усыплён")
    await env.until(host, lambda: sid not in host.held.runners, what="раннер снят")
    events = await _events(env, sid)
    assert _warns(events) == [held_mod.SUB_MISSING_TEXT]
    for _ in range(4):
        await env.cycle(host)
    assert len(env.calls()) == 1  # одна попытка на значение `want_sub` за жизнь хоста
    # Владелец поправил фильтр — новая попытка.
    r = await env.ui("PATCH", f"/sessions/{sid}", {"want_sub": {"mode": "cards"}})
    assert r.status_code == 200, r.text
    await env.until(host, lambda: len(env.calls()) == 2, what="подъём после правки фильтра")


async def test_no_sub_wake_without_want_or_when_off(env):
    host = await env.started(held_signal_wake_gap_s=0, held_sub_wake=False)
    project = env.project("Initech")
    plain, _, _ = await _session(env, project=project)
    wanted, _, _ = await _session(env, project=project, role=ROLE, want_sub=WANT)
    for _ in range(4):
        await env.cycle(host)
    assert env.calls() == [] and host.held.runners == {}
    assert (await _rec(env, plain))["sub_stale"] is False and (await _rec(env, wanted))["sub_stale"] is True


# --- П8: пауза лимита и сигналы -----------------------------------------------------------------------------


async def test_limit_pause_holds_signals(env):
    env.scenario(turns=[[{"text": "первый"}], [{"text": "после паузы"}]])
    host = await env.started()
    sid, pid, workdir = await _session(env, role=ROLE)
    await _subscribe(env, sid, workdir)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    host.on_rate_limit({"status": "rejected"})
    await _peer(env, pid)
    for _ in range(4):
        await env.cycle(host)
    rec = await _rec(env, sid)
    # Сигнал ждёт в ядре: выдача не зовётся, живому процессу ничего не подаётся.
    assert len(_users(env)) == 1 and rec["wait_reason"] == "paused" and rec["sub"]["pending"] == 1
    assert not any("пауза лимита" in e["summary"] for e in await _events(env, sid))  # событие — про реплики
    host.state["pauses"]["claude"]["until"] = 0
    await env.until(host, lambda: len(_users(env)) == 2, what="пауза кончилась — сигнал подан")
    await _turn_done(env, host, sid)
    assert (await _rec(env, sid))["wait_reason"] is None


async def test_idle_close_spares_session_with_ready_signal(env):
    """П3в: сон по простою не усыпляет сессию, у которой есть готовые сигналы (здесь их держит пауза)."""
    env.scenario(turns=[[{"text": "первый"}]])
    host = await env.started()
    sid, _pid, _workdir = await _session(env, role=ROLE)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    held = host.held
    runner = held.runners[sid]
    host.config["held_idle_close_s"] = 0
    host.on_rate_limit({"status": "rejected"})
    held.sig_ready[sid] = 1
    await held.tick()
    assert runner.reason is None and runner.alive  # сигнал ждёт — простой не усыпляет
    held.sig_ready.clear()
    await held.tick()
    await env.until(host, lambda: sid not in held.runners, what="без сигналов простой усыпил")
    assert runner.reason == "idle"
