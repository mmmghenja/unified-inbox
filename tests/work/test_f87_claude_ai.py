"""Ф87-1 (docs/architecture.md–4): коннекторы claude.ai у сессии хаба на
Claude Code. Хост, уже видевший серверы `claude.ai …`, ждёт их до первой подачи; не появились за
`CLAUDE_AI_WAIT_S` — один перезапуск процесса тем же разговором (не неудача запуска), второй раз подряд —
`warn` и подача как есть. Движок — `fake_claude` (ответы `mcp_status` по номеру запуска)."""

import asyncio
import logging

import pytest

from hub.work import files, runner_base
from hub.work import held as held_mod

from .conftest import flag_value
from .test_held_host import _controls, _events, _has_event, _rec, _running, _session, _turn_done, _users

LOCAL = [{"name": "hub-all", "status": "connected"}, {"name": "playwright", "status": "connected"}]
LOCAL_PENDING = [{"name": "hub-all", "status": "pending"}, {"name": "playwright", "status": "connected"}]
WITH_AI = [
    *LOCAL,
    {"name": "claude.ai Claude Docs", "status": "connected"},
    {"name": "claude.ai Slack", "status": "disabled"},
]
ONLY_DISABLED_AI = [*LOCAL, {"name": "claude.ai globex-youtrack", "status": "disabled"}]


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(runner_base, "MCP_POLL_S", 0.05)
    monkeypatch.setattr(runner_base, "CLAUDE_AI_WAIT_S", 0.4)


def _first_user_after(env, subtype: str, nth: int) -> bool:
    """Первая строка `user` пришла после `nth`-го (с нуля) управляющего запроса `subtype`."""
    lines = env.stdin()
    asked = [
        i
        for i, m in enumerate(lines)
        if m.get("type") == "control_request" and (m.get("request") or {}).get("subtype") == subtype
    ]
    first_user = next(i for i, m in enumerate(lines) if m.get("type") == "user")
    return len(asked) > nth and asked[nth] < first_user


async def _summaries(env, sid, kind: str) -> list[str]:
    """События ленты вида `kind`; у `warn` — только про claude.ai (без файлов правил стенда)."""
    return [
        e["summary"]
        for e in await _events(env, sid)
        if e["kind"] == kind and (kind != "warn" or "claude.ai" in e["summary"])
    ]


async def test_no_mark_no_claude_ai_feeds_at_once(env, fast):
    env.scenario(mcp_status=[LOCAL], turns=[[{"text": "ок"}]])
    host = await env.started()
    sid, _, _ = await _session(env, role="программист")
    await _running(env, host, sid, "привет")
    await _turn_done(env, host, sid)
    # один опрос — и подача, как до Ф87-1; метки нет, перезапуска нет
    assert len(_controls(env, "mcp_status")) == 1 and len(_users(env)) == 1
    assert len(env.calls()) == 1
    assert not files.has_claude_ai_mark(env.data_dir)
    assert not await _summaries(env, sid, "warn")


async def test_expected_waits_until_claude_ai_appears(env, fast):
    files.set_claude_ai_mark(env.data_dir)
    env.scenario(mcp_status=[LOCAL, LOCAL, WITH_AI], turns=[[{"text": "ок"}]])
    host = await env.started()
    sid, _, _ = await _session(env, role="программист")
    await _running(env, host, sid, "посмотри задачу")
    await _turn_done(env, host, sid)
    # локальные готовы с первого опроса, но подача — после третьего, где появились `claude.ai …`
    assert len(_controls(env, "mcp_status")) == 3 and _first_user_after(env, "mcp_status", 2)
    assert len(env.calls()) == 1 and len(_users(env)) == 1
    assert files.has_claude_ai_mark(env.data_dir)
    assert sid not in host.held.claude_ai_restarts
    assert not [s for s in await _summaries(env, sid, "status") if "claude.ai" in s]


async def test_missing_restarts_once_and_feeds_new_process(env, fast):
    files.set_claude_ai_mark(env.data_dir)
    env.scenario(mcp_status_launch=[[LOCAL], [LOCAL, WITH_AI]], turns=[[{"text": "ок"}]])
    host = await env.started()
    sid, _, _ = await _session(env, role="программист")
    fails_before = (await _rec(env, sid)).get("launch_fails")
    await _running(env, host, sid, "посмотри задачу")
    await _turn_done(env, host, sid)
    calls = env.calls()
    assert len(calls) == 2
    # тот же разговор: id сессии движка — тот же у обоих процессов
    ids = [flag_value(c["argv"], "--resume" if "--resume" in c["argv"] else "--session-id") for c in calls]
    assert ids[0] == ids[1]
    # реплика ушла новому процессу: после его `initialize`; первый процесс вышел до неё
    assert _first_user_after(env, "initialize", 1) and len(_users(env)) == 1
    assert len(env.ends()) >= 1 and env.ends()[0]["pid"] == calls[0]["pid"]
    assert held_mod.SLEEP_NOTES["claude_ai"] in await _summaries(env, sid, "status")
    rec = await _rec(env, sid)
    assert rec.get("launch_fails") == fails_before and rec.get("failure") is None
    assert not await _summaries(env, sid, "error") and not await _summaries(env, sid, "warn")
    assert sid not in host.held.claude_ai_restarts  # коннекторы появились — счётчик сброшен
    assert sid not in host.held.retry_at


async def test_missing_twice_warns_and_feeds_as_is(env, fast, caplog):
    files.set_claude_ai_mark(env.data_dir)
    env.scenario(mcp_status=[LOCAL], turns=[[{"text": "ок"}]])
    host = await env.started()
    sid, _, _ = await _session(env, role="программист")
    with caplog.at_level(logging.WARNING, logger="hub.work"):
        await _running(env, host, sid, "посмотри задачу")
        await _turn_done(env, host, sid)
        await env.until(host, _has_event(env, sid, "warn", "после перезапуска"), what="warn")
        for _ in range(5):
            await env.cycle(host)
            await asyncio.sleep(0.05)
    assert len(env.calls()) == 2  # третьего запуска нет
    assert len(_users(env)) == 1 and _first_user_after(env, "initialize", 1)
    assert await _summaries(env, sid, "warn") == [held_mod.CLAUDE_AI_WARN]
    assert (await _summaries(env, sid, "status")).count(held_mod.SLEEP_NOTES["claude_ai"]) == 1
    assert not await _summaries(env, sid, "error")
    assert (await _rec(env, sid)).get("failure") is None
    assert any("и после перезапуска" in r.message for r in caplog.records)
    assert sid not in host.held.claude_ai_restarts  # следующий запуск снова получит свой перезапуск


async def test_mark_set_on_disabled_claude_ai_and_read_by_new_host(env, fast):
    env.scenario(mcp_status=[LOCAL_PENDING, ONLY_DISABLED_AI], turns=[[{"text": "ок"}]])
    host = await env.started()
    assert host.held.claude_ai_expected() is False
    sid, _, _ = await _session(env, role="программист")
    await _running(env, host, sid, "привет")
    await _turn_done(env, host, sid)
    assert files.has_claude_ai_mark(env.data_dir)  # и `disabled` — коннекторы есть
    assert host.held.claude_ai_expected() is True
    fresh = env.host()  # новый хост (перезапуск, деплой) читает метку из файла
    assert fresh.held.claude_ai_mark is None and fresh.held.claude_ai_expected() is True


class _Stub(runner_base.BaseRunner):
    def __init__(self, engine: str) -> None:
        self.engine = engine
        self.process = object()
        self.reason = None

    def claude_ai_expected(self) -> bool:
        return True

    async def _control_wait(self, subtype, timeout_s):
        raise AssertionError("не claude — `mcp_status` не спрашивается")


@pytest.mark.parametrize("engine", ["opencode", "cursor", "codex"])
async def test_other_engines_unchanged(engine):
    assert await _Stub(engine)._wait_mcp() is False
