"""Ф34-2 решение 6 + П5: hub-agent шлёт уведомление о сработавшем напоминании и отмечает его в ядре — один
раз; в тихие часы не шлёт и НЕ отмечает (уйдёт после окна); напоминание сессии с ролью — не раньше 20 минут;
сбой ручки напоминаний разбору не мешает."""

from datetime import datetime, timedelta

from hub.agent.core_api import CoreUnavailable
from hub.core import reminders

NOTE = "обещал ответить после 14:00"
TITLE = "Хаб: напоминание — Партнёр"


def _fired(env, *, note=NOTE, role=None) -> int:
    """Сработавшее напоминание по беседе — строкой в базе (постановку проверяют тесты ядра)."""
    now_ms = env.now_ms

    def _fn(conn):
        channel_id = conn.execute(
            "INSERT INTO main.channel (account_id, ext_id, kind, title) VALUES (?, 'c1', 'dm', 'Партнёр')",
            (env.account_id,),
        ).lastrowid
        return conn.execute(
            "INSERT INTO main.reminder (channel_id, project_id, due_ts, note, cancel_if_reply, "
            "base_message_ts, role, role_key, api_token_id, status, created_ts, updated_ts, fired_ts, seq) "
            "VALUES (?, NULL, ?, ?, 0, 0, ?, ?, ?, 'fired', ?, ?, ?, 1)",
            (channel_id, now_ms, note, role, role, 1 if role else None, now_ms, now_ms, now_ms),
        ).lastrowid

    return env.db.write_sync(_fn)


def _notified(env, reminder_id) -> int | None:
    with env.db.reader() as conn:
        return reminders.get(conn, reminder_id).notified_ts


def _window_around_now(env) -> str:
    now = datetime.fromtimestamp(env.clock())
    start, end = now - timedelta(minutes=30), now + timedelta(minutes=30)
    return f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}"


async def _agent(env, **config):
    agent = env.agent(**config)
    await agent.start()
    return agent


async def test_notifies_once_and_marks(env):
    agent = await _agent(env)
    rid = _fired(env)
    await agent.tick()
    assert env.notes == [(TITLE, NOTE, "critical")]
    assert _notified(env, rid) == env.now_ms
    await agent.tick()
    assert len(env.notes) == 1


async def test_quiet_hours_do_not_send_and_do_not_mark(env):
    agent = await _agent(env, quiet_hours=_window_around_now(env))
    rid = _fired(env, note=None)
    await agent.tick()
    assert env.notes == [] and _notified(env, rid) is None
    env.advance(2 * 60 * 60)  # окно кончилось — уведомление уходит
    await agent.tick()
    assert env.notes == [(TITLE, "срок напоминания наступил", "critical")]
    assert _notified(env, rid) == env.now_ms


async def test_role_reminder_waits_20_minutes_for_the_session(env):
    agent = await _agent(env)
    rid = _fired(env, role="маркетолог")
    env.advance(19 * 60)
    await agent.tick()
    assert env.notes == [] and _notified(env, rid) is None
    env.advance(61)
    await agent.tick()
    assert [n[0] for n in env.notes] == [TITLE]


async def test_closed_by_session_is_not_notified(env):
    agent = await _agent(env)
    rid = _fired(env, role="маркетолог")
    env.db.write_sync(
        lambda conn: conn.execute("UPDATE main.reminder SET status = 'done' WHERE id = ?", (rid,))
    )
    env.advance(30 * 60)
    await agent.tick()
    assert env.notes == []


async def test_reminders_endpoint_failure_does_not_break_tick(env):
    agent = await _agent(env)
    rid = _fired(env)
    real_get = agent.core.get

    async def _get(path, params=None):
        if path == "/agent/reminders/due":
            raise CoreUnavailable("HTTP 503")
        return await real_get(path, params)

    agent.core.get = _get
    assert await agent.tick() == 0  # тик не упал — разбор идёт своим чередом
    assert env.notes == [] and _notified(env, rid) is None
    assert agent.state["errors"][-1]["where"] == "reminders"
    agent.core.get = real_get
    await agent.tick()
    assert len(env.notes) == 1


async def test_failed_mark_does_not_repeat_notification(env):
    """Уведомление ушло, а отметка в ядре не прошла — следующий тик только доотмечает, не шлёт второй раз."""
    agent = await _agent(env)
    rid = _fired(env)
    real_post = agent.core.post

    async def _post(path, json=None):
        if path == "/agent/reminders/notified":
            raise CoreUnavailable("HTTP 503")
        return await real_post(path, json)

    agent.core.post = _post
    await agent.tick()
    assert len(env.notes) == 1 and _notified(env, rid) is None
    agent.core.post = real_post
    await agent.tick()
    assert len(env.notes) == 1 and _notified(env, rid) == env.now_ms


async def test_f89_no_channel_title_and_repeating_after_missed(env):
    """Ф89-1 (П9): без беседы — «Хаб: напоминание <роль>»; повторяемое сессии — только при пропущенном
    сроке; своё повторяемое владельца (без токена) — в каждый срок (ревью Ф89-1)."""
    agent = await _agent(env)
    now_ms = env.now_ms
    token_id = env.db.write_sync(
        lambda conn: conn.execute("SELECT id FROM main.api_token ORDER BY id LIMIT 1").fetchone()[0]
    )

    def _row(conn, every, missed, token=token_id):
        return conn.execute(
            "INSERT INTO main.reminder (channel_id, project_id, due_ts, note, cancel_if_reply, "
            "base_message_ts, role, role_key, api_token_id, status, created_ts, updated_ts, fired_ts, seq, "
            "every, at_hm, tz, "
            "missed) VALUES (0, NULL, ?, 'запустить', 0, 0, NULL, NULL, ?, 'fired', ?, ?, ?, 1, ?, ?, "
            "'Europe/Moscow', ?)",
            (now_ms + 60_000, token, now_ms, now_ms, now_ms, every, "09:00" if every else None, missed),
        ).lastrowid

    quiet = env.db.write_sync(lambda conn: _row(conn, "day", 0))
    missed = env.db.write_sync(lambda conn: _row(conn, "day", 1))
    own = env.db.write_sync(lambda conn: _row(conn, "day", 0, None))
    await agent.tick()
    assert env.notes == [("Хаб: напоминание", "запустить", "critical")] * 2
    assert _notified(env, quiet) is None and _notified(env, missed) == env.now_ms
    assert _notified(env, own) == env.now_ms
