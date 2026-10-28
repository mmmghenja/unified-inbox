"""Ядро тредов (спека `docs/architecture.md`/D) — тесты на `ingest.apply_batch`
напрямую, до слоя API. Все тесты — на work-аккаунте (Slack пишет в work), плюс контрольный на main
(Telegram не должен измениться ни в одном наблюдаемом поведении — он не заполняет `thread_ext_id`).
"""

from hub.core.ingest import apply_batch
from hub.core.threads import is_reply, is_top_level


def _insert_account(db, *, source="slack", db_name="work"):
    return db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.account (source, db) VALUES (?, ?)", (source, db_name)).lastrowid
        )
    )


def _insert_token(db, *, kind="ui", name="tok"):
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.api_token (name, kind, hash) VALUES (?, ?, 'x')", (name, kind)
            ).lastrowid
        )
    )


def _upsert(
    ext_id,
    ts,
    *,
    channel_ext="c1",
    channel_kind="channel",
    text="hi",
    is_me=False,
    kind="text",
    thread_ext_id=None,
    thread_broadcast=False,
    thread=None,
    mentions_me=False,
    **extra,
):
    event = {
        "type": "upsert",
        "channel": {"ext_id": channel_ext, "kind": channel_kind},
        "ext_id": ext_id,
        "ts": ts,
        "is_me": is_me,
        "kind": kind,
        "text": text,
        "mentions_me": mentions_me,
        "thread_ext_id": thread_ext_id,
        "thread_broadcast": thread_broadcast,
    }
    if thread is not None:
        event["thread"] = thread
    event.update(extra)
    return event


def _apply(db, account_id, events, now_ms=1000, cursor=None):
    return db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=events, cursor=cursor, now_ms=now_ms)
    )


def _thread_row(db, channel_ext="c1", schema="work"):
    with db.reader() as conn:
        return conn.execute(
            f"SELECT t.last_reply_ts, t.subscribed, t.source_reply_count, t.source_subscribed, "
            f"t.first_seen_ts, t.last_read_ts, t.last_read_ext FROM {schema}.thread t "
            f"JOIN {schema}.channel c ON c.id = t.channel_id WHERE c.ext_id = ?",
            (channel_ext,),
        ).fetchone()


def _message(db, ext_id, *, channel_ext="c1", schema="work"):
    with db.reader() as conn:
        return conn.execute(
            f"SELECT thread_ext_id, thread_broadcast, deleted, is_me FROM {schema}.message "
            f"WHERE ext_id = ? AND channel_id = (SELECT id FROM {schema}.channel WHERE ext_id = ?)",
            (ext_id, channel_ext),
        ).fetchone()


def _channel_row(db, channel_ext="c1", schema="work"):
    with db.reader() as conn:
        return conn.execute(
            f"SELECT last_msg_ts, seq FROM {schema}.channel WHERE ext_id = ?", (channel_ext,)
        ).fetchone()


# --- Единый предикат ------------------------------------------------------------------------------------


def test_is_reply_and_top_level_predicate():
    assert is_reply("P1", "R1") is True
    assert is_reply("P1", "P1") is False  # родитель со своим thread_ext_id == ext_id — не ответ
    assert is_reply(None, "R1") is False
    assert is_reply("P1", None) is True  # заготовка outbox: ext_id NULL, IS NOT — не <>

    assert is_top_level(None, "M1", False) is True
    assert is_top_level("P1", "R1", False) is False
    assert is_top_level("P1", "R1", True) is True  # broadcast — top_level, несмотря на is_reply


# --- Тест 2: родитель + ответы; broadcast; ответ без родителя; родитель после ответов -----------------------


def test_parent_and_three_replies_thread_summary(db):
    account_id = _insert_account(db)
    _apply(db, account_id, [_upsert("P1", 1000)])
    result = _apply(
        db,
        account_id,
        [
            _upsert("R1", 1001, thread_ext_id="P1"),
            _upsert("R2", 1002, thread_ext_id="P1"),
            _upsert("R3", 1003, thread_ext_id="P1"),
        ],
    )
    assert result.applied == 3

    row = _thread_row(db)
    assert row is not None
    last_reply_ts, _subscribed, _src_count, _src_sub, first_seen_ts, _lr_ts, _lr_ext = row
    assert last_reply_ts == 1003
    assert first_seen_ts is not None

    for ext_id in ("R1", "R2", "R3"):
        m = _message(db, ext_id)
        assert m[0] == "P1"
        assert m[1] == 0


def test_broadcast_reply_marked_and_visible_both_places(db):
    account_id = _insert_account(db)
    _apply(db, account_id, [_upsert("P1", 1000)])
    _apply(db, account_id, [_upsert("B1", 1001, thread_ext_id="P1", thread_broadcast=True)])
    m = _message(db, "B1")
    assert m[0] == "P1"
    assert m[1] == 1  # thread_broadcast
    assert is_top_level("P1", "B1", True) is True


def test_reply_without_parent_creates_thread_with_null_parent(db):
    account_id = _insert_account(db)
    _apply(db, account_id, [_upsert("R1", 1001, thread_ext_id="ORPHAN")])
    row = _thread_row(db)
    assert row is not None  # тред создан из ответа, хотя родителя P нет в message вовсе
    with db.reader() as conn:
        parent = conn.execute(
            "SELECT id FROM work.message WHERE ext_id = 'ORPHAN' AND channel_id = "
            "(SELECT id FROM work.channel WHERE ext_id = 'c1')"
        ).fetchone()
    assert parent is None


def test_parent_arriving_after_replies_is_found_by_ext_id(db):
    account_id = _insert_account(db)
    _apply(db, account_id, [_upsert("R1", 1001, thread_ext_id="LATE_P")])
    _apply(db, account_id, [_upsert("LATE_P", 999)])
    with db.reader() as conn:
        parent = conn.execute(
            "SELECT id FROM work.message WHERE ext_id = 'LATE_P' AND channel_id = "
            "(SELECT id FROM work.channel WHERE ext_id = 'c1')"
        ).fetchone()
    assert parent is not None


# --- Тест 3: заготовка-ответ из outbox, эхо/report, cancel ------------------------------------------------


def test_echo_reply_matches_placeholder_and_touches_thread(db):
    from hub.core.outbox import enqueue

    account_id = _insert_account(db)
    token_id = _insert_token(db)
    _apply(db, account_id, [_upsert("P1", 1000)])
    channel_id = db.write_sync(
        lambda conn: conn.execute("SELECT id FROM work.channel WHERE ext_id = 'c1'").fetchone()[0]
    )

    def _enqueue(conn):
        return enqueue(
            conn,
            channel_id=channel_id,
            text="моя реплика",
            thread_ext_id="P1",
            origin="ui",
            api_token_id=token_id,
            idempotency_key="k1",
            now_ms=1005,
        )

    result = db.write_sync(_enqueue)
    assert result.status == "queued"

    echo = _upsert(
        "R_ME",
        1006,
        text="моя реплика",
        is_me=True,
        thread_ext_id="P1",
        send_token=None,
    )
    applied = _apply(db, account_id, [echo], now_ms=1006)
    assert applied.applied == 1

    row = _thread_row(db)
    assert row is not None
    last_reply_ts, subscribed, _src_count, _src_sub, _fs, last_read_ts, last_read_ext = row
    assert last_reply_ts == 1006
    assert subscribed == 1  # мой ответ подписывает
    assert last_read_ext == "R_ME"  # мой ответ двигает маркер треда до себя


def test_report_sent_sets_ts_from_slack_ts_and_touches_thread(db):
    """Тест 1 (частично) + тест 3: `_report_sent` для ext_id вида ts Slack ставит
    `ts = slack_ts_to_ms(ext_id)` (сейчас там часы хаба со времени enqueue) и бампит сводку треда/канал,
    если это ответ."""
    from hub.core import outbox as outbox_mod

    account_id = _insert_account(db)
    token_id = _insert_token(db)
    _apply(db, account_id, [_upsert("P1", 1000)])
    channel_id = db.write_sync(
        lambda conn: conn.execute("SELECT id FROM work.channel WHERE ext_id = 'c1'").fetchone()[0]
    )

    def _enqueue(conn):
        return outbox_mod.enqueue(
            conn,
            channel_id=channel_id,
            text="ответ из хаба",
            thread_ext_id="P1",
            origin="ui",
            api_token_id=token_id,
            idempotency_key="k-report",
            now_ms=5_000,  # часы хаба на момент постановки в очередь — заведомо не похоже на Slack ts
        )

    enq = db.write_sync(_enqueue)

    leases = db.write_sync(
        lambda conn: outbox_mod.lease(conn, account_id=account_id, owner="conn-1", now_ms=5_100)
    )
    assert len(leases) == 1

    slack_ts = "1700000000.123456"
    db.write_sync(
        lambda conn: outbox_mod.report(
            conn,
            outbox_id=enq.outbox_id,
            owner="conn-1",
            lease_gen=leases[0].lease_gen,
            outcome="sent",
            ext_id=slack_ts,
            now_ms=5_200,
        )
    )

    with db.reader() as conn:
        ts = conn.execute("SELECT ts FROM work.message WHERE id = ?", (enq.message_id,)).fetchone()[0]
    assert ts == 1700000000_000 + 123  # slack_ts_to_ms(slack_ts), не 5000/5200 (часы хаба)

    row = _thread_row(db)
    assert row is not None
    last_reply_ts, subscribed, _src_count, _src_sub, _fs, last_read_ts, last_read_ext = row
    assert last_reply_ts == ts
    assert subscribed == 1  # мой ответ подписывает
    assert last_read_ext == slack_ts


def test_cancel_placeholder_reply_does_not_crash_and_hides_it(db):
    from hub.core import outbox as outbox_mod

    account_id = _insert_account(db)
    token_id = _insert_token(db)
    _apply(db, account_id, [_upsert("P1", 1000)])
    channel_id = db.write_sync(
        lambda conn: conn.execute("SELECT id FROM work.channel WHERE ext_id = 'c1'").fetchone()[0]
    )

    def _enqueue(conn):
        return outbox_mod.enqueue(
            conn,
            channel_id=channel_id,
            text="черновик",
            thread_ext_id="P1",
            origin="ui",
            api_token_id=token_id,
            idempotency_key="k2",
            now_ms=1005,
        )

    result = db.write_sync(_enqueue)
    status = db.write_sync(lambda conn: outbox_mod.cancel(conn, result.outbox_id, now_ms=1006))
    assert status == "cancelled"
    with db.reader() as conn:
        deleted = conn.execute(
            "SELECT deleted FROM work.message WHERE id = ?", (result.message_id,)
        ).fetchone()[0]
    assert deleted == 1


# --- Тест 4: подписка ------------------------------------------------------------------------------------


def test_subscription_auto_rules_and_source_override(db):
    account_id = _insert_account(db)
    _apply(db, account_id, [_upsert("P1", 1000)])

    # чужой ответ, без меня — не подписан
    _apply(db, account_id, [_upsert("R1", 1001, thread_ext_id="P1")])
    row = _thread_row(db)
    assert row[1] == 0

    # упоминание -> подписан
    _apply(db, account_id, [_upsert("R2", 1002, thread_ext_id="P1", mentions_me=True)])
    row = _thread_row(db)
    assert row[1] == 1


def test_source_subscribed_false_wins_over_auto(db):
    account_id = _insert_account(db)
    _apply(db, account_id, [_upsert("P1", 1000)])
    # мой ответ авто-подписывает тред
    _apply(db, account_id, [_upsert("R1", 1001, thread_ext_id="P1", is_me=True)])
    assert _thread_row(db)[1] == 1

    # источник явно говорит subscribed=false на родителе — главное его мнение, даже вопреки авто-правилу
    _apply(
        db,
        account_id,
        [_upsert("P1", 1000, thread={"reply_count": 1, "subscribed": False})],
        now_ms=1002,
    )
    assert _thread_row(db)[1] == 0


# --- Тест 6: seq — бамп родителя и канала, идентичный повтор не даёт seq ------------------------------------


def test_reply_bumps_parent_and_channel_seq(db):
    account_id = _insert_account(db)
    _apply(db, account_id, [_upsert("P1", 1000)])
    with db.reader() as conn:
        parent_seq_before = conn.execute(
            "SELECT seq FROM work.message WHERE ext_id = 'P1'"
        ).fetchone()[0]
        channel_seq_before = conn.execute("SELECT seq FROM work.channel WHERE ext_id = 'c1'").fetchone()[0]

    _apply(db, account_id, [_upsert("R1", 1001, thread_ext_id="P1")], now_ms=1001)

    with db.reader() as conn:
        parent_seq_after = conn.execute("SELECT seq FROM work.message WHERE ext_id = 'P1'").fetchone()[0]
        channel_seq_after = conn.execute("SELECT seq FROM work.channel WHERE ext_id = 'c1'").fetchone()[0]
    assert parent_seq_after > parent_seq_before
    assert channel_seq_after > channel_seq_before

    # идентичный повтор — 0 новых seq
    with db.reader() as conn:
        parent_seq_mid = conn.execute("SELECT seq FROM work.message WHERE ext_id = 'P1'").fetchone()[0]
    result = _apply(db, account_id, [_upsert("R1", 1001, thread_ext_id="P1")], now_ms=1002)
    assert result.applied == 0
    with db.reader() as conn:
        parent_seq_final = conn.execute("SELECT seq FROM work.message WHERE ext_id = 'P1'").fetchone()[0]
    assert parent_seq_final == parent_seq_mid


def test_full_thread_resync_gives_zero_new_seq(db):
    """«Сверка всего треда» (коннектор перечитал окно и прислал ВСЕ ответы заново) — идентичный набор,
    ни одного нового seq."""
    account_id = _insert_account(db)
    _apply(db, account_id, [_upsert("P1", 1000)])
    _apply(
        db,
        account_id,
        [
            _upsert("R1", 1001, thread_ext_id="P1"),
            _upsert("R2", 1002, thread_ext_id="P1"),
            _upsert("R3", 1003, thread_ext_id="P1"),
        ],
        now_ms=1003,
    )
    with db.reader() as conn:
        seq_before = conn.execute("SELECT value FROM main.seq_counter WHERE id = 1").fetchone()[0]

    # сверка перечитывает окно треда целиком и присылает те же 3 ответа повторно, одной пачкой
    result = _apply(
        db,
        account_id,
        [
            _upsert("R1", 1001, thread_ext_id="P1"),
            _upsert("R2", 1002, thread_ext_id="P1"),
            _upsert("R3", 1003, thread_ext_id="P1"),
        ],
        now_ms=2000,
    )
    assert result.applied == 0
    assert result.noop == 3
    with db.reader() as conn:
        seq_after = conn.execute("SELECT value FROM main.seq_counter WHERE id = 1").fetchone()[0]
    assert seq_after == seq_before


# --- Тест 5: непрочитанное — тредовая read только тредовое, канальная — только верхний уровень; ReadEvent ---


def test_thread_read_event_advances_thread_marker_only(db):
    """`ReadEvent` с `thread_ext_id` двигает МАРКЕР ТРЕДА, не канала."""
    account_id = _insert_account(db)
    _apply(db, account_id, [_upsert("P1", 1000)])
    _apply(db, account_id, [_upsert("R1", 1001, thread_ext_id="P1")])

    read_event = {
        "type": "read",
        "channel": {"ext_id": "c1", "kind": "channel"},
        "up_to_ext_id": "R1",
        "thread_ext_id": "P1",
    }
    result = _apply(db, account_id, [read_event], now_ms=1002)
    assert result.applied == 1

    row = _thread_row(db)
    assert row[5] == 1001  # last_read_ts треда
    assert row[6] == "R1"
    with db.reader() as conn:
        channel_last_read = conn.execute(
            "SELECT last_read_ts FROM work.channel WHERE ext_id = 'c1'"
        ).fetchone()[0]
    assert channel_last_read is None  # канальный маркер НЕ тронут


def test_read_event_unknown_slack_ts_advances_marker_by_position(db):
    """Раздел A.5: `up_to_ext_id` в формате ts Slack, сообщения в ядре ещё нет (отфильтрованный подтип) —
    позиция выводится из самой строки, маркер не застревает. Проверяем и канальный, и тредовый путь.

    Поправки после opus-критика кода (T7): `ReadEvent` с `thread_ext_id` на НЕИЗВЕСТНЫЙ тред — no-op, строку
    не создавать. Тредовая часть этого теста поэтому сначала заводит тред «P1» настоящим ответом (R0) — сам
    тест по-прежнему проверяет именно вывод позиции из строки `ReadEvent`, когда сообщение с `up_to_ext_id`
    ещё не пришло, а не «может ли ReadEvent открыть тред с нуля» (это отдельно проверяет `test_thr6` из
    `test_critic_2b.py`)."""
    account_id = _insert_account(db)
    _apply(db, account_id, [_upsert("P1", 1000)])
    _apply(db, account_id, [_upsert("R0", 1000, thread_ext_id="P1")], now_ms=1001)  # заводит тред «P1»

    channel_read_event = {
        "type": "read",
        "channel": {"ext_id": "c1", "kind": "channel"},
        "up_to_ext_id": "1700000000.500000",  # сообщения с таким ext_id в ядре нет вовсе
    }
    result = _apply(db, account_id, [channel_read_event], now_ms=2000)
    assert result.applied == 1
    with db.reader() as conn:
        last_read_ts, last_read_ext = conn.execute(
            "SELECT last_read_ts, last_read_ext FROM work.channel WHERE ext_id = 'c1'"
        ).fetchone()
    assert last_read_ts == 1700000000_000 + 500
    assert last_read_ext == "1700000000.500000"

    thread_read_event = {
        "type": "read",
        "channel": {"ext_id": "c1", "kind": "channel"},
        "up_to_ext_id": "1700000001.000000",
        "thread_ext_id": "P1",
    }
    result2 = _apply(db, account_id, [thread_read_event], now_ms=2001)
    assert result2.applied == 1
    row = _thread_row(db)
    assert row[5] == 1700000001_000
    assert row[6] == "1700000001.000000"


def test_reply_older_than_first_seen_minus_24h_not_unread(db):
    """Раздел «Непрочитанное»: ответ старше `first_seen_ts - 24 ч` не считается непрочитанным (стартовая
    загрузка большого старого треда без `last_read` от источника не даёт сотен непрочитанных)."""
    from hub.core.ingest import unread_breakdown

    account_id = _insert_account(db)
    day_ms = 24 * 60 * 60 * 1000
    _apply(db, account_id, [_upsert("P1", 1000, is_me=True)], now_ms=1000)  # мой родитель -> подписка сразу
    channel_id = db.write_sync(
        lambda conn: conn.execute("SELECT id FROM work.channel WHERE ext_id = 'c1'").fetchone()[0]
    )
    # ответ значительно СТАРШЕ момента появления треда (first_seen_ts = ts первого увиденного ответа)
    old_reply_ts = 1000 + 10 * day_ms
    _apply(db, account_id, [_upsert("R_OLD", old_reply_ts, thread_ext_id="P1")], now_ms=old_reply_ts)
    with db.reader() as conn:
        first_seen_ts = conn.execute(
            "SELECT first_seen_ts FROM work.thread WHERE ext_id = 'P1'"
        ).fetchone()[0]
    _apply(
        db,
        account_id,
        [_upsert("R_STALE", first_seen_ts - 25 * 60 * 60 * 1000, thread_ext_id="P1")],
        now_ms=first_seen_ts,
    )
    with db.reader() as conn:
        top, threads_unread = unread_breakdown(conn, channel_id)
    del top
    assert threads_unread == 1  # только R_OLD; R_STALE (старше first_seen_ts - 24ч) не в счёт


# --- Тест 12: last_msg_ts канала: чужой тред не поднимает, подписанный — поднимает --------------------------


def test_last_msg_ts_not_bumped_for_unsubscribed_thread_reply(db):
    account_id = _insert_account(db)
    _apply(db, account_id, [_upsert("P1", 1000)], now_ms=1000)
    last_msg_ts_before = _channel_row(db)[0]
    assert last_msg_ts_before == 1000

    # чужой ответ в неподписанном треде — last_msg_ts НЕ двигается
    _apply(db, account_id, [_upsert("R1", 2000, thread_ext_id="P1")], now_ms=2000)
    assert _channel_row(db)[0] == 1000

    # мой ответ подписывает тред -> последующий чужой ответ уже поднимает канал
    _apply(db, account_id, [_upsert("R2", 3000, thread_ext_id="P1", is_me=True)], now_ms=3000)
    assert _channel_row(db)[0] == 3000  # моё сообщение — top_level-эквивалент по подписке, поднимает сразу

    _apply(db, account_id, [_upsert("R3", 4000, thread_ext_id="P1")], now_ms=4000)
    assert _channel_row(db)[0] == 4000


# --- Тест 6 (продолжение): бамп не двигает first_seq/позицию родителя, не делает его непрочитанным --------


def test_thread_bump_does_not_move_parent_first_seq_or_unread_marker(db):
    from hub.core import channels as channels_mod
    from hub.core.ingest import unread_count

    account_id = _insert_account(db)
    # родитель НЕ мой (иначе «я автор родителя» сразу подписал бы тред — раздел «Модель») — читаем канал
    # руками до его позиции, чтобы изолированно проверить: бамп ответа в НЕподписанном треде не откатывает
    # канал в непрочитанное состояние по родителю.
    _apply(db, account_id, [_upsert("P1", 1000)], now_ms=1000)
    with db.reader() as conn:
        channel_id, parent_first_seq_before, parent_ts, parent_ext_sort, parent_ext_id = conn.execute(
            "SELECT channel_id, first_seq, ts, ext_sort, ext_id FROM work.message WHERE ext_id = 'P1'"
        ).fetchone()
    # отмечаем канал прочитанным по родителю (маркер = позиция P1)
    db.write_sync(
        lambda conn: channels_mod.advance_read_marker(
            conn,
            "work",
            channel_id,
            ts=parent_ts,
            ext_sort=parent_ext_sort,
            ext_id=parent_ext_id,
            first_seq=parent_first_seq_before,
        )
    )

    _apply(db, account_id, [_upsert("R1", 2000, thread_ext_id="P1")], now_ms=2000)

    with db.reader() as conn:
        parent_first_seq_after, parent_deleted = conn.execute(
            "SELECT first_seq, deleted FROM work.message WHERE ext_id = 'P1'"
        ).fetchone()
    assert parent_first_seq_after == parent_first_seq_before  # бамп НЕ меняет first_seq
    assert parent_deleted == 0
    with db.reader() as conn:
        top_unread = unread_count(conn, channel_id)
    # канал уже отмечен прочитанным ПО ПОЗИЦИИ родителя -> unread_top = 0; ответ чужой, но тред не подписан
    # (родитель не мой, ответ не мой и без упоминания) -> unread_threads = 0 тоже. Итог 0 доказывает: бамп не
    # откатил канал в непрочитанное состояние по родителю (не поменял его first_seq/позицию).
    assert top_unread == 0


def test_thread_summary_applies_even_without_edited_ts_change(db):
    """Раздел D.1: снимок родителя БЕЗ edited_ts (обычный повтор upsert) не должен съедать новый
    reply_count — ThreadInfo обрабатывается ДО ранних выходов _apply_message."""
    account_id = _insert_account(db)
    parent_event = _upsert("P1", 1000)
    _apply(db, account_id, [parent_event], now_ms=1000)
    _apply(db, account_id, [_upsert("R1", 1001, thread_ext_id="P1")], now_ms=1001)

    with db.reader() as conn:
        seq_before = conn.execute("SELECT seq FROM work.message WHERE ext_id = 'P1'").fetchone()[0]

    # тот же самый родитель (identical content, no edited_ts) приходит снова, но со сводкой ThreadInfo
    repeat_with_thread_info = _upsert("P1", 1000, thread={"reply_count": 1, "subscribed": True})
    result = _apply(db, account_id, [repeat_with_thread_info], now_ms=1002)
    assert result.applied == 1  # НЕ noop, несмотря на идентичный текст и отсутствие edited_ts

    with db.reader() as conn:
        seq_after, subscribed, source_reply_count = conn.execute(
            "SELECT m.seq, t.subscribed, t.source_reply_count FROM work.message m "
            "JOIN work.thread t ON t.channel_id = m.channel_id AND t.ext_id = m.ext_id "
            "WHERE m.ext_id = 'P1'"
        ).fetchone()
    assert seq_after > seq_before
    assert subscribed == 1
    assert source_reply_count == 1


# --- Telegram-регресс (main, без thread_ext_id) -------------------------------------------------------------


def test_telegram_main_account_unaffected(db):
    account_id = _insert_account(db, source="tg", db_name="main")
    result = _apply(db, account_id, [_upsert("1", 1000, channel_kind="dm")], now_ms=1000)
    assert result.applied == 1
    with db.reader() as conn:
        row = conn.execute(
            "SELECT thread_ext_id, thread_broadcast, purged FROM main.message WHERE ext_id = '1'"
        ).fetchone()
    assert row == (None, 0, 0)


def test_source_last_read_unknown_slack_ts_advances_thread_marker(db):
    """Приёмка 2b (архитектор): `ThreadInfo.last_read_ext_id`, сообщения которого в ядре нет, двигает маркер
    треда по позиции самого ts Slack — как `ReadEvent` (раздел A), иначе маркер застревал бы."""
    account_id = _insert_account(db)
    parent = _upsert(
        "1700000000.000100",
        1700000000_000,
        thread={"reply_count": 2, "latest_reply_ext_id": "1700000005.000000",
                "last_read_ext_id": "1700000003.000000"},
    )
    _apply(db, account_id, [parent])
    row = _thread_row(db)
    assert row[5] == 1700000003_000
    assert row[6] == "1700000003.000000"


# --- Прямое упоминание меня в треде — непрочитано, даже если источник отписал тред (баг #product_news) ------


def _reply_unread(db, ext_id, schema="work"):
    from hub.core.threads import message_unread_sql

    with db.reader() as conn:
        return conn.execute(
            f"SELECT {message_unread_sql('m', 'ch', 't')} FROM {schema}.message m "
            f"JOIN {schema}.channel ch ON ch.id = m.channel_id "
            f"LEFT JOIN {schema}.thread t ON t.channel_id = m.channel_id AND t.ext_id = m.thread_ext_id "
            f"WHERE m.ext_id = ?",
            (ext_id,),
        ).fetchone()[0]


def _channel_unread(db):
    from hub.core.ingest import unread_breakdown

    with db.reader() as conn:
        cid = conn.execute("SELECT id FROM work.channel WHERE ext_id = 'c1'").fetchone()[0]
        return unread_breakdown(conn, cid)


def test_direct_mention_in_unsubscribed_thread_is_unread_after_source_reconcile(db):
    account_id = _insert_account(db)
    _apply(db, account_id, [_upsert("P1", 1000)], now_ms=1000)
    _apply(db, account_id, [_upsert("R1", 1001, thread_ext_id="P1")], now_ms=1001)
    # сверка: источник явно говорит subscribed=false
    _apply(db, account_id, [_upsert("P1", 1000, thread={"reply_count": 1, "subscribed": False})], now_ms=1002)
    # потом приходит прямое упоминание меня
    _apply(db, account_id, [_upsert("R2", 1003, thread_ext_id="P1", mentions_me=True)], now_ms=1003)
    # Ф28-1: упоминание после снимка — новое свидетельство, снимок «не подписан» сброшен, тред подписан
    assert _thread_row(db)[1] == 1
    assert _thread_row(db)[3] is None
    assert _reply_unread(db, "R2") == 1
    assert _reply_unread(db, "R1") == 1  # тред теперь подписан — и прежний ответ непрочитан
    assert _channel_unread(db)[1] == 2


def test_direct_mention_unread_when_source_unsubscribes_after_mention(db):
    account_id = _insert_account(db)
    _apply(db, account_id, [_upsert("P1", 1000)], now_ms=1000)
    _apply(db, account_id, [_upsert("R1", 1001, thread_ext_id="P1", mentions_me=True)], now_ms=1001)
    assert _thread_row(db)[1] == 1
    # сверка потом снимает подписку (источник отписал)
    _apply(db, account_id, [_upsert("P1", 1000, thread={"reply_count": 1, "subscribed": False})], now_ms=1002)
    assert _thread_row(db)[1] == 0
    assert _reply_unread(db, "R1") == 1
    assert _channel_unread(db)[1] == 1


def test_broadcast_only_mention_in_unsubscribed_thread_not_unread(db):
    account_id = _insert_account(db)
    _apply(db, account_id, [_upsert("P1", 1000)], now_ms=1000)
    _apply(db, account_id, [_upsert("P1", 1000, thread={"reply_count": 1, "subscribed": False})], now_ms=1001)
    _apply(
        db,
        account_id,
        [_upsert("R1", 1002, thread_ext_id="P1", mentions_me=True, mentions_broadcast_only=True)],
        now_ms=1002,
    )
    assert _reply_unread(db, "R1") == 0
    assert _channel_unread(db)[1] == 0
