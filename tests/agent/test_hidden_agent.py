"""Ф8-4 П8 (docs/architecture.md): разборщик не берёт удалённое владельцем у себя в хабе — ни
новое, уже скрытое к опросу, ни кандидата, скрытого, пока беседа зрела. Обвязка —
`tests/agent/conftest.py`."""

from hub.core import hiding

from .conftest import msg, read_event

MIN = 60


def _hide(env, message_id):
    env.db.write_sync(lambda conn: hiding.hide_message(conn, message_id, now_ms=env.now_ms))


async def _started(env):
    env.ingest([msg("0", env.now_ms - 120 * MIN * 1000, text="давно")])
    env.ingest([read_event("100", "0")])
    agent = env.agent()
    await agent.start()
    return agent


async def test_p8_hidden_before_poll_not_buffered(env):
    agent = await _started(env)
    env.ingest([msg("1", env.now_ms, text="тайна")])
    _hide(env, env.message_id("100", "1"))
    await agent.poll_once()
    assert agent.buffers == {}
    env.advance(20 * MIN)
    await agent.tick()
    assert env.items() == [] and len(env.notes) <= 1


async def test_p8_candidate_hidden_while_maturing_is_dropped(env):
    agent = await _started(env)
    env.ingest([msg("1", env.now_ms, text="тайна")])
    await agent.poll_once()
    cid = env.channel_id("100")
    assert list(agent.buffers[cid].candidates) == [env.message_id("100", "1")]
    _hide(env, env.message_id("100", "1"))
    await agent.poll_once()
    assert agent.buffers == {}
    assert agent.state["counters"]["skipped_hidden"] == 1
    env.advance(20 * MIN)
    await agent.tick()
    assert env.items() == [] and len(env.notes) <= 1
