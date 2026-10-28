"""Ф14-7 A1/A3 (`docs/architecture.md`): моё сообщение в ОСНОВНОЙ ленте лички
читает ответы в тредах до себя; в каналах — нет."""

from hub.core import agent_items, attention
from hub.core import outbox as outbox_mod
from hub.core.ingest import apply_batch, unread_breakdown


def _insert_account(db):
    return db.write_sync(
        lambda conn: conn.execute("INSERT INTO main.account (source, db) VALUES ('slack', 'work')").lastrowid
    )


def _insert_token(db):
    return db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.api_token (name, kind, hash) VALUES ('tok', 'ui', 'x')").lastrowid
        )
    )


def _upsert(ext_id, ts, *, channel_ext="c1", channel_kind="channel", text="hi", is_me=False,
            thread_ext_id=None, **extra):  # fmt: skip
    event = {
        "type": "upsert",
        "channel": {"ext_id": channel_ext, "kind": channel_kind},
        "ext_id": ext_id,
        "ts": ts,
        "is_me": is_me,
        "kind": "text",
        "text": text,
        "mentions_me": False,
        "thread_ext_id": thread_ext_id,
        "thread_broadcast": False,
    }
    event.update(extra)
    return event


def _apply(db, account_id, events, now_ms=1000):
    return db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=events, cursor=None, now_ms=now_ms)
    )


def _dm(ext_id, ts, **kw):
    return _upsert(ext_id, ts, channel_ext="d1", channel_kind="dm", **kw)


def _channel_id(db, ext="d1"):
    with db.reader() as conn:
        return conn.execute("SELECT id FROM work.channel WHERE ext_id = ?", (ext,)).fetchone()[0]


def _marker(db, ext="d1", parent="P1"):
    with db.reader() as conn:
        return conn.execute(
            "SELECT t.last_read_ext FROM work.thread t JOIN work.channel c ON c.id = t.channel_id "
            "WHERE c.ext_id = ? AND t.ext_id = ?",
            (ext, parent),
        ).fetchone()[0]


def _unread(db, ext="d1"):
    cid = _channel_id(db, ext)
    with db.reader() as conn:
        return unread_breakdown(conn, cid)


def _unread_react(db, ext="d1"):
    cid = _channel_id(db, ext)
    with db.reader() as conn:
        return attention.unread_react_counts(conn, "work", where="c.id = ?", params=[cid]).get(cid, 0)


def _parent_seq(db, ext="d1", parent="P1"):
    with db.reader() as conn:
        return conn.execute(
            "SELECT m.seq FROM work.message m JOIN work.channel c ON c.id = m.channel_id "
            "WHERE c.ext_id = ? AND m.ext_id = ?",
            (ext, parent),
        ).fetchone()[0]


def _setup_dm_with_thread_reply(db, *, channel_kind="dm", attrs=None, ext="d1"):
    """Мой родитель P1 в основной ленте, чужой ответ R1 в его тред — тред подписан, ответ непрочитан."""
    account_id = _insert_account(db)
    channel = {"ext_id": ext, "kind": channel_kind}
    if attrs is not None:
        channel["attrs"] = attrs
    events = [
        _upsert("P1", 1000, is_me=True, text="вопрос"),
        _upsert("R1", 2000, thread_ext_id="P1", text="ответ в треде"),
    ]
    for ev in events:
        ev["channel"] = dict(channel)
    _apply(db, account_id, events)
    return account_id


def test_dm_my_main_message_reads_thread_replies(db):
    account_id = _setup_dm_with_thread_reply(db)
    assert _unread(db) == (0, 1)
    assert _unread_react(db) == 1
    seq_before = _parent_seq(db)

    _apply(db, account_id, [_dm("M1", 3000, is_me=True, text="ответ в ленте")])

    assert _marker(db) == "R1"
    assert _unread(db) == (0, 0)
    assert _unread_react(db) == 0
    assert _parent_seq(db) > seq_before  # сводка треда бампнута — /sync увидит


def test_mpim_my_main_message_reads_thread_replies(db):
    account_id = _setup_dm_with_thread_reply(db, channel_kind="group", attrs={"slack_type": "mpim"})
    assert _unread(db) == (0, 1)
    ev = _upsert("M1", 3000, channel_ext="d1", channel_kind="group", is_me=True)
    ev["channel"]["attrs"] = {"slack_type": "mpim"}
    _apply(db, account_id, [ev])
    assert _unread(db) == (0, 0)


def test_group_channel_my_main_message_does_not_read_threads(db):
    account_id = _setup_dm_with_thread_reply(db, channel_kind="group", attrs={"slack_type": "public"})
    ev = _upsert("M1", 3000, channel_ext="d1", channel_kind="group", is_me=True)
    ev["channel"]["attrs"] = {"slack_type": "public"}
    _apply(db, account_id, [ev])
    assert _marker(db) is None
    assert _unread(db) == (0, 1)


def test_reply_after_my_message_stays_unread(db):
    account_id = _setup_dm_with_thread_reply(db)
    _apply(db, account_id, [_dm("M1", 3000, is_me=True), _dm("R2", 4000, thread_ext_id="P1")])
    assert _marker(db) == "R1"
    assert _unread(db) == (0, 1)


def test_reconcile_old_my_message_moves_only_up_to_its_position(db):
    account_id = _setup_dm_with_thread_reply(db)
    _apply(db, account_id, [_dm("R2", 4000, thread_ext_id="P1")])
    assert _unread(db) == (0, 2)
    # Сверка догнала моё старое сообщение между R1 и R2.
    _apply(db, account_id, [_dm("M_OLD", 3000, is_me=True)])
    assert _marker(db) == "R1"
    assert _unread(db) == (0, 1)


def test_other_person_main_message_does_not_read_threads(db):
    account_id = _setup_dm_with_thread_reply(db)
    _apply(db, account_id, [_dm("M1", 3000)])
    assert _marker(db) is None


def test_my_thread_reply_elsewhere_and_edit_do_not_read_other_threads(db):
    account_id = _setup_dm_with_thread_reply(db)
    # Второй тред с моим родителем P2 и чужим ответом; мой ответ в тред P2 читает только P2.
    _apply(
        db,
        account_id,
        [
            _dm("P2", 1500, is_me=True),
            _dm("R3", 2600, thread_ext_id="P2"),
            _dm("R_ME", 2700, thread_ext_id="P2", is_me=True),
        ],
    )
    assert _marker(db, parent="P1") is None
    assert _marker(db, parent="P2") == "R_ME"
    assert _unread(db) == (0, 1)


def test_edit_and_repeat_of_my_message_do_not_read(db):
    """Чужой ответ догнал сверкой ПОЗЖЕ моего сообщения ленты (по времени — раньше него): правка и повтор
    моего сообщения — не вставка, маркер треда не трогают."""
    account_id = _insert_account(db)
    _apply(db, account_id, [_dm("P1", 1000, is_me=True), _dm("M1", 3000, is_me=True, text="лента")])
    _apply(db, account_id, [_dm("R1", 2000, thread_ext_id="P1")])
    assert _unread(db) == (0, 1)
    _apply(db, account_id, [_dm("M1", 3000, is_me=True, text="лента")])
    _apply(db, account_id, [_dm("M1", 3000, is_me=True, text="правка", edited_ts=5000)])
    assert _marker(db) is None
    assert _unread(db) == (0, 1)


def _enqueue_main(db, channel_id, token_id, key="k1"):
    return db.write_sync(
        lambda conn: outbox_mod.enqueue(
            conn,
            channel_id=channel_id,
            text="моя реплика",
            origin="ui",
            api_token_id=token_id,
            idempotency_key=key,
            now_ms=3000,
        )
    )


def test_placeholder_does_not_read_but_echo_does(db):
    account_id = _setup_dm_with_thread_reply(db)
    token_id = _insert_token(db)
    enq = _enqueue_main(db, _channel_id(db), token_id)
    assert enq.status == "queued"
    assert _marker(db) is None
    assert _unread(db) == (0, 1)
    leases = db.write_sync(lambda conn: outbox_mod.lease(conn, account_id=account_id, owner="c", now_ms=3100))
    assert len(leases) == 1
    assert _marker(db) is None  # выдача коннектору — тоже не вставка

    echo = _dm("M_ME", 3000, is_me=True, text="моя реплика", send_token=None)
    result = _apply(db, account_id, [echo], now_ms=3000)
    assert result.applied == 1
    with db.reader() as conn:
        assert (
            conn.execute("SELECT ext_id FROM work.message WHERE id = ?", (enq.message_id,)).fetchone()[0]
            == "M_ME"
        )  # эхо склеилось с заготовкой, не отдельная вставка
    assert _marker(db) == "R1"
    assert _unread(db) == (0, 0)


def test_report_before_echo_reads_threads(db):
    account_id = _setup_dm_with_thread_reply(db)
    token_id = _insert_token(db)
    enq = _enqueue_main(db, _channel_id(db), token_id)
    leases = db.write_sync(lambda conn: outbox_mod.lease(conn, account_id=account_id, owner="c", now_ms=3100))
    assert len(leases) == 1
    db.write_sync(
        lambda conn: outbox_mod.report(
            conn,
            outbox_id=enq.outbox_id,
            owner="c",
            lease_gen=leases[0].lease_gen,
            outcome="sent",
            ext_id="M_ME",
            now_ms=3200,
        )
    )
    assert _marker(db) == "R1"
    assert _unread(db) == (0, 0)


def test_triage_card_on_thread_reply_closes_after_my_main_message(db):
    account_id = _setup_dm_with_thread_reply(db)
    cid = _channel_id(db)
    with db.reader() as conn:
        r1 = conn.execute("SELECT id FROM work.message WHERE ext_id = 'R1'").fetchone()[0]

    def _card(conn, ids):
        return agent_items.upsert(
            conn,
            channel_id=cid,
            message_ids=ids,
            label="reply",
            summary="s",
            action=None,
            provider="rules",
            model="-",
            api_token_id=None,
            now_ms=2500,
        )

    card = db.write_sync(lambda conn: _card(conn, [r1]))
    with db.reader() as conn:
        assert agent_items.card_state(conn, "work", card.item_id) == (False, False)

    db.write_sync(
        lambda conn: apply_batch(
            conn, account_id=account_id, events=[_dm("M1", 3000, is_me=True)], cursor=None, now_ms=3000
        )
    )
    with db.reader() as conn:
        assert agent_items.card_state(conn, "work", card.item_id) == (True, True)
