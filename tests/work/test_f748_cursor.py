"""Ф74-8: долги Cursor по матрице паритета (docs/architecture.md). `cursor-agent` —
ПОДДЕЛЬНЫЙ (`fake_cursor.py`), ядро — в процессе."""

from hub.engine import cursor_process as cp

from .conftest import flag_value, pid_alive
from .test_held_cursor import _calls_n, _cur_session, cur, started  # noqa: F401 — фикстура `cur`
from .test_held_host import _has_event, _rec, _running, _say, _turn_done


def test_limit_hit_reads_codes_words_and_reset():
    assert cp.limit_hit("ConnectError: [resource_exhausted] PRO_USER_USAGE_LIMIT", now=0) == (True, None)
    assert cp.limit_hit("You've hit your usage limit. Try again in 2 hours.", now=100) == (True, 100 + 7200)
    assert cp.limit_hit("rate limited, retry in 30 min", now=0) == (True, 1800)
    assert cp.limit_hit("Error: USAGE_PRICING_REQUIRED", now=0)[0]
    assert cp.limit_hit("Error: usage pool exhausted", now=0) == (False, None)  # не лимит подписки
    assert cp.limit_hit("network error", now=0) == (False, None)
    assert cp.limit_hit(None) == (False, None)


async def test_cursor_limit_in_stderr_pauses_cursor_only(env, cur):  # noqa: F811
    host = await started(env)
    sid, _pid, _workdir = await _cur_session(env)
    stderr = "Error: You've hit your usage limit (PRO_USER_USAGE_LIMIT)"
    cur.scenario([{"text": "начал"}, {"exit": 1, "stderr": stderr}])
    await _running(env, host, sid)
    await _calls_n(env, host, cur, 1)
    await _turn_done(env, host, sid)
    pause = host.state["pauses"]["cursor"]
    assert pause["reason"] == cp.LIMIT_REASON and pause["until"] > host._now_ms()  # noqa: SLF001
    assert host.held._hold({"engine": "cursor"})[1] == "пауза лимита подписки"  # noqa: SLF001
    assert host.held._hold({"engine": "claude"}) is None  # noqa: SLF001 — пауза только своего движка
    assert "claude" not in host.state["pauses"]


async def test_cursor_limit_in_error_result_takes_reset_from_text(env, cur):  # noqa: F811
    host = await started(env)
    sid, _pid, _workdir = await _cur_session(env)
    text = "Rate limit exceeded. Try again in 3 hours."
    cur.scenario([{"final": {"subtype": "error", "is_error": True, "result": text}}])
    before = host._now_ms()  # noqa: SLF001
    await _running(env, host, sid)
    await _calls_n(env, host, cur, 1)
    await _turn_done(env, host, sid)
    pause = host.state["pauses"]["cursor"]
    assert pause["until"] >= before + 3 * 3600 * 1000


async def test_cursor_plain_error_does_not_pause(env, cur):  # noqa: F811
    host = await started(env)
    sid, _pid, _workdir = await _cur_session(env)
    cur.scenario([{"exit": 1, "stderr": "Error: network unreachable"}])
    await _running(env, host, sid)
    await _calls_n(env, host, cur, 1)
    await _turn_done(env, host, sid)
    assert "cursor" not in (host.state.get("pauses") or {})


async def test_cursor_subagent_is_a_task_of_the_session(env, cur):  # noqa: F811
    host = await started(env)
    sid, _pid, _workdir = await _cur_session(env)
    task = {
        "tool": "task",
        "args": {"description": "найти тесты", "prompt": "…", "subagentType": {"explore": {}}},
        "result": {"success": {"summary": "нашёл три"}},
    }
    cur.scenario([{"text": "зову помощника"}, task, {"text": "готово"}])
    await _running(env, host, sid)
    await _calls_n(env, host, cur, 1)
    await _turn_done(env, host, sid)
    runner = host.held.runners[sid]
    [entry] = runner.bg.values()
    assert entry["kind"] == "agent" and entry["title"] == "найти тесты" and entry["agent_type"] == "explore"
    assert entry["state"] == "completed" and entry["note"] == "нашёл три"
    assert not runner.bg_holds(host.monotonic())


async def test_cursor_subagent_without_end_ends_with_the_turn(env, cur):  # noqa: F811
    """Поток не сказал о конце подагента (ход оборвался) — подагент не держит сессию вечно."""
    host = await started(env)
    sid, _pid, _workdir = await _cur_session(env)
    hanging = {"tool": "task", "args": {"description": "висит"}, "no_end": True}
    cur.scenario([{"text": "зову"}, hanging, {"exit": 1, "stderr": "Error: boom"}])
    await _running(env, host, sid)
    runner = host.held.runners[sid]
    await _calls_n(env, host, cur, 1)
    await _turn_done(env, host, sid)
    assert [e["state"] for e in runner.bg.values()] == ["failed"]


async def test_cursor_effort_raise_is_a_feed_line(env, cur):  # noqa: F811
    host = await started(env)
    sid, _pid, _workdir = await _cur_session(env, effort="low")
    cur.scenario([{"text": "ок"}])
    await _running(env, host, sid)
    await _calls_n(env, host, cur, 1)
    await _turn_done(env, host, sid)
    assert flag_value(cur.calls()[0]["argv"], "--model") == "grok-4.7-high"
    assert await _has_event(env, sid, "warn", "поднято до high")()


def _usage(prompt_per_call: int, calls: int) -> dict:
    return {"inputTokens": 100, "outputTokens": 10, "cacheReadTokens": prompt_per_call * calls - 100}


async def test_cursor_self_summary_resends_session_rules(env, cur):  # noqa: F811
    """Подсказка на вызов модели упала с 100k до 10k — Cursor сжал разговор: правила сессии уходят снова."""
    host = await started(env)
    sid, _pid, _workdir = await _cur_session(env)
    cur.scenario(
        [{"text": "раз"}, {"final": {"usage": _usage(100_000, 1)}}],
        [{"text": "два"}, {"text": "два-б"}, {"final": {"usage": _usage(90_000, 2)}}],
        [{"text": "три"}, {"final": {"usage": _usage(10_000, 1)}}],
        [{"text": "четыре"}],
    )
    await _running(env, host, sid)
    await _calls_n(env, host, cur, 1)
    await _turn_done(env, host, sid)
    assert cur.calls()[0]["stdin"].startswith("<hub_session_rules>")
    for n, text in ((2, "ещё"), (3, "и ещё")):
        await _say(env, sid, text)
        await _calls_n(env, host, cur, n)
        await _turn_done(env, host, sid)
    assert "<hub_session_rules>" not in cur.calls()[1]["stdin"]  # 90k на вызов — не сжатие
    assert "<hub_session_rules>" not in cur.calls()[2]["stdin"]
    assert await _has_event(env, sid, "status", "сжал разговор сам")()
    await _say(env, sid, "дальше")
    await _calls_n(env, host, cur, 4)
    await _turn_done(env, host, sid)
    assert cur.calls()[3]["stdin"].startswith("<hub_session_rules>")
    assert (await _rec(env, sid))["state"] == "idle"


async def test_cursor_cache_goes_cold_after_held_cache_s_cursor(env):
    """Холодный подъём (Ф41-4, 3.2) у Cursor — как у Codex: кэш провайдера живёт `held_cache_s_cursor` от
    конца последнего хода; без срока — холодная."""
    host = await env.started(held_cache_s_cursor=60)
    held = host.held
    now = held.now_ms()
    assert held._cold({"engine": "cursor", "last_turn_ts": now - 30_000}) is False  # noqa: SLF001
    assert held._cold({"engine": "cursor", "last_turn_ts": now - 61_000}) is True  # noqa: SLF001
    assert held._cold({"engine": "cursor"}) is True  # noqa: SLF001


async def test_cursor_interrupt_kills_commands_of_the_turn(env, cur):  # noqa: F811
    """Ф74-8 (живой стенд): команда Shell Cursor стоит в своей группе процессов — после «Прервать» она жила
    дальше. Теперь хост снимает команды прерванного хода."""
    host = await started(env)
    sid, _pid, _workdir = await _cur_session(env)
    cur.scenario([{"text": "начал"}, {"spawn": "exec sleep 120"}, {"hang": 30}])
    await _running(env, host, sid, "долгое")
    await _calls_n(env, host, cur, 1)
    await env.until(host, _has_event(env, sid, "text", "начал"), what="ход идёт")
    await env.until(host, lambda: cur.jsonl("spawned.jsonl"), what="команда запущена")
    child = cur.jsonl("spawned.jsonl")[0]["pid"]
    assert pid_alive(child)
    r = await env.ui("POST", f"/sessions/{sid}/interrupt")
    assert r.status_code == 200, r.text
    await _turn_done(env, host, sid)
    await env.until(host, lambda: not pid_alive(child), what="команда снята", limit_s=10)

