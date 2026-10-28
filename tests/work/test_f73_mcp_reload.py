"""Ф73-1 (docs/architecture.md и 4): «Перечитать MCP» у сессии хаба — хост
перезапускает процесс с продолжением разговора, не обрывая идущий ход; спящую поднимает без хода; итог —
событие ленты, просьба снимается исполненным значением. Движки поддельные: `fake_claude`, `fake_cursor`,
поддельный OpenCode."""

import json

from hub.work import held as held_mod

from .oc_helpers import oc  # noqa: F401 — фикстура `oc`
from .oc_helpers import started as oc_started
from .test_f64_workday_host import _day, _workday
from .test_held_cursor import _calls_n, _cur_session, cur  # noqa: F401 — фикстура `cur`
from .test_held_cursor import started as cur_started
from .test_held_host import _alive, _events, _rec, _running, _session, _state, _turn_done, _users
from .test_held_opencode import MODEL, _oc_session
from .test_held_opencode import _running as _oc_running

SERVERS = [
    {"name": "hub-all", "status": "connected"},
    {"name": "notion", "status": "needs-auth"},
    {"name": "stage-logs", "status": "failed"},
]
CLAUDE_TEXT = "MCP перечитаны: hub-all ✓, notion — нужен вход (needs-auth), stage-logs — не подключился"


async def _press(env, sid) -> int:
    r = await env.ui("POST", f"/sessions/{sid}/mcp-reload")
    assert r.status_code == 200, r.text
    return r.json()["session"]["mcp_reload_ts"]


def _reloaded(env, sid):
    async def check():
        return any(
            e["kind"] == "status" and e["summary"].startswith(held_mod.MCP_RELOAD_HEAD)
            for e in await _events(env, sid)
        )

    return check


def _cleared(env, sid):
    async def check():
        return (await _rec(env, sid))["mcp_reload_ts"] is None

    return check


async def _summaries(env, sid) -> list[str]:
    return [e["summary"] for e in await _events(env, sid)]


def test_mcp_reload_text_words():
    assert held_mod.mcp_reload_text([(s["name"], s["status"]) for s in SERVERS]) == CLAUDE_TEXT
    assert held_mod.mcp_reload_text([("x", "pending"), ("y", "weird")]) == (
        "MCP перечитаны: x — подключается, y — weird"
    )
    assert held_mod.mcp_reload_text([]) == "MCP перечитаны: серверов нет"
    assert "не сообщил" in held_mod.mcp_reload_text(None)


def test_mcp_reload_text_skips_disabled():
    """Ф71-3: выключенные (`disabledMcpServers` у Claude, выключенные хабом у OpenCode) — не в итоге."""
    assert held_mod.mcp_reload_text([("hub-all", "connected"), ("stage-k8s", "disabled")]) == (
        "MCP перечитаны: hub-all ✓"
    )
    assert held_mod.mcp_reload_text([("x", "disabled")]) == "MCP перечитаны: серверов нет"


# --- Claude Code --------------------------------------------------------------------------------------------


async def test_idle_process_restarts_resumes_and_reports(env):
    env.scenario(mcp_status=[SERVERS], turns=[[{"text": "ок"}]])
    host = await env.started(held_signal_wake_gap_s=0)
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    first = host.held.runners[sid]
    assert first.mcp_for is None

    asked = await _press(env, sid)
    await env.until(host, lambda: len(env.calls()) == 2, what="перезапуск процесса")
    await env.until(host, _reloaded(env, sid), what="итог в ленте")
    await env.until(host, _cleared(env, sid), what="просьба снята")
    assert first.reason == "mcp"
    assert "--resume" in env.calls()[1]["argv"]  # разговор тот же
    second = host.held.runners[sid]
    assert second.wake == "mcp" and second.mcp_for == asked and second.alive
    assert len(_users(env)) == 1  # подъём без хода
    summaries = await _summaries(env, sid)
    assert held_mod.SLEEP_NOTES["mcp"] in summaries and CLAUDE_TEXT in summaries
    assert summaries.index(held_mod.SLEEP_NOTES["mcp"]) < summaries.index(CLAUDE_TEXT)
    for _ in range(3):
        await env.cycle(host)
    # Процесс, поднятый ради MCP, живёт по обычным правилам простоя; повторного перезапуска нет.
    assert len(env.calls()) == 2 and host.held.runners[sid] is second and second.reason is None
    assert (await _summaries(env, sid)).count(CLAUDE_TEXT) == 1


async def test_running_turn_is_not_cut_restart_after_it(env):
    env.scenario(mcp_status=[SERVERS], turns=[[{"sleep": 1.0}, {"text": "дошёл"}]])
    host = await env.started(held_signal_wake_gap_s=0)
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    await env.until(host, _state(env, sid, "running"), what="ход идёт")
    await _press(env, sid)
    for _ in range(3):
        await env.cycle(host)
    runner = host.held.runners[sid]
    assert runner.busy and runner.reason is None and len(env.calls()) == 1  # ход не трогается
    assert (await _rec(env, sid))["mcp_reload_ts"] is not None
    await env.until(host, lambda: len(env.calls()) == 2, what="перезапуск после хода")
    await env.until(host, _cleared(env, sid), what="просьба снята")
    summaries = await _summaries(env, sid)
    assert summaries.index("дошёл") < summaries.index(held_mod.SLEEP_NOTES["mcp"])
    assert CLAUDE_TEXT in summaries and runner.cut is False
    assert len(_users(env)) == 1


async def test_asleep_session_woken_without_turn_and_repeat_press_restarts(env):
    env.scenario(mcp_status=[SERVERS])
    host = await env.started(held_signal_wake_gap_s=0)
    sid, _, _ = await _session(env)
    for _ in range(3):
        await env.cycle(host)
    assert env.calls() == [] and host.held.runners == {}  # без просьбы — лениво

    asked = await _press(env, sid)
    await env.until(host, lambda: sid in _alive(host), what="подъём без хода")
    runner = host.held.runners[sid]
    assert runner.wake == "mcp" and runner.mcp_for == asked
    await env.until(host, _cleared(env, sid), what="просьба снята")
    assert _users(env) == [] and (await _summaries(env, sid)).count(CLAUDE_TEXT) == 1
    assert (await _rec(env, sid))["state"] == "idle"

    # Нажал ещё раз: процесс запущен раньше нового нажатия — перезапуск при простое.
    again = await _press(env, sid)
    assert again > asked
    await env.until(host, lambda: len(env.calls()) == 2, what="второй перезапуск")
    await env.until(host, _cleared(env, sid), what="вторая просьба снята")
    assert runner.reason == "mcp" and host.held.runners[sid].mcp_for == again
    assert _users(env) == [] and (await _summaries(env, sid)).count(CLAUDE_TEXT) == 2


async def test_asleep_session_not_woken_in_closed_day(env):
    host = await env.started(held_signal_wake_gap_s=0)
    sid, pid, _ = await _session(env)
    await _workday(env, pid)
    await _day(env, pid, False)
    await _press(env, sid)
    for _ in range(4):
        await env.cycle(host)
    assert env.calls() == [] and host.held.runners == {}  # закрытый день спящую не поднимает
    assert (await _rec(env, sid))["mcp_reload_ts"] is not None
    await _day(env, pid, True)
    await env.until(host, _cleared(env, sid), what="день открыт — перечитано")
    assert len(env.calls()) == 1 and _users(env) == []


# --- Cursor -------------------------------------------------------------------------------------------------


async def test_cursor_plugin_rewritten_with_new_server(env, cur):  # noqa: F811
    host = await cur_started(env, held_signal_wake_gap_s=0)
    sid, _pid, _workdir = await _cur_session(env)
    cur.scenario([{"text": "первый"}])
    await _running(env, host, sid, "привет")
    await _calls_n(env, host, cur, 1)
    await _turn_done(env, host, sid)
    plugin = held_mod.files.cursor_plugin_dir(env.data_dir, sid) / "mcp.json"
    assert set(json.loads(plugin.read_text())["mcpServers"]) == {"hub-all"}

    # Владелец зарегистрировал MCP в Claude Code — плагин перепишется при перезапуске.
    (env.home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"youtrack": {"command": "yt-mcp", "args": ["--stdio"]}}}), encoding="utf-8"
    )
    await _press(env, sid)
    await env.until(host, _cleared(env, sid), what="просьба снята")
    assert set(json.loads(plugin.read_text())["mcpServers"]) == {"hub-all", "youtrack"}
    text = f"{held_mod.MCP_RELOAD_CURSOR_HEAD}: hub-all, youtrack"
    summaries = await _summaries(env, sid)
    assert text in summaries and held_mod.SLEEP_NOTES["mcp"] in summaries
    assert len(cur.calls()) == 1  # подъём без хода: cursor-agent не запускался


# --- OpenCode -----------------------------------------------------------------------------------------------


async def test_opencode_server_restarted_and_status_reported(env, oc):  # noqa: F811
    oc.scenario([{"text": "раз"}], mcp_extra={"notion": {"status": "needs_auth"}})
    host = await oc_started(env, held_signal_wake_gap_s=0)
    sid, _, _ = await _oc_session(env, model=MODEL)
    await _oc_running(env, host, oc, sid)
    await _turn_done(env, host, sid)
    ses = (await _rec(env, sid))["engine_session_id"]
    await _press(env, sid)
    await env.until(host, _cleared(env, sid), what="просьба снята")
    assert len(oc.jsonl("starts.jsonl")) == 2 and len(oc.created()) == 1  # тот же разговор
    assert (await _rec(env, sid))["engine_session_id"] == ses
    [text] = [s for s in await _summaries(env, sid) if s.startswith(held_mod.MCP_RELOAD_HEAD)]
    assert text.startswith("MCP перечитаны: ") and "notion — нужен вход (needs_auth)" in text
    assert len(oc.prompts()) == 1  # без хода


async def test_opencode_without_mcp_answer_says_server_restarted(env, oc, monkeypatch):  # noqa: F811
    oc.scenario([{"text": "раз"}])
    host = await oc_started(env, held_signal_wake_gap_s=0)
    sid, _, _ = await _oc_session(env, model=MODEL)
    await _oc_running(env, host, oc, sid)
    await _turn_done(env, host, sid)

    async def broken(self):
        raise ValueError("ответ сервера не разобран")

    from hub.engine import oc_server

    monkeypatch.setattr(oc_server.OcClient, "mcp_status", broken)
    await _press(env, sid)
    await env.until(host, _cleared(env, sid), what="просьба снята")
    assert held_mod.MCP_RELOAD_OC_TEXT in await _summaries(env, sid)
