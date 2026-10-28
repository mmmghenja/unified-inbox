"""Ф8-3, разборщик: кандидаты только `react`, карточка «реагирую» по правилам после тишины, уведомление
сразу, присутствие/прочитанное/отвеченное снимают кандидатов. Спецификация:
docs/architecture.md, тесты T8–T12 и поправки П7, П8, П10, П16, П17; Ф57-1 — режима
«разобрать» и модели нет.

Обвязка — `tests/agent/conftest.py` (ядро в процессе, одни часы): режим бесед — настоящий, по правилам ядра.
"""

from datetime import datetime, timedelta

import pytest

from hub.agent import dispatcher, files
from hub.core import admin

from .conftest import insert_project, msg, read_event

MIN = 60


@pytest.fixture
async def aenv(env):
    return env


def _move(env, chat: str, project_id: int | None) -> int:
    cid = env.channel_id(chat)
    env.db.write_sync(
        lambda conn: conn.execute("UPDATE main.channel SET project_id = ? WHERE id = ?", (project_id, cid))
    )
    return cid


def _history(env, chat: str, *, kind: str, project_id: int | None, title: str = "Беседа") -> int:
    """Беседа с одним старым сообщением (до старта агента — прошлое он не разбирает), в нужном проекте."""
    env.ingest([msg("0", env.now_ms - 120 * MIN * 1000, chat=chat, kind=kind, title=title, text="давно")])
    env.ingest([read_event(chat, "0", kind=kind)])
    return _move(env, chat, project_id)


async def _agent(env, **config):
    agent = env.agent(**config)
    await agent.start()
    return agent


def _quiet_now(env) -> str:
    now = datetime.fromtimestamp(env.clock())
    return (
        f"{(now - timedelta(minutes=30)).strftime('%H:%M')}-{(now + timedelta(minutes=30)).strftime('%H:%M')}"
    )


def _present(env, channel_id: int, thread: str | None = None) -> None:
    """Вкладка UI открыла беседу; часы — на 6 с вперёд: разборщик держит `GET /presence` в кэше до 5 с."""
    env.app.state.presence.set(
        token_id=1, client_id="tab", channel_id=channel_id, thread_ext_id=thread, now_ms=env.now_ms
    )
    env.advance(6)


# --- T8: «к сведению» без упоминания — ничего; с упоминанием — карточка по правилам -------------------------


async def test_t8_info_without_mention_nothing_with_mention_rules_card(aenv):
    env = aenv
    pid = insert_project(env.db, name="Initech")
    cid = _history(env, "g1", kind="group", project_id=pid, title="фронт")
    agent = await _agent(env)
    env.ingest([msg("1", env.now_ms, chat="g1", kind="group", title="фронт", text="собрал ветку")])
    await agent.poll_once()
    assert agent.buffers == {}
    assert agent.state["counters"]["skipped_attention"] == 1
    env.advance(20 * MIN)
    await agent.tick()
    assert env.items() == [] and env.notes == []

    env.ingest(
        [msg("2", env.now_ms, chat="g1", kind="group", title="фронт", text="владелец, глянь", mentions_me=True)]
    )
    await agent.poll_once()
    assert cid in agent.buffers
    env.advance(121)
    assert await agent.tick() == 1
    (item,) = env.items()
    assert item["provider"] == "rules" and item["label"] == "reply"
    assert item["action"] == "упоминание"
    assert item["summary"] == "Пётр Иванов: владелец, глянь"
    assert item["message_ids"] == [env.message_id("g1", "2")]
    with env.db.reader() as conn:
        assert conn.execute("SELECT model FROM main.agent_item").fetchone() == ("-",)


# --- T9: тишина 15 с после ПОСЛЕДНЕГО, но не позже 120 с от первого (12.10.2026; было 120 и 600) ---


async def test_t9_react_card_after_silence_from_last_message(aenv):
    env = aenv
    pid = insert_project(env.db, name="P")
    cid = _history(env, "d1", kind="dm", project_id=pid)
    agent = await _agent(env)
    for i in (1, 2, 3):
        env.ingest([msg(str(i), env.now_ms, chat="d1", text=f"часть {i}")])
        await agent.poll_once()
        assert await agent.tick() == 0
        if i < 3:
            env.advance(10)
    env.advance(14)
    assert await agent.tick() == 0
    env.advance(2)
    assert await agent.tick() == 1
    (item,) = env.items()
    assert item["message_ids"] == [env.message_id("d1", str(i)) for i in (1, 2, 3)]
    assert item["action"] == "личное" and item["summary"] == "Пётр Иванов: часть 3"
    assert cid not in agent.buffers


async def test_t9_continuous_stream_card_not_later_than_120s(aenv):
    env = aenv
    pid = insert_project(env.db, name="P")
    _history(env, "d1", kind="dm", project_id=pid)
    agent = await _agent(env)
    cards_at = None
    for i in range(1, 16):
        env.ingest([msg(str(i), env.now_ms, chat="d1", text=f"поток {i}")])
        await agent.poll_once()
        if await agent.tick():
            cards_at = i
            break
        env.advance(10)
    # первое — в 0 с, двенадцатое — в 110 с; тик в 120 с (тринадцатое) — предел ожидания
    assert cards_at == 13
    assert len(env.items()[0]["message_ids"]) == 13


# --- T10 + П8 + П17: присутствие, прочитанное, мой ответ (включая треды) ------------------------------------


async def test_t10_present_read_or_answered_gives_no_card(aenv):
    env = aenv
    pid = insert_project(env.db, name="P")
    cid_present = _history(env, "d1", kind="dm", project_id=pid)
    _history(env, "d2", kind="dm", project_id=pid)
    _history(env, "d3", kind="dm", project_id=pid)
    agent = await _agent(env)
    env.ingest(
        [msg("1", env.now_ms, chat="d1"), msg("1", env.now_ms, chat="d2"), msg("1", env.now_ms, chat="d3")]
    )
    await agent.poll_once()
    env.ingest([read_event("d2", "1")])
    env.ingest([msg("2", env.now_ms + 1, chat="d3", is_me=True, text="да, сейчас")])
    env.advance(121)
    _present(env, cid_present)  # UI обновляет присутствие каждые 20 с — к моменту разбора оно свежее
    await agent.tick()
    assert env.items() == []
    counters = agent.state["counters"]
    assert counters["present"] == 1 and counters["read_or_deleted"] == 1 and counters["answered"] == 1


async def test_p17_answer_in_other_thread_counts_as_answered(aenv):
    """П17: «отвечено» — любое моё сообщение в беседе позже кандидата, включая другой тред."""
    env = aenv
    acc = env.slack_account_id
    pid = insert_project(env.db, name="P")
    env.ingest([msg("1.0", env.now_ms - 7_200_000, chat="C1", kind="group", title="#dev")], account_id=acc)
    env.ingest([read_event("C1", "1.0", kind="group")], account_id=acc)
    env.db.write_sync(lambda conn: conn.execute("UPDATE work.channel SET project_id = ?", (pid,)))
    agent = await _agent(env)
    env.ingest(
        [msg("2.0", env.now_ms, chat="C1", kind="group", title="#dev", text="владелец?", mentions_me=True)],
        account_id=acc,
    )
    await agent.poll_once()
    env.ingest(
        [msg("1.1", env.now_ms + 5, chat="C1", kind="group", title="#dev", thread="1.0", is_me=True)],
        account_id=acc,
    )
    env.advance(121)
    await agent.tick()
    assert env.items() == []
    assert agent.state["counters"]["answered"] == 1


async def test_p8_thread_reply_needs_open_thread_panel(aenv):
    """П8: ответ в треде «виден», только если открыта панель ЭТОГО треда, а не просто окно канала."""
    env = aenv
    acc = env.slack_account_id
    pid = insert_project(env.db, name="P")
    env.ingest(
        [msg("1.0", env.now_ms - 7_200_000, chat="C1", kind="group", title="#dev", is_me=True)],
        account_id=acc,
    )
    env.db.write_sync(lambda conn: conn.execute("UPDATE work.channel SET project_id = ?", (pid,)))
    cid = env.channel_id("C1", "work")
    agent = await _agent(env)
    env.ingest([msg("1.1", env.now_ms, chat="C1", kind="group", title="#dev", thread="1.0")], account_id=acc)
    _present(env, cid)  # окно канала открыто, панель треда — нет
    await agent.poll_once()
    assert len(env.notes) == 1  # ответ в моём треде — «тред», уведомление сразу
    env.advance(121)
    await agent.tick()
    (item,) = env.items()
    assert item["action"] == "тред"

    first_ids = item["message_ids"]
    env.ingest([msg("1.2", env.now_ms, chat="C1", kind="group", title="#dev", thread="1.0")], account_id=acc)
    _present(env, cid, "1.0")  # панель ЭТОГО треда открыта
    await agent.poll_once()
    assert len(env.notes) == 1 and agent.state["counters"]["notify_present"] == 1
    env.advance(121)
    _present(env, cid, "1.0")
    await agent.tick()
    (item,) = env.items()
    assert item["message_ids"] == first_ids
    assert agent.state["counters"]["present"] == 1


# --- T11 + П7: уведомление «реагирую» -----------------------------------------------------------------------


async def test_t11_react_notification_immediately_throttled_present_quiet(aenv):
    env = aenv
    pid = insert_project(env.db, name="Umbrella")
    cid = _history(env, "d1", kind="dm", project_id=pid, title="Клиент")
    _history(env, "d2", kind="dm", project_id=pid, title="Другой")
    agent = await _agent(env)
    env.ingest([msg("1", env.now_ms, chat="d1", title="Клиент", text="Где <счёт> & акт?")])
    await agent.poll_once()
    assert env.notes == [("Хаб · Umbrella: Клиент", "Пётр Иванов: Где &lt;счёт&gt; &amp; акт?", "critical")]
    env.advance(60)
    env.ingest([msg("2", env.now_ms, chat="d1", title="Клиент")])
    await agent.poll_once()
    assert len(env.notes) == 1
    env.advance(3 * MIN)
    env.ingest([msg("3", env.now_ms, chat="d1", title="Клиент", text="ну что?")])
    await agent.poll_once()
    assert len(env.notes) == 2 and env.notes[1][1] == "Пётр Иванов: ну что?"

    # в присутствии — нет
    _present(env, env.channel_id("d2"))
    env.ingest([msg("1", env.now_ms, chat="d2", title="Другой")])
    await agent.poll_once()
    assert len(env.notes) == 2
    del cid


async def test_t11_quiet_hours_no_react_notification(aenv):
    env = aenv
    pid = insert_project(env.db, name="P")
    _history(env, "d1", kind="dm", project_id=pid)
    agent = await _agent(env, quiet_hours=_quiet_now(env))
    env.ingest([msg("1", env.now_ms, chat="d1")])
    await agent.poll_once()
    assert env.notes == []
    assert agent.state["counters"]["notify_quiet"] == 1


async def test_p7_old_or_read_no_individual_notification_one_summary_line(aenv):
    env = aenv
    pid = insert_project(env.db, name="P")
    for chat in ("d1", "d2", "d3"):
        _history(env, chat, kind="dm", project_id=pid)
    agent = await _agent(env)
    old = env.now_ms - 30 * MIN * 1000  # догон после сна
    env.ingest([msg("1", old, chat="d1"), msg("2", old + 1, chat="d1"), msg("1", old, chat="d2")])
    await agent.poll_once()
    assert env.notes == [("Хаб: ждут реакции", "2 бесед ждут реакции", "critical")]
    # прочитанное к моменту опроса — без уведомления
    env.ingest([msg("1", env.now_ms, chat="d3")])
    env.ingest([read_event("d3", "1")])
    await agent.poll_once()
    assert len(env.notes) == 1


async def test_p7_throttle_survives_restart(aenv):
    env = aenv
    pid = insert_project(env.db, name="P")
    _history(env, "d1", kind="dm", project_id=pid)
    agent = await _agent(env)
    env.ingest([msg("1", env.now_ms, chat="d1")])
    await agent.poll_once()
    assert len(env.notes) == 1
    agent._save(force=True)
    assert str(env.channel_id("d1")) in files.load_state(env.data_dir)["react_notified"]
    env.advance(60)
    again = await _agent(env)
    env.ingest([msg("2", env.now_ms, chat="d1")])
    await again.poll_once()
    assert len(env.notes) == 1


# --- T12 (Ф57-1): группа без проекта — «к сведению»; упоминание в ней и личка — карточки по правилам ------


async def test_t12_unsorted_group_quiet_mention_and_dm_get_rules_cards(aenv):
    env = aenv
    pid = insert_project(env.db, name="P")
    gid = _history(env, "g1", kind="group", project_id=None, title="Неразобранная группа")
    _history(env, "g2", kind="group", project_id=None, title="Тихая группа")
    did = _history(env, "d1", kind="dm", project_id=pid)
    agent = await _agent(env)
    env.ingest(
        [
            msg("1", env.now_ms, chat="g1", kind="group", title="Неразобранная группа", mentions_me=True),
            msg("1", env.now_ms, chat="g2", kind="group", title="Тихая группа"),
            msg("1", env.now_ms, chat="d1"),
        ]
    )
    await agent.poll_once()
    assert set(agent.buffers) == {gid, did}
    assert agent.state["counters"]["skipped_attention"] == 1
    env.advance(16)
    assert await agent.tick() == 2
    by_channel = {item["channel_id"]: item for item in env.items()}
    assert set(by_channel) == {gid, did}
    assert {item["provider"] for item in by_channel.values()} == {"rules"}
    assert by_channel[gid]["action"] == "упоминание" and by_channel[did]["action"] == "личное"


async def test_p17_mode_changed_while_maturing_drops_candidates(aenv):
    env = aenv
    pid = insert_project(env.db, name="P")
    cid = _history(env, "d1", kind="dm", project_id=pid)
    agent = await _agent(env)
    env.ingest([msg("1", env.now_ms, chat="d1")])
    await agent.poll_once()
    env.db.write_sync(
        lambda conn: conn.execute("UPDATE main.channel SET attention = 'info' WHERE id = ?", (cid,))
    )
    env.advance(121)
    await agent.tick()
    assert env.items() == []
    assert agent.state["counters"]["skipped_attention"] == 1


async def test_p16_message_without_attention_is_not_taken(aenv):
    """Ф57-1: сообщение без поля `attention` не берётся (раньше считалось `triage` и шло в модель)."""
    env = aenv
    assert dispatcher.message_attention({}) is None
    assert dispatcher.message_attention({"attention": None}) is None
    assert dispatcher.message_attention({"attention": "info"}) == "info"
    assert dispatcher.TAKEN_ATTENTION == ("react",)
    _history(env, "d1", kind="dm", project_id=None)  # личка без проекта — react
    agent = await _agent(env)
    env.ingest([msg("1", env.now_ms, chat="d1")])
    old_core_page = dict((await agent.core.get("/sync", {"since": agent.sync_pos}))["messages"][0])
    assert old_core_page.pop("attention") == "react"
    assert await agent._consider(old_core_page, agent.sync_pos) is False
    assert agent.buffers == {} and env.notes == []


async def test_policy_info_mail_not_buffered(aenv):
    """Почта проекта в режиме «к сведению» — разборщик её не трогает, даже письмо «Кому: я» (П1)."""
    env = aenv
    pid = insert_project(env.db, name="Globex")
    env.db.write_sync(lambda conn: admin.set_project_attention(conn, pid, changes={"mail_thread": "info"}))
    _history(env, "t1", kind="mail_thread", project_id=pid)
    agent = await _agent(env)
    env.ingest([msg("1", env.now_ms, chat="t1", kind="mail_thread", mentions_me=True)])
    await agent.poll_once()
    assert agent.buffers == {} and env.notes == []
