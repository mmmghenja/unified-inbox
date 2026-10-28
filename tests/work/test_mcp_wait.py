"""Первая подача процессу Claude Code — после того, как его MCP-серверы вышли из `pending` (12.10.2026: сессия
хаба после подъёма из сна получала реплику через секунду после запуска и оставалась без инструментов Jira)."""

import logging

from hub.work import runner_base

from .test_held_host import _controls, _running, _session, _turn_done, _users

JIRA = "plugin:atlassian:atlassian"
PENDING = [{"name": JIRA, "status": "pending"}, {"name": "hub", "status": "connected"}]
READY = [
    {"name": JIRA, "status": "connected"},
    {"name": "plugin:github:github", "status": "failed"},
    {"name": "claude.ai Gmail", "status": "needs-auth"},
]


async def test_first_say_waits_until_mcp_not_pending(env, monkeypatch):
    monkeypatch.setattr(runner_base, "MCP_POLL_S", 0.05)
    env.scenario(mcp_status=[PENDING, PENDING, READY], turns=[[{"text": "ок"}]])
    host = await env.started()
    sid, _, _ = await _session(env, role="программист")
    await _running(env, host, sid, "посмотри доску")
    await _turn_done(env, host, sid)
    lines = env.stdin()
    asked = [i for i, m in enumerate(lines) if (m.get("request") or {}).get("subtype") == "mcp_status"]
    first_user = next(i for i, m in enumerate(lines) if m.get("type") == "user")
    # три опроса (два `pending`, третий — готово; `failed` и `needs-auth` подачу не держат), реплика — после
    assert len(asked) == 3 and asked[-1] < first_user
    assert len(_users(env)) == 1


async def test_mcp_wait_gives_up_after_limit(env, monkeypatch, caplog):
    monkeypatch.setattr(runner_base, "MCP_POLL_S", 0.05)
    monkeypatch.setattr(runner_base, "MCP_WAIT_S", 0.3)
    env.scenario(mcp_status=[PENDING], turns=[[{"text": "ок"}]])
    host = await env.started()
    sid, _, _ = await _session(env, role="программист")
    with caplog.at_level(logging.WARNING, logger="hub.work"):
        await _running(env, host, sid, "привет")
        await _turn_done(env, host, sid)
    assert len(_users(env)) == 1 and len(_controls(env, "mcp_status")) >= 2
    assert any("MCP не подключились" in r.message and JIRA in r.message for r in caplog.records)


async def test_engine_without_mcp_status_is_not_held(env, monkeypatch):
    monkeypatch.setattr(runner_base, "MCP_STATUS_TIMEOUT_S", 0.2)
    env.scenario(mcp_status_silent=True, turns=[[{"text": "ок"}]])
    host = await env.started()
    sid, _, _ = await _session(env, role="программист")
    await _running(env, host, sid, "привет")
    await _turn_done(env, host, sid)
    # один вопрос без ответа — и подача
    assert len(_users(env)) == 1 and len(_controls(env, "mcp_status")) == 1
