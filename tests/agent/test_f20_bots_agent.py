"""Ф20-1 A4, A5, B2 (docs/architecture.md), разборщик: бот в личке — без
карточки по правилам и без приоритета; `@channel` и упоминание от бота — без приоритета. Обвязка — как
test_attention_agent.py (настоящий режим бесед). Ф57-1: модели нет — группа без проекта «к сведению», в
буфер попадает только личное упоминание человека."""

import pytest

from hub.agent import dispatcher

from .conftest import insert_project, msg, read_event

MIN = 60


@pytest.fixture
async def aenv(env):
    return env


def _history(env, chat: str, *, kind: str, project_id: int | None, title: str = "Беседа") -> int:
    env.ingest([msg("0", env.now_ms - 120 * MIN * 1000, chat=chat, kind=kind, title=title, text="давно")])
    env.ingest([read_event(chat, "0", kind=kind)])
    cid = env.channel_id(chat)
    env.db.write_sync(
        lambda conn: conn.execute("UPDATE main.channel SET project_id = ? WHERE id = ?", (project_id, cid))
    )
    return cid


def _bot(event: dict) -> dict:
    event["sender"]["is_bot"] = True
    return event


def _broadcast(event: dict) -> dict:
    event["mentions_me"] = True
    event["mentions_broadcast_only"] = True
    return event


async def test_bot_in_dm_no_rules_card_no_notification(aenv):
    env = aenv
    pid = insert_project(env.db, name="Globex")
    _history(env, "d1", kind="dm", project_id=pid)
    _history(env, "d2", kind="dm", project_id=None)  # Неразобранное: лички — «реагирую»
    agent = env.agent()
    await agent.start()
    env.ingest([_bot(msg("1", env.now_ms, chat="d1", text="деплой готов"))])
    env.ingest([_bot(msg("1", env.now_ms, chat="d2", text="ваш код входа"))])
    await agent.poll_once()
    assert agent.buffers == {}
    assert agent.state["counters"]["skipped_attention"] == 2
    env.advance(20 * MIN)
    await agent.tick()
    assert env.items() == [] and env.notes == []


@pytest.mark.parametrize(
    ("make", "priority"),
    [
        (lambda e: e, False),
        (lambda e: {**e, "mentions_me": True}, True),
        (_broadcast, False),
        (lambda e: _bot({**e, "mentions_me": True, "sender": dict(e["sender"])}), False),
    ],
    ids=["plain", "personal-mention", "at-channel", "bot-mention"],
)
async def test_priority_only_personal_mention_not_bot(aenv, make, priority):
    env = aenv
    cid = _history(env, "g1", kind="group", project_id=None, title="Неразобранная группа")
    agent = env.agent()
    await agent.start()
    env.ingest([make(msg("1", env.now_ms, chat="g1", kind="group", title="Неразобранная группа"))])
    await agent.poll_once()
    # группа без проекта — «к сведению»: в буфер (и в карточку) идёт только личное упоминание человека
    assert (cid in agent.buffers) is priority
    if priority:
        assert agent.buffers[cid].priority is True
    env.advance(16)
    await agent.tick()
    assert len(env.items()) == (1 if priority else 0)


def test_is_priority():
    dm, group = {"kind": "dm"}, {"kind": "group"}
    assert dispatcher.is_priority({}, dm) is True
    assert dispatcher.is_priority({"is_bot": True}, dm) is False
    assert dispatcher.is_priority({"mentions_me": True}, group) is True
    assert dispatcher.is_priority({"mentions_me": True, "mentions_broadcast": True}, group) is False
    assert dispatcher.is_priority({"mentions_me": True, "is_bot": True}, group) is False
