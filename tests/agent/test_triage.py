"""Процесс hub-agent против ядра в процессе. Спецификация: docs/architecture.md, мелочи.

Ф57-1 (docs/architecture.md): модели нет — сценарии общего поведения разборщика
(что не берётся, курсор и рестарт, К2/К3, служебное, `skip_*`, тихие часы, токен ядра, журнал без текстов)
перенесены с пути модели на путь карточки по правилам (`react`); тесты самой модели (тело запроса, невалидный
ответ, пауза и остановка, часовой лимит, `notify_labels`, прокси) удалены вместе с ней.
"""

import logging

import pytest

from hub.agent import dispatcher, files
from hub.agent.core_api import CoreUnauthorized
from hub.core import security

from .conftest import insert_project, msg, read_event

HOUR_MS = 60 * 60 * 1000


async def _started(env, **config):
    agent = env.agent(**config)
    await agent.start()
    return agent


async def _cycle(env, agent, seconds: float = 16) -> int:
    """Опрос ленты, ожидание созревания, тик."""
    await agent.poll_once()
    env.advance(seconds)
    return await agent.tick()


# --- Тест 7: личка -> через 15 с тишины одна карточка по правилам ------------------------------------------


async def test_7b_7_dm_card_by_rules_after_15s(env):
    agent = await _started(env)
    env.ingest([msg("1", env.now_ms - 1000, text="Когда релиз? Нужен ответ сегодня")])
    await agent.poll_once()
    env.advance(10)
    assert await agent.tick() == 0
    assert env.items() == []

    env.advance(6)
    assert await agent.tick() == 1
    (item,) = env.items()
    assert item["channel_id"] == env.channel_id("100")
    assert item["message_ids"] == [env.message_id("100", "1")]
    assert (item["label"], item["provider"], item["status"]) == ("reply", "rules", "new")
    assert item["summary"] == "Пётр Иванов: Когда релиз? Нужен ответ сегодня"
    assert item["action"] == "личное"
    with env.db.reader() as conn:
        assert conn.execute("SELECT model FROM main.agent_item").fetchone() == ("-",)


# --- Тест 8 (+П1): что не берётся ---------------------------------------------------------------------------


async def test_7b_8_not_taken_own_event_edit_delete_muted_skip(env, db):
    now = env.now_ms
    env.ingest([msg("1", now - 5000, chat="300", text="старое")])
    pid_muted = insert_project(db, name="Тихий", muted=1)
    pid_skip = insert_project(db, name="Пропуск")
    env.ingest([msg("1", now - 5000, chat="401", title="В тихом проекте")])
    env.ingest([msg("1", now - 5000, chat="402", title="В пропускаемом")])
    env.ingest([msg("1", now - 5000, chat="403", title="Тихий канал")])
    cid_401, cid_402, cid_403 = env.channel_id("401"), env.channel_id("402"), env.channel_id("403")
    db.write_sync(
        lambda c: c.execute("UPDATE main.channel SET project_id = ? WHERE id = ?", (pid_muted, cid_401))
    )
    db.write_sync(
        lambda c: c.execute("UPDATE main.channel SET project_id = ? WHERE id = ?", (pid_skip, cid_402))
    )
    db.write_sync(lambda c: c.execute("UPDATE main.channel SET muted = 1 WHERE id = ?", (cid_403,)))

    agent = await _started(env, skip_projects=[pid_skip])
    env.ingest(
        [
            msg("2", now, chat="300", is_me=True, text="моё"),
            msg("3", now, chat="300", mkind="event", text="событие"),
            {
                **msg("1", now - 5000, chat="300", text="старое, поправленное"),
                "type": "edit",
                "edited_ts": now,
            },
            msg("2", now, chat="401", text="в тихом проекте"),
            msg("2", now, chat="402", text="в пропускаемом проекте"),
            msg("2", now, chat="403", text="в тихом канале"),
            msg("9", now, chat="301", text="удалят"),
        ]
    )
    env.ingest([{"type": "delete", "channel": {"ext_id": "301", "kind": "dm"}, "ext_ids": ["9"]}])
    await _cycle(env, agent, 100)
    assert env.items() == []
    assert env.notes == []


async def test_7b_p1_history_with_read_marker_not_taken(env):
    agent = await _started(env)
    now = env.now_ms
    # Стартовая загрузка коннектора: история + маркер прочтения из источника в той же пачке.
    env.ingest([msg(str(i), now - 60_000 + i, chat="500", text=f"история {i}") for i in range(1, 6)])
    env.ingest([read_event("500", "5")])
    await _cycle(env, agent, 100)
    assert env.items() == [] and env.notes == []


async def test_7b_p1_read_between_signal_and_card_not_taken(env):
    agent = await _started(env)
    env.ingest([msg("1", env.now_ms - 1000, chat="600")])
    await agent.poll_once()
    env.ingest([read_event("600", "1")])  # прочитал на телефоне, пока беседа зрела
    env.advance(16)
    await agent.tick()
    assert env.items() == []
    assert agent.buffers == {}
    assert agent.state["counters"]["read_or_deleted"] == 1


async def test_7b_p1_catch_up_after_sleep_gets_card(env):
    agent = await _started(env)
    now = env.now_ms
    # Агент «стоял» (сон ноутбука); догон принёс непрочитанные с ts 8 часов назад.
    env.ingest([msg("1", now - 8 * HOUR_MS, chat="700", text="вчерашний вопрос")])
    await _cycle(env, agent)
    assert len(env.items()) == 1
    # П7: старое — не отдельным уведомлением, а одной сводной строкой
    assert env.notes == [("Хаб: ждут реакции", "1 бесед ждут реакции", "critical")]


async def test_7b_p1_max_age_guard(env):
    agent = await _started(env)
    env.ingest([msg("1", env.now_ms - 4 * 24 * HOUR_MS, chat="701", text="очень старое")])
    await _cycle(env, agent)
    assert env.items() == []
    assert agent.state["counters"]["too_old"] == 1


# --- Тест 9: первый старт и рестарт -------------------------------------------------------------------------


async def test_7b_9_first_start_cursor_is_max_seq_restart_continues(env):
    env.ingest([msg("1", env.now_ms - 1000, chat="800", text="до первого старта")])
    agent = await _started(env)
    await _cycle(env, agent)
    assert env.items() == []  # прошлое не берём
    state = files.load_state(env.data_dir)
    assert state["seq"] == agent.sync_pos

    # Агент лежит; пришло новое.
    env.ingest([msg("2", env.now_ms - 1000, chat="800", text="пока агент лежал")])
    again = await _started(env)
    assert again.sync_pos == state["seq"]
    await _cycle(env, again)
    assert env.items()[0]["message_ids"] == [env.message_id("800", "2")]


async def test_7b_9_cursor_above_max_seq_or_broken_state_resets(env, caplog):
    files.save_state(env.data_dir, {"seq": 10**9})
    with caplog.at_level(logging.WARNING, logger="hub.agent"):
        agent = await _started(env)
    assert agent.sync_pos < 10**9
    assert "вне диапазона" in caplog.text

    files.state_path(env.data_dir).write_text("{битый", encoding="utf-8")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="hub.agent"):
        agent2 = await _started(env)
    assert "битый" in caplog.text
    assert agent2.sync_pos == agent.sync_pos


# --- Тест 10 (П9): беседа — своя карточка -------------------------------------------------------------------


async def test_7b_10_one_card_per_conversation(env):
    agent = await _started(env)
    env.ingest([msg("1", env.now_ms - 1000, chat=str(1000 + i), title=f"Беседа {i}") for i in range(15)])
    assert await _cycle(env, agent) == 15
    items = env.items()
    assert len(items) == 15 and len({item["channel_id"] for item in items}) == 15
    assert all(len(item["message_ids"]) == 1 for item in items)


# --- Тест 14: тихие часы; заголовок уведомления — одна строка ---------------------------------------------


def _local_window_around_now(env) -> str:
    from datetime import datetime, timedelta

    now = datetime.fromtimestamp(env.clock())
    start = (now - timedelta(minutes=30)).strftime("%H:%M")
    end = (now + timedelta(minutes=30)).strftime("%H:%M")
    return f"{start}-{end}"


async def test_7b_14_quiet_hours_card_without_notification(env):
    agent = await _started(env, quiet_hours=_local_window_around_now(env))
    env.ingest([msg("1", env.now_ms - 1000, chat="1700")])
    await _cycle(env, agent)
    assert len(env.items()) == 1
    assert env.notes == []
    assert agent.state["counters"]["notify_quiet"] == 1


async def test_7b_14_notification_title_one_line_text_escaped(env):
    agent = await _started(env)
    env.ingest(
        [msg("1", env.now_ms - 1000, chat="1800", title="Вася <i>&</i>\nнаглый", text="Срок <b>ж</b> & акт")]
    )
    await agent.poll_once()
    assert env.notes == [
        (
            "Хаб · Неразобранное: Вася <i>&</i> наглый",  # заголовок — простой текст, одна строка
            "Пётр Иванов: Срок &lt;b&gt;ж&lt;/b&gt; &amp; акт",
            "critical",
        )
    ]


# --- П13: служебное ---------------------------------------------------------------------------------------


async def test_7b_p13_service_chats_skipped(env):
    agent = await _started(env, skip_channels=[])
    env.ingest([msg("1", env.now_ms - 1000, chat="777000", title="Telegram", text="Код входа: 12345")])
    env.ingest(
        [msg("1", env.now_ms - 1000, chat="D1", title="Slackbot", text="напоминание")],
        account_id=env.slack_account_id,
    )
    await _cycle(env, agent, 100)
    assert env.items() == [] and env.notes == []


async def test_7b_skip_channel_titles_default_youtrack(env):
    """Решение владельца: «#youtrack» (лента изменений задач YouTrack в Slack) не берётся — по умолчанию,
    без учёта регистра, даже с упоминанием меня."""
    assert files.DEFAULT_CONFIG["skip_channel_titles"] == ["#youtrack"]
    agent = await _started(env)
    env.ingest(
        [
            msg(
                "1700000000.000100",
                env.now_ms - 1000,
                chat="CYT",
                kind="group",
                title="#YouTrack",
                text="@владелец DEV-1 изменена",
                mentions_me=True,
            )
        ],
        account_id=env.slack_account_id,
    )
    await _cycle(env, agent, 100)
    assert env.items() == [] and env.notes == []

    # В конфиге без «#» — тоже совпадает с «#релизы»; другой канал (упоминание в нём) берётся.
    agent2 = await _started(env, skip_channel_titles=["релизы"])
    env.ingest(
        [
            msg(
                "1700000000.000200",
                env.now_ms - 1000,
                chat="CR",
                kind="group",
                title="#Релизы",
                mentions_me=True,
            ),
            msg(
                "1700000000.000300",
                env.now_ms - 1000,
                chat="CO",
                kind="group",
                title="#общий",
                mentions_me=True,
            ),
        ],
        account_id=env.slack_account_id,
    )
    await _cycle(env, agent2, 100)
    assert [item["channel_id"] for item in env.items()] == [env.channel_id("CO", "work")]


async def test_7b_skip_channels(env):
    env.ingest([msg("1", env.now_ms - 1000, chat="2100")])
    cid = env.channel_id("2100")
    agent = await _started(env, skip_channels=[cid])
    env.ingest([msg("2", env.now_ms - 1000, chat="2100")])
    await _cycle(env, agent, 100)
    assert env.items() == [] and env.notes == []


# --- К2 / П10 / К3: переполнение выборки, курсор, рестарт без потерь ----------------------------------------


async def test_7b_k2_many_unread_one_card_overflow_counted_cursor_moves(env):
    agent = await _started(env)
    now = env.now_ms
    env.ingest([msg(str(i), now - 300_000 + i * 100, chat="2200", text=f"m{i}") for i in range(1, 251)])
    await agent.poll_once()
    assert len(agent.buffers[env.channel_id("2200")].candidates) == 250
    assert files.load_state(env.data_dir)["seq"] < agent.sync_pos  # П10: курсор держится за кандидатов
    env.advance(16)
    assert await agent.tick() == 1
    assert agent.state["counters"]["overflow"] == 50
    assert agent.buffers == {}
    assert files.load_state(env.data_dir)["seq"] == agent.sync_pos
    (item,) = env.items()
    assert len(item["message_ids"]) == 200


async def test_7b_p10_restart_with_buffered_candidates_loses_nothing(env):
    agent = await _started(env)
    env.ingest([msg("1", env.now_ms - 1000, chat="2300")])
    await agent.poll_once()  # в буфере, не созрела (15 с тишины)
    cursor = files.load_state(env.data_dir)["seq"]
    assert cursor < agent.sync_pos

    again = await _started(env)  # рестарт: буфер потерян, курсор — перед кандидатом
    assert again.sync_pos == cursor
    await _cycle(env, again, 100)
    assert len(env.items()) == 1


async def test_7b_k3_restart_does_not_card_covered_again(env):
    agent = await _started(env)
    env.ingest([msg("1", env.now_ms - 1000, chat="2401", title="Долгая")])
    env.ingest([msg("1", env.now_ms - 1000, chat="2400")])
    await agent.poll_once()
    env.advance(10)
    env.ingest([msg("2", env.now_ms - 1000, chat="2401", title="Долгая")])  # «Долгая» ещё пишет — зреет
    await agent.poll_once()
    env.advance(6)
    assert await agent.tick() == 1  # карточка беседы 2400; «Долгая» ещё зреет
    assert [item["channel_id"] for item in env.items()] == [env.channel_id("2400")]
    cursor = files.load_state(env.data_dir)["seq"]

    again = await _started(env)
    assert again.sync_pos == cursor  # курсор перед кандидатом «Долгой» — беседа 2400 придёт снова
    await _cycle(env, again, 100)
    assert again.state["counters"]["covered"] == 1  # 2400 отсёк К3
    items = env.items()
    assert len(items) == 2
    assert items[1]["message_ids"] == [env.message_id("2401", "1"), env.message_id("2401", "2")]


async def test_7b_f1_night_catch_up_after_morning_card_is_merged(env):
    """Приёмка 7b, F1: утреннее живое сообщение первым создаёт карточку; затем догон после сна вставляет
    ночное непрочитанное (ts СТАРШЕ, first_seq новее) — оно не отсекается как «покрытое» и вливается в
    открытую карточку."""
    agent = await _started(env)
    now = env.now_ms
    env.ingest([msg("900", now - 1000, chat="3000", text="утро: ты тут?")])
    await _cycle(env, agent)
    (card,) = env.items()

    env.ingest([msg("300", now - 6 * HOUR_MS, chat="3000", text="ночью: сервер упал")])
    await _cycle(env, agent)
    (merged,) = env.items()
    assert merged["id"] == card["id"] and merged["provider"] == "rules"
    assert merged["message_ids"] == [env.message_id("3000", "300"), env.message_id("3000", "900")]
    assert merged["summary"] == "Пётр Иванов: ночью: сервер упал"
    assert "covered" not in agent.state.get("counters", {})


async def test_7b_f2_card_covers_all_candidates_restart_adds_nothing(env):
    """Приёмка 7b, F2: карточка покрывает ВСЕ кандидаты беседы — после рестарта второй карточки нет."""
    agent = await _started(env)
    now = env.now_ms
    env.ingest([msg(str(i), now - 1000 + i, chat="3100", text=f"m{i}") for i in (1, 2, 3)])
    await _cycle(env, agent)
    (card,) = env.items()
    assert card["message_ids"] == [env.message_id("3100", str(i)) for i in (1, 2, 3)]

    again = await _started(env)
    await _cycle(env, again, 100)
    assert env.items() == [card]


async def test_7b_edit_between_sync_pages_no_double_candidate(env, monkeypatch):
    monkeypatch.setattr(dispatcher, "SYNC_PAGE_LIMIT", 1)
    agent = await _started(env)
    env.ingest([msg("1", env.now_ms - 1000, chat="2500"), msg("2", env.now_ms - 900, chat="2500")])
    original_get = agent.core.get
    edited = {"done": False}

    async def get_with_edit(path, params=None):
        result = await original_get(path, params)
        if path == "/sync" and not edited["done"]:
            edited["done"] = True
            edit = {**msg("1", env.now_ms - 1000, chat="2500", text="поправил"), "type": "edit"}
            env.ingest([{**edit, "edited_ts": env.now_ms}])
        return result

    monkeypatch.setattr(agent.core, "get", get_with_edit)
    await agent.poll_once()
    buf = agent.buffers[env.channel_id("2500")]
    assert sorted(buf.candidates) == [env.message_id("2500", "1"), env.message_id("2500", "2")]
    env.advance(16)
    assert await agent.tick() == 1
    assert await _cycle(env, agent, 100) == 0
    (item,) = env.items()
    assert item["message_ids"] == [env.message_id("2500", "1"), env.message_id("2500", "2")]


async def test_7b_thread_reply_with_mention_gets_card_in_work_file(env):
    agent = await _started(env)
    now = env.now_ms
    acc = env.slack_account_id
    env.ingest(
        [
            msg(
                "1700000000.000100",
                now - 5000,
                chat="C1",
                kind="group",
                title="#релиз",
                text="Родитель треда",
            ),
            msg(
                "1700000000.000200",
                now - 4000,
                chat="C1",
                kind="group",
                title="#релиз",
                text="ответ",
                is_me=True,
                thread="1700000000.000100",
            ),
        ],
        account_id=acc,
    )
    env.ingest(
        [
            msg(
                "1700000000.000300",
                now - 1000,
                chat="C1",
                kind="group",
                title="#релиз",
                text="@владелец глянь",
                thread="1700000000.000100",
                mentions_me=True,
            )
        ],
        account_id=acc,
    )
    assert await _cycle(env, agent) == 1
    (item,) = env.items()
    assert item["channel_id"] == env.channel_id("C1", "work") and item["id"] > 2**40
    assert item["action"] == "упоминание" and item["summary"] == "Пётр Иванов: @владелец глянь"
    assert item["message_ids"] == [env.message_id("C1", "1700000000.000300", "work")]


# --- П7: токен ядра сменился (повторный setup) — агент подхватывает; не сменился — выход 78 -----------------


async def test_7b_p7_core_token_rotation_and_revocation(env, db):
    agent = await _started(env)
    created = db.write_sync(
        lambda conn: security.create_api_token(
            conn,
            name=files.TOKEN_NAME,
            kind="background",
            scopes=["read", "triage"],
            project_ids=None,
            expires_ts=None,
        )
    )
    files.write_secret(files.token_path(env.config_dir), created.token)
    db.write_sync(lambda conn: security.revoke_api_token(conn, env.token_id))
    env.ingest([msg("1", env.now_ms - 1000, chat="2600")])
    await _cycle(env, agent)
    assert len(env.items()) == 1

    db.write_sync(lambda conn: security.revoke_api_token(conn, created.token_id))
    with pytest.raises(CoreUnauthorized):
        await agent.poll_once()


async def test_7b_run_exits_78_when_core_token_revoked(env, db):
    import asyncio

    agent = env.agent()
    db.write_sync(lambda conn: security.revoke_api_token(conn, env.token_id))
    assert await agent.run(asyncio.Event()) == 78


# --- Тест 16 (+П8): ни токена, ни текстов — в журнале, stderr, state и исключениях -------------------------


async def test_7b_16_no_secrets_or_texts_in_logs_state_stderr(env, caplog, capsys, monkeypatch):
    secret_text = "СЕКРЕТНЫЙ-ТЕКСТ-СООБЩЕНИЯ"
    secret_title = "СЕКРЕТНОЕ-НАЗВАНИЕ"
    caplog.set_level(logging.DEBUG)
    agent = await _started(env)
    # 1) обычная карточка по правилам (пересказ и уведомление несут текст и название);
    env.ingest([msg("1", env.now_ms - 1000, chat="2700", title=secret_title, text=secret_text)])
    await _cycle(env, agent)
    assert len(env.items()) == 1
    assert any(secret_text in note[1] for note in env.notes)
    # 2) 422 от ядра: содержание длиннее лимита ядра.
    monkeypatch.setattr(dispatcher, "REACT_SUMMARY_MAX", 5000)
    env.ingest([msg("1", env.now_ms - 1000, chat="2701", title=secret_title, text=secret_text * 100)])
    await _cycle(env, agent)
    assert agent.state["counters"]["core_rejected"] == 1
    # 3) notify-send «не найден» — настоящий путь уведомления в тихом режиме.
    from hub import watch

    monkeypatch.setattr(watch.shutil, "which", lambda _name: None)
    watch.notify_send(f"Хаб · {secret_title}", secret_text, quiet=True)
    agent._save(force=True)

    state_text = files.state_path(env.data_dir).read_text(encoding="utf-8")
    err = capsys.readouterr().err
    logs = caplog.text + "".join(str(r.exc_info) for r in caplog.records if r.exc_info)
    for haystack in (logs, state_text, err):
        for needle in (env.token, secret_text, secret_title):
            assert needle not in haystack


# --- П6 (hub watch): «дежурный молчит» --------------------------------------------------------------------


def test_7b_p6_watch_agent_silent(tmp_path):
    from hub import watch

    assert watch._agent_snapshot(tmp_path) == {"agent_state_present": False}  # noqa: SLF001
    files.save_state(tmp_path, {"seq": 1, "last_ok_ts": 1_000})
    snap = watch._agent_snapshot(tmp_path)  # noqa: SLF001
    assert snap == {"agent_state_present": True, "agent_last_ok_ts": 1_000}
    base = {"accounts": [], "dead_letter_total": 0, "outbox_unknown": 0}
    assert watch.evaluate({**base, **snap}, now_ms=1_000 + 14 * 60_000) == []
    problems = watch.evaluate({**base, **snap}, now_ms=1_000 + 16 * 60_000)
    assert [p.key for p in problems] == ["agent_silent"]
    assert watch.evaluate({**base, "agent_state_present": False}, now_ms=10**12) == []
