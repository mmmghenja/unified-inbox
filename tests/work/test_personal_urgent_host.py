"""21.10 (живой случай: личка коллеги ждала дежурную Globex посреди хода ровно `held_midturn_after_s`):
личная карточка разборщика для занятой сессии Claude Code подаётся посреди хода сразу (ядро считает её
срочной), обычная — по-прежнему после порога; спящую «тяжёлую холодную» личное поднимает, обычное — нет.

Ядро — в процессе, `claude` — подставной (как в test_f50_mid_turn.py). Подписка сессии — режим «карточки»."""

import time

from .conftest import _api
from .test_f50_mid_turn import LONG, _busy, _heavy
from .test_held_host import _rec, _session, _turn_done, _users
from .test_held_incoming import ROLE, _no_debounce, _subscribe, _text


async def _start(env, turns, **config):
    env.scenario(lifecycle=True, turns=turns)
    host = await env.started(**config)
    project = env.project("Globex")
    sid, pid, workdir = await _session(env, project=project, role=ROLE)
    await _subscribe(env, sid, workdir, mode="cards")
    acc = _api.insert_account(env.db, source="slack")
    triage = _api.make_bearer_token(env.db, kind="background", scopes=("read", "triage"))
    return host, sid, pid, acc, triage


async def _card(env, triage, pid, acc, ext, *, kind) -> int:
    """Карточка разборщика по правилам (метка `reply`) по свежему чужому сообщению беседы `kind`."""
    channel = _api.insert_channel(env.db, acc, project_id=pid, ext_id=ext, kind=kind, title=ext)
    msg = _api.insert_message(env.db, channel, ext_id="1", ts=int(time.time() * 1000), text="чужой текст")
    r = await env.client.post(
        "/api/v1/agent/items",
        json={
            "channel_id": channel,
            "message_ids": [msg],
            "label": "reply",
            "summary": "сводка",
            "action": "личное" if kind == "dm" else "беседа «реагирую»",
            "provider": "rules",
            "model": "-",
        },
        headers={"Authorization": f"Bearer {triage}"},
    )
    assert r.status_code == 200, r.text
    return channel


async def test_personal_card_is_fed_mid_turn_at_once_plain_waits_threshold(env):
    host, sid, pid, acc, triage = await _start(env, [LONG], held_midturn_after_s=3600)
    await _busy(env, host, sid)
    runner = host.held.runners[sid]
    group = await _card(env, triage, pid, acc, "grp", kind="group")
    await env.until(host, lambda: host.held.sig_ready.get(sid) == 1, what="обычная карточка готова")
    for _ in range(3):
        await env.cycle(host)
    # Обычная карточка занятую сессию до порога не тревожит (как раньше).
    assert len(_users(env)) == 1 and host.held.sig_urgent == {}
    _no_debounce(env)
    dm = await _card(env, triage, pid, acc, "dm-colleague", kind="dm")
    await env.until(host, lambda: len(_users(env)) == 2, what="личная карточка подана посреди хода")
    assert runner.busy and host.monotonic() - runner.turn_started < 3600
    text = _text(_users(env)[1])
    # Срочное забирает и накопленное обычное одной подачей.
    assert f"#{dm}" in text and f"#{group}" in text


async def test_personal_card_wakes_heavy_cold_session_plain_does_not(env):
    host, sid, pid, acc, triage = await _start(env, [[{"text": "подняли"}]], held_wake_max_tokens=1000)
    _heavy(env, sid)
    await _card(env, triage, pid, acc, "grp", kind="group")
    for _ in range(5):
        await env.cycle(host)
    rec = await _rec(env, sid)
    assert env.calls() == [] and rec["wait_reason"] == "heavy"  # обычная карточка — как раньше
    _no_debounce(env)
    await _card(env, triage, pid, acc, "dm-colleague", kind="dm")
    await env.until(host, lambda: len(_users(env)) == 1, what="личное подняло тяжёлую холодную")
    await _turn_done(env, host, sid)
    assert (await _rec(env, sid))["wait_reason"] is None and len(env.calls()) == 1
