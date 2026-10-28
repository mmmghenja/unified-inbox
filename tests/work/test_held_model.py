"""Ф41-7а (docs/architecture.md): смена модели и усилия у живого процесса
постоянной сессии — хост перезапускает его при простое (тот же путь, что у ослабления выключателей
автопилота); следующий вход — с новой моделью. Движок поддельный."""

from hub.work import held as held_mod

from .conftest import flag_value
from .test_held_host import _events, _rec, _running, _say, _session, _state, _turn_done


async def _patch(env, sid, **body) -> dict:
    r = await env.client.patch(f"/api/v1/sessions/{sid}", json=body, headers=await env.ui_headers())
    assert r.status_code == 200, r.text
    return r.json()["session"]


async def test_model_change_restarts_the_live_process_when_idle(env):
    env.scenario(turns=[[{"sleep": 1.0}, {"text": "дошёл"}]])
    host = await env.started()
    sid, _, _ = await _session(env, model="opus", effort="high")
    await _running(env, host, sid)
    await env.until(host, _state(env, sid, "running"), what="ход идёт")
    first = env.calls()[0]["argv"]
    assert flag_value(first, "--model") == "opus"
    assert host.held.runners[sid].launch_model == ("opus", "high")

    await _patch(env, sid, model="sonnet")  # посреди хода — ход не трогается
    for _ in range(3):
        await env.cycle(host)
    assert sid in host.held.runners and host.held.runners[sid].busy
    await env.until(host, lambda: sid not in host.held.runners, what="перезапуск при простое")
    await env.until(host, _state(env, sid, "asleep"), what="спит")
    events = await _events(env, sid)
    summaries = [e["summary"] for e in events]
    assert "дошёл" in summaries and held_mod.SLEEP_NOTES["model"] in summaries
    assert [e["brief"] for e in events if (e.get("brief") or {}).get("type") == "model"] == [
        {"type": "model", "model": "sonnet", "effort": "high"}
    ]
    assert len(env.calls()) == 1  # сама не поднимается — со следующего входа

    env.scenario(turns=[[{"text": "на новой"}]])
    await _say(env, sid, "ещё")
    await env.until(host, lambda: len(env.calls()) == 2, what="запуск с новой моделью")
    second = env.calls()[1]["argv"]
    assert flag_value(second, "--model") == "sonnet"
    assert "--resume" in second  # разговор тот же
    await _turn_done(env, host, sid)
    assert host.held.runners[sid].launch_model == ("sonnet", "high")

    # То же значение — процесс живёт; смена режима и модели разом — один перезапуск.
    await _patch(env, sid, model="sonnet", effort="high")
    for _ in range(3):
        await env.cycle(host)
    assert sid in host.held.runners and host.held.runners[sid].reason is None
    await _patch(env, sid, effort=None)
    await env.until(host, lambda: sid not in host.held.runners, what="усилие сменилось — перезапуск")
    assert (await _rec(env, sid))["effort"] is None
    env.scenario(turns=[[{"text": "ок"}]])
    await _say(env, sid, "и ещё")
    await env.until(host, lambda: len(env.calls()) == 3, what="запуск после смены усилия")
    assert flag_value(env.calls()[2]["argv"], "--model") == "sonnet"
    await _turn_done(env, host, sid)
    assert host.held.runners[sid].launch_model == ("sonnet", None)
