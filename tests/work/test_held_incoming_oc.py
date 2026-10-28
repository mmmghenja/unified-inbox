"""Ф41-4а (docs/architecture.md): входящее постоянной сессии на OpenCode — сигнал
хаба уходит частью `synthetic: true` (реплика владельца — обычной частью); срока кэша у OpenCode нет — «холодной»
такая сессия не бывает. `opencode` — ПОДДЕЛЬНЫЙ сервер `tests/engine/fake_opencode.py`."""

import json

from .oc_helpers import (
    oc,  # noqa: F401 — фикстура `oc`
    started,
)
from .test_held_host import _alive, _events, _rec, _say, _session, _turn_done
from .test_held_incoming import ROLE, _peer, _signal_events, _subscribe, _touch

MODEL = "deepseek/deepseek-flash"


async def test_oc_signal_is_synthetic_part_and_say_is_not(env, oc):  # noqa: F811
    oc.scenario([{"text": "первый"}], [{"text": "прочитала"}])
    host = await started(env)
    sid, pid, workdir = await _session(env, engine="opencode", role=ROLE, model=MODEL)
    await _subscribe(env, sid, workdir)
    await _say(env, sid, "привет")
    await env.until(host, lambda: sid in _alive(host) and len(oc.prompts()) == 1, what="первый ход")
    await _turn_done(env, host, sid)
    msg_id = await _peer(env, pid)
    await env.until(host, lambda: len(oc.prompts()) == 2, what="сигнал подан")
    await _turn_done(env, host, sid)
    say, signal = (p["body"]["parts"] for p in oc.prompts())
    assert say == [{"type": "text", "text": "привет"}]  # реплика владельца — обычной частью
    [part] = signal
    assert part["synthetic"] is True and part["type"] == "text"
    assert part["text"].startswith("Сигнал хаба по подписке")
    assert f"сообщение владельца из хаба #{msg_id} — hub_agent_inbox()" in part["text"]
    assert "секретный текст" not in json.dumps(oc.prompts(), ensure_ascii=False)
    [note] = _signal_events(await _events(env, sid))
    assert note["brief"]["from_user"] == 1
    assert (await _rec(env, sid))["state"] == "idle"


async def test_oc_heavy_session_is_never_cold(env, oc):  # noqa: F811
    """Большой контекст сессии OpenCode подъёму по сигналу не мешает: срока кэша у движка нет."""
    oc.scenario([{"text": "проснулась"}])
    host = await started(env)
    host.config["held_wake_max_tokens"] = 1000
    sid, pid, workdir = await _session(env, engine="opencode", role=ROLE, model=MODEL)
    await _subscribe(env, sid, workdir)
    _touch(env, sid, usage_json=json.dumps({"context_tokens": 500_000, "context_limit": None}))
    await _peer(env, pid)
    await env.until(host, lambda: len(oc.prompts()) == 1, what="подъём по сигналу")
    await _turn_done(env, host, sid)
    assert oc.prompts()[0]["body"]["parts"][0]["synthetic"] is True
    assert (await _rec(env, sid))["wait_reason"] is None
