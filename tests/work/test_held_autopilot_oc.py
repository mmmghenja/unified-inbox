"""Ф41-5а для постоянных сессий на OpenCode (docs/architecture.md): правила каждого
вызова решает хост — ключ карточки назван им же, снятое правило не спрашивает, набор обновляется на месте.
`opencode` — ПОДДЕЛЬНЫЙ сервер `tests/engine/fake_opencode.py`."""

from hub.work.held import AUTO_ANSWER_TEXT

from .oc_helpers import (
    oc,  # noqa: F401 — фикстура `oc`
    started,
)
from .test_held_autopilot import QUESTION, _free, _hold, _requests
from .test_held_host import _events, _has_event, _turn_done
from .test_held_opencode import MODEL, _oc_session, _running


async def _card(env, host, sid) -> dict:
    async def pending():
        return [r for r in await _requests(env, sid) if r["status"] == "pending"]

    [request] = await env.until(host, pending, what="карточка")
    return request


async def _decide(env, request, decision="deny") -> None:
    r = await env.ui("POST", f"/session-requests/{request['id']}/decide", {"decision": decision})
    assert r.status_code == 200, r.text


async def test_oc_freed_rules_pass_and_cards_name_the_rule(env, oc):  # noqa: F811
    """Доказательство критика (3): «аргументы не получены» при снятом `project_permissions` — карточка
    `guard_failure`, а не авто-«разрешить»."""
    host = await started(env)
    await _free(env, "push", "question", "project_permissions", "foreign_hook")
    sid, _pid, workdir = await _oc_session(env, model=MODEL, mode="auto")
    oc.scenario(
        [
            {"ask": {"tool": "bash", "args": {"command": "git push origin main"}}},  # снят — проходит
            {"question": QUESTION},  # снят — сессия решает сама
            {"ask": {"tool": "bash", "no_input": True}},  # аргументы не получены
            {"ask": {"tool": "bash", "args": {"command": "ssh prod uptime"}}},  # стоит — карточка
            {"ask": {"tool": "write", "args": {"filePath": str(workdir / "CLAUDE.md"), "content": "x"}}},
            {"ask": {"tool": "task", "args": {"subagent_type": "reviewer", "prompt": "x"}}},  # запрет
            {"text": "всё"},
        ]
    )
    await _running(env, host, oc, sid)
    request = await _card(env, host, sid)
    assert request["rule"] == "guard_failure" and "аргументы вызова не получены" in request["reason"]
    await _decide(env, request)
    request = await _card(env, host, sid)
    assert request["rule"] == "server" and "[hub-rule" not in request["reason"]
    await _decide(env, request)
    request = await _card(env, host, sid)
    assert request["rule"] == "project_rules"
    await _decide(env, request)
    await _turn_done(env, host, sid)
    replies = [(r.get("tool") or r.get("kind"), r["reply"]) for r in oc.replies()]
    assert replies == [
        ("bash", "once"),
        ("question", "reply"),  # авто-ответ — свободным ответом: у отказа OpenCode текста нет
        ("bash", "reject"),
        ("bash", "reject"),
        ("write", "reject"),
        ("task", "reject"),
    ]
    [answer] = [r for r in oc.replies() if r.get("kind") == "question"]
    assert answer["answers"] == [[AUTO_ANSWER_TEXT]]
    events = await _events(env, sid)
    [note] = [e for e in events if (e.get("brief") or {}).get("type") == "auto_answer"]
    assert note["brief"]["question"] == "Какой стенд брать?"
    assert await _has_event(env, sid, "warn", "подагенты рабочей сессии")()
    assert len([e for e in events if e["kind"] == "request"]) == 3


async def test_oc_set_is_updated_in_place_without_restart(env, oc):  # noqa: F811
    host = await started(env)
    sid, _pid, _workdir = await _oc_session(env, model=MODEL, mode="auto")
    push = {"ask": {"tool": "bash", "args": {"command": "git push"}}}
    slack = {"ask": {"tool": "slack_slack_send_message", "args": {"text": "x"}}}
    oc.scenario([push, {"text": "раз"}], [push, slack, {"text": "два"}], [push, {"text": "три"}])
    await _running(env, host, oc, sid)
    request = await _card(env, host, sid)  # все выключатели стоят
    assert request["rule"] == "push"
    await _decide(env, request, "allow")
    await _turn_done(env, host, sid)

    # Ослабление: набор меняется на месте — тот же сервер, следующий вызов уже без карточки.
    await _free(env, "push", "mcp_write", "oc_subagents", "guard_failure")
    await _running(env, host, oc, sid, "ещё")
    await _turn_done(env, host, sid)
    runner = host.held.runners[sid]
    assert runner.auto_free == {"push", "mcp_write", "oc_subagents", "guard_failure"}
    asked = [e for e in await _events(env, sid) if e["kind"] == "request"]
    assert len(asked) == 1 and len(oc.jsonl("starts.jsonl")) == 1

    # Ужесточение — тоже на месте и сразу: следующий вызов снова под правилом.
    await _hold(env, "push", True)
    await _running(env, host, oc, sid, "и ещё")
    request = await _card(env, host, sid)
    assert request["rule"] == "push"
    await _decide(env, request)
    await _turn_done(env, host, sid)
    assert len(oc.jsonl("starts.jsonl")) == 1
    assert not any((e.get("brief") or {}).get("type") == "autopilot" for e in await _events(env, sid))
    replies = [r["reply"] for r in oc.replies()]
    assert replies == ["once", "once", "once", "reject"]
    # Смена режима — системный текст меняется на месте, со следующего хода; сервер тот же.
    assert all("Ты в авто-режиме" in p["body"]["system"] for p in oc.prompts())
    r = await env.client.patch(
        f"/api/v1/sessions/{sid}", json={"mode": "manual"}, headers=await env.ui_headers()
    )
    assert r.status_code == 200, r.text
    oc.scenario([{"text": "четыре"}])
    await _running(env, host, oc, sid, "в ручном")
    await _turn_done(env, host, sid)
    system = oc.prompts()[-1]["body"]["system"]
    assert "Действия под кнопкой (push" in system and "авто-режиме" not in system
    assert len(oc.jsonl("starts.jsonl")) == 1


async def test_oc_denied_question_card_is_answered_with_the_refusal_text(env, oc):  # noqa: F811
    """Ручной режим: отказ владельца на карточку вопроса — не `/question/{id}/reject` (настоящий сервер после него
    ход не продолжает и не заканчивает), а свободный ответ с текстом отказа; ход доходит до конца."""
    host = await started(env)
    sid, _pid, _workdir = await _oc_session(env, model=MODEL)
    oc.scenario([{"question": QUESTION}, {"text": "понял"}])
    await _running(env, host, oc, sid)
    request = await _card(env, host, sid)
    assert request["rule"] == "question"
    await _decide(env, request)
    await _turn_done(env, host, sid)
    [answer] = [r for r in oc.replies() if r.get("kind") == "question"]
    assert answer["reply"] == "reply" and len(answer["answers"]) == 1
    assert "отказал" in answer["answers"][0][0]


async def test_oc_model_change_restarts_the_server_when_idle(env, oc):  # noqa: F811
    """Ф41-7а: у OpenCode модель и вариант зашиты в запуск сервера сессии — смена перезапускает его при
    простое, следующий ход идёт с новыми."""
    from hub.work import held as held_mod

    host = await started(env)
    sid, _pid, _workdir = await _oc_session(env, model=MODEL, effort="low")
    oc.scenario([{"text": "раз"}], [{"text": "два"}])
    await _running(env, host, oc, sid)
    await _turn_done(env, host, sid)
    assert oc.prompts()[-1]["body"]["variant"] == "low" and len(oc.jsonl("starts.jsonl")) == 1
    r = await env.client.patch(
        f"/api/v1/sessions/{sid}",
        json={"model": "deepseek/deepseek-chat", "effort": "high"},
        headers=await env.ui_headers(),
    )
    assert r.status_code == 200, r.text
    await env.until(host, lambda: sid not in host.held.runners, what="перезапуск при простое")
    assert await _has_event(env, sid, "status", held_mod.SLEEP_NOTES["model"])()
    await _running(env, host, oc, sid, "ещё")
    await _turn_done(env, host, sid)
    body = oc.prompts()[-1]["body"]
    assert body["model"] == {"providerID": "deepseek", "modelID": "deepseek-chat"}
    assert body["variant"] == "high"
    assert len(oc.jsonl("starts.jsonl")) == 2
