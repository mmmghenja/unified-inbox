import json

import pytest

from hub.core.db import WORK_ID_BASE, Db, db_of
from hub.core.ingest import apply_batch
from hub.core.outbox import (
    EnqueueResult,
    IdempotencyConflict,
    InvalidTransition,
    StaleLease,
    approve,
    cancel,
    enqueue,
    lease,
    report,
    resolve_unknown,
)

DAY_MS = 24 * 60 * 60 * 1000


def _insert_account(db, *, source="tg", db_name="main", idempotent=False, flood_until=None):
    config = json.dumps({"idempotent_send": True}) if idempotent else None
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, config_json, flood_until) VALUES (?, ?, ?, ?)",
                (source, db_name, config, flood_until),
            ).lastrowid
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


def _insert_channel(
    db,
    account_id,
    *,
    ext_id="c1",
    kind="dm",
    my_msg_count=1,
    first_seen_ts=0,
    title_history_json=None,
    archived=0,
    db_name="main",
):
    return db.write_sync(
        lambda conn: (
            conn.execute(
                f"INSERT INTO {db_name}.channel "
                "(account_id, ext_id, kind, my_msg_count, first_seen_ts, title_history_json, archived) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (account_id, ext_id, kind, my_msg_count, first_seen_ts, title_history_json, archived),
            ).lastrowid
        )
    )


def _enqueue(
    db,
    channel_id,
    *,
    text="hi",
    origin="ui",
    api_token_id,
    idempotency_key=None,
    now_ms=1000,
    reply_to_ext_id=None,
    thread_ext_id=None,
):
    return db.write_sync(
        lambda conn: enqueue(
            conn,
            channel_id=channel_id,
            text=text,
            origin=origin,
            api_token_id=api_token_id,
            idempotency_key=idempotency_key,
            now_ms=now_ms,
            reply_to_ext_id=reply_to_ext_id,
            thread_ext_id=thread_ext_id,
        )
    )


def _approve(db, outbox_id, *, text=None, now_ms):
    return db.write_sync(lambda conn: approve(conn, outbox_id, text=text, now_ms=now_ms))


def _cancel(db, outbox_id, *, now_ms):
    return db.write_sync(lambda conn: cancel(conn, outbox_id, now_ms=now_ms))


def _lease(db, account_id, owner, *, now_ms, lease_ms=60_000, limit=10):
    return db.write_sync(
        lambda conn: lease(
            conn, account_id=account_id, owner=owner, now_ms=now_ms, lease_ms=lease_ms, limit=limit
        )
    )


def _report(db, outbox_id, owner, outcome, *, lease_gen=None, **kw):
    """lease_gen по умолчанию — текущее поколение из БД (честный коннектор); устаревшее передаётся явно."""

    def _fn(conn):
        gen = lease_gen
        if gen is None:
            schema = db_of(outbox_id)
            gen = conn.execute(
                f"SELECT lease_gen FROM {schema}.outbox WHERE id = ?", (outbox_id,)
            ).fetchone()[0]
        return report(conn, outbox_id=outbox_id, owner=owner, lease_gen=gen, outcome=outcome, **kw)

    return db.write_sync(_fn)


def _resolve_unknown(db, outbox_id, decision, *, now_ms):
    return db.write_sync(lambda conn: resolve_unknown(conn, outbox_id, decision=decision, now_ms=now_ms))


def _outbox_row(db, outbox_id, db_name="main"):
    cols = (
        "status",
        "ext_id",
        "attempts",
        "send_token",
        "reconcile",
        "lease_owner",
        "lease_expires_ts",
        "not_before_ts",
        "send_started_ts",
        "sent_ts",
        "error",
    )
    with db.reader() as conn:
        row = conn.execute(
            f"SELECT {', '.join(cols)} FROM {db_name}.outbox WHERE id = ?", (outbox_id,)
        ).fetchone()
    return dict(zip(cols, row, strict=True))


def _my_msg_count(db, channel_id, db_name="main"):
    with db.reader() as conn:
        return conn.execute(
            f"SELECT my_msg_count FROM {db_name}.channel WHERE id = ?", (channel_id,)
        ).fetchone()[0]


def _apply(db, account_id, events, *, now_ms):
    return db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=events, cursor=None, now_ms=now_ms)
    )


def _upsert_echo(ext_id, ts, *, text="hi", send_token=None, channel_ext="c1"):
    event = {
        "type": "upsert",
        "channel": {"ext_id": channel_ext, "kind": "dm"},
        "ext_id": ext_id,
        "ts": ts,
        "is_me": True,
        "kind": "text",
        "text": text,
        "mentions_me": False,
    }
    if send_token is not None:
        event["send_token"] = send_token
    return event


# 1. enqueue: ровно одна строка outbox и одна message; повтор ключа -> те же id;
#    другой текст -> IdempotencyConflict.
def test_enqueue_creates_rows_and_dedups_by_idempotency_key(db):
    account_id = _insert_account(db)
    token_id = _insert_token(db)
    channel_id = _insert_channel(db, account_id, my_msg_count=5, first_seen_ts=0)

    result = _enqueue(db, channel_id, text="hi", api_token_id=token_id, idempotency_key="k1", now_ms=1000)
    assert result.created is True

    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.outbox").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM main.message").fetchone()[0] == 1

    repeat = _enqueue(db, channel_id, text="hi", api_token_id=token_id, idempotency_key="k1", now_ms=2000)
    assert repeat == EnqueueResult(result.outbox_id, result.message_id, result.status, False)
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.outbox").fetchone()[0] == 1

    with pytest.raises(IdempotencyConflict):
        _enqueue(
            db, channel_id, text="другой текст", api_token_id=token_id, idempotency_key="k1", now_ms=3000
        )


# 2. Стартовый статус: ui -> queued; api в недоверенный канал (пустой/молодой/переименованный) ->
#    pending_approval; api в старый доверенный канал — со спеки 7a (A1, план 5.1 «умолчание confirm») тоже
#    pending_approval: всё, что пришло через bearer, — черновик (было queued — смена правила, не поломка).
def test_start_status_by_origin_and_trust(db):
    account_id = _insert_account(db)
    token_id = _insert_token(db)
    now = 20 * DAY_MS

    fresh_channel = _insert_channel(db, account_id, ext_id="fresh", my_msg_count=0, first_seen_ts=0)
    r_ui = _enqueue(db, fresh_channel, origin="ui", api_token_id=token_id, idempotency_key="ui1", now_ms=now)
    assert r_ui.status == "queued"

    r_api_empty = _enqueue(
        db, fresh_channel, origin="api", api_token_id=token_id, idempotency_key="api1", now_ms=now
    )
    assert r_api_empty.status == "pending_approval"  # my_msg_count == 0

    young_channel = _insert_channel(
        db, account_id, ext_id="young", my_msg_count=3, first_seen_ts=now - 5 * DAY_MS
    )
    r_young = _enqueue(
        db, young_channel, origin="api", api_token_id=token_id, idempotency_key="api2", now_ms=now
    )
    assert r_young.status == "pending_approval"  # моложе 14 суток

    renamed_channel = _insert_channel(
        db,
        account_id,
        ext_id="renamed",
        my_msg_count=3,
        first_seen_ts=0,
        title_history_json=json.dumps([{"title": "старое", "ts": now - 3 * DAY_MS}]),
    )
    r_renamed = _enqueue(
        db, renamed_channel, origin="api", api_token_id=token_id, idempotency_key="api3", now_ms=now
    )
    assert r_renamed.status == "pending_approval"  # переименован < 7 суток назад

    old_channel = _insert_channel(db, account_id, ext_id="old", my_msg_count=3, first_seen_ts=0)
    r_old = _enqueue(db, old_channel, origin="api", api_token_id=token_id, idempotency_key="api4", now_ms=now)
    assert r_old.status == "pending_approval"  # спека 7a, A1: доверие канала bearer-у больше не даёт queued


# 3. approve с правкой текста меняет оба текста и FTS; approve не из pending -> ошибка; cancel из queued ->
#    заготовка deleted=1; cancel из sending -> InvalidTransition, ничего не изменилось.
def test_approve_and_cancel(db):
    account_id = _insert_account(db)
    token_id = _insert_token(db)
    channel_id = _insert_channel(db, account_id, my_msg_count=0)  # untrusted -> api даёт pending_approval

    enq = _enqueue(
        db, channel_id, text="черновик", origin="api", api_token_id=token_id, idempotency_key="a1", now_ms=100
    )
    assert enq.status == "pending_approval"

    _approve(db, enq.outbox_id, text="финальный текст", now_ms=200)
    assert _outbox_row(db, enq.outbox_id)["status"] == "queued"
    with db.reader() as conn:
        text = conn.execute("SELECT text FROM main.message WHERE id = ?", (enq.message_id,)).fetchone()[0]
        assert text == "финальный текст"
        found = conn.execute(
            "SELECT text FROM main.message_fts WHERE message_fts MATCH 'финальный*'"
        ).fetchall()
        assert found

    with pytest.raises(InvalidTransition):
        _approve(db, enq.outbox_id, now_ms=300)  # уже не pending_approval

    channel2 = _insert_channel(db, account_id, ext_id="c2", my_msg_count=5, first_seen_ts=0)
    enq2 = _enqueue(db, channel2, origin="ui", api_token_id=token_id, idempotency_key="a2", now_ms=100)
    _cancel(db, enq2.outbox_id, now_ms=150)
    with db.reader() as conn:
        deleted = conn.execute(
            "SELECT deleted FROM main.message WHERE id = ?", (enq2.message_id,)
        ).fetchone()[0]
    assert deleted == 1

    channel3 = _insert_channel(db, account_id, ext_id="c3", my_msg_count=5, first_seen_ts=0)
    enq3 = _enqueue(db, channel3, origin="ui", api_token_id=token_id, idempotency_key="a3", now_ms=100)
    _lease(db, account_id, "w1", now_ms=100)
    before = _outbox_row(db, enq3.outbox_id)
    with pytest.raises(InvalidTransition):
        _cancel(db, enq3.outbox_id, now_ms=200)
    assert _outbox_row(db, enq3.outbox_id) == before


# 4. lease: not_before_ts и flood_until соблюдаются; чужой аккаунт не получает; второй вызов не возвращает
#    ту же строку; FIFO по каналу; другой канал не блокируется.
def test_lease_respects_not_before_flood_and_fifo(db):
    account_id = _insert_account(db)
    other_account = _insert_account(db, source="tg2")
    token_id = _insert_token(db)
    channel_a = _insert_channel(db, account_id, ext_id="a", my_msg_count=5, first_seen_ts=0)
    channel_b = _insert_channel(db, account_id, ext_id="b", my_msg_count=5, first_seen_ts=0)
    other_channel = _insert_channel(db, other_account, ext_id="oc", my_msg_count=5, first_seen_ts=0)

    # отложенная строка — в своём канале: в общем она по FIFO держала бы всё, что встало за ней
    channel_late = _insert_channel(db, account_id, ext_id="late", my_msg_count=5, first_seen_ts=0)
    later = _enqueue(
        db, channel_late, text="later", api_token_id=token_id, idempotency_key="k-late", now_ms=100
    )
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.outbox SET not_before_ts = ? WHERE id = ?", (10_000, later.outbox_id)
        )
    )
    other_enq = _enqueue(db, other_channel, api_token_id=token_id, idempotency_key="k-other", now_ms=100)

    assert (
        _lease(db, account_id, "w1", now_ms=100) == []
    )  # not_before_ts не настал; чужой аккаунт не выдаётся

    first_a = _enqueue(
        db, channel_a, text="first", api_token_id=token_id, idempotency_key="k-first", now_ms=100
    )
    second_a = _enqueue(
        db, channel_a, text="second", api_token_id=token_id, idempotency_key="k-second", now_ms=100
    )
    first_b = _enqueue(db, channel_b, text="b1", api_token_id=token_id, idempotency_key="k-b1", now_ms=100)

    leases = _lease(db, account_id, "w1", now_ms=100)
    ids = {leased.outbox_id for leased in leases}
    assert first_a.outbox_id in ids
    assert second_a.outbox_id not in ids  # FIFO: ждёт первую в sending
    assert first_b.outbox_id in ids  # другой канал не блокируется
    assert other_enq.outbox_id not in ids

    leases2 = _lease(db, account_id, "w1", now_ms=100)
    ids2 = {leased.outbox_id for leased in leases2}
    assert first_a.outbox_id not in ids2 and first_b.outbox_id not in ids2  # второй вызов не повторяет выдачу

    _report(db, first_a.outbox_id, "w1", "sent", ext_id="ext-a1", now_ms=110)
    leases3 = _lease(db, account_id, "w1", now_ms=110)
    assert second_a.outbox_id in {leased.outbox_id for leased in leases3}

    _report(db, first_b.outbox_id, "w1", "flood", retry_after_ms=5000, now_ms=110)
    assert _lease(db, account_id, "w1", now_ms=111) == []  # весь аккаунт стоит до flood_until
    after_flood = _lease(db, account_id, "w1", now_ms=5111)
    assert first_b.outbox_id in {leased.outbox_id for leased in after_flood}


# 5. Упал ДО отправки, идемпотентный аккаунт: лиз истёк -> снова mode='send', тот же send_token, attempts=2.
def test_lease_retries_same_send_token_for_idempotent_account_before_send(db):
    account_id = _insert_account(db, idempotent=True)
    token_id = _insert_token(db)
    channel_id = _insert_channel(db, account_id, my_msg_count=5, first_seen_ts=0)
    _enqueue(db, channel_id, api_token_id=token_id, idempotency_key="k1", now_ms=100)

    first = _lease(db, account_id, "w1", now_ms=100, lease_ms=1000)[0]
    assert first.attempts == 1

    second = _lease(db, account_id, "w1", now_ms=1500)
    assert len(second) == 1
    assert second[0].outbox_id == first.outbox_id
    assert second[0].mode == "send"
    assert second[0].send_token == first.send_token
    assert second[0].attempts == 2


# 6. Упал ПОСЛЕ отправки до результата, неидемпотентный: лиз истёк -> mode='reconcile'; report(sent) ->
#    sent; отдельно report(unknown) -> unknown; report(temporary) на reconcile-лизе -> InvalidTransition,
#    без возврата в queued.
def test_lease_reconciles_after_send_before_result_for_non_idempotent(db):
    account_id = _insert_account(db, idempotent=False)
    token_id = _insert_token(db)

    channel1 = _insert_channel(db, account_id, ext_id="c1", my_msg_count=5, first_seen_ts=0)
    enq1 = _enqueue(db, channel1, api_token_id=token_id, idempotency_key="k1", now_ms=100)
    _lease(db, account_id, "w1", now_ms=100, lease_ms=1000)
    reconciled1 = _lease(db, account_id, "w2", now_ms=1500)
    assert len(reconciled1) == 1
    assert reconciled1[0].mode == "reconcile"
    assert reconciled1[0].attempts == 2
    assert _outbox_row(db, enq1.outbox_id)["reconcile"] == 1
    _report(db, enq1.outbox_id, "w2", "sent", ext_id="e1", now_ms=1600)
    assert _outbox_row(db, enq1.outbox_id)["status"] == "sent"

    channel2 = _insert_channel(db, account_id, ext_id="c2", my_msg_count=5, first_seen_ts=0)
    enq2 = _enqueue(db, channel2, api_token_id=token_id, idempotency_key="k2", now_ms=100)
    _lease(db, account_id, "w1", now_ms=100, lease_ms=1000)
    reconciled2 = _lease(db, account_id, "w2", now_ms=1500)
    assert reconciled2[0].mode == "reconcile"
    _report(db, enq2.outbox_id, "w2", "unknown", now_ms=1600)
    assert _outbox_row(db, enq2.outbox_id)["status"] == "unknown"

    channel3 = _insert_channel(db, account_id, ext_id="c3", my_msg_count=5, first_seen_ts=0)
    enq3 = _enqueue(db, channel3, api_token_id=token_id, idempotency_key="k3", now_ms=100)
    _lease(db, account_id, "w1", now_ms=100, lease_ms=1000)
    reconciled3 = _lease(db, account_id, "w2", now_ms=1500)
    assert reconciled3[0].mode == "reconcile"
    before = _outbox_row(db, enq3.outbox_id)
    with pytest.raises(InvalidTransition):
        _report(db, enq3.outbox_id, "w2", "temporary", now_ms=1600)
    assert _outbox_row(db, enq3.outbox_id) == before  # не откатилось в queued вслепую


# 7. Эхо РАНЬШЕ результата (по send_token): одна видимая строка, ext_id есть, outbox sent; затем
#    report(sent) с тем же ext_id — тихий успех, строка по-прежнему одна, my_msg_count вырос на 1 (а не на 2).
def test_echo_before_result_merges_by_send_token(db):
    account_id = _insert_account(db)
    token_id = _insert_token(db)
    channel_id = _insert_channel(db, account_id, ext_id="c1", my_msg_count=5, first_seen_ts=0)
    enq = _enqueue(db, channel_id, text="hi", api_token_id=token_id, idempotency_key="k1", now_ms=100)
    leased = _lease(db, account_id, "w1", now_ms=100)[0]

    result = _apply(db, account_id, [_upsert_echo("ext-1", 150, send_token=leased.send_token)], now_ms=150)
    assert result.applied == 1

    with db.reader() as conn:
        rows = conn.execute(
            "SELECT ext_id, deleted FROM main.message WHERE channel_id = ?", (channel_id,)
        ).fetchall()
    visible = [r for r in rows if r[1] == 0]
    assert len(visible) == 1
    assert visible[0][0] == "ext-1"
    assert _outbox_row(db, enq.outbox_id)["status"] == "sent"
    assert _my_msg_count(db, channel_id) == 6

    _report(db, enq.outbox_id, "w1", "sent", ext_id="ext-1", now_ms=200)  # эхо уже применилось — тихий успех
    assert _my_msg_count(db, channel_id) == 6  # не выросло повторно


# 8. Результат РАНЬШЕ эха: эхо-upsert попадает в ту же строку; видимая строка одна.
def test_result_before_echo_merges_into_same_row(db):
    account_id = _insert_account(db)
    token_id = _insert_token(db)
    channel_id = _insert_channel(db, account_id, ext_id="c1", my_msg_count=5, first_seen_ts=0)
    enq = _enqueue(db, channel_id, text="hi", api_token_id=token_id, idempotency_key="k1", now_ms=100)
    _lease(db, account_id, "w1", now_ms=100)
    _report(db, enq.outbox_id, "w1", "sent", ext_id="ext-1", now_ms=150)

    _apply(db, account_id, [_upsert_echo("ext-1", 150)], now_ms=200)

    with db.reader() as conn:
        rows = conn.execute(
            "SELECT id, deleted FROM main.message WHERE channel_id = ? AND ext_id = 'ext-1'", (channel_id,)
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][1] == 0


# 9. Эхо без токена: в окне 60с и с точным текстом — склеилось; за окном или с другим текстом —
#    отдельная строка.
def test_echo_without_token_matches_by_text_and_window(db):
    account_id = _insert_account(db)
    token_id = _insert_token(db)
    # Разные каналы на каждый под-сценарий — иначе FIFO по каналу (тест 4) блокирует вторую и третью строку,
    # пока первая висит в sending, и мешает проверять именно склейку эха.
    channel_in_window = _insert_channel(db, account_id, ext_id="c-in-window", my_msg_count=5, first_seen_ts=0)
    channel_outside = _insert_channel(db, account_id, ext_id="c-outside", my_msg_count=5, first_seen_ts=0)
    channel_other_text = _insert_channel(
        db, account_id, ext_id="c-other-text", my_msg_count=5, first_seen_ts=0
    )

    enq_in_window = _enqueue(
        db, channel_in_window, text="hi", api_token_id=token_id, idempotency_key="k1", now_ms=100
    )
    _lease(db, account_id, "w1", now_ms=100)
    _apply(
        db,
        account_id,
        [_upsert_echo("ext-in-window", 100 + 60_000, text="hi", channel_ext="c-in-window")],
        now_ms=100 + 60_000,
    )
    with db.reader() as conn:
        merged = conn.execute(
            "SELECT outbox_id FROM main.message WHERE channel_id = ? AND ext_id = 'ext-in-window'",
            (channel_in_window,),
        ).fetchone()
    assert merged[0] == enq_in_window.outbox_id
    assert _outbox_row(db, enq_in_window.outbox_id)["status"] == "sent"

    enq_outside = _enqueue(
        db, channel_outside, text="bye", api_token_id=token_id, idempotency_key="k2", now_ms=100
    )
    _lease(db, account_id, "w1", now_ms=100)
    _apply(
        db,
        account_id,
        [_upsert_echo("ext-outside-window", 100 + 60_001, text="bye", channel_ext="c-outside")],
        now_ms=100 + 60_001,
    )
    with db.reader() as conn:
        row = conn.execute(
            "SELECT outbox_id FROM main.message WHERE channel_id = ? AND ext_id = 'ext-outside-window'",
            (channel_outside,),
        ).fetchone()
    assert row[0] is None  # за окном — не склеилось
    assert _outbox_row(db, enq_outside.outbox_id)["status"] == "sending"

    enq_other_text = _enqueue(
        db, channel_other_text, text="third", api_token_id=token_id, idempotency_key="k3", now_ms=100
    )
    _lease(db, account_id, "w1", now_ms=100)
    _apply(
        db,
        account_id,
        [_upsert_echo("ext-different-text", 100 + 1000, text="не третье", channel_ext="c-other-text")],
        now_ms=100 + 1000,
    )
    with db.reader() as conn:
        row3 = conn.execute(
            "SELECT outbox_id FROM main.message WHERE channel_id = ? AND ext_id = 'ext-different-text'",
            (channel_other_text,),
        ).fetchone()
    assert row3[0] is None  # другой текст — не склеилось
    assert _outbox_row(db, enq_other_text.outbox_id)["status"] == "sending"


# 10. Эхо не склеилось (другой текст из-за форматирования источника), потом report(sent) с тем же ext_id:
#     заготовка deleted=1, настоящая строка получила outbox_id; видимая строка одна.
def test_report_sent_merges_unmatched_echo_afterwards(db):
    account_id = _insert_account(db)
    token_id = _insert_token(db)
    channel_id = _insert_channel(db, account_id, ext_id="c1", my_msg_count=5, first_seen_ts=0)
    enq = _enqueue(db, channel_id, text="hi", api_token_id=token_id, idempotency_key="k1", now_ms=100)
    _lease(db, account_id, "w1", now_ms=100)

    _apply(db, account_id, [_upsert_echo("ext-1", 100, text="hi (форматирование источника)")], now_ms=100)
    with db.reader() as conn:
        real_row = conn.execute(
            "SELECT id, outbox_id, deleted FROM main.message WHERE channel_id = ? AND ext_id = 'ext-1'",
            (channel_id,),
        ).fetchone()
    assert real_row[1] is None  # не склеилось при вставке
    assert real_row[2] == 0

    _report(db, enq.outbox_id, "w1", "sent", ext_id="ext-1", now_ms=200)

    with db.reader() as conn:
        placeholder_deleted = conn.execute(
            "SELECT deleted FROM main.message WHERE id = ?", (enq.message_id,)
        ).fetchone()[0]
        real_after = conn.execute(
            "SELECT outbox_id, deleted FROM main.message WHERE id = ?", (real_row[0],)
        ).fetchone()
        visible = conn.execute(
            "SELECT COUNT(*) FROM main.message WHERE channel_id = ? AND deleted = 0", (channel_id,)
        ).fetchone()[0]
    assert placeholder_deleted == 1
    assert real_after == (enq.outbox_id, 0)
    assert visible == 1


# 11. FLOOD: report(flood) ставит account.flood_until; lease пуст до срока, в т.ч. после close()/нового
#     Db.start(); после срока — выдаёт.
def test_flood_persists_across_restart(db):
    account_id = _insert_account(db)
    token_id = _insert_token(db)
    channel_id = _insert_channel(db, account_id, my_msg_count=5, first_seen_ts=0)
    enq = _enqueue(db, channel_id, api_token_id=token_id, idempotency_key="k1", now_ms=100)
    _lease(db, account_id, "w1", now_ms=100)
    _report(db, enq.outbox_id, "w1", "flood", retry_after_ms=10_000, now_ms=100)  # flood_until = 10100

    assert _lease(db, account_id, "w2", now_ms=10_050) == []

    data_dir = db.data_dir
    db.close()
    db2 = Db(data_dir)
    db2.start()
    try:
        assert _lease(db2, account_id, "w2", now_ms=10_050) == []
        after = _lease(db2, account_id, "w2", now_ms=10_101)
        assert len(after) == 1
        assert after[0].outbox_id == enq.outbox_id
    finally:
        db2.close()


# 12. temporary: бэкофф растёт, retry_after_ms имеет приоритет; после 5 попыток -> failed.
def test_temporary_backoff_grows_and_fails_after_five_attempts(db):
    account_id = _insert_account(db)
    token_id = _insert_token(db)
    channel_id = _insert_channel(db, account_id, my_msg_count=5, first_seen_ts=0)
    enq = _enqueue(db, channel_id, api_token_id=token_id, idempotency_key="k1", now_ms=0)

    now = 0
    owner = "w"
    not_befores = []
    for attempt in range(1, 6):
        leased = _lease(db, account_id, owner, now_ms=now)
        assert len(leased) == 1
        assert leased[0].attempts == attempt
        _report(db, enq.outbox_id, owner, "temporary", now_ms=now)
        row = _outbox_row(db, enq.outbox_id)
        if attempt < 5:
            assert row["status"] == "queued"
            not_befores.append(row["not_before_ts"])
            now = row["not_before_ts"]
        else:
            assert row["status"] == "failed"

    assert not_befores == [5000, 15000, 35000, 75000]  # 5с·2^(n-1) накопительно

    # retry_after_ms имеет приоритет над бэкоффом
    channel2 = _insert_channel(db, account_id, ext_id="c2", my_msg_count=5, first_seen_ts=0)
    enq2 = _enqueue(db, channel2, text="x", api_token_id=token_id, idempotency_key="k2", now_ms=0)
    _lease(db, account_id, owner, now_ms=0)
    _report(db, enq2.outbox_id, owner, "temporary", retry_after_ms=999, now_ms=0)
    assert _outbox_row(db, enq2.outbox_id)["not_before_ts"] == 999


# 13. Устаревший отчёт: чужой owner, либо отчёт старого владельца после перевыдачи лиза -> StaleLease,
#     состояние не изменилось.
def test_report_from_stale_owner_is_rejected(db):
    account_id = _insert_account(db)
    token_id = _insert_token(db)
    channel_id = _insert_channel(db, account_id, my_msg_count=5, first_seen_ts=0)
    enq = _enqueue(db, channel_id, api_token_id=token_id, idempotency_key="k1", now_ms=0)
    _lease(db, account_id, "w1", now_ms=0, lease_ms=1000)

    with pytest.raises(StaleLease):
        _report(db, enq.outbox_id, "someone-else", "sent", ext_id="e1", now_ms=100)
    assert _outbox_row(db, enq.outbox_id)["status"] == "sending"

    _lease(db, account_id, "w2", now_ms=1500)  # лиз истёк, перевыдан w2
    before = _outbox_row(db, enq.outbox_id)
    with pytest.raises(StaleLease):
        _report(db, enq.outbox_id, "w1", "sent", ext_id="e1", now_ms=1600)  # старый владелец
    assert _outbox_row(db, enq.outbox_id) == before


# 14. resolve_unknown: три решения; не из unknown -> InvalidTransition.
def test_resolve_unknown_three_decisions(db):
    account_id = _insert_account(db)
    token_id = _insert_token(db)

    def _make_unknown(suffix):
        channel_id = _insert_channel(db, account_id, ext_id=f"c-{suffix}", my_msg_count=5, first_seen_ts=0)
        enq = _enqueue(db, channel_id, api_token_id=token_id, idempotency_key=f"k-{suffix}", now_ms=0)
        _lease(db, account_id, "w1", now_ms=0)
        _report(db, enq.outbox_id, "w1", "unknown", now_ms=100)
        assert _outbox_row(db, enq.outbox_id)["status"] == "unknown"
        return enq

    enq_sent = _make_unknown("sent")
    _resolve_unknown(db, enq_sent.outbox_id, "sent", now_ms=200)
    assert _outbox_row(db, enq_sent.outbox_id)["status"] == "sent"

    enq_ns = _make_unknown("not-sent")
    _resolve_unknown(db, enq_ns.outbox_id, "not_sent", now_ms=200)
    assert _outbox_row(db, enq_ns.outbox_id)["status"] == "failed"

    enq_retry = _make_unknown("retry")
    _resolve_unknown(db, enq_retry.outbox_id, "retry", now_ms=200)
    assert _outbox_row(db, enq_retry.outbox_id)["status"] == "queued"

    with pytest.raises(InvalidTransition):
        _resolve_unknown(db, enq_retry.outbox_id, "sent", now_ms=300)  # уже не unknown


# 15. Канал в work: все строки в work; в main.audit есть записи переходов, и ни в одной нет текста сообщения.
def test_work_channel_isolated_and_audit_has_no_text(db):
    account_id = _insert_account(db, db_name="work")
    token_id = _insert_token(db)
    channel_id = _insert_channel(db, account_id, my_msg_count=5, first_seen_ts=0, db_name="work")
    assert channel_id >= WORK_ID_BASE

    secret_text = "секретный корпоративный текст"
    enq = _enqueue(db, channel_id, text=secret_text, api_token_id=token_id, idempotency_key="k1", now_ms=100)
    assert enq.outbox_id >= WORK_ID_BASE
    _lease(db, account_id, "w1", now_ms=100)
    _report(db, enq.outbox_id, "w1", "sent", ext_id="ext-1", now_ms=200)

    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.outbox").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM main.message").fetchone()[0] == 0
        rows = conn.execute(
            "SELECT action, result, detail_json FROM main.audit WHERE outbox_id = ?", (enq.outbox_id,)
        ).fetchall()
    assert len(rows) >= 3  # enqueue, lease, report
    for action, result, detail_json in rows:
        haystack = f"{action} {result} {detail_json or ''}"
        assert secret_text not in haystack


# 16. Каждый переход статуса поднимает message.seq заготовки.
def test_every_transition_bumps_placeholder_seq(db):
    account_id = _insert_account(db)
    token_id = _insert_token(db)
    channel_id = _insert_channel(db, account_id, my_msg_count=0, first_seen_ts=0)  # untrusted

    enq = _enqueue(db, channel_id, origin="api", api_token_id=token_id, idempotency_key="k1", now_ms=100)
    assert enq.status == "pending_approval"

    def _seq():
        with db.reader() as conn:
            return conn.execute("SELECT seq FROM main.message WHERE id = ?", (enq.message_id,)).fetchone()[0]

    seqs = [_seq()]

    _approve(db, enq.outbox_id, now_ms=100)  # -> queued
    seqs.append(_seq())

    _lease(db, account_id, "w1", now_ms=100)  # -> sending
    seqs.append(_seq())

    _report(db, enq.outbox_id, "w1", "temporary", now_ms=100)  # -> queued (backoff)
    seqs.append(_seq())

    not_before = _outbox_row(db, enq.outbox_id)["not_before_ts"]
    _lease(db, account_id, "w1", now_ms=not_before)  # -> sending
    seqs.append(_seq())

    _report(db, enq.outbox_id, "w1", "permanent", error="oops", now_ms=not_before)  # -> failed
    seqs.append(_seq())

    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


# 17. Скачок часов на 8 часов при строке в sending: лиз считается истёкшим (настенные часы), дальше — по
#     п.5/6; отложенные not_before_ts становятся выдаваемыми.
def test_wall_clock_jump_expires_lease_and_frees_delayed_rows(db):
    account_id = _insert_account(db, idempotent=True)
    token_id = _insert_token(db)
    channel_id = _insert_channel(db, account_id, ext_id="c1", my_msg_count=5, first_seen_ts=0)
    # Отдельный канал для отложенной строки — иначе FIFO по каналу (тест 4) заблокировал бы её всё равно
    # висящей в sending строкой enq, и скачок часов проверял бы не то, что нужно.
    delayed_channel_id = _insert_channel(db, account_id, ext_id="c2", my_msg_count=5, first_seen_ts=0)

    enq = _enqueue(db, channel_id, api_token_id=token_id, idempotency_key="k1", now_ms=0)
    first = _lease(db, account_id, "w1", now_ms=0, lease_ms=60_000)[0]
    assert first.attempts == 1

    delayed = _enqueue(
        db, delayed_channel_id, text="delayed", api_token_id=token_id, idempotency_key="k2", now_ms=0
    )
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.outbox SET not_before_ts = ? WHERE id = ?", (5 * 60 * 60 * 1000, delayed.outbox_id)
        )
    )

    sleep_jump_ms = 8 * 60 * 60 * 1000  # «сон ноутбука» — скачок настенных часов на 8 часов
    leases = _lease(db, account_id, "w1", now_ms=sleep_jump_ms)
    by_id = {leased.outbox_id: leased for leased in leases}

    assert by_id[enq.outbox_id].mode == "send"  # лиз (60с) истёк по настенным часам — идемпотентный повтор
    assert by_id[enq.outbox_id].attempts == 2
    assert delayed.outbox_id in by_id  # отложенный not_before_ts тоже уже настал


# Поколение лиза: коннектор переживает рестарт с тем же owner, поэтому опоздавший отчёт старого лиза
# отличим только по lease_gen.
def test_report_with_stale_lease_gen_is_rejected(db):
    account_id = _insert_account(db)
    token_id = _insert_token(db)
    channel_id = _insert_channel(db, account_id)
    enq = _enqueue(db, channel_id, api_token_id=token_id, now_ms=0)

    (first,) = _lease(db, account_id, "hub-fake", now_ms=0, lease_ms=1000)
    (second,) = _lease(db, account_id, "hub-fake", now_ms=5000, lease_ms=1000)  # лиз истёк, тот же owner
    assert second.outbox_id == first.outbox_id == enq.outbox_id
    assert second.lease_gen == first.lease_gen + 1

    with pytest.raises(StaleLease):
        _report(db, enq.outbox_id, "hub-fake", "unknown", lease_gen=first.lease_gen, now_ms=5100)
    assert _outbox_row(db, enq.outbox_id)["status"] == "sending"

    _report(db, enq.outbox_id, "hub-fake", "unknown", lease_gen=second.lease_gen, now_ms=5200)
    assert _outbox_row(db, enq.outbox_id)["status"] == "unknown"


# Коннектор отчитался temporary, а сообщение на деле ушло: эхо с send_token обязано погасить строку в queued,
# иначе она будет отправлена второй раз.
def test_echo_with_token_stops_requeued_row_from_double_send(db):
    account_id = _insert_account(db)
    token_id = _insert_token(db)
    channel_id = _insert_channel(db, account_id)
    enq = _enqueue(db, channel_id, api_token_id=token_id, text="важное", now_ms=0)

    (lz,) = _lease(db, account_id, "w1", now_ms=0)
    _report(db, enq.outbox_id, "w1", "temporary", now_ms=100)
    assert _outbox_row(db, enq.outbox_id)["status"] == "queued"

    _apply(db, account_id, [_upsert_echo("ext-1", 150, text="важное", send_token=lz.send_token)], now_ms=200)

    assert _outbox_row(db, enq.outbox_id)["status"] == "sent"
    assert _lease(db, account_id, "w1", now_ms=10_000_000) == []
    with db.reader() as conn:
        visible = conn.execute(
            "SELECT COUNT(*) FROM main.message WHERE channel_id = ? AND deleted = 0", (channel_id,)
        ).fetchone()[0]
    assert visible == 1
    assert _my_msg_count(db, channel_id) == 2  # было 1 у канала из помощника, выросло ровно на одну отправку


# FIFO: более ранняя строка канала ждёт бэкоффа в queued — поздняя не должна её обогнать.
def test_fifo_holds_behind_earlier_row_in_backoff(db):
    account_id = _insert_account(db)
    token_id = _insert_token(db)
    channel_id = _insert_channel(db, account_id)
    first = _enqueue(db, channel_id, api_token_id=token_id, text="первое", idempotency_key="k1", now_ms=0)
    second = _enqueue(db, channel_id, api_token_id=token_id, text="второе", idempotency_key="k2", now_ms=0)

    (lz,) = _lease(db, account_id, "w1", now_ms=0)
    assert lz.outbox_id == first.outbox_id
    _report(db, first.outbox_id, "w1", "temporary", now_ms=100)  # бэкофф 5 с

    assert _lease(db, account_id, "w1", now_ms=1000) == []  # второе не обгоняет первое
    (again,) = _lease(db, account_id, "w1", now_ms=6000)
    assert again.outbox_id == first.outbox_id
    assert second.outbox_id not in {x.outbox_id for x in _lease(db, account_id, "w1", now_ms=6001)}


# Идемпотентный аккаунт: повтор после истёкшего лиза — тоже отправка, во время FLOOD_WAIT запрещена.
def test_stale_idempotent_relase_respects_flood(db):
    account_id = _insert_account(db, idempotent=True)
    token_id = _insert_token(db)
    channel_id = _insert_channel(db, account_id)
    other_channel = _insert_channel(db, account_id, ext_id="c2")
    stuck = _enqueue(db, channel_id, api_token_id=token_id, idempotency_key="k1", now_ms=0)
    flooded = _enqueue(db, other_channel, api_token_id=token_id, idempotency_key="k2", now_ms=0)

    leased = _lease(db, account_id, "w1", now_ms=0, lease_ms=1000)
    assert {x.outbox_id for x in leased} == {stuck.outbox_id, flooded.outbox_id}
    _report(db, flooded.outbox_id, "w1", "flood", retry_after_ms=60_000, now_ms=100)

    assert _lease(db, account_id, "w1", now_ms=5000) == []  # лиз stuck истёк, но идёт FLOOD_WAIT
    assert stuck.outbox_id in {x.outbox_id for x in _lease(db, account_id, "w1", now_ms=61_000)}


# --- Находки opus-критика (19.09): каждая была воспроизведена скриптом до исправления ---


def _visible(db, channel_id):
    with db.reader() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM main.message WHERE channel_id = ? AND deleted = 0", (channel_id,)
        ).fetchone()[0]


# Эхо БЕЗ токена (коннектор потерял карту токенов при рестарте) к строке, вернувшейся в queued: клеится,
# иначе две видимые строки и повторная отправка.
@pytest.mark.parametrize("outcome", ["temporary", "flood", "permanent"])
def test_echo_without_token_stops_double_send(db, outcome):
    account_id = _insert_account(db)
    token_id = _insert_token(db)
    channel_id = _insert_channel(db, account_id)
    enq = _enqueue(db, channel_id, api_token_id=token_id, text="важное", now_ms=0)

    _lease(db, account_id, "w1", now_ms=0)
    extra = {"retry_after_ms": 1000} if outcome == "flood" else {}
    _report(db, enq.outbox_id, "w1", outcome, now_ms=100, **extra)

    _apply(db, account_id, [_upsert_echo("ext-1", 150, text="важное")], now_ms=200)

    assert _outbox_row(db, enq.outbox_id)["status"] == "sent"
    assert _visible(db, channel_id) == 1
    assert _lease(db, account_id, "w1", now_ms=10_000_000) == []


# Строку ни разу не выдавали коннектору — чужое сообщение с тем же текстом не может быть её эхом.
def test_echo_never_glues_to_row_that_was_never_leased(db):
    account_id = _insert_account(db)
    token_id = _insert_token(db)
    channel_id = _insert_channel(db, account_id)
    enq = _enqueue(db, channel_id, api_token_id=token_id, text="ок", now_ms=0)

    _apply(db, account_id, [_upsert_echo("ext-phone", 10, text="ок")], now_ms=20)  # написал то же с телефона

    assert _outbox_row(db, enq.outbox_id)["status"] == "queued"
    assert _visible(db, channel_id) == 2
    assert [x.outbox_id for x in _lease(db, account_id, "w1", now_ms=30)] == [enq.outbox_id]


# FLOOD_WAIT не тратит бюджет попыток; а достоверный sent после исчерпания (строка уже unknown) принимается.
def test_flood_keeps_attempts_and_late_sent_is_accepted_after_exhaustion(db):
    account_id = _insert_account(db)
    token_id = _insert_token(db)
    channel_id = _insert_channel(db, account_id)
    enq = _enqueue(db, channel_id, api_token_id=token_id, now_ms=0)

    now = 0
    for _ in range(8):
        (lz,) = _lease(db, account_id, "w1", now_ms=now)
        assert lz.attempts == 1  # сколько бы FLOOD ни было, попытка остаётся первой
        _report(db, enq.outbox_id, "w1", "flood", retry_after_ms=10, now_ms=now)
        now += 100

    # загоняем в исчерпание: лизы протухают без отчёта
    last = None
    for _ in range(10):
        got = _lease(db, account_id, "w1", now_ms=now, lease_ms=10)
        now += 1000
        if not got:
            break
        last = got[0]
    assert _outbox_row(db, enq.outbox_id)["status"] == "unknown"

    _report(db, enq.outbox_id, "w1", "sent", ext_id="ext-1", lease_gen=last.lease_gen, now_ms=now)
    row = _outbox_row(db, enq.outbox_id)
    assert row["status"] == "sent" and row["ext_id"] == "ext-1"
    assert _visible(db, channel_id) == 1


# unknown держит очередь канала: иначе при retry собеседник увидит сообщения в обратном порядке.
def test_unknown_blocks_channel_fifo(db):
    account_id = _insert_account(db)
    token_id = _insert_token(db)
    channel_id = _insert_channel(db, account_id)
    first = _enqueue(db, channel_id, api_token_id=token_id, text="1", idempotency_key="k1", now_ms=0)
    second = _enqueue(db, channel_id, api_token_id=token_id, text="2", idempotency_key="k2", now_ms=0)

    _lease(db, account_id, "w1", now_ms=0)
    _report(db, first.outbox_id, "w1", "unknown", now_ms=100)
    assert _lease(db, account_id, "w1", now_ms=200) == []

    _resolve_unknown(db, first.outbox_id, "not_sent", now_ms=300)
    assert [x.outbox_id for x in _lease(db, account_id, "w1", now_ms=400)] == [second.outbox_id]


# Инвариант «один outbox — одна строка message»: погашенная заготовка отдаёт outbox_id настоящей строке.
def test_one_message_row_per_outbox_after_unmatched_echo(db):
    account_id = _insert_account(db)
    token_id = _insert_token(db)
    channel_id = _insert_channel(db, account_id)
    enq = _enqueue(db, channel_id, api_token_id=token_id, text="*жирный*", idempotency_key="k1", now_ms=0)
    _lease(db, account_id, "w1", now_ms=0)
    _apply(db, account_id, [_upsert_echo("ext-1", 50, text="жирный")], now_ms=60)  # источник переформатировал
    _report(db, enq.outbox_id, "w1", "sent", ext_id="ext-1", now_ms=100)

    with db.reader() as conn:
        rows = conn.execute(
            "SELECT ext_id, deleted FROM main.message WHERE outbox_id = ?", (enq.outbox_id,)
        ).fetchall()
    assert rows == [("ext-1", 0)]
    again = _enqueue(db, channel_id, api_token_id=token_id, text="*жирный*", idempotency_key="k1", now_ms=200)
    assert again.created is False
    with db.reader() as conn:
        assert conn.execute(
            "SELECT deleted FROM main.message WHERE id = ?", (again.message_id,)
        ).fetchone() == (0,)


# cancel строки, которую уже пытались отправить, не врёт «отменено»: повторы стоп, статус unknown.
def test_cancel_after_attempt_becomes_unknown(db):
    account_id = _insert_account(db)
    token_id = _insert_token(db)
    channel_id = _insert_channel(db, account_id)
    enq = _enqueue(db, channel_id, api_token_id=token_id, now_ms=0)
    _lease(db, account_id, "w1", now_ms=0)
    _report(db, enq.outbox_id, "w1", "temporary", now_ms=100)

    assert _cancel(db, enq.outbox_id, now_ms=200) == "unknown"
    assert _outbox_row(db, enq.outbox_id)["status"] == "unknown"
    assert _visible(db, channel_id) == 1
    assert _lease(db, account_id, "w1", now_ms=10_000_000) == []


# has_leasable (long-poll коннектора) обязан будить и ради строки с исчерпанными попытками: выдавать её
# lease() не станет, но именно он переводит её в unknown. Иначе коннектор, ждущий на /outbox/wait, lease() не
# позовёт, и строка молча застрянет в sending навсегда.
def test_has_leasable_wakes_for_exhausted_row(db):
    from hub.core.outbox_queries import has_leasable

    account_id = _insert_account(db)
    token_id = _insert_token(db)
    channel_id = _insert_channel(db, account_id)
    enq = _enqueue(db, channel_id, api_token_id=token_id, now_ms=0)

    now = 0
    while True:  # лизы протухают без отчёта, пока попытки не кончатся
        assert db.write_sync(lambda conn, now=now: has_leasable(conn, account_id=account_id, now_ms=now))
        if not _lease(db, account_id, "w1", now_ms=now, lease_ms=10):
            break
        now += 1000
    assert _outbox_row(db, enq.outbox_id)["status"] == "unknown"
    assert not db.write_sync(lambda conn: has_leasable(conn, account_id=account_id, now_ms=now + 1000))


# Поправки после opus-критика кода 2c+2d (`docs/architecture.md`): `known_own_ext_ids`
# у НЕидемпотентного аккаунта (Slack) — только свои сообщения, уже привязанные к outbox; эхо, не склеившееся
# с заготовкой из-за другого текста (`:+1:` против 👍), остаётся кандидатом поиска. Идемпотентный (Telegram) —
# как раньше: все свои сообщения окна.
@pytest.mark.parametrize("idempotent", [False, True])
def test_known_own_ext_ids_non_idempotent_only_bound_to_outbox(db, idempotent):
    account_id = _insert_account(db, idempotent=idempotent)
    token_id = _insert_token(db)
    channel_id = _insert_channel(db, account_id, ext_id="c1", my_msg_count=5, first_seen_ts=0)

    a = _enqueue(db, channel_id, text="A", api_token_id=token_id, idempotency_key="ka", now_ms=100)
    _lease(db, account_id, "w1", now_ms=100)
    _report(db, a.outbox_id, "w1", "sent", ext_id="e-A", now_ms=150)
    # своё сообщение с «другим» текстом, не склеенное ни с одной заготовкой (outbox_id IS NULL)
    _apply(db, account_id, [_upsert_echo("e-X", 160, text="готово 👍")], now_ms=160)

    b = _enqueue(db, channel_id, text="готово :+1:", api_token_id=token_id, idempotency_key="kb", now_ms=200)
    fresh = _lease(db, account_id, "w1", now_ms=200, lease_ms=1000)
    assert [x.outbox_id for x in fresh] == [b.outbox_id]
    expected = {"e-A", "e-X"} if idempotent else {"e-A"}
    assert set(fresh[0].known_own_ext_ids) == expected

    # ветка истёкшего лиза (reconcile у неидемпотентного, повторная отправка у идемпотентного) — то же правило
    stale = _lease(db, account_id, "w2", now_ms=5_000)
    assert [x.outbox_id for x in stale] == [b.outbox_id]
    assert stale[0].mode == ("send" if idempotent else "reconcile")
    assert set(stale[0].known_own_ext_ids) == expected
