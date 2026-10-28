"""Хост постоянных сессий хаба (Ф41-2б-1б; docs/architecture.md с поправками 4а П1,
П3–П10, тесты раздела 7 и П15). Ядро — в процессе, `claude` — подставной (`fake_claude.py`): настоящий не
запускается никогда. Действия владельца — ручками UI (кука + CSRF), как в интерфейсе."""

import asyncio
import contextlib
import json
import os
import time

import pytest

from hub.agent.core_api import CoreUnavailable
from hub.core import held as held_core
from hub.core import hub_sessions, subs
from hub.work import files, session
from hub.work import held as held_mod
from hub.work import main as work_main

from .conftest import FORBIDDEN_ENV, GUARD_PATH, flag_value, pid_alive


async def _session(env, *, name="Initech", project=None, engine="claude", **body):
    pid, workdir = project or env.project(name)
    env.db.write_sync(lambda conn: subs.set_project_subs(conn, pid, True, now_ms=0))
    r = await env.ui("POST", "/sessions", {"project_id": pid, "engine": engine, **body})
    assert r.status_code == 200, r.text
    return r.json()["session"]["id"], pid, workdir


async def _say(env, sid, text="привет", priority="next") -> int:
    r = await env.ui("POST", f"/sessions/{sid}/say", {"text": text, "priority": priority})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _full(env, sid) -> dict:
    r = await env.ui("GET", f"/sessions/{sid}")
    assert r.status_code == 200, r.text
    return r.json()


async def _rec(env, sid) -> dict:
    return (await _full(env, sid))["session"]


async def _interrupted(env, sid) -> list[int]:
    """Ф48-2 (A): id прерванных реплик очереди сессии."""
    return [s["id"] for s in (await _full(env, sid))["says"] if s["status"] == "interrupted"]


async def _drop_interrupted(env, sid) -> list[int]:
    """Ф48-2 (A): «Убрать» все прерванные реплики — очередь за ними идёт дальше."""
    ids = await _interrupted(env, sid)
    for say_id in ids:
        r = await env.ui("DELETE", f"/sessions/{sid}/says/{say_id}")
        assert r.status_code == 200, r.text
    return ids


async def _events(env, sid) -> list[dict]:
    r = await env.ui("GET", f"/sessions/{sid}/events")
    assert r.status_code == 200, r.text
    return r.json()


def _alive(host) -> list[int]:
    return sorted(r.sid for r in host.held.runners.values() if r.alive and r.initialized)


def _state(env, sid, state):
    async def check():
        return (await _rec(env, sid))["state"] == state

    return check


def _has_event(env, sid, kind, text=""):
    async def check():
        return any(e["kind"] == kind and text in e["summary"] for e in await _events(env, sid))

    return check


def _users(env) -> list[dict]:
    return [m for m in env.stdin() if m.get("type") == "user"]


def _user_text(line: dict) -> str:
    return "".join(b["text"] for b in line["message"]["content"] if b.get("type") == "text")


def _controls(env, subtype) -> list[dict]:
    return [
        m
        for m in env.stdin()
        if m.get("type") == "control_request" and (m.get("request") or {}).get("subtype") == subtype
    ]


async def _running(env, host, sid, text="привет", **say_kw) -> int:
    """Реплика -> процесс поднят и ход идёт."""
    say_id = await _say(env, sid, text, **say_kw)
    await env.until(host, lambda: sid in _alive(host), what="процесс сессии")
    return say_id


async def _turn_done(env, host, sid) -> None:
    runner = host.held.runners[sid]
    await env.until(host, lambda: not runner.busy and runner.turn_say_id is None, what="конец хода")
    await env.until(host, _state(env, sid, "idle"), what="состояние idle")


# --- ленивый запуск, флаги, окружение, приёмник -------------------------------------------------------------


async def test_lazy_launch_flags_env_settings_and_feed(env, monkeypatch):
    (env.home / ".claude").mkdir()
    (env.home / ".claude" / "hub-rules.md").write_text("ОБЩЕЕ: пиши коротко", encoding="utf-8")
    env.scenario(turns=[[{"text": "смотрю"}, {"tool": "Bash", "input": {"command": "ls"}, "result": "a"}]])
    host = await env.started()
    sid, pid, workdir = await _session(env, role="программист", title="Главная")
    for _ in range(3):
        await env.cycle(host)
    assert env.calls() == [] and host.held.runners == {}  # создание сессии и старт хоста никого не поднимают
    assert (await _rec(env, sid))["state"] == "asleep"

    bound_before_start = []
    real_start = session.Process.start

    async def start(self):
        with env.db.reader() as conn:
            bound_before_start.append(held_core.get(conn, sid).engine_session_id)
        await real_start(self)

    monkeypatch.setattr(session.Process, "start", start)
    say_id = await _running(env, host, sid, "посмотри оплату")
    await _turn_done(env, host, sid)
    [call] = env.calls()
    argv = call["argv"]
    rec = await _rec(env, sid)
    # `bind` — до запуска: uuid выбрал хост
    assert bound_before_start == [rec["engine_session_id"]] and rec["engine_session_id"]
    assert flag_value(argv, "--session-id") == rec["engine_session_id"] and "--resume" not in argv
    for flag, value in (
        ("--input-format", "stream-json"),
        ("--permission-mode", "bypassPermissions"),
        ("--permission-prompt-tool", "stdio"),
        ("--model", "sonnet"),
    ):
        assert flag_value(argv, flag) == value
    assert call["cwd"] == str(workdir)
    # окружение: личность сессии хаба есть, признаков задачи по «Го» нет
    assert call["env"]["HUB_SESSION"] == str(sid)
    assert "HUB_WORK" not in call["env"] and "HUB_WORK_TASK" not in call["env"]
    assert not set(FORBIDDEN_ENV) & set(call["env"])
    # системная вставка: постоянная сессия, без worktree и «Го», сама не подписывается (П14)
    system = flag_value(argv, "--append-system-prompt")
    assert f"постоянная сессия хаба владельца #{sid}" in system and "роль «программист» 1" in system
    # Ф41-4: сигналы приходят на вход сами; подписку оформляет хаб по настройке владельца.
    assert "сама на проект не подписывайся" in system and "Сигнал хаба" in system
    assert "пока не приходят" not in system and "Команду ожидания не запускай" in system
    assert "worktree" not in system and "«Го»" not in system and "одобрил" not in system
    assert "Правила владельца для всех сессий хаба:\nОБЩЕЕ: пиши коротко" in system
    # настройки — в своём каталоге, гард с профилем held
    settings_file = files.held_settings_path(env.data_dir, sid)
    assert flag_value(argv, "--settings") == str(settings_file)
    assert settings_file.parent.name == f"held-{sid}"
    settings = json.loads(settings_file.read_text(encoding="utf-8"))
    [hook] = settings["hooks"]["PreToolUse"]
    command = hook["hooks"][0]["command"]
    # Ф50-3: набор снятых выключателей хук спрашивает у хоста по сокету; файла с набором нет.
    tail = f"--free-sock {host.held.free.name} --free-pid {os.getpid()} --free-session {sid}"
    assert command.startswith(GUARD_PATH) and command.endswith(tail)
    assert sorted(p.name for p in settings_file.parent.iterdir()) == ["settings.json"]
    assert "SendMessage" in settings["permissions"]["deny"]
    # реплика: одна строка user со своим uuid, отмечена доставленной
    [user] = _users(env)
    assert user["uuid"] == session.held_say_uuid(say_id) != session.say_uuid(say_id)
    assert user["message"]["content"][0]["text"] == "посмотри оплату"
    assert rec["queued_says"] == 0 and rec["state"] == "idle" and rec["last_turn_ts"]
    assert rec["failure"] is None and rec["wait_reason"] is None
    kinds = [(e["kind"], e["summary"]) for e in await _events(env, sid)]
    assert ("start", "запуск новой сессии") in kinds and ("text", "смотрю") in kinds
    assert any(k == "tool" and s == "ls" for k, s in kinds) and any(k == "turn_end" for k, s in kinds)
    assert not any(k == "error" for k, _s in kinds)
    host.save()
    state = files.load_state(env.data_dir)
    assert state["held"][str(sid)]["engine_session_id"] == rec["engine_session_id"]
    assert f"сессия хаба #{sid}" in work_main.status_text(data_dir=env.data_dir, now_s=time.time())


async def test_model_from_record_and_default(env):
    env.scenario(turns=[[{"text": "ок"}]])
    host = await env.started(held_model="haiku")
    project = env.project("Initech")
    own, _, _ = await _session(env, project=project, model="opus")
    default, _, _ = await _session(env, project=project)
    await _running(env, host, own)
    await _running(env, host, default)
    models = {c["env"]["HUB_SESSION"]: flag_value(c["argv"], "--model") for c in env.calls()}
    assert models == {str(own): "opus", str(default): "haiku"}


async def test_says_wait_for_the_turn_end_and_go_as_one_batch(env):
    """Ф55-1 (2.1): движок без событий очереди ввода — реплики ждут конца хода и уходят ОДНИМ вводом."""
    env.scenario(turns=[[{"sleep": 0.8}, {"text": "первый"}], [{"text": "второй"}], [{"text": "третий"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    first = await _running(env, host, sid, "раз")
    second = await _say(env, sid, "два")
    third = await _say(env, sid, "три")
    for _ in range(4):
        await env.cycle(host)
    # ход идёт: во ввод ушла только первая, очередь — в ядре (П4)
    assert [m["uuid"] for m in _users(env)] == [session.held_say_uuid(first)]
    assert (await _rec(env, sid))["queued_says"] == 2
    await env.until(host, lambda: len(_users(env)) == 2, what="пачка подана")
    await _turn_done(env, host, sid)
    for _ in range(3):
        await env.cycle(host)
    one, batch = _users(env)
    assert one["message"]["content"] == [{"type": "text", "text": "раз"}]  # одна реплика — без обёртки
    assert batch["uuid"] == session.held_say_uuid(second)
    assert batch["message"]["content"] == [
        {"type": "text", "text": "Реплика 1 из 2:\nдва\n\nРеплика 2 из 2:\nтри"}
    ]
    events = await _events(env, sid)
    assert [e["summary"] for e in events if e["kind"] == "text"] == ["первый", "второй"]  # два хода, не три
    # Ф55-1 (2.5): событие `say` — при доставке, по одному на реплику, в порядке очереди
    assert [(e["summary"], e["ref"]) for e in events if e["kind"] == "say"] == [
        ("владелец (next): раз", str(first)),
        ("владелец (next): два", str(second)),
        ("владелец (next): три", str(third)),
    ]
    assert len(env.calls()) == 1 and (await _rec(env, sid))["queued_says"] == 0
    assert third not in host.held.says.get(sid, {})


# --- карточки -----------------------------------------------------------------------------------------------


async def test_permission_card_decision_delivery(env):
    ask = {"tool": "Bash", "input": {"command": "git push origin main"}, "reason": "push"}
    env.scenario(turns=[[{"ask": ask}, {"text": "готово"}]])
    host = await env.started()
    sid, _, _ = await _session(env, title="Главная")

    async def pending():
        return (await _full(env, sid))["requests"]

    await _running(env, host, sid)
    [request] = await env.until(host, pending, what="карточка")
    assert request["kind"] == "permission" and request["tool"] == "Bash"
    assert request["input"] == ask["input"] and request["session_id"] == sid
    assert (await _rec(env, sid))["state"] == "running" and (await _rec(env, sid))["pending_requests"] == 1
    notice = ("Хаб · Initech", "просит Bash (сессия «Главная»)", "critical")
    await env.until(host, lambda: notice in env.notes, what="уведомление о карточке")
    # хост решить карточку сам не может
    r = await host.core._http.post(f"/session-requests/{request['id']}/decide", json={"decision": "allow"})  # noqa: SLF001
    assert r.status_code == 403
    r = await env.ui("POST", f"/session-requests/{request['id']}/decide", {"decision": "allow"})
    assert r.status_code == 200 and r.json()["status"] == "allowed"
    await env.until(host, lambda: env.responses(), what="решение у процесса")
    assert env.responses()[0]["response"]["behavior"] == "allow"
    await _turn_done(env, host, sid)
    summaries = [(e["kind"], e["summary"]) for e in await _events(env, sid)]
    assert ("request", f"просит Bash (запрос #{request['id']})") in summaries
    assert ("decision", "владелец разрешил: Bash") in summaries
    # событие вызова — раньше карточки
    kinds = [k for k, _s in summaries]
    assert kinds.index("tool") < kinds.index("request")
    assert (await _rec(env, sid))["pending_requests"] == 0


async def test_question_card_and_denial(env):
    question = {
        "questions": [{"question": "Какой стенд?", "options": [{"label": "dev"}, {"label": "stage"}]}]
    }
    env.scenario(
        turns=[
            [
                {"ask": {"tool": "AskUserQuestion", "input": question}},
                {"ask": {"tool": "Bash", "input": {"command": "ssh prod"}}},
            ]
        ]
    )
    host = await env.started()
    sid, _, _ = await _session(env)

    async def pending():
        return (await _full(env, sid))["requests"]

    await _running(env, host, sid)
    [request] = await env.until(host, pending, what="вопрос")
    assert request["kind"] == "question"
    r = await env.ui(
        "POST",
        f"/session-requests/{request['id']}/decide",
        {"decision": "allow", "answers": {"Какой стенд?": "dev"}},
    )
    assert r.json()["status"] == "answered"
    [second] = await env.until(
        host, lambda: _other(pending, request["id"]), what="карточка разрешения после ответа"
    )
    r = await env.ui(
        "POST", f"/session-requests/{second['id']}/decide", {"decision": "deny", "message": "не сейчас"}
    )
    assert r.json()["status"] == "denied"
    await env.until(host, lambda: len(env.responses()) == 2, what="оба решения у процесса")
    first, last = env.responses()
    assert first["response"]["updatedInput"]["answers"] == {"Какой стенд?": "dev"}
    assert last["response"] == {
        "behavior": "deny",
        "message": "не сейчас",
        "toolUseID": last["response"]["toolUseID"],
    }
    await _turn_done(env, host, sid)


async def _other(pending, known_id):
    return [r for r in await pending() if r["id"] != known_id]


# --- прерывание ---------------------------------------------------------------------------------------------


async def test_now_interrupts_foreign_turn_and_is_not_error(env):
    env.scenario(turns=[[{"sleep": 30}], [{"text": "срочное принято"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    first = await _running(env, host, sid, "долгое")
    await env.until(host, lambda: host.held.runners[sid].busy, what="ход идёт")
    urgent = await _say(env, sid, "срочно", priority="now")
    assert (await _rec(env, sid))["interrupt_requested"] is False  # П3а: `now` флага не ставит
    await env.until(host, lambda: len(_users(env)) == 2, what="срочная реплика подана")
    [interrupt] = _controls(env, "interrupt")
    assert "cancel_queued" not in interrupt["request"]  # П4: в движке очереди нет
    assert [m["uuid"] for m in _users(env)] == [session.held_say_uuid(i) for i in (first, urgent)]
    await _turn_done(env, host, sid)
    events = await _events(env, sid)
    kinds = [e["kind"] for e in events]
    assert kinds.count("interrupt") == 1 and "error" not in kinds
    assert [e["summary"] for e in events if e["kind"] == "interrupt"] == ["прервано"]
    assert not any(e["is_error"] for e in events)
    assert any(e["kind"] == "text" and e["summary"] == "срочное принято" for e in events)
    assert len(env.calls()) == 1  # процесс жив, контекст цел


async def test_now_for_asleep_and_idle_is_plain_say(env):
    env.scenario(turns=[[{"text": "раз"}], [{"text": "два"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    await _running(env, host, sid, "первая", priority="now")  # спящая: `now` = `next`
    await _turn_done(env, host, sid)
    await _say(env, sid, "вторая", priority="now")  # простаивающая: тоже
    await env.until(host, lambda: len(_users(env)) == 2, what="вторая реплика")
    await _turn_done(env, host, sid)
    assert _controls(env, "interrupt") == []
    assert "interrupt" not in [e["kind"] for e in await _events(env, sid)]


async def test_now_goes_ahead_of_older_next(env):
    env.scenario(turns=[[{"sleep": 30}], [{"text": "срочная принята"}], [{"text": "обычная принята"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    first = await _running(env, host, sid, "долгое")
    await env.until(host, lambda: host.held.runners[sid].busy, what="ход идёт")
    plain = await _say(env, sid, "обычная")
    urgent = await _say(env, sid, "срочная", priority="now")
    await env.until(host, lambda: len(_users(env)) == 2, what="срочная подана")
    await _turn_done(env, host, sid)
    # «прервать и написать» не ждёт ходов более старых обычных реплик. Ф48-2 (A): оборванная ею реплика
    # вернулась в начало очереди прерванной и держит обычную за собой — до решения владельца.
    assert [m["uuid"] for m in _users(env)] == [session.held_say_uuid(i) for i in (first, urgent)]
    assert await _interrupted(env, sid) == [first]
    assert await _drop_interrupted(env, sid) == [first]
    await env.until(host, lambda: len(_users(env)) == 3, what="обычная подана после «Убрать»")
    assert _users(env)[2]["uuid"] == session.held_say_uuid(plain)
    await _turn_done(env, host, sid)
    texts = [e["summary"] for e in await _events(env, sid) if e["kind"] == "text"]
    assert texts == ["срочная принята", "обычная принята"] and len(_controls(env, "interrupt")) == 1


async def test_two_now_in_a_row(env):
    """Ф55-1 (2.4): две срочные подряд — ход прерывается один раз, обе уходят одной пачкой."""
    env.scenario(turns=[[{"sleep": 30}], [{"text": "итог"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    await _running(env, host, sid, "долгое")
    await env.until(host, lambda: host.held.runners[sid].busy, what="ход идёт")
    a = await _say(env, sid, "срочно раз", priority="now")
    b = await _say(env, sid, "срочно два", priority="now")
    await env.until(host, lambda: len(_users(env)) == 2, what="обе срочные поданы")
    await _turn_done(env, host, sid)
    batch = _users(env)[1]
    assert batch["uuid"] == session.held_say_uuid(a)
    assert _user_text(batch) == "Реплика 1 из 2:\nсрочно раз\n\nРеплика 2 из 2:\nсрочно два"
    events = await _events(env, sid)
    assert len(await _interrupted(env, sid)) == 1  # Ф48-2 (A): оборванная «долгое» ждёт решения владельца
    assert [e["kind"] for e in events].count("interrupt") == 1 and len(_controls(env, "interrupt")) == 1
    assert any(e["summary"] == "итог" for e in events) and not any(e["kind"] == "error" for e in events)
    assert (await _rec(env, sid))["queued_says"] == 1 and b not in host.held.says.get(sid, {})


async def test_interrupt_button_clears_flag_conditionally(env):
    env.scenario(turns=[[{"sleep": 30}], [{"text": "дальше"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    await env.until(host, _state(env, sid, "running"), what="ход идёт")
    queued = await _say(env, sid, "следом")
    r = await env.ui("POST", f"/sessions/{sid}/interrupt")
    assert r.status_code == 200 and r.json()["session"]["interrupt_requested"] is True
    await env.until(host, _has_event(env, sid, "interrupt", "прервано"), what="прервано")
    # Ф48-2 (A): оборванная реплика вернулась прерванной; «Убрать» — следующая подаётся обычным порядком
    await env.until(host, lambda: _drop_interrupted(env, sid), what="прерванная реплика в очереди")
    await env.until(host, lambda: len(_users(env)) == 2, what="реплика после прерывания")
    assert _users(env)[1]["uuid"] == session.held_say_uuid(queued)
    await _turn_done(env, host, sid)
    rec = await _rec(env, sid)
    assert rec["interrupt_requested"] is False and rec["state"] == "idle"
    assert len(_controls(env, "interrupt")) == 1
    assert "error" not in [e["kind"] for e in await _events(env, sid)]


async def test_interrupt_on_turn_boundary_is_dropped_without_action(env):
    """П3в: просьба «прервать» старше начала текущего хода снимается без действия."""
    env.scenario(turns=[[{"sleep": 2}, {"text": "доделал"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    await env.until(host, _state(env, sid, "running"), what="ход идёт")
    r = await env.ui("POST", f"/sessions/{sid}/interrupt")
    assert r.status_code == 200
    runner = host.held.runners[sid]
    runner.turn_started_ms = int(time.time() * 1000) + 60_000  # будто этот ход начат уже после просьбы

    async def cleared():
        return (await _rec(env, sid))["interrupt_requested"] is False

    await env.until(host, cleared, what="просьба снята")
    assert _controls(env, "interrupt") == [] and runner.busy
    await _turn_done(env, host, sid)
    events = await _events(env, sid)
    assert "interrupt" not in [e["kind"] for e in events] and any(e["summary"] == "доделал" for e in events)


# --- сон ----------------------------------------------------------------------------------------------------


async def test_sleep_button_then_resume_by_next_say(env):
    env.scenario(turns=[[{"text": "раз"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    pid = env.calls()[0]["pid"]
    engine_sid = (await _rec(env, sid))["engine_session_id"]
    r = await env.ui("POST", f"/sessions/{sid}/sleep")
    assert r.json()["changed"] is True
    await env.until(host, _state(env, sid, "asleep"), what="усыплена")
    await env.until(host, lambda: sid not in host.held.runners, what="раннер снят")
    assert not pid_alive(pid)
    rec = await _rec(env, sid)
    assert rec["sleep_requested"] is False and rec["engine_session_id"] == engine_sid
    assert await _has_event(env, sid, "status", "сессия усыплена")()
    assert _controls(env, "interrupt") == []  # хода не было — прерывать нечего
    # следующая реплика поднимает тот же разговор
    await _say(env, sid, "ещё")
    await env.until(host, lambda: len(env.calls()) == 2, what="второй запуск")
    argv = env.calls()[1]["argv"]
    assert flag_value(argv, "--resume") == engine_sid and "--session-id" not in argv
    await env.until(host, lambda: len(_users(env)) == 2, what="реплика подана")
    await _turn_done(env, host, sid)
    assert await _has_event(env, sid, "start", "продолжение сессии (--resume)")()


async def test_sleep_during_turn_interrupts_and_drops_queue(env):
    env.scenario(turns=[[{"sleep": 30}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    await env.until(host, _state(env, sid, "running"), what="ход идёт")
    await _say(env, sid, "не успела")
    r = await env.ui("POST", f"/sessions/{sid}/sleep")
    assert r.json()["dropped_says"] == 1
    await env.until(host, _state(env, sid, "asleep"), what="усыплена")
    await env.until(host, lambda: sid not in host.held.runners, what="раннер снят")
    assert len(_controls(env, "interrupt")) == 1 and len(_users(env)) == 1  # снятая реплика не подана
    events = [(e["kind"], e["summary"]) for e in await _events(env, sid)]
    assert ("say", "не отправлено: сессия усыплена") in events and ("status", "сессия усыплена") in events
    assert not any(k == "error" for k, _s in events)
    for _ in range(3):
        await env.cycle(host)
    assert len(env.calls()) == 1 and (await _rec(env, sid))["queued_says"] == 0  # никто не поднялся


async def test_say_during_turn_then_interrupt_keeps_queue(env):
    env.scenario(turns=[[{"sleep": 30}], [{"text": "вторая принята"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    await env.until(host, _state(env, sid, "running"), what="ход идёт")
    await _say(env, sid, "посреди хода")
    await env.ui("POST", f"/sessions/{sid}/interrupt")
    await env.until(host, lambda: _drop_interrupted(env, sid), what="прерванная реплика в очереди")
    await env.until(host, _has_event(env, sid, "text", "вторая принята"), what="вторая реплика отработана")
    assert len(_users(env)) == 2


async def test_idle_close_sleeps_and_keeps_conversation(env):
    env.scenario(turns=[[{"text": "раз"}]])
    host = await env.started(held_idle_close_s=0.3)
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    await env.until(host, _state(env, sid, "asleep"), what="усыплена по простою")
    assert await _has_event(env, sid, "status", "простой — процесс закрыт")()
    assert (await _rec(env, sid))["engine_session_id"]


async def test_missing_transcript_is_failure_not_silent_new_conversation(env):
    env.scenario(turns=[[{"text": "раз"}]])
    host = await env.started(held_launch_retry_s=3600)
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    engine_sid = (await _rec(env, sid))["engine_session_id"]
    await env.ui("POST", f"/sessions/{sid}/sleep")
    await env.until(host, lambda: sid not in host.held.runners, what="раннер снят")
    [transcript] = held_mod.transcript_paths(env.home, engine_sid)
    (env.home / ".claude" / "projects" / "fake" / f"{engine_sid}.jsonl").unlink()
    assert transcript.endswith(f"{engine_sid}.jsonl")
    await _say(env, sid, "ещё")

    async def failed():
        return (await _rec(env, sid))["failure"]

    failure = await env.until(host, failed, what="причина у сессии")
    assert "стенограмма не найдена" in failure and len(env.calls()) == 1  # не запускался с тем же id заново
    assert (await _rec(env, sid))["engine_session_id"] == engine_sid


# --- подтверждение состояния (П1) ---------------------------------------------------------------------------


async def test_confirm_keeps_idle_session_alive_past_stale_threshold(env):
    env.scenario(turns=[[{"text": "раз"}]])
    host = await env.started(held_confirm_s=0)
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    later = time.time() + 130  # простой дольше 120 с при живом процессе
    env.app.state.clock = lambda: later
    await env.cycle(host)  # круг хоста подтверждает состояние
    touched = env.db.write_sync(lambda conn: hub_sessions.stale_to_asleep(conn, now_ms=int(later * 1000)))
    assert touched == [] and (await _rec(env, sid))["state"] == "idle" and sid in _alive(host)
    # хост молчит — ядро усыпляет само
    much_later = later + 130
    env.app.state.clock = lambda: much_later
    touched = env.db.write_sync(
        lambda conn: hub_sessions.stale_to_asleep(conn, now_ms=int(much_later * 1000))
    )
    assert touched == [sid]


# --- рестарт и остановка хоста ------------------------------------------------------------------------------


async def test_host_restart_recovers_and_does_not_resend_say(env, monkeypatch):
    ask = {"tool": "Bash", "input": {"command": "git push"}}
    env.scenario(turns=[[{"ask": ask}]])
    host = await env.started()
    sid, _, _ = await _session(env)

    async def not_marked(self, session_id, say_id):  # реплика во вводе, а отметка «доставлено» не дошла
        self.says.get(session_id, {}).pop(say_id, None)

    monkeypatch.setattr(held_mod.HeldSessions, "_mark_delivered", not_marked)
    say_id = await _say(env, sid, "сделай")

    async def pending():
        return (await _full(env, sid))["requests"]

    [request] = await env.until(host, pending, what="карточка")
    monkeypatch.undo()
    r = await env.ui("POST", f"/session-requests/{request['id']}/decide", {"decision": "allow"})
    assert r.status_code == 200  # решение принято, но прежний хост его уже не доставит
    runner = host.held.runners[sid]
    runner.reason = "dead"  # хост «упал»: процесс убит, ядру ничего не сказано
    await runner.kill()
    assert (await _rec(env, sid))["state"] == "running" and (await _rec(env, sid))["queued_says"] == 1

    env.scenario(turns=[[{"text": "новая"}]])
    await asyncio.sleep(0.01)
    fresh = await env.started()
    rec = await _rec(env, sid)
    assert rec["state"] == "asleep" and rec["pending_requests"] == 0
    events = [(e["kind"], e["summary"]) for e in await _events(env, sid)]
    assert any(k == "status" and "ход прерван остановкой хоста" in s for k, s in events)
    with env.db.reader() as conn:
        [status] = conn.execute(
            "SELECT status FROM main.held_request WHERE id = ?", (request["id"],)
        ).fetchone()
    assert status == "expired"  # и решённый, но не доставленный
    # реплика в очереди поднимает сессию, но во ввод второй раз не идёт: её uuid уже в стенограмме (П10)
    await env.until(fresh, lambda: len(env.calls()) == 2, what="подъём после рестарта")
    await env.until(fresh, lambda: sid in _alive(fresh), what="процесс")

    async def queue_empty():
        return (await _rec(env, sid))["queued_says"] == 0

    await env.until(fresh, queue_empty, what="реплика отмечена доставленной")
    assert [m["uuid"] for m in _users(env)] == [
        session.held_say_uuid(say_id)
    ]  # одна строка — от первого хоста
    # новая реплика после рестарта подаётся обычным порядком
    new = await _say(env, sid, "а теперь?")
    await env.until(fresh, lambda: len(_users(env)) == 2, what="новая реплика")
    assert _users(env)[1]["uuid"] == session.held_say_uuid(new)


async def test_transcript_search_goes_back_to_say_creation(tmp_path):
    """П10: uuid ищется от конца до записей старше создания реплики, а не «в хвосте»."""
    directory = tmp_path / ".claude" / "projects" / "-x"
    directory.mkdir(parents=True)
    target = "11111111-1111-1111-1111-111111111111"
    lines = [json.dumps({"type": "user", "uuid": "old", "timestamp": "2026-10-08T09:00:00.000Z"})]
    lines.append(json.dumps({"type": "user", "uuid": target, "timestamp": "2026-10-08T10:00:00.000Z"}))
    lines += [
        json.dumps({"type": "assistant", "uuid": f"a{i}", "timestamp": "2026-10-08T10:05:00.000Z"})
        for i in range(5000)
    ]
    (directory / "conv.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    created = 1791453600000  # 2026-10-08T10:00:00Z
    assert held_mod.transcript_has(tmp_path, "conv", target, created)
    assert not held_mod.transcript_has(tmp_path, "conv", "22222222-2222-2222-2222-222222222222", created)
    assert not held_mod.transcript_has(tmp_path, "нет", target, created)
    assert held_mod.transcript_paths(tmp_path, "../conv") == []


async def test_host_shutdown_puts_live_sessions_asleep(env):
    env.scenario(turns=[[{"text": "раз"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    pid = env.calls()[0]["pid"]
    await host.shutdown()
    assert not pid_alive(pid)
    assert (await _rec(env, sid))["state"] == "asleep"
    assert await _has_event(env, sid, "status", "остановка хоста")()
    assert host.held.runners == {}


# --- закрытие -----------------------------------------------------------------------------------------------


async def test_close_kills_process_and_410_drops_runner(env):
    env.scenario(turns=[[{"sleep": 30}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    await env.until(host, _state(env, sid, "running"), what="ход идёт")
    pid = env.calls()[0]["pid"]
    r = await env.ui("POST", f"/sessions/{sid}/close")
    assert r.json()["closed"] is True
    await env.until(host, lambda: sid not in host.held.runners and not pid_alive(pid), what="процесс погашен")
    assert sid not in host.held.records and sid not in host.held.buffers


async def test_410_from_core_drops_runner_without_wait_snapshot(env):
    env.scenario(turns=[[{"sleep": 30}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    pid = env.calls()[0]["pid"]
    await env.ui("POST", f"/sessions/{sid}/close")
    runner = host.held.runners[sid]
    runner.event("text", "хвост после закрытия")
    await host.held.flush()  # ядро отвечает 410 — раннер гасится и без известия из ожидания
    await env.until(host, lambda: not pid_alive(pid), what="процесс погашен")
    assert sid not in host.held.runners


# --- неудачные запуски --------------------------------------------------------------------------------------


async def test_three_failed_launches_then_only_new_say(env):
    host = await env.started(claude_path=str(env.tmp / "нет-такого"), held_launch_retry_s=0)
    sid, _, _ = await _session(env)
    await _say(env, sid)

    async def fails():
        return (await _rec(env, sid))["failure"] is not None and len(await _errors(env, sid)) >= 3

    await env.until(host, fails, what="три неудачи")
    for _ in range(5):
        await env.cycle(host)
    rec = await _rec(env, sid)
    assert "claude не запустился" in rec["failure"] and rec["state"] == "asleep"
    assert len(await _errors(env, sid)) == 3 and rec["queued_says"] == 1  # больше не пробует
    assert host.held.records[sid]["launch_fails"] == 3
    # новая реплика владельца — новая попытка (ядро обнулило счётчик)
    env.write_config(held_launch_retry_s=0)
    host.config["claude_path"] = str(env.claude)
    env.scenario(turns=[[{"text": "жив"}], [{"text": "жив"}]])
    await _say(env, sid, "ещё раз")
    await env.until(host, lambda: sid in _alive(host), what="запуск после новой реплики")
    await env.until(host, lambda: len(_users(env)) == 1, what="обе реплики поданы")
    await _turn_done(env, host, sid)
    assert _user_text(_users(env)[0]).startswith("Реплика 1 из 2:\n")  # Ф55-1: поднятой — одной пачкой
    rec = await _rec(env, sid)
    assert rec["failure"] is None and rec["queued_says"] == 0


async def _errors(env, sid):
    return [e for e in await _events(env, sid) if e["kind"] == "error"]


async def test_wrong_permission_mode_is_session_failure(env):
    env.scenario(initialize_mode="default")
    host = await env.started(held_launch_retry_s=3600)
    sid, _, _ = await _session(env)
    await _say(env, sid)

    async def failed():
        return (await _rec(env, sid))["failure"]

    failure = await env.until(host, failed, what="причина у сессии")
    assert "режим разрешений default" in failure
    rec = await _rec(env, sid)
    assert rec["state"] == "asleep" and rec["queued_says"] == 1
    await env.until(host, lambda: sid not in host.held.runners, what="раннер снят")
    assert not pid_alive(env.calls()[0]["pid"])


async def test_auth_refusal_is_session_failure(env):
    host = env.host(held_launch_retry_s=3600)
    host._auth_checker = lambda path, environ: (False, "claude не по подписке")  # noqa: SLF001
    await host.start()
    sid, _, _ = await _session(env)
    await _say(env, sid)

    async def failed():
        return (await _rec(env, sid))["failure"]

    assert await env.until(host, failed, what="причина у сессии") == "claude не по подписке"
    assert env.calls() == []
    assert host.state["auths"]["claude"]["ok"] is False  # запись входа — на движок (П6)


# --- места --------------------------------------------------------------------------------------------------


async def test_slot_puts_longest_idle_session_asleep(env):
    env.scenario(turns=[[{"text": "ок"}]])
    host = await env.started(held_max_parallel=2)
    project = env.project("Initech")
    a, _, _ = await _session(env, project=project)
    b, _, _ = await _session(env, project=project)
    c, _, _ = await _session(env, project=project)
    await _running(env, host, a)
    await _turn_done(env, host, a)
    await _running(env, host, b)
    await _turn_done(env, host, b)
    await _running(env, host, c)  # места нет — усыпляется простаивающая дольше всех (a)
    await _turn_done(env, host, c)
    assert _alive(host) == [b, c]
    assert (await _rec(env, a))["state"] == "asleep" and (await _rec(env, c))["wait_reason"] is None
    assert await _has_event(env, a, "status", "место понадобилось другой сессии")()


async def test_slot_waits_while_everyone_is_in_a_turn(env):
    env.scenario(sessions={"1": {"turns": [[{"sleep": 1.5}]]}, "2": {"turns": [[{"text": "дождалась"}]]}})
    host = await env.started(held_max_parallel=1)
    project = env.project("Initech")
    a, _, _ = await _session(env, project=project)
    b, _, _ = await _session(env, project=project)
    assert (a, b) == (1, 2)
    await _running(env, host, a)
    await env.until(host, _state(env, a, "running"), what="ход идёт")
    await _say(env, b)

    async def waits():
        return (await _rec(env, b))["wait_reason"] == "slot"

    await env.until(host, waits, what="ждёт места")
    assert len(env.calls()) == 1 and (await _rec(env, b))["queued_says"] == 1
    await env.until(host, _has_event(env, b, "text", "дождалась"), what="место освободилось")
    rec = await _rec(env, b)
    assert rec["wait_reason"] is None and (await _rec(env, a))["state"] == "asleep"


# --- движки, пауза --------------------------------------------------------------------------------------


async def test_engine_switch_and_pause_do_not_touch_held(env):
    env.scenario(turns=[[{"text": "ок"}]])
    host = await env.started()
    project = env.project("Initech")
    a, _, _ = await _session(env, project=project)
    b, _, _ = await _session(env, project=project)
    await _running(env, host, a)
    await _turn_done(env, host, a)
    # переключатель движка хоста постоянные сессии не гасит: движок — свойство сессии
    await host.engine_changed("opencode")
    for _ in range(3):
        await env.cycle(host)
    host.engine = "opencode"
    assert _alive(host) == [a] and host.held.runners[a].engine == "claude"
    # пауза лимита Claude. Ф41-4 П8 (решение владельца, отменяет П6 спеки 2б):
    # реплика владельца на паузе процесс не запускает и живому не подаётся — ждёт в очереди с причиной `paused`.
    host.on_rate_limit({"status": "rejected"})
    assert host.state["pauses"]["claude"]["until"] > 0 and "pause" not in host.state  # Ф78-1: только `pauses`
    await _say(env, b, "после паузы")
    live_say = await _say(env, a, "живой — тоже после паузы")
    for _ in range(4):
        await env.cycle(host)
    assert _alive(host) == [a] and len(_users(env)) == 1  # ни запуска, ни подачи живому процессу
    for sid in (a, b):
        rec = await _rec(env, sid)
        assert rec["wait_reason"] == "paused" and rec["queued_says"] == 1
        notes = [e["summary"] for e in await _events(env, sid) if "пауза лимита подписки до" in e["summary"]]
        assert len(notes) == 1 and notes[0].endswith("— реплика подождёт")  # одно событие на эпизод
    # пауза кончилась — причина снимается, реплики подаются обычным порядком
    host.state["pauses"]["claude"]["until"] = 0
    await env.until(host, lambda: b in _alive(host), what="процесс сессии после паузы")
    await _turn_done(env, host, b)
    await _turn_done(env, host, a)
    assert _alive(host) == [a, b] and (await _rec(env, b))["wait_reason"] is None
    assert (await _rec(env, a))["wait_reason"] is None and (await _rec(env, a))["queued_says"] == 0
    assert session.held_say_uuid(live_say) in [m["uuid"] for m in _users(env)]
    text = work_main.status_text(data_dir=env.data_dir, now_s=time.time())
    assert "ПАУЗА" not in text  # пауза кончилась


# --- «одна сессия — один процесс» (П9) ----------------------------------------------------------------------


async def test_session_open_elsewhere_is_not_launched(env, monkeypatch):
    env.scenario(turns=[[{"text": "раз"}], [{"text": "два"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    assert not (env.bin_dir / "agents_calls.jsonl").exists()  # первый запуск: разговора ещё нет
    engine_sid = (await _rec(env, sid))["engine_session_id"]
    await env.ui("POST", f"/sessions/{sid}/sleep")
    await env.until(host, _state(env, sid, "asleep"), what="усыплена")
    await env.until(host, lambda: sid not in host.held.runners, what="раннер снят")
    (env.bin_dir / "agents.json").write_text(
        json.dumps([{"pid": 4242, "sessionId": engine_sid, "kind": "interactive", "status": "idle"}])
    )
    await _say(env, sid, "ещё")

    async def elsewhere():
        return (await _rec(env, sid))["wait_reason"] == "elsewhere"

    await env.until(host, elsewhere, what="открыта в другом окне")
    monkeypatch.setattr(held_mod, "ELSEWHERE_RECHECK_S", 0.0)
    # Проверка «другого окна» — подпроцесс в задаче запуска, круг её не ждёт (19.10: с быстрым чтением ядра
    # пять кругов проходили раньше, чем подпроцесс успевал ответить) — ждём повтора по числу вызовов.
    await env.until(
        host,
        lambda: len(env._jsonl("agents_calls.jsonl")) >= 3,  # noqa: SLF001
        what="проверка окна повторилась",
    )
    warns = [e for e in await _events(env, sid) if e["kind"] == "warn" and "в другом окне" in e["summary"]]
    assert [e["summary"] for e in warns] == ["сессия открыта в другом окне (pid 4242) — закрой его"]
    assert len(env.calls()) == 1 and (await _rec(env, sid))["queued_says"] == 1
    assert len(env._jsonl("agents_calls.jsonl")) >= 2  # noqa: SLF001 — проверка повторялась, предупреждение одно
    # окно закрыли — реплика поднимает сессию
    (env.bin_dir / "agents.json").write_text("[]")
    await env.until(host, lambda: len(env.calls()) == 2, what="запуск после закрытия окна")
    await env.until(host, lambda: len(_users(env)) == 2, what="реплика подана")
    await _turn_done(env, host, sid)
    assert (await _rec(env, sid))["wait_reason"] is None


async def test_broken_agents_output_does_not_block_launch(env, caplog):
    env.scenario(turns=[[{"text": "раз"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    await env.ui("POST", f"/sessions/{sid}/sleep")
    await env.until(host, lambda: sid not in host.held.runners, what="раннер снят")
    (env.bin_dir / "agents.json").write_text(json.dumps({"raw": "не json", "rc": 0}))
    with caplog.at_level("WARNING", logger="hub.work"):
        await _say(env, sid, "ещё")
        await env.until(host, lambda: len(env.calls()) == 2, what="запуск идёт")
    assert any("вывод claude agents не разобран" in r.getMessage() for r in caplog.records)
    assert (await _rec(env, sid))["wait_reason"] is None


# --- журнал хоста — без текстов -----------------------------------------------------------------------------


async def test_host_log_has_no_texts(env, caplog):
    secret = "СЕКРЕТНЫЙ-ТЕКСТ-РЕПЛИКИ"
    env.scenario(
        turns=[[{"text": "ОТВЕТ-МОДЕЛИ-ТЕКСТ"}, {"ask": {"tool": "Bash", "input": {"command": "x"}}}]]
    )
    host = await env.started()
    sid, _, _ = await _session(env, title="ТАЙНОЕ-НАЗВАНИЕ")
    with caplog.at_level("DEBUG", logger="hub.work"):
        await _running(env, host, sid, secret)

        async def pending():
            return (await _full(env, sid))["requests"]

        await env.until(host, pending, what="карточка")
        await env.ui("POST", f"/sessions/{sid}/close")
        await env.until(host, lambda: sid not in host.held.runners, what="закрыта")
    log = "\n".join(r.getMessage() for r in caplog.records)
    assert f"сессия хаба {sid}" in log
    for text in (secret, "ОТВЕТ-МОДЕЛИ-ТЕКСТ", "ТАЙНОЕ-НАЗВАНИЕ"):
        assert text not in log
    assert files.held_settings_path(env.data_dir, sid).exists()


# --- правки по ревью 2б -------------------------------------------------------------------------------------


def _project_policy(env, project_id: int, work: dict) -> None:
    value = json.dumps({"subs": True, "work": work})
    env.db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.project SET agent_policy_json = ? WHERE id = ?", (value, project_id)
        )
    )


async def test_project_buttons_reach_held_session_on_every_launch(env, monkeypatch):
    """Добавки проекта к кнопкам (`work.ask` / `work.self`) — в файле настроек постоянной сессии, как у задачи
    по «Го»; политика читается на каждый запуск процесса (правка проекта запись сессии не меняет)."""
    env.scenario(turns=[[{"text": "раз"}], [{"text": "два"}]])
    host = await env.started()
    project = env.project("Initech")
    _project_policy(env, project[0], {"stage": 1, "ask": ["mcp__crm__send_invoice"], "self": []})
    sid, _, _ = await _session(env, project=project)
    await _running(env, host, sid)
    path = files.held_settings_path(env.data_dir, sid)
    ask = json.loads(path.read_text(encoding="utf-8"))["permissions"]["ask"]
    assert "mcp__crm__send_invoice" in ask and "RemoteTrigger" in ask
    await _turn_done(env, host, sid)
    await env.ui("POST", f"/sessions/{sid}/sleep")
    await env.until(host, lambda: sid not in host.held.runners, what="раннер снят")
    # Владелец поменял кнопки проекта, пока сессия спала: запись сессии не менялась, `wait` её не приносил.
    _project_policy(env, project[0], {"stage": 1, "ask": ["mcp__crm__refund"], "self": ["RemoteTrigger"]})
    # Запись у хоста «новее» ответа ядра (гонка ответов): политика всё равно берётся свежая.
    host.held.records[sid]["seq"] += 1000
    assert (await host.held._refresh(sid))["policy"]["ask"] == ["mcp__crm__refund"]  # noqa: SLF001
    host.held.records[sid]["seq"] -= 1000
    refreshed = []
    real = host.held._refresh  # noqa: SLF001

    async def refresh(session_id):
        refreshed.append(session_id)
        return await real(session_id)

    monkeypatch.setattr(host.held, "_refresh", refresh)
    await _say(env, sid, "ещё")
    await env.until(host, lambda: len(env.calls()) == 2 and sid in _alive(host), what="второй запуск")
    assert refreshed == [sid]  # запись перечитана перед запуском процесса
    ask = json.loads(path.read_text(encoding="utf-8"))["permissions"]["ask"]
    assert "mcp__crm__refund" in ask and "mcp__crm__send_invoice" not in ask and "RemoteTrigger" not in ask


async def test_exception_inside_launch_is_failure_and_next_try(env, monkeypatch):
    """Сбой внутри запуска (запись файла настроек — «диск полон») не оставляет сессию без процесса и без
    причины: раннер снят, `failure` — тип исключения без пути, счётчик неудач растёт, новая попытка идёт."""
    env.scenario(turns=[[{"text": "ок"}]])
    host = await env.started(held_launch_retry_s=0)
    sid, _, _ = await _session(env)
    real = held_mod.HeldRunner._settings  # noqa: SLF001
    boom = {"n": 0}

    def settings(self):
        boom["n"] += 1
        raise OSError(28, "No space left on device", "/секретный/путь")

    monkeypatch.setattr(held_mod.HeldRunner, "_settings", settings)
    await _say(env, sid)

    async def failed():
        return (await _rec(env, sid))["failure"]

    failure = await env.until(host, failed, what="причина у сессии")
    assert failure == "сбой запуска (OSError)" and boom["n"] >= 1
    assert host.held.records[sid]["launch_fails"] >= 1 and env.calls() == []
    monkeypatch.setattr(held_mod.HeldRunner, "_settings", real)  # место освободили
    await env.until(host, lambda: sid in _alive(host), what="следующая попытка")
    await _turn_done(env, host, sid)
    assert (await _rec(env, sid))["failure"] is None


async def test_host_tick_survives_held_failure(env, monkeypatch, caplog):
    """Непредвиденный сбой круга постоянных сессий не роняет круг хоста (задачи и оркестратор идут дальше); в
    журнале — только тип."""
    host = await env.started()

    async def boom():
        raise RuntimeError("ТЕКСТ-ОШИБКИ")

    monkeypatch.setattr(host.held, "tick", boom)
    with caplog.at_level("ERROR", logger="hub.work"):
        await host.tick()
    log = "\n".join(r.getMessage() for r in caplog.records)
    assert "сбой круга (RuntimeError)" in log and "ТЕКСТ-ОШИБКИ" not in log


async def test_say_delivered_mark_is_retried(env, monkeypatch):
    """Реплика записана во ввод, а отметка «доставлено» до ядра не дошла: отметка повторяется следующим
    кругом, реплика второй раз не подаётся."""
    env.scenario(turns=[[{"text": "ок"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    real_post = host.core.post
    failed = {"n": 0}

    async def post(path, *args, **kwargs):
        if "/session-says/" in path and failed["n"] == 0:
            failed["n"] += 1
            raise CoreUnavailable("timeout")
        return await real_post(path, *args, **kwargs)

    monkeypatch.setattr(host.core, "post", post)
    await _say(env, sid)
    await env.until(host, lambda: sid in _alive(host), what="процесс сессии")
    runner = host.held.runners[sid]
    await env.until(host, lambda: not runner.busy and failed["n"] == 1, what="ход окончен")
    for _ in range(4):
        await env.cycle(host)
    assert len(_users(env)) == 1 and host.held.unmarked == {}
    assert (await _rec(env, sid))["queued_says"] == 0


async def test_request_delivered_mark_is_retried(env, monkeypatch):
    """То же для решения по запросу: отметка `delivered` повторяется, пока ядро её не примет."""
    env.scenario(turns=[[{"ask": {"tool": "Bash", "input": {"command": "make deploy"}}}, {"text": "готово"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    real_post = host.core.post
    failed = {"n": 0}

    async def post(path, *args, **kwargs):
        if "/session-requests/" in path and path.endswith("/delivered") and failed["n"] < 2:
            failed["n"] += 1
            raise CoreUnavailable("timeout")
        return await real_post(path, *args, **kwargs)

    monkeypatch.setattr(host.core, "post", post)
    await _say(env, sid)

    async def pending():
        return (await _full(env, sid))["requests"]

    rid = (await env.until(host, pending, what="карточка"))[0]["id"]
    r = await env.ui("POST", f"/session-requests/{rid}/decide", {"decision": "allow"})
    assert r.status_code == 200, r.text

    def delivered():
        with env.db.reader() as conn:
            row = conn.execute("SELECT delivered_ts FROM main.held_request WHERE id = ?", (rid,)).fetchone()
        return row[0] is not None

    await env.until(host, delivered, what="отметка дошла")
    assert failed["n"] == 2 and host.held.unmarked == {}


async def test_exit_before_first_turn_counts_as_failed_launch(env, monkeypatch):
    """(а): процесс отвечает на `initialize` и сам выходит, не начав хода, — это неудачный запуск: три подряд
    — `failure` и хост больше не пробует, а не бесконечные подъёмы."""
    env.scenario(initialize_exit=True)
    host = await env.started(held_launch_retry_s=0)
    sid, _, _ = await _session(env)

    async def no_send(self, says, text, blocks=None):
        return False

    monkeypatch.setattr(held_mod.HeldRunner, "send_says", no_send)
    await _say(env, sid)

    async def stopped():
        return (host.held.records.get(sid) or {}).get("launch_fails") == 3 and sid not in host.held.runners

    await env.until(host, stopped, what="три неудачи", limit_s=30.0)
    for _ in range(5):
        await env.cycle(host)
    rec = await _rec(env, sid)
    assert "процесс завершился до первого хода" in rec["failure"] and rec["state"] == "asleep"
    assert len(env.calls()) == 3 and rec["queued_says"] == 1


async def test_confirm_does_not_overwrite_running_of_started_turn(env, monkeypatch):
    """(б): подтверждение состояния собирается под замком сессии — пока `turn running` начатого хода летит в
    ядро, подтверждение ждёт и уходит уже с `running`, а не со старым `idle` поверх."""
    env.scenario(turns=[[{"text": "раз"}]])
    host = await env.started(held_confirm_s=3600)
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    runner = host.held.runners[sid]
    real_post = host.core.post
    gate = asyncio.Event()
    order: list[tuple[str, str]] = []

    async def post(path, *args, **kwargs):
        body = kwargs.get("json") or {}
        if path.endswith(f"/sessions/{sid}/turn") and body.get("state") == "running":
            await gate.wait()  # `turn running` в полёте
        result = await real_post(path, *args, **kwargs)
        if path.endswith((f"/sessions/{sid}/turn", f"/sessions/{sid}/state")):
            order.append((path.rsplit("/", 1)[1], body.get("state")))
        return result

    monkeypatch.setattr(host.core, "post", post)
    began = asyncio.ensure_future(runner._turn_started())  # noqa: SLF001 — ход начался, POST висит
    await asyncio.sleep(0.05)
    assert runner.busy
    runner.busy = False  # снимок «до хода», каким его увидело бы подтверждение без замка
    confirm = asyncio.ensure_future(host.held.confirm(runner))
    await asyncio.sleep(0.05)
    runner.busy = True
    assert not confirm.done()  # ждёт своей очереди, тело ещё не собрано
    gate.set()
    await began
    assert await confirm
    assert order == [("turn", "running"), ("state", "running")]
    assert (await _rec(env, sid))["state"] == "running"


async def test_slow_launch_does_not_hold_host_round(env, monkeypatch):
    """(в): проверка `claude agents` (и весь запуск) идёт своей задачей — круг хоста и вторая сессия её не
    ждут; место при этом уже занято запускающейся."""
    env.scenario(turns=[[{"text": "раз"}], [{"text": "два"}], [{"text": "три"}]])
    host = await env.started()
    first, _, _ = await _session(env)
    second, _, _ = await _session(env, name="Globex")
    await _running(env, host, first)
    await _turn_done(env, host, first)
    await env.ui("POST", f"/sessions/{first}/sleep")
    await env.until(host, lambda: first not in host.held.runners, what="раннер снят")
    gate = asyncio.Event()
    real = host.held._open_elsewhere  # noqa: SLF001

    async def slow(rec):
        await gate.wait()
        return await real(rec)

    monkeypatch.setattr(host.held, "_open_elsewhere", slow)
    await _say(env, first, "ещё")  # у неё есть разговор — перед запуском идёт проверка «в другом окне»
    await _say(env, second, "вторая")
    await asyncio.wait_for(env.until(host, lambda: second in _alive(host), what="вторая сессия"), 10)
    runner = host.held.runners[first]
    assert runner.launching and not runner.alive and len(host.held._live()) == 2  # noqa: SLF001
    gate.set()
    await env.until(host, lambda: first in _alive(host), what="первая сессия")


# --- ревью Ф56 (5), (8) -------------------------------------------------------------------------------------


async def _fake_proc(name: str = "claude"):
    """Подделка процесса сессии: `sleep` под именем движка (`argv[0]`), в своей группе."""
    return await asyncio.create_subprocess_exec(
        "bash", "-c", f"exec -a {name} sleep 300", start_new_session=True
    )


async def _proc_identity(proc, name: str) -> int:
    for _ in range(100):  # `exec -a` меняет имя не мгновенно
        if session.proc_cmdline(proc.pid)[:1] == [name]:
            break
        await asyncio.sleep(0.02)
    start = session.proc_start(proc.pid)
    assert start
    return start


async def _dead_pid() -> int:
    gone = await asyncio.create_subprocess_exec("true")
    await gone.wait()
    return gone.pid


def _write_procs(env, procs: dict, *, host_pid: int, host_start: int = 1, boot: str | None = None) -> None:
    files.save_held_procs(
        env.data_dir,
        {
            "boot_id": boot if boot is not None else session.boot_id(),
            "host_pid": host_pid,
            "host_start": host_start,
            "procs": {str(k): {"pid": v[0], "start": v[1]} for k, v in procs.items()},
        },
    )


async def _reap(*procs) -> None:
    for proc in procs:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()


async def test_start_kills_orphan_process_of_previous_host(env):
    """Ревью Ф56 (5), круг 2 (2а): хост упал посреди хода (SIGKILL), `claude` сессии остался жить и держит
    разговор «открытым в другом окне» — новый хост на старте гасит процесс из СВОЕГО файла
    (`held-procs.json`), даже когда ядро за время простоя уже усыпило сессию и стёрло её pid
    (`stale_to_asleep`)."""
    sid, _, _ = await _session(env)
    orphan = await _fake_proc()
    start = await _proc_identity(orphan, "claude")
    now = int(time.time() * 1000)
    env.db.write_sync(
        lambda conn: hub_sessions.touch(
            conn, sid, now, state="running", proc_pid=orphan.pid, proc_start=start
        )
    )
    swept = env.db.write_sync(
        lambda conn: hub_sessions.stale_to_asleep(conn, now_ms=now + held_core.HOST_STALE_MS + 1_000)
    )
    assert swept == [sid]
    # Чужой процесс с тем же pid, но другой личностью, трогать нельзя.
    other, _, _ = await _session(env, name="Другой")
    keeper = await _fake_proc()
    keeper_start = await _proc_identity(keeper, "claude")
    # Время старта в файле — заведомо не настоящее (`start + 7` на медленном раннере совпадало с ним).
    _write_procs(
        env,
        {sid: (orphan.pid, start), other: (keeper.pid, keeper_start + 1_000_000)},
        host_pid=await _dead_pid(),
    )
    try:
        host = await env.started()
        await asyncio.wait_for(orphan.wait(), 5)
        assert keeper.returncode is None and pid_alive(keeper.pid)
        assert (await _rec(env, sid))["state"] == "asleep"
        assert sid not in _alive(host)
        assert files.load_held_procs(env.data_dir)["procs"] == {}  # файл теперь этого хоста
        assert files.load_held_procs(env.data_dir)["host_pid"] == os.getpid()
    finally:
        await _reap(keeper, orphan)


@pytest.mark.parametrize("case", ["cmdline", "live_host", "boot", "host_parent"])
async def test_start_does_not_kill_process_that_is_not_surely_an_orphan(env, case, caplog, monkeypatch):
    """Круг 2 (2б): личность сошлась, но не сошлось что-то ещё — процесс не трогается, причина в журнале:
    чужая командная строка; хост, записавший файл, ещё жив и он родитель; запись другой загрузки машины;
    родитель — процесс `hub work`."""
    sid, _, _ = await _session(env)
    proc = await _fake_proc("sleeper" if case == "cmdline" else "claude")
    start = await _proc_identity(proc, "sleeper" if case == "cmdline" else "claude")
    host_pid, host_start, boot = await _dead_pid(), 1, None
    if case == "live_host":
        # родитель подделки — этот процесс
        host_pid, host_start = os.getpid(), session.proc_start(os.getpid())
    if case == "boot":
        boot = "00000000-0000-0000-0000-000000000000"
    if case == "host_parent":
        real = session.proc_cmdline
        monkeypatch.setattr(
            session,
            "proc_cmdline",
            lambda pid: ["python", "/x/bin/hub", "work", "run"] if pid == os.getpid() else real(pid),
        )
    _write_procs(env, {sid: (proc.pid, start)}, host_pid=host_pid, host_start=host_start, boot=boot)
    try:
        with caplog.at_level("WARNING"):
            await env.started()
        await asyncio.sleep(0.3)
        assert proc.returncode is None and pid_alive(proc.pid)
        assert "не гасится" in caplog.text
    finally:
        await _reap(proc)


async def test_host_records_its_session_process_and_forgets_it_on_exit(env):
    """Круг 2 (2а): процесс живой сессии записан в файле хоста вместе с загрузкой машины и личностью хоста;
    процесс вышел — запись убрана."""
    env.scenario(turns=[[{"text": "ок"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    await _say(env, sid)
    await env.until(
        host, lambda: str(sid) in files.load_held_procs(env.data_dir).get("procs", {}), what="процесс записан"
    )
    data = files.load_held_procs(env.data_dir)
    entry = data["procs"][str(sid)]
    assert session.proc_start(entry["pid"]) == entry["start"]
    assert data["boot_id"] == session.boot_id() and data["host_pid"] == os.getpid()
    r = await env.ui("POST", f"/sessions/{sid}/sleep")
    assert r.status_code == 200, r.text
    await env.until(
        host, lambda: str(sid) not in files.load_held_procs(env.data_dir).get("procs", {}), what="убрана"
    )
    assert sid not in _alive(host)


async def test_turn_end_carries_delivered_says_so_core_sees_owner_reply(env, monkeypatch):
    """Ревью Ф56 (8): отметка «доставлено» реплики не дошла (ядро моргнуло), а модель ответила раньше повтора
    — конец хода несёт `delivered_says`, ядро отмечает реплику доставленной само и заводит карточку `turn`
    «по реплике владельца» (`by_ui_say`), а не теряет её."""
    env.scenario(turns=[[{"text": "сделал"}]])
    host = await env.started()
    sid, _, _ = await _session(env, role="программист")
    real_post = host.core.post

    async def post(path, *args, **kwargs):
        if "/session-says/" in path and path.endswith("/delivered"):
            raise CoreUnavailable("timeout")  # отметка не доходит никогда — только конец хода
        return await real_post(path, *args, **kwargs)

    monkeypatch.setattr(host.core, "post", post)
    say_id = await _running(env, host, sid, "сделай")
    await _turn_done(env, host, sid)
    monkeypatch.undo()
    r = await env.ui("GET", "/owner_channel/queue")
    cards = [c for c in r.json()["items"] if c["kind"] == "turn"]
    assert len(cards) == 1 and cards[0]["full"]["by_ui_say"] is True
    with env.db.reader() as conn:
        status = conn.execute("SELECT status FROM main.held_say WHERE id = ?", (say_id,)).fetchone()[0]
    assert status == "delivered"
    assert (await _rec(env, sid))["queued_says"] == 0
    for _ in range(3):
        await env.cycle(host)
    assert host.held.unmarked == {}  # повтор отметки получил 409 «уже не в очереди» и снят
