"""Ф40-4 C (`docs/architecture.md`), разборщик и Mattermost: канал уведомлений YouTrack
называется без решётки — `skip_channel_titles` (умолчание `["#youtrack"]`) его пропускает; бот-отправитель
Mattermost (`is_bot`) обходится тем же правилом, что бот Slack: без приоритета, без карточки по правилам и
уведомления. Обвязка — как `test_triage.py` и `test_f20_bots_agent.py`."""

import pytest

from hub.agent import dispatcher, files

from .conftest import insert_project, msg, read_event

MIN = 60


@pytest.fixture
def mm_account(env) -> int:
    return env.db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.account (source, db) VALUES ('mattermost', 'work')").lastrowid
        )
    )


def test_title_key_ignores_leading_hash_and_case():
    assert dispatcher._title_key("#YouTrack") == dispatcher._title_key("youtrack") == "youtrack"
    assert dispatcher._title_key("  ## youtrack ") != "youtrack"  # только один ведущий «#»


async def test_default_skip_title_matches_mattermost_channel_without_hash(env, mm_account):
    assert files.DEFAULT_CONFIG["skip_channel_titles"] == ["#youtrack"]
    agent = env.agent()
    await agent.start()
    env.ingest(
        [
            msg(
                "a1",
                env.now_ms - 1000,
                chat="yt",
                kind="group",
                title="YouTrack",
                text="DEV-1 изменена",
                mentions_me=True,
            ),
        ],
        account_id=mm_account,
    )
    await agent.poll_once()
    env.advance(100)
    await agent.tick()
    assert env.items() == [] and env.notes == []

    # другой канал того же аккаунта (упоминание в нём) берётся — пропуск именно по названию
    env.ingest(
        [msg("a2", env.now_ms - 1000, chat="general", kind="group", title="general", mentions_me=True)],
        account_id=mm_account,
    )
    await agent.poll_once()
    env.advance(100)
    await agent.tick()
    assert [item["channel_id"] for item in env.items()] == [env.channel_id("general", "work")]


async def test_configured_title_with_hash_matches_mattermost_name_without_it(env, mm_account):
    agent = env.agent(skip_channel_titles=["#релизы"])
    await agent.start()
    env.ingest(
        [msg("b1", env.now_ms - 1000, chat="rel", kind="group", title="Релизы", mentions_me=True)],
        account_id=mm_account,
    )
    await agent.poll_once()
    assert agent.buffers == {}
    env.advance(100)
    await agent.tick()
    assert env.items() == [] and env.notes == []


async def test_mattermost_bot_in_dm_gets_no_card_and_no_priority(env, mm_account):
    pid = insert_project(env.db, name="Globex")
    env.ingest(
        [msg("0", env.now_ms - 120 * MIN * 1000, chat="d1", kind="dm", title="deploy-bot", text="давно")],
        account_id=mm_account,
    )
    env.ingest([read_event("d1", "0", kind="dm")], account_id=mm_account)
    cid = env.channel_id("d1", "work")
    env.db.write_sync(
        lambda conn: conn.execute("UPDATE work.channel SET project_id = ? WHERE id = ?", (pid, cid))
    )
    agent = env.agent()
    await agent.start()
    event = msg("1", env.now_ms, chat="d1", kind="dm", title="deploy-bot", text="деплой готов")
    event["sender"]["is_bot"] = True
    env.ingest([event], account_id=mm_account)
    await agent.poll_once()
    assert agent.buffers == {}
    env.advance(20 * MIN)
    await agent.tick()
    assert env.items() == [] and env.notes == []
    assert dispatcher.is_priority({"is_bot": True}, {"kind": "dm"}) is False
