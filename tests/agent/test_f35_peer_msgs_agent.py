"""Ф35-2a (П7, Н8): hub-agent шлёт уведомление об ответе агента владельцу — один раз на строку; в тихие часы
не шлёт и НЕ отмечает; уведомлений об ответах — не больше 6 в час (лишние отмечаются без показа);
старые строки `blocked` (Ф42-2) не уведомляют; сбой ручки разбору не мешает."""

from datetime import datetime, timedelta

from hub.agent.core_api import CoreUnavailable
from hub.core import peer_msgs


def _row(env, *, blocked=False, text="готово, отчёт в папке", role="маркетолог", role_no=1) -> int:
    """Строка переписки прямо в базе (отправку проверяют тесты ядра): ответ агента владельцу или `blocked`."""
    now_ms = env.now_ms

    def _fn(conn):
        return conn.execute(
            "INSERT INTO main.peer_msg (from_user, from_sub_id, from_project_id, from_role, from_role_key, "
            "from_no, to_user, to_project_id, to_role, to_role_key, to_no, text, hop, status, reason, "
            "created_ts, updated_ts, seq) VALUES (0, 1, 3, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, 1)",
            (
                role,
                role,
                role_no,
                0 if blocked else 1,
                *((4, "программист", "программист", 1) if blocked else (None, None, None, None)),
                text,
                "blocked" if blocked else "pending",
                "pair_limit" if blocked else None,
                now_ms,
                now_ms,
            ),
        ).lastrowid

    return env.db.write_sync(_fn)


def _notified(env, msg_id) -> int | None:
    with env.db.reader() as conn:
        return peer_msgs.get(conn, msg_id).notified_ts


def _window_around_now(env) -> str:
    now = datetime.fromtimestamp(env.clock())
    start, end = now - timedelta(minutes=30), now + timedelta(minutes=30)
    return f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}"


async def _agent(env, **config):
    agent = env.agent(**config)
    await agent.start()
    return agent


async def test_answer_is_notified_once_and_marked(env):
    agent = await _agent(env)
    answer = _row(env, text="первая строка\nвторая " + "я" * 400)
    second = _row(env, role=None, role_no=2)
    await agent.tick()
    assert [n[0] for n in env.notes] == [
        "Хаб: ответ агента «маркетолог»",
        "Хаб: ответ агента «без роли 2»",
    ]
    text = env.notes[0][1]
    assert text.startswith("первая строка вторая ") and len(text) == 200 and "\n" not in text
    assert [_notified(env, i) for i in (answer, second)] == [env.now_ms] * 2
    await agent.tick()
    assert len(env.notes) == 2


async def test_old_blocked_row_does_not_notify(env):
    agent = await _agent(env)
    blocked = _row(env, blocked=True, text="старая попытка")
    await agent.tick()
    assert env.notes == []
    with env.db.reader() as conn:
        row = conn.execute("SELECT notified_ts FROM main.peer_msg WHERE id = ?", (blocked,)).fetchone()
    assert row[0] is None  # строка в базе осталась нетронутой


async def test_quiet_hours_do_not_send_and_do_not_mark(env):
    agent = await _agent(env, quiet_hours=_window_around_now(env))
    answer = _row(env)
    await agent.tick()
    assert env.notes == [] and _notified(env, answer) is None
    env.advance(2 * 60 * 60)  # окно кончилось — уведомление уходит
    await agent.tick()
    assert [n[0] for n in env.notes] == ["Хаб: ответ агента «маркетолог»"]
    assert _notified(env, answer) == env.now_ms


async def test_seventh_answer_within_an_hour_is_marked_but_not_shown(env):
    agent = await _agent(env)
    answers = [_row(env, text=f"ответ {i}") for i in range(7)]
    await agent.tick()
    assert [n[1] for n in env.notes] == [f"ответ {i}" for i in range(6)]
    assert all(_notified(env, i) is not None for i in answers)  # седьмой отмечен без показа

    late = _row(env, text="ещё один")
    await agent.tick()
    assert len(env.notes) == 6 and _notified(env, late) is not None
    env.advance(61 * 60)  # час прошёл — уведомления снова идут
    _row(env, text="через час")
    await agent.tick()
    assert env.notes[-1][1] == "через час" and len(env.notes) == 7


async def test_read_answer_is_not_notified(env):
    agent = await _agent(env)
    answer = _row(env)
    env.db.write_sync(
        lambda conn: conn.execute("UPDATE main.peer_msg SET status = 'read' WHERE id = ?", (answer,))
    )
    await agent.tick()
    assert env.notes == []


async def test_alerts_failure_does_not_break_tick_and_failed_mark_does_not_repeat(env):
    agent = await _agent(env)
    answer = _row(env)
    real_get, real_post = agent.core.get, agent.core.post

    async def _get(path, params=None):
        if path == "/agent/peer-msgs/alerts":
            raise CoreUnavailable("HTTP 503")
        return await real_get(path, params)

    agent.core.get = _get
    assert await agent.tick() == 0  # тик не упал
    assert env.notes == [] and agent.state["errors"][-1]["where"] == "peer_msgs"
    agent.core.get = real_get

    async def _post(path, json=None):
        if path == "/agent/peer-msgs/notified":
            raise CoreUnavailable("HTTP 503")
        return await real_post(path, json)

    agent.core.post = _post
    await agent.tick()
    assert len(env.notes) == 1 and _notified(env, answer) is None
    agent.core.post = real_post
    await agent.tick()  # отметка дошла, второго уведомления нет
    assert len(env.notes) == 1 and _notified(env, answer) == env.now_ms
