"""Ф41-5а (docs/architecture.md): хост постоянных сессий в
авто-режиме — набор процесса при запуске, ключ правила у карточки, проверка хостом перед авто-«разрешить»,
авто-ответ на вопрос, смена выключателей у живого процесса. Ядро — в процессе, `claude` — подставной."""

import hashlib
import json
import os
import shlex

from hub.work import guard
from hub.work import held as held_mod

from .conftest import flag_value
from .test_held_host import _events, _full, _rec, _running, _say, _session, _state, _turn_done
from .test_held_incoming import _core_rec

QUESTION = {
    "questions": [{"question": "Какой стенд брать?", "options": [{"label": "dev"}, {"label": "stage"}]}]
}
MOVE = "mcp__hub-all__hub_move_channel"


async def _hold(env, key: str, hold, scope="global") -> None:
    r = await env.ui("PUT", "/autopilot", {"scope": scope, "key": key, "hold": hold})
    assert r.status_code == 200, r.text


async def _free(env, *keys: str) -> None:
    for key in keys:
        await _hold(env, key, False)


def _settings(call: dict) -> dict:
    with open(flag_value(call["argv"], "--settings"), encoding="utf-8") as f:
        return json.load(f)


def _hook(call: dict) -> str:
    return _settings(call)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


def _free_args(call: dict) -> tuple[str, int, int]:
    """Ф50-3: набор правил гарда хук спрашивает у хоста — `(имя сокета, pid хоста, id сессии)`."""
    argv = shlex.split(_hook(call))
    assert "--free" not in argv and "--free-file" not in argv
    return guard._parse_args(argv[argv.index("--workdir") :])[2]


def _free_keys(call: dict) -> list[str]:
    """Набор сессии, как его СЕЙЧАС видит хук (ответ сокета хоста)."""
    keys, why = guard.read_free_sock(*_free_args(call))
    assert why is None, why
    return sorted(keys)


async def _requests(env, sid) -> list[dict]:
    return (await _full(env, sid))["requests"]


def _bash(command: str, **extra) -> dict:
    return {"ask": {"tool": "Bash", "input": {"command": command}, **extra}}


# --- набор процесса при запуске (4.3, П1, П5) ---------------------------------------------------------------


async def test_launch_fixes_the_set_in_hook_settings_and_launch_key(env):
    env.scenario(turns=[[{"text": "раз"}], [{"text": "два"}], [{"text": "три"}]])
    await _free(env, "push", "server", "mcp_write", "plan_mode", "question", "hub_send")
    host = await env.started()
    auto, _, _ = await _session(env, mode="auto")
    await _running(env, host, auto)
    await _turn_done(env, host, auto)
    [call] = env.calls()
    # Хуку — только ключи гарда; настройкам — без снятых правил `permissions`.
    assert shlex.split(_hook(call))[-6] == "--free-sock" and "--profile" not in shlex.split(_hook(call))
    assert _free_args(call) == (host.held.free.name, os.getpid(), auto)
    assert _free_keys(call) == ["push", "server"]
    permissions = _settings(call)["permissions"]
    assert permissions["ask"] == [] and "EnterPlanMode" not in permissions["deny"]
    assert "SendMessage" in permissions["deny"] and "Bash(hub *)" in permissions["deny"]
    runner = host.held.runners[auto]
    assert runner.auto_free == {"push", "server", "mcp_write", "plan_mode", "question", "hub_send"}
    # П1: ключ запуска — в окружении процесса, ядру — только его SHA-256.
    key = call["env"]["HUB_SESSION_KEY"]
    assert len(key) == 64 and call["env"]["HUB_SESSION"] == str(auto)
    await env.until(host, lambda: _core_rec(env, auto).launch_key_hash is not None, what="хеш ключа в ядре")
    assert _core_rec(env, auto).launch_key_hash == hashlib.sha256(key.encode()).hexdigest()
    assert key not in json.dumps((await _full(env, auto)), ensure_ascii=False)

    # Ручная сессия того же проекта: выключатели к ней не применяются.
    manual, _, _ = await _session(env, project=(_core_rec(env, auto).project_id, runner.workdir))
    await _running(env, host, manual)
    await _turn_done(env, host, manual)
    call2 = env.calls()[1]
    assert _free_args(call2) == (host.held.free.name, os.getpid(), manual) and _free_keys(call2) == []
    permissions = _settings(call2)["permissions"]
    assert MOVE in permissions["ask"] and "EnterPlanMode" in permissions["deny"]
    assert host.held.runners[manual].auto_free == frozenset()
    assert call2["env"]["HUB_SESSION_KEY"] != key  # у каждого запуска свой
    # П5: у сессии в авто (и со всеми стоящими выключателями) переноса беседы в `mcp_write` нет.
    r = await env.ui("POST", "/autopilot/hold-all", {})
    assert r.status_code == 200
    await env.until(host, lambda: auto not in host.held.runners, what="набор сменился — процесс закрыт")
    await _say(env, auto, "ещё")
    await env.until(host, lambda: len(env.calls()) == 3, what="запуск с новым набором")
    call3 = env.calls()[2]
    ask = _settings(call3)["permissions"]["ask"]
    assert MOVE not in ask and "mcp__hub-all__hub_cancel_draft" in ask and _free_keys(call3) == []
    assert call3["env"]["HUB_SESSION_KEY"] not in (key, call2["env"]["HUB_SESSION_KEY"])


# --- запросы движка: ключ правила, авто-ответы, проверка хоста (4.3, П2, П3, П11) ---------------------------


async def test_auto_answers_question_project_permissions_and_foreign_hook(env):
    env.scenario(
        turns=[
            [
                {"ask": {"tool": "AskUserQuestion", "input": QUESTION}},
                _bash("git commit -m x"),  # правило `ask` самого проекта
                _bash("make test", reason_type="hook", reason="общий гард: похоже на секрет"),
                {"text": "готово"},
            ]
        ]
    )
    await _free(env, "question", "project_permissions", "foreign_hook")
    host = await env.started()
    sid, _, _ = await _session(env, mode="auto")
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    responses = [r["response"] for r in env.responses()]
    assert [r["behavior"] for r in responses] == ["deny", "allow", "allow"]
    assert responses[0]["message"] == held_mod.AUTO_ANSWER_TEXT
    assert responses[1]["updatedInput"] == {"command": "git commit -m x"}
    assert await _requests(env, sid) == []  # ни одной карточки
    events = await _events(env, sid)
    [note] = [e for e in events if (e.get("brief") or {}).get("type") == "auto_answer"]
    assert note["kind"] == "status" and note["brief"]["question"] == "Какой стенд брать?"
    assert "request" not in [e["kind"] for e in events]
    assert env.notes == []  # и уведомлений владельцу нет
    log = (env.data_dir / "work" / "guard.log").read_text(encoding="utf-8")
    assert log.count("\tauto\tавтопилот: project_permissions") == 1
    assert log.count("\tauto\tавтопилот: foreign_hook") == 1


async def test_cards_carry_the_rule_and_host_checks_before_allowing(env):
    """П2 и доказательство критика (2): запрос без причины с типом `rule` при снятом `project_permissions`
    хост не разрешает вслепую — прогоняет через гард сам: `git commit … && git push` при стоящем `push` —
    карточка `push`."""
    marked = "гард хаба: связь с сервером — решает владелец\n[hub-rule:server]"
    env.scenario(
        turns=[
            [
                _bash("git commit -m x && git push"),  # тип `rule`, причины нет (проба 5а, случай а1)
                _bash("ssh prod uptime", reason_type="hook", reason=marked),
                # чужой хук перекрыл причину хука хаба (проба, случай б1): метки нет
                _bash("git push --force", reason_type="hook", reason="второй хук: спроси человека"),
                {"ask": {"tool": "mcp__claude_ai_Slack__slack_send_message", "input": {"text": "x"}}},
                {"ask": {"tool": "AskUserQuestion", "input": QUESTION}},
                {"text": "готово"},
            ]
        ]
    )
    await _free(env, "project_permissions", "foreign_hook", "deploy")
    host = await env.started()
    sid, _, _ = await _session(env, mode="auto")
    await _running(env, host, sid)
    want = [
        ("push", "rule: гард хаба: push — решает владелец"),
        ("server", "hook: гард хаба: связь с сервером — решает владелец"),
        ("push", "hook: гард хаба: push — решает владелец"),
        ("mcp_write", None),
        ("question", None),
    ]
    for rule, reason in want:

        async def pending():
            return [r for r in await _requests(env, sid) if r["status"] == "pending"]

        [request] = await env.until(host, pending, what=f"карточка {rule}")
        assert request["rule"] == rule and request["rule_title"], request
        if reason is not None:
            assert request["reason"] == reason
        assert "[hub-rule:" not in (request["reason"] or "")
        body = {"decision": "deny"}
        r = await env.ui("POST", f"/session-requests/{request['id']}/decide", body)
        assert r.status_code == 200, r.text
        await env.until(host, lambda rid=request["id"]: _delivered(env, rid), what="отказ доставлен")
    await _turn_done(env, host, sid)
    assert [r["response"]["behavior"] for r in env.responses()] == ["deny"] * 5


def _delivered(env, request_id: int) -> bool:
    with env.db.reader() as conn:
        row = conn.execute(
            "SELECT delivered_ts FROM main.held_request WHERE id = ?", (request_id,)
        ).fetchone()
    return row is not None and row[0] is not None


async def test_project_rule_is_not_answered_while_foreign_hook_is_held(env):
    """Проба 5а (случай б3): правило проекта и чужой хук, спросившие разом, приходят как `rule` без причины —
    при снятом `project_permissions` и стоящем `foreign_hook` хост сам не отвечает."""
    env.scenario(turns=[[_bash("git commit -m x"), {"text": "готово"}]])
    await _free(env, "project_permissions")
    host = await env.started()
    sid, _, _ = await _session(env, mode="auto")
    await _running(env, host, sid)
    [request] = await env.until(host, lambda: _requests(env, sid), what="карточка")
    assert request["rule"] == "foreign_hook" and env.responses() == []
    r = await env.ui("POST", f"/session-requests/{request['id']}/decide", {"decision": "allow"})
    assert r.status_code == 200
    await _turn_done(env, host, sid)


async def test_manual_session_asks_as_before_with_the_rule_named(env):
    """Ручная сессия — поведение прежнее (выключатели к ней не применяются), у карточки появился ключ."""
    env.scenario(
        turns=[
            [
                {"ask": {"tool": "AskUserQuestion", "input": QUESTION}},
                _bash("git commit -m x"),
                {"text": "ок"},
            ]
        ]
    )
    await _free(env, "question", "project_permissions", "foreign_hook", "push")
    host = await env.started()
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    for rule, answer in (
        ("question", {"decision": "allow", "answers": {"Какой стенд брать?": "dev"}}),
        (
            "project_permissions",
            {"decision": "allow"},
        ),
    ):

        async def pending():
            return [r for r in await _requests(env, sid) if r["status"] == "pending"]

        [request] = await env.until(host, pending, what=f"карточка {rule}")
        assert request["rule"] == rule
        r = await env.ui("POST", f"/session-requests/{request['id']}/decide", answer)
        assert r.status_code == 200, r.text
        await env.until(host, lambda rid=request["id"]: _delivered(env, rid), what="решение доставлено")
    await _turn_done(env, host, sid)
    assert not any((e.get("brief") or {}).get("type") == "auto_answer" for e in await _events(env, sid))


# --- смена выключателей у живого процесса (П4) --------------------------------------------------------------


async def test_settings_switch_set_mid_turn_interrupts_and_next_launch_is_under_the_rule(env):
    """Доказательство критика (4) — после Ф50-3 только для выключателей настроек процесса (Claude Code файл
    `--settings` на лету не перечитывает): выключатель поставлен посреди хода — ход прерван сразу, событие
    называет выключатель, следующий запуск процесса уже под правилом. Выключатели гарда — на месте:
    `test_f50_free_sock.py`."""
    env.scenario(turns=[[{"sleep": 30}, {"text": "не дойдёт"}]])
    await _free(env, "mcp_write", "server")
    host = await env.started()
    sid, _, _ = await _session(env, mode="auto")
    await _running(env, host, sid)
    await env.until(host, _state(env, sid, "running"), what="ход идёт")
    assert _free_keys(env.calls()[0]) == ["server"] and _settings(env.calls()[0])["permissions"]["ask"] == []
    await _say(env, sid, "следующая")  # ждёт конца хода
    env.scenario(turns=[[{"text": "по новым правилам"}]])  # сценарий следующего процесса
    await _hold(env, "mcp_write", True)

    async def interrupted():
        return [e for e in await _events(env, sid) if (e.get("brief") or {}).get("type") == "autopilot"]

    [note] = await env.until(host, interrupted, what="ход прерван, процесс закрыт")
    events = await _events(env, sid)
    assert note["summary"] == note["brief"]["text"]
    assert note["summary"].startswith(held_mod.SLEEP_NOTES["autopilot_hold"])
    assert "пишущие инструменты MCP" in note["summary"]
    assert "не дойдёт" not in [e["summary"] for e in events]
    # Очередь цела: реплика поднимает сессию с новым набором.
    await env.until(host, lambda: len(env.calls()) == 2, what="запуск под правилом")
    assert _free_keys(env.calls()[1]) == ["server"]
    assert MOVE not in _settings(env.calls()[1])["permissions"]["ask"]
    assert "mcp__hub-all__hub_cancel_draft" in _settings(env.calls()[1])["permissions"]["ask"]
    await _turn_done(env, host, sid)
    assert host.held.runners[sid].auto_free == {"server"}

    async def delivered():
        return (await _rec(env, sid))["queued_says"] == 0

    await env.until(host, delivered, what="реплика снята с очереди")  # на медленном раннере — не сразу


async def test_loosening_waits_for_idle_and_mode_change_counts(env):
    env.scenario(turns=[[{"sleep": 1.0}, {"text": "дошёл"}]])
    host = await env.started()
    sid, _, _ = await _session(env, mode="auto")
    await _running(env, host, sid)
    await env.until(host, _state(env, sid, "running"), what="ход идёт")
    # Ослабление выключателя настроек процесса посреди хода — ход не трогается, ключ у хоста стоит.
    await _hold(env, "plan_mode", False)
    for _ in range(3):
        await env.cycle(host)
    assert sid in host.held.runners and host.held.runners[sid].busy
    assert host.held.runners[sid].auto_free == frozenset()
    await env.until(host, lambda: sid not in host.held.runners, what="перезапуск при простое")
    await env.until(host, _state(env, sid, "asleep"), what="спит")
    events = await _events(env, sid)
    assert "дошёл" in [e["summary"] for e in events]
    notes = [e["summary"] for e in events if (e.get("brief") or {}).get("type") == "autopilot"]
    assert notes == [held_mod.SLEEP_NOTES["autopilot"]]
    assert (await _rec(env, sid))["state"] == "asleep" and len(env.calls()) == 1  # сама не поднимается
    env.scenario(turns=[[{"text": "ок"}]])
    await _say(env, sid, "ещё")
    await env.until(host, lambda: len(env.calls()) == 2, what="запуск с новым набором")
    assert _free_keys(env.calls()[1]) == [] and host.held.runners[sid].auto_free == {"plan_mode"}
    assert "EnterPlanMode" not in _settings(env.calls()[1])["permissions"]["deny"]
    system = flag_value(env.calls()[1]["argv"], "--append-system-prompt")
    assert "Ты в авто-режиме" in system and "Действия под кнопкой" not in system and "push" not in system
    await _turn_done(env, host, sid)
    # Режим `auto -> manual` — ужесточение: процесс закрыт сразу, следующий — без снятых правил.
    r = await env.client.patch(
        f"/api/v1/sessions/{sid}", json={"mode": "manual"}, headers=await env.ui_headers()
    )
    assert r.status_code == 200, r.text
    await env.until(host, lambda: sid not in host.held.runners, what="режим сменился — процесс закрыт")
    await _say(env, sid, "в ручном")
    await env.until(host, lambda: len(env.calls()) == 3, what="запуск в ручном режиме")
    assert _free_keys(env.calls()[2]) == []
    assert "EnterPlanMode" in _settings(env.calls()[2])["permissions"]["deny"]
    system = flag_value(env.calls()[2]["argv"], "--append-system-prompt")
    assert "Действия под кнопкой (push" in system and "авто-режиме" not in system
    await _turn_done(env, host, sid)
    # Выключатели ручную сессию не касаются: процесс живёт.
    await _hold(env, "server", False)
    for _ in range(3):
        await env.cycle(host)
    assert sid in host.held.runners and host.held.runners[sid].reason is None
    # `manual -> auto` при тех же (пустых) наборах — тоже перезапуск: у процесса другой системный текст.
    await _hold(env, "server", True)
    r = await env.client.patch(
        f"/api/v1/sessions/{sid}", json={"mode": "auto"}, headers=await env.ui_headers()
    )
    assert r.status_code == 200, r.text
    await env.until(
        host, lambda: sid not in host.held.runners, what="режим сменился — перезапуск при простое"
    )
    await _say(env, sid, "в авто")
    await env.until(host, lambda: len(env.calls()) == 4, what="запуск в авто")
    assert "Ты в авто-режиме" in flag_value(env.calls()[3]["argv"], "--append-system-prompt")


async def test_open_card_stays_a_card_when_the_switch_is_lifted(env):
    env.scenario(turns=[[_bash("git push"), {"text": "готово"}]])
    host = await env.started()
    sid, _, _ = await _session(env, mode="auto")
    await _running(env, host, sid)
    [request] = await env.until(host, lambda: _requests(env, sid), what="карточка")
    assert request["rule"] == "push"
    await _hold(env, "push", False)
    for _ in range(4):
        await env.cycle(host)
    assert sid in host.held.runners and env.responses() == []  # ответит владелец
    r = await env.ui("POST", f"/session-requests/{request['id']}/decide", {"decision": "allow"})
    assert r.status_code == 200
    await _turn_done(env, host, sid)
    # Ф50-3: правило гарда снято на месте — перезапуска нет, открытая карточка осталась за владельцем.
    for _ in range(3):
        await env.cycle(host)
    assert sid in host.held.runners and host.held.runners[sid].auto_free == {"push"}
    assert len(env.calls()) == 1 and _free_keys(env.calls()[0]) == ["push"]
