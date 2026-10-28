"""Ф83-1 (docs/architecture.md; план phase83-00, П1, П7, П9): хост и запасной движок.

Стенд: ядро в процессе, `claude` — подставной (`fake_claude.py`); пауза — событие лимита хоста
(`on_rate_limit`), как от `rate_limit_event`. Проверяется:
- паузы хоста уходят в ядро в каждом ожидании (`pauses`, П9) с видом (`kind`);
- сессия в авто на паузе лимита с репликой переходит на запасной ровно один раз (передача дел, `by: auto`);
- ручная — не переходит: карточка паузы у владельца с ответом «Перейти на …»;
- «работа сверх подписки» и отказ окна Opus — не повод;
- запасной движок выключен у хоста — одна строка в окне на эпизод, перехода нет."""

import pytest

from hub.core import engine as engine_mod
from hub.core import engine_fallback, held

from .test_held_host import _rec, _running, _say, _session, _turn_done, _users


@pytest.fixture
def codex_home(env, monkeypatch):
    """Ядро видит подставной `codex` (доступность запасного)."""
    bin_dir = env.home / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / "codex"
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o755)
    monkeypatch.setattr(engine_mod, "_home", lambda: env.home)
    return env.home


async def _paused_with_say(env, host, *, mode, fallback="codex", info=None):
    sid, pid, _ = await _session(env, role="маркетолог", mode=mode)
    if fallback is not None:
        r = await env.ui("PATCH", f"/sessions/{sid}", {"fallback": {"engine": fallback}})
        assert r.status_code == 200, r.text
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    host.on_rate_limit(info or {"status": "rejected"})
    await _say(env, sid, "после паузы")
    return sid, pid


def _sessions(env, pid) -> list[held.HubSession]:
    with env.db.reader() as conn:
        return held.list_sessions(conn, project_id=pid)


def _status_events(env, sid) -> list[str]:
    with env.db.reader() as conn:
        rec = held.get(conn, sid)
        rows = conn.execute(
            f"SELECT summary FROM {rec.store}.held_event WHERE session_id = ? AND kind = 'status'",  # noqa: S608
            (sid,),
        ).fetchall()
    return [r[0] or "" for r in rows]


async def test_pauses_reach_core_with_kind(env):
    env.scenario(turns=[[{"text": "ок"}]])
    host = await env.started()
    host.on_rate_limit({"status": "rejected"})
    assert host.state["pauses"]["claude"]["kind"] == "limit"
    await env.cycle(host)
    with env.db.reader() as conn:
        pause = engine_fallback.pause_of(conn, "claude", host.held.now_ms())
    assert pause is not None and pause["kind"] == "limit"
    # Отказ только окна Opus — не лимит подписки (П5): вид `other`.
    host.on_rate_limit({"status": "rejected", "rateLimitType": "seven_day_opus"})
    assert host.state["pauses"]["claude"]["kind"] == "other"


async def test_auto_switches_once(env, codex_home):
    env.scenario(turns=[[{"text": "ок"}]])
    host = await env.started()
    sid, pid = await _paused_with_say(env, host, mode="auto")

    def _switched():
        return any(s.engine == "codex" for s in _sessions(env, pid))

    await env.until(host, _switched, what="переход на запасной")
    for _ in range(4):
        await env.cycle(host)
    opened = _sessions(env, pid)
    assert [s.engine for s in opened] == ["codex"]  # одна новая, старая закрыта
    new = opened[0]
    assert new.on_fallback_json is not None and '"by": "auto"' in new.on_fallback_json
    with env.db.reader() as conn:
        assert held.get(conn, sid).closed_ts is not None
        switches = conn.execute(
            "SELECT COUNT(*) FROM main.audit WHERE action = ?", (engine_fallback.AUDIT_SESSION_SWITCH,)
        ).fetchone()[0]
    assert switches == 1
    assert sid not in host.held.runners
    assert len(_users(env)) == 1  # реплика под паузой старому процессу не подана


async def test_manual_gets_card_not_switch(env, codex_home):
    env.scenario(turns=[[{"text": "ок"}]])
    host = await env.started()
    sid, pid = await _paused_with_say(env, host, mode="manual")
    await env.until(host, lambda: _rec(env, sid), what="запись")

    async def _paused():
        return (await _rec(env, sid))["wait_reason"] == "paused"

    await env.until(host, _paused, what="пауза")
    for _ in range(3):
        await env.cycle(host)
    assert [s.id for s in _sessions(env, pid)] == [sid]
    r = await env.ui("GET", "/owner_channel/queue")
    card = next(c for c in r.json()["items"] if c["ref"] == f"paused:{sid}")
    assert [a["action"] for a in card["answers"]] == ["wake", "fallback"]


async def test_overage_is_not_a_reason(env, codex_home):
    env.scenario(turns=[[{"text": "ок"}]])
    host = await env.started()
    sid, pid = await _paused_with_say(
        env, host, mode="auto", info={"status": "allowed_warning", "isUsingOverage": True}
    )
    for _ in range(5):
        await env.cycle(host)
    assert host.state.get("overage")
    assert [s.id for s in _sessions(env, pid)] == [sid]
    assert sid not in host.held.fallback_tried


async def test_fallback_engine_off_at_host_one_line(env, codex_home):
    env.scenario(turns=[[{"text": "ок"}]])
    host = await env.started(held_codex=False)
    sid, pid = await _paused_with_say(env, host, mode="auto")
    for _ in range(6):
        await env.cycle(host)
    assert [s.id for s in _sessions(env, pid)] == [sid]
    lines = [t for t in _status_events(env, sid) if t.startswith("запасной движок")]
    assert len(lines) == 1, lines
