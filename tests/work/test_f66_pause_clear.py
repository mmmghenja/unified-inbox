"""Ф66-3 F (docs/architecture.md): «Снять паузу лимита». Ручка `POST /work/pause/clear`
пишет в ядре отметку; хост видит её в снимке ожидания и убирает паузы лимита и «работу сверх подписки»,
начатые не позже отметки; реплики из очереди подаются сразу.

Ядро — в процессе, `claude` — подставной (`fake_claude.py`)."""

import asyncio

import pytest

from hub.agent.core_api import CoreError

from .test_held_host import _alive, _rec, _running, _say, _session, _turn_done, _users


async def _clear(env) -> dict:
    r = await env.ui("POST", "/work/pause/clear")
    assert r.status_code == 200, r.text
    return r.json()


async def test_route_is_cookie_only(env):
    env.scenario(turns=[[{"text": "ок"}]])
    host = await env.started()
    with pytest.raises(CoreError) as exc:  # токен хоста (bearer `work_host`) — 403
        await host.core.post("/work/pause/clear")
    assert exc.value.status == 403
    cleared = await _clear(env)
    assert cleared["ok"] is True
    with env.db.reader() as conn:
        value = conn.execute("SELECT value FROM main.meta WHERE key = 'work_pause_cleared_ts'").fetchone()
        audit = conn.execute("SELECT actor FROM main.audit WHERE action = 'work_pause_clear'").fetchall()
    assert int(value[0]) == cleared["cleared_ts"] and [a[0] for a in audit] == ["ui"]


async def test_clear_lifts_pause_and_delivers_queued_say(env):
    env.scenario(turns=[[{"text": "ок"}], [{"text": "после паузы"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    host.on_rate_limit({"status": "rejected"})
    assert host.state["pauses"]["claude"]["reason"] == "лимит подписки"
    assert host.state["pauses"]["claude"]["since"] > 0
    await _say(env, sid, "после паузы")
    for _ in range(4):
        await env.cycle(host)
    assert len(_users(env)) == 1  # реплика ждёт паузу
    assert (await _rec(env, sid))["wait_reason"] == "paused"

    await asyncio.sleep(0.01)
    await _clear(env)
    await env.until(host, lambda: len(_users(env)) == 2, what="реплика подана после снятия паузы")
    assert "pause" not in host.state and not host.state.get("pauses") and "overage" not in host.state
    assert _alive(host) == [sid]
    assert (await _rec(env, sid))["wait_reason"] is None


async def test_old_mark_does_not_lift_newer_pause(env):
    env.scenario(turns=[[{"text": "ок"}]])
    host = await env.started()
    await _clear(env)  # отметка ставится ДО паузы
    await asyncio.sleep(0.01)
    host.on_rate_limit({"status": "rejected"})
    host.on_rate_limit({"status": "allowed_warning", "isUsingOverage": True})
    for _ in range(3):
        await env.cycle(host)
    # Хост узнал отметку, но пауза и переплата начались позже неё — остаются.
    assert host.pause_cleared_ts > 0
    assert host.state["pauses"]["claude"]["reason"] == "лимит подписки" and host.state.get("overage")
    await asyncio.sleep(0.01)
    await _clear(env)
    await env.cycle(host)
    assert not host.state.get("pauses") and not host.state.get("overage")
