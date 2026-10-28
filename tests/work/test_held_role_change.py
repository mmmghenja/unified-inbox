"""Смена роли у живого процесса постоянной сессии: правила роли вклеиваются в
системный текст при запуске, поэтому хост перезапускает процесс при простое — тем же путём, что при смене
модели (Ф41-7а); следующий вход — с правилами новой роли. Движок поддельный."""

from hub import roles
from hub.work import held as held_mod

from .conftest import flag_value
from .test_held_host import _events, _rec, _running, _say, _session, _state, _turn_done

SYSTEM = "--append-system-prompt"


async def _patch(env, sid, **body) -> dict:
    r = await env.client.patch(f"/api/v1/sessions/{sid}", json=body, headers=await env.ui_headers())
    assert r.status_code == 200, r.text
    return r.json()["session"]


async def test_role_change_restarts_the_live_process_when_idle(env):
    env.scenario(turns=[[{"sleep": 1.0}, {"text": "дошёл"}]])
    host = await env.started()
    sid, _, _ = await _session(env, role="программист")
    await _running(env, host, sid)
    await env.until(host, _state(env, sid, "running"), what="ход идёт")
    first = flag_value(env.calls()[0]["argv"], SYSTEM)
    assert "роль «программист» 1" in first and roles.common_text() in first
    assert host.held.runners[sid].launch_role == ("программист", 1)

    await _patch(env, sid, role="тестер", number=2)  # посреди хода — ход не трогается
    for _ in range(3):
        await env.cycle(host)
    assert sid in host.held.runners and host.held.runners[sid].busy
    await env.until(host, lambda: sid not in host.held.runners, what="перезапуск при простое")
    await env.until(host, _state(env, sid, "asleep"), what="спит")
    events = await _events(env, sid)
    summaries = [e["summary"] for e in events]
    assert "дошёл" in summaries and held_mod.SLEEP_NOTES["role"] in summaries
    assert [e["brief"] for e in events if (e.get("brief") or {}).get("type") == "role"] == [
        {"type": "role", "role": "тестер 2"}
    ]
    assert len(env.calls()) == 1  # сама не поднимается — со следующего входа

    env.scenario(turns=[[{"text": "в новой роли"}]])
    await _say(env, sid, "ещё")
    await env.until(host, lambda: len(env.calls()) == 2, what="запуск с новой ролью")
    second = env.calls()[1]["argv"]
    system = flag_value(second, SYSTEM)
    assert "роль «тестер» 2" in system and roles.common_text() in system
    assert "роль «программист»" not in system
    assert "--resume" in second  # разговор тот же
    await _turn_done(env, host, sid)
    assert host.held.runners[sid].launch_role == ("тестер", 2)

    # Та же роль — процесс живёт; роль снята — перезапуск, блока роли в системном тексте нет.
    await _patch(env, sid, role="тестер")
    for _ in range(3):
        await env.cycle(host)
    assert sid in host.held.runners and host.held.runners[sid].reason is None
    await _patch(env, sid, role=None)
    await env.until(host, lambda: sid not in host.held.runners, what="роль снята — перезапуск")
    assert (await _rec(env, sid))["role"] is None
    env.scenario(turns=[[{"text": "ок"}]])
    await _say(env, sid, "и ещё")
    await env.until(host, lambda: len(env.calls()) == 3, what="запуск без роли")
    third = flag_value(env.calls()[2]["argv"], SYSTEM)
    assert "без роли" in third and "Роль и взаимодействие (правила хаба):" not in third
    await _turn_done(env, host, sid)
    assert host.held.runners[sid].launch_role == (None, None)


async def test_role_change_of_sleeping_session_needs_no_restart(env):
    """Спящая сессия просто поднимется с новой ролью."""
    host = await env.started()
    sid, _, _ = await _session(env, role="программист")
    await _patch(env, sid, role="тестер")
    for _ in range(2):
        await env.cycle(host)
    assert sid not in host.held.runners and not env.calls()
    env.scenario(turns=[[{"text": "привет"}]])
    await _running(env, host, sid)
    system = flag_value(env.calls()[0]["argv"], SYSTEM)
    assert "роль «тестер» 1" in system and roles.common_text() in system
    summaries = [e["summary"] for e in await _events(env, sid)]
    assert held_mod.SLEEP_NOTES["role"] not in summaries
