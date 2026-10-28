"""Ф64-1 (docs/architecture.md): разборщик по проекту с закрытым рабочим днём
молчит — карточки по правилам заводятся как обычно, но уведомления «ждёт реакции», о сработавшем напоминании
и об ответе агента не шлются и НЕ отмечаются (уйдут после «Начать день», как после тихих часов)."""

import json

from hub.core import peer_msgs, reminders

from .conftest import insert_project, msg
from .test_attention_agent import _agent, _history

NOTE = "обещал ответить после 14:00"


def _set_day(env, pid, open_: bool) -> None:
    policy = json.dumps({"workday": True, "day": {"open": open_, "ts": env.now_ms}})
    env.db.write_sync(
        lambda conn: conn.execute("UPDATE main.project SET agent_policy_json = ? WHERE id = ?", (policy, pid))
    )


def _reminder(env, pid) -> int:
    now_ms = env.now_ms

    def _fn(conn):
        channel_id = conn.execute(
            "INSERT INTO main.channel (account_id, ext_id, kind, title, project_id) "
            "VALUES (?, 'r1', 'dm', 'Партнёр', ?)",
            (env.account_id, pid),
        ).lastrowid
        return conn.execute(
            "INSERT INTO main.reminder (channel_id, project_id, due_ts, note, cancel_if_reply, "
            "base_message_ts, role, role_key, api_token_id, status, created_ts, updated_ts, fired_ts, seq) "
            "VALUES (?, ?, ?, ?, 0, 0, NULL, NULL, NULL, 'fired', ?, ?, ?, 1)",
            (channel_id, pid, now_ms, NOTE, now_ms, now_ms, now_ms),
        ).lastrowid

    return env.db.write_sync(_fn)


def _answer(env, pid) -> int:
    """Ответ агента проекта `pid` владельцу — строкой в базе (отправку проверяют тесты ядра)."""
    now_ms = env.now_ms
    return env.db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.peer_msg (from_user, from_sub_id, from_project_id, from_role, "
                "from_role_key, from_no, to_user, text, hop, status, created_ts, updated_ts, seq) "
                "VALUES (0, 1, ?, 'маркетолог', 'маркетолог', 1, 1, 'готово', 0, 'pending', ?, ?, 1)",
                (pid, now_ms, now_ms),
            ).lastrowid
        )
    )


async def test_reminder_and_agent_answer_wait_for_open_day(env):
    pid = insert_project(env.db, name="P")
    other = insert_project(env.db, name="Q")
    _set_day(env, pid, False)
    agent = await _agent(env)
    rid = _reminder(env, pid)
    answer = _answer(env, pid)
    foreign = _answer(env, other)  # проект без «рабочего дня» — как раньше
    await agent.tick()
    assert [n[0] for n in env.notes] == ["Хаб: ответ агента «маркетолог»"]
    with env.db.reader() as conn:
        assert reminders.get(conn, rid).notified_ts is None
        assert peer_msgs.get(conn, answer).notified_ts is None
        assert peer_msgs.get(conn, foreign).notified_ts is not None
    # «Начать день»: разборщик перечитывает проекты «по времени» не реже раза в минуту — уведомления уходят.
    _set_day(env, pid, True)
    env.advance(61)
    await agent.tick()
    titles = [n[0] for n in env.notes]
    assert titles.count("Хаб: ответ агента «маркетолог»") == 2 and any("напоминание" in t for t in titles)
    with env.db.reader() as conn:
        assert reminders.get(conn, rid).notified_ts is not None
        assert peer_msgs.get(conn, answer).notified_ts is not None


async def test_react_card_is_made_but_not_notified_in_closed_day(env):
    pid = insert_project(env.db, name="P")
    _history(env, "d1", kind="dm", project_id=pid)
    _set_day(env, pid, False)
    agent = await _agent(env)
    env.ingest([msg("1", env.now_ms, chat="d1")])
    await agent.poll_once()
    assert env.notes == []
    assert agent.state["counters"]["notify_day_closed"] == 1
    env.advance(20)  # карточка «реагирую» созревает после 15 с тишины — заводится и в закрытый день
    assert await agent.tick() == 1
    assert env.notes == [] and [i["label"] for i in env.items()] == ["reply"]
