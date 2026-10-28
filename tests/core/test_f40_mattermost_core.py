"""Ф40-1a (`docs/architecture.md`–D): ядро под источник `mattermost`.

A — время своей строки из отчёта об отправке (`ts_ms`); B — «прочитано до времени» (`ReadEvent.up_to_ts`,
`ThreadInfo.last_read_ts`), правило «своя отправка из хаба — не прочтение», порядок по времени при
`ext_sort = 0`; C — родитель треда формата Mattermost в сигнале подписки; D — источник в списках модулей
(правка своих, реакции, отметка прочитанного, медиа). Файлы в отправке (D, `uploads`) и альбом (A п. 2) —
`tests/api/test_f40_mattermost_api.py`.
"""

import json

import pytest

from hub import sub_wait
from hub.core import chan_subs, extsort, media, msg_ops, reactions, read_ack
from hub.core import outbox as outbox_mod
from hub.core.ingest import apply_batch, unread_breakdown
from hub.core.threads import message_unread_sql

CH = {"ext_id": "c1", "kind": "channel"}


def pid(tag: str) -> str:
    """Идентификатор поста Mattermost: 26 символов `[a-z0-9]`, порядка не несёт (`ext_sort` = 0)."""
    value = (tag + "x" * 26)[:26]
    assert extsort.ext_sort(value) == 0
    return value


def _account(db, *, source="mattermost", db_name="work"):
    return db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.account (source, db) VALUES (?, ?)", (source, db_name)).lastrowid
        )
    )


def _token(db):
    return db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.api_token (name, kind, hash) VALUES ('tok', 'ui', 'x')").lastrowid
        )
    )


def _upsert(ext_id, ts, *, is_me=False, text="hi", thread_ext_id=None, thread=None, **extra):
    event = {
        "type": "upsert",
        "channel": CH,
        "ext_id": ext_id,
        "ts": ts,
        "is_me": is_me,
        "kind": "text",
        "text": text,
        "mentions_me": False,
        "thread_ext_id": thread_ext_id,
    }
    if thread is not None:
        event["thread"] = thread
    event.update(extra)
    return event


def _read(up_to_ts=None, *, thread=None, **extra):
    event = {"type": "read", "channel": CH}
    if up_to_ts is not None:
        event["up_to_ts"] = up_to_ts
    if thread is not None:
        event["thread_ext_id"] = thread
    event.update(extra)
    return event


def _apply(db, account_id, events, now_ms=10_000):
    return db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=events, cursor=None, now_ms=now_ms)
    )


def _channel_id(db, schema="work"):
    with db.reader() as conn:
        return conn.execute(f"SELECT id FROM {schema}.channel WHERE ext_id = 'c1'").fetchone()[0]


def _marker(db):
    with db.reader() as conn:
        return conn.execute(
            "SELECT last_read_ts, last_read_sort, last_read_ext FROM work.channel WHERE ext_id = 'c1'"
        ).fetchone()


def _thread_marker(db, thread_ext):
    with db.reader() as conn:
        return conn.execute(
            "SELECT last_read_ts, last_read_sort, last_read_ext FROM work.thread WHERE ext_id = ?",
            (thread_ext,),
        ).fetchone()


def _unread(db):
    with db.reader() as conn:
        return unread_breakdown(conn, _channel_id(db))


def _msg_unread(db, ext_id):
    with db.reader() as conn:
        return conn.execute(
            f"SELECT {message_unread_sql('m', 'ch', 't')} FROM work.message m "
            "JOIN work.channel ch ON ch.id = m.channel_id "
            "LEFT JOIN work.thread t ON t.channel_id = m.channel_id AND t.ext_id = m.thread_ext_id "
            "WHERE m.ext_id = ?",
            (ext_id,),
        ).fetchone()[0]


def _msg(db, message_id):
    with db.reader() as conn:
        cur = conn.execute("SELECT * FROM work.message WHERE id = ?", (message_id,))
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, cur.fetchone(), strict=True))


def _msg_id(db, ext_id):
    with db.reader() as conn:
        return conn.execute("SELECT id FROM work.message WHERE ext_id = ?", (ext_id,)).fetchone()[0]


def _enqueue_and_lease(db, account_id, token_id, *, text="из хаба", thread=None, key="k1", now_ms=5_000):
    channel_id = _channel_id(db)
    enq = db.write_sync(
        lambda conn: outbox_mod.enqueue(
            conn,
            channel_id=channel_id,
            text=text,
            thread_ext_id=thread,
            origin="ui",
            api_token_id=token_id,
            idempotency_key=key,
            now_ms=now_ms,
        )
    )
    (lease,) = db.write_sync(
        lambda conn: outbox_mod.lease(conn, account_id=account_id, owner="conn-1", now_ms=now_ms + 100)
    )
    return enq, lease


def _report_sent(db, enq, lease, ext_id, *, now_ms=5_200, **extra):
    db.write_sync(
        lambda conn: outbox_mod.report(
            conn,
            outbox_id=enq.outbox_id,
            owner="conn-1",
            lease_gen=lease.lease_gen,
            outcome="sent",
            ext_id=ext_id,
            now_ms=now_ms,
            **extra,
        )
    )


def _hub_send(db, account_id, token_id, ext_id, ts_ms, *, thread=None, key="k1"):
    """Своё сообщение, отправленное из хаба: заготовка -> лиз -> отчёт `sent` со временем источника."""
    enq, lease = _enqueue_and_lease(db, account_id, token_id, thread=thread, key=key)
    _report_sent(db, enq, lease, ext_id, ts_ms=ts_ms)
    row = _msg(db, enq.message_id)
    assert row["is_me"] == 1 and row["outbox_id"] is not None and row["ts"] == ts_ms
    return enq


# --- A. Время своей строки приходит с отчётом об отправке --------------------------------------------------


def test_report_before_echo_sets_ts_from_report(db):
    account_id, token_id = _account(db), _token(db)
    _apply(db, account_id, [_upsert(pid("m1"), 1_000)])
    enq, lease = _enqueue_and_lease(db, account_id, token_id)
    assert _msg(db, enq.message_id)["ts"] == 5_000  # часы хаба на момент черновика

    _report_sent(db, enq, lease, pid("own"), ts_ms=1_700_000_000_123)

    row = _msg(db, enq.message_id)
    assert row["ext_id"] == pid("own") and row["ts"] == 1_700_000_000_123
    with db.reader() as conn:
        last_msg_ts = conn.execute("SELECT last_msg_ts FROM work.channel WHERE ext_id = 'c1'").fetchone()[0]
    assert last_msg_ts == 1_700_000_000_123

    # Эхо с уже известным `ext_id` и тем же временем строку не двоит и время не портит.
    _apply(db, account_id, [_upsert(pid("own"), 1_700_000_000_123, is_me=True, text="из хаба")])
    assert _msg(db, enq.message_id)["ts"] == 1_700_000_000_123
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM work.message WHERE is_me = 1").fetchone()[0] == 1


def test_echo_before_report_keeps_echo_ts(db):
    account_id, token_id = _account(db), _token(db)
    _apply(db, account_id, [_upsert(pid("m1"), 1_000)])
    enq, lease = _enqueue_and_lease(db, account_id, token_id)

    _apply(db, account_id, [_upsert(pid("own"), 5_150, is_me=True, text="из хаба")], now_ms=5_160)
    row = _msg(db, enq.message_id)
    assert row["ext_id"] == pid("own") and row["ts"] == 5_150  # эхо склеилось и принесло своё время

    _report_sent(db, enq, lease, pid("own"), ts_ms=9_999)  # отчёт опоздал — время эха не портит
    assert _msg(db, enq.message_id)["ts"] == 5_150


def test_echo_as_separate_row_before_report_keeps_echo_ts(db):
    """Эхо не склеилось (другой текст): настоящая строка остаётся со временем эха, заготовка гаснет."""
    account_id, token_id = _account(db), _token(db)
    _apply(db, account_id, [_upsert(pid("m1"), 1_000)])
    enq, lease = _enqueue_and_lease(db, account_id, token_id)
    _apply(db, account_id, [_upsert(pid("own"), 5_150, is_me=True, text="другой текст")], now_ms=5_160)
    real_id = _msg_id(db, pid("own"))
    assert real_id != enq.message_id

    _report_sent(db, enq, lease, pid("own"), ts_ms=9_999)

    real = _msg(db, real_id)
    assert real["ts"] == 5_150 and real["outbox_id"] == enq.outbox_id
    assert _msg(db, enq.message_id)["deleted"] == 1


def test_report_without_ts_ms_keeps_hub_clock(db):
    account_id, token_id = _account(db), _token(db)
    _apply(db, account_id, [_upsert(pid("m1"), 1_000)])
    enq, lease = _enqueue_and_lease(db, account_id, token_id)
    _report_sent(db, enq, lease, pid("own"))
    row = _msg(db, enq.message_id)
    assert row["ext_id"] == pid("own") and row["ts"] == 5_000  # как раньше


@pytest.mark.parametrize("bad", ["1700000000123", -5, 0, True, 2**60, 1.5, [1]])
def test_report_with_bad_ts_ms_is_accepted_and_ignored(db, bad):
    account_id, token_id = _account(db), _token(db)
    _apply(db, account_id, [_upsert(pid("m1"), 1_000)])
    enq, lease = _enqueue_and_lease(db, account_id, token_id)
    _report_sent(db, enq, lease, pid("own"), ts_ms=bad)
    row = _msg(db, enq.message_id)
    assert row["ext_id"] == pid("own") and row["ts"] == 5_000


def test_slack_report_time_still_comes_from_ext_id(db):
    account_id, token_id = _account(db, source="slack"), _token(db)
    _apply(db, account_id, [_upsert("1699999999.000100", 1_699_999_999_000)])
    enq, lease = _enqueue_and_lease(db, account_id, token_id)
    _report_sent(db, enq, lease, "1700000000.123456", ts_ms=42_000)  # пришло и то и другое — из `ext_id`
    assert _msg(db, enq.message_id)["ts"] == 1_700_000_000_123

    enq2, lease2 = _enqueue_and_lease(db, account_id, token_id, key="k2", now_ms=6_000)
    _report_sent(db, enq2, lease2, "1700000001.000000", now_ms=6_200)  # без `ts_ms` — как раньше
    assert _msg(db, enq2.message_id)["ts"] == 1_700_000_001_000


# --- B. «Прочитано до времени»: событие `read` -------------------------------------------------------------


def _three(db):
    account_id = _account(db)
    _apply(
        db,
        account_id,
        [_upsert(pid("m1"), 1_000), _upsert(pid("m2"), 2_000), _upsert(pid("m3"), 3_000)],
    )
    assert _unread(db) == (3, 0)
    return account_id


@pytest.mark.parametrize(
    ("up_to_ts", "unread_top"),
    [
        (2_500, 1),  # между сообщениями
        (500, 3),  # раньше первого
        (9_000, 0),  # позже последнего
        (2_000, 1),  # ровно на границе: сообщение с `ts == up_to_ts` прочитано
        (1_999, 2),
    ],
)
def test_read_up_to_ts_positions(db, up_to_ts, unread_top):
    account_id = _three(db)
    result = _apply(db, account_id, [_read(up_to_ts)])
    assert (result.applied, result.dead) == (1, 0)
    assert _marker(db) == (up_to_ts, extsort.EXT_SORT_MAX, "")
    assert _unread(db) == (unread_top, 0)


def test_read_up_to_ts_before_message_it_covers(db):
    """Событие пришло РАНЬШЕ сообщения, которое оно покрывает: после вставки сообщение прочитано."""
    account_id = _three(db)
    assert _apply(db, account_id, [_read(5_000)]).applied == 1
    _apply(db, account_id, [_upsert(pid("m4"), 4_000), _upsert(pid("m5"), 5_000), _upsert(pid("m6"), 5_001)])
    assert _msg_unread(db, pid("m4")) == 0
    assert _msg_unread(db, pid("m5")) == 0
    assert _msg_unread(db, pid("m6")) == 1
    assert _unread(db) == (1, 0)


def test_read_up_to_ts_marker_never_goes_back(db):
    account_id = _three(db)
    assert _apply(db, account_id, [_read(2_500)]).applied == 1
    back = _apply(db, account_id, [_read(1_500)])
    assert (back.applied, back.noop) == (0, 1)
    same = _apply(db, account_id, [_read(2_500)])
    assert (same.applied, same.noop) == (0, 1)
    assert _marker(db) == (2_500, extsort.EXT_SORT_MAX, "")
    assert _unread(db) == (1, 0)


def test_read_up_to_ts_unknown_channel_is_noop(db):
    account_id = _account(db)
    result = _apply(db, account_id, [_read(2_500)])
    assert (result.applied, result.noop, result.dead) == (0, 1, 0)


@pytest.mark.parametrize(
    "event",
    [
        {"type": "read", "channel": CH},  # ни одного
        {"type": "read", "channel": CH, "up_to_ext_id": "x", "up_to_ts": 2_500},  # оба
        {"type": "read", "channel": CH, "up_to_ts": 0},
        {"type": "read", "channel": CH, "up_to_ts": -1},
    ],
)
def test_read_with_both_or_neither_position_is_dead_letter(db, event):
    account_id = _three(db)
    result = _apply(db, account_id, [event])
    assert (result.applied, result.dead) == (0, 1)
    assert _marker(db) == (None, None, None) or _marker(db)[0] is None
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM work.dead_letter").fetchone()[0] == 1


def test_read_up_to_ext_id_path_unchanged_for_mattermost_ids(db):
    """Позиция сообщением работает как раньше: маркер — на самом сообщении, не на `EXT_SORT_MAX`."""
    account_id = _three(db)
    event = {"type": "read", "channel": CH, "up_to_ext_id": pid("m2")}
    assert _apply(db, account_id, [event]).applied == 1
    assert _marker(db) == (2_000, 0, pid("m2"))
    unknown = {"type": "read", "channel": CH, "up_to_ext_id": pid("zz")}
    assert _apply(db, account_id, [unknown]).noop == 1


def test_own_send_from_hub_is_not_a_read(db):
    """П2 плана: позиция источника, у которой последнее сообщение не позже неё — своё из хаба, — no-op; после
    чужого сообщения новее него тот же сценарий двигает маркер."""
    account_id, token_id = _account(db), _token(db)
    _apply(db, account_id, [_upsert(pid("m1"), 1_000), _upsert(pid("m2"), 1_500)])
    _hub_send(db, account_id, token_id, pid("own"), 2_000)
    before = _marker(db)
    unread_before = _unread(db)

    # Mattermost после своего поста ставит позицию канала ровно в его `create_at` (и чуть позже — тоже).
    for ts in (2_000, 2_005):
        result = _apply(db, account_id, [_read(ts)])
        assert (result.applied, result.noop) == (0, 1)
    assert _marker(db) == before
    assert _unread(db) == unread_before

    # Позиция раньше своего сообщения — обычное прочтение (последнее не позже неё — чужое).
    assert _apply(db, account_id, [_read(1_200)]).applied == 1
    assert _marker(db) == (1_200, extsort.EXT_SORT_MAX, "")

    # Пришло чужое сообщение новее своего — та же позиция источника теперь двигает маркер.
    _apply(db, account_id, [_upsert(pid("m3"), 2_500)])
    assert _apply(db, account_id, [_read(2_400)]).noop == 1  # до чужого — последнее всё ещё своё из хаба
    assert _apply(db, account_id, [_read(2_600)]).applied == 1
    assert _marker(db) == (2_600, extsort.EXT_SORT_MAX, "")
    assert _unread(db) == (0, 0)


def test_own_message_not_from_hub_moves_marker(db):
    """Своё сообщение без `outbox_id` (написано в родном клиенте) — владелец был в беседе: маркер двигается."""
    account_id = _account(db)
    _apply(
        db,
        account_id,
        [_upsert(pid("m1"), 1_000), _upsert(pid("m2"), 1_500), _upsert(pid("mine"), 2_000, is_me=True)],
    )
    assert _apply(db, account_id, [_read(2_000)]).applied == 1
    assert _marker(db) == (2_000, extsort.EXT_SORT_MAX, "")
    assert _unread(db) == (0, 0)


def test_unsent_placeholder_does_not_block_read(db):
    """Заготовка (ещё не ушла, `ext_id IS NULL`) под правило «своя отправка» не подпадает."""
    account_id, token_id = _account(db), _token(db)
    _apply(db, account_id, [_upsert(pid("m1"), 1_000)])
    _enqueue_and_lease(db, account_id, token_id, now_ms=1_500)
    assert _apply(db, account_id, [_read(2_000)]).applied == 1
    assert _marker(db) == (2_000, extsort.EXT_SORT_MAX, "")


def test_own_hub_reply_in_thread_blocks_channel_read_too(db):
    """Для позиции канала смотрится любое сообщение беседы, включая ответы тредов."""
    account_id, token_id = _account(db), _token(db)
    _apply(db, account_id, [_upsert(pid("p1"), 1_000), _upsert(pid("r1"), 1_100, thread_ext_id=pid("p1"))])
    _hub_send(db, account_id, token_id, pid("own"), 1_200, thread=pid("p1"))
    before = _marker(db)
    assert _apply(db, account_id, [_read(1_250)]).noop == 1
    assert _marker(db) == before


# --- B. Тред: `ReadEvent` с `thread_ext_id` и `ThreadInfo.last_read_ts` ------------------------------------


def _thread(db):
    account_id = _account(db)
    parent = pid("p1")
    _apply(
        db,
        account_id,
        [
            _upsert(parent, 1_000),
            _upsert(pid("r1"), 1_100, thread_ext_id=parent),
            _upsert(pid("r2"), 1_200, thread_ext_id=parent),
        ],
    )
    return account_id, parent


@pytest.mark.parametrize(
    ("up_to_ts", "r1_unread", "r2_unread"),
    [(1_150, 0, 1), (900, 1, 1), (5_000, 0, 0), (1_100, 0, 1), (1_200, 0, 0)],
)
def test_thread_read_up_to_ts_positions(db, up_to_ts, r1_unread, r2_unread):
    account_id, parent = _thread(db)
    # Тред без подписки непрочитанным не считается — подписываем снимком источника.
    _apply(db, account_id, [_upsert(parent, 1_000, thread={"reply_count": 2, "subscribed": True})])
    assert (_msg_unread(db, pid("r1")), _msg_unread(db, pid("r2"))) == (1, 1)

    assert _apply(db, account_id, [_read(up_to_ts, thread=parent)]).applied == 1

    assert _thread_marker(db, parent) == (up_to_ts, extsort.EXT_SORT_MAX, "")
    assert (_msg_unread(db, pid("r1")), _msg_unread(db, pid("r2"))) == (r1_unread, r2_unread)
    assert _marker(db)[0] is None  # маркер канала не тронут


def test_thread_read_up_to_ts_only_forward_and_no_thread_row_is_noop(db):
    account_id, parent = _thread(db)
    assert _apply(db, account_id, [_read(1_150, thread=parent)]).applied == 1
    assert _apply(db, account_id, [_read(1_120, thread=parent)]).noop == 1
    assert _thread_marker(db, parent) == (1_150, extsort.EXT_SORT_MAX, "")

    # Прочтение треда без строки треда — no-op, строка не создаётся.
    _apply(db, account_id, [_upsert(pid("p2"), 2_000)])
    assert _apply(db, account_id, [_read(2_500, thread=pid("p2"))]).noop == 1
    assert _thread_marker(db, pid("p2")) is None


def test_thread_read_before_reply_it_covers(db):
    account_id, parent = _thread(db)
    _apply(db, account_id, [_upsert(parent, 1_000, thread={"reply_count": 2, "subscribed": True})])
    assert _apply(db, account_id, [_read(1_400, thread=parent)]).applied == 1
    _apply(
        db,
        account_id,
        [_upsert(pid("r3"), 1_300, thread_ext_id=parent), _upsert(pid("r4"), 1_500, thread_ext_id=parent)],
    )
    assert (_msg_unread(db, pid("r3")), _msg_unread(db, pid("r4"))) == (0, 1)


def test_thread_read_own_hub_reply_is_not_a_read(db):
    """Правило п. 3 для треда: смотрятся корень и ответы ЭТОГО треда, не вся беседа."""
    account_id, token_id = _account(db), _token(db)
    parent = pid("p1")
    _apply(db, account_id, [_upsert(parent, 1_000), _upsert(pid("r1"), 1_100, thread_ext_id=parent)])
    _hub_send(db, account_id, token_id, pid("own"), 1_200, thread=parent)
    # Чужое сообщение основной ленты новее своего ответа — к треду отношения не имеет.
    _apply(db, account_id, [_upsert(pid("top"), 1_250)])
    before = _thread_marker(db, parent)

    # Mattermost после своего ответа ставит позицию треда в «сейчас» — на несколько мс позже ответа.
    assert _apply(db, account_id, [_read(1_300, thread=parent)]).noop == 1
    assert _thread_marker(db, parent) == before

    _apply(db, account_id, [_upsert(pid("r2"), 1_400, thread_ext_id=parent)])
    assert _apply(db, account_id, [_read(1_450, thread=parent)]).applied == 1
    assert _thread_marker(db, parent) == (1_450, extsort.EXT_SORT_MAX, "")
    assert _msg_unread(db, pid("r2")) == 0


def test_thread_read_own_reply_not_from_hub_moves_marker(db):
    account_id, parent = _thread(db)
    _apply(db, account_id, [_upsert(pid("mine"), 1_300, is_me=True, thread_ext_id=parent)])
    assert _apply(db, account_id, [_read(1_310, thread=parent)]).applied == 1
    assert _thread_marker(db, parent) == (1_310, extsort.EXT_SORT_MAX, "")


def test_thread_info_last_read_ts_before_parent_and_replies(db):
    """Сводка применяется раньше, чем вставлены родитель и ответы, — прямая позиция от этого не зависит."""
    account_id = _account(db)
    parent = pid("p1")
    info = {"reply_count": 2, "subscribed": True, "last_read_ts": 1_150}
    _apply(db, account_id, [_upsert(parent, 1_000, thread=info)])
    assert _thread_marker(db, parent) == (1_150, extsort.EXT_SORT_MAX, "")

    _apply(
        db,
        account_id,
        [_upsert(pid("r1"), 1_100, thread_ext_id=parent), _upsert(pid("r2"), 1_200, thread_ext_id=parent)],
    )
    assert (_msg_unread(db, pid("r1")), _msg_unread(db, pid("r2"))) == (0, 1)

    # Граница включительно, маркер только вперёд.
    _apply(db, account_id, [_upsert(parent, 1_000, thread={"reply_count": 2, "last_read_ts": 1_200})])
    assert _thread_marker(db, parent) == (1_200, extsort.EXT_SORT_MAX, "")
    assert _msg_unread(db, pid("r2")) == 0
    _apply(db, account_id, [_upsert(parent, 1_000, thread={"reply_count": 2, "last_read_ts": 1_050})])
    assert _thread_marker(db, parent) == (1_200, extsort.EXT_SORT_MAX, "")


def test_thread_info_last_read_ts_without_thread_row_is_noop(db):
    account_id = _account(db)
    _apply(db, account_id, [_upsert(pid("p1"), 1_000, thread={"reply_count": 0, "last_read_ts": 1_150})])
    assert _thread_marker(db, pid("p1")) is None


def test_thread_info_last_read_ts_own_hub_reply_is_not_a_read(db):
    account_id, token_id = _account(db), _token(db)
    parent = pid("p1")
    _apply(db, account_id, [_upsert(parent, 1_000), _upsert(pid("r1"), 1_100, thread_ext_id=parent)])
    _hub_send(db, account_id, token_id, pid("own"), 1_200, thread=parent)
    before = _thread_marker(db, parent)

    _apply(db, account_id, [_upsert(parent, 1_000, thread={"reply_count": 2, "last_read_ts": 1_205})])
    assert _thread_marker(db, parent) == before

    _apply(db, account_id, [_upsert(pid("r2"), 1_300, thread_ext_id=parent)])
    _apply(db, account_id, [_upsert(parent, 1_000, thread={"reply_count": 3, "last_read_ts": 1_350})])
    assert _thread_marker(db, parent) == (1_350, extsort.EXT_SORT_MAX, "")


def test_thread_info_with_both_read_positions_is_dead_letter(db):
    account_id = _account(db)
    info = {"reply_count": 1, "last_read_ext_id": pid("r1"), "last_read_ts": 1_150}
    result = _apply(db, account_id, [_upsert(pid("p1"), 1_000, thread=info)])
    assert (result.applied, result.dead) == (0, 1)
    bad = _apply(db, account_id, [_upsert(pid("p1"), 1_000, thread={"reply_count": 1, "last_read_ts": 0})])
    assert bad.dead == 1


# --- B. Порядок при `ext_sort = 0` -------------------------------------------------------------------------


def _source_sub_ext(db, thread_ext):
    with db.reader() as conn:
        return conn.execute(
            "SELECT source_sub_ext FROM work.thread WHERE ext_id = ?", (thread_ext,)
        ).fetchone()[0]


def test_last_reply_for_source_sub_ext_is_chosen_by_time(db):
    """`threads.apply_source_info`: последний ответ треда — по времени, а не по одному `ext_sort`/`ext_id`
    (у Mattermost `ext_sort` всегда 0, а идентификатор порядка не несёт)."""
    account_id = _account(db)
    parent = pid("p1")
    # Идентификатор позднего ответа «меньше» раннего — старый порядок (`ext_sort`, `ext_id`) выбрал бы ранний.
    early, late = pid("zz"), pid("aa")
    _apply(
        db,
        account_id,
        [
            _upsert(parent, 1_000),
            _upsert(early, 1_100, thread_ext_id=parent),
            _upsert(late, 1_200, thread_ext_id=parent),
        ],
    )
    _apply(db, account_id, [_upsert(parent, 1_000, thread={"reply_count": 2, "subscribed": False})])
    assert _source_sub_ext(db, parent) == late


def test_equal_ts_and_zero_ext_sort_order_is_stable(db):
    """Два сообщения в одну миллисекунду: порядок — по `ext_id`, одинаков при любом порядке доставки; позиция
    «до времени» покрывает оба."""
    for order in (("aa", "bb"), ("bb", "aa")):
        account_id = _account(db)
        parent = pid("p1")
        _apply(db, account_id, [_upsert(parent, 1_000)])
        for tag in order:
            _apply(db, account_id, [_upsert(pid(tag), 1_300, thread_ext_id=parent)])
        _apply(db, account_id, [_upsert(parent, 1_000, thread={"reply_count": 2, "subscribed": True})])
        assert _source_sub_ext(db, parent) == pid("bb")
        with db.reader() as conn:
            rows = conn.execute(
                "SELECT m.ext_id FROM work.message m WHERE m.account_id = ? AND m.thread_ext_id = ? "
                "ORDER BY m.ts, m.ext_sort, m.ext_id",
                (account_id, parent),
            ).fetchall()
        assert [r[0] for r in rows] == [pid("aa"), pid("bb")]
        assert _apply(db, account_id, [_read(1_300, thread=parent)]).applied == 1
        assert (_msg_unread(db, pid("aa")), _msg_unread(db, pid("bb"))) == (0, 0)
        # Следующему проходу — чистая беседа того же имени у нового аккаунта.
        db.write_sync(lambda conn: conn.execute("UPDATE work.channel SET ext_id = ext_id || '-old'"))
        db.write_sync(lambda conn: conn.execute("UPDATE work.message SET ext_id = ext_id || '-old'"))


# --- C. Родитель треда в сигнале подписки ------------------------------------------------------------------


def test_message_cat_carries_mattermost_thread_parent(db):
    account_id = _account(db)
    parent = pid("p1")
    _apply(db, account_id, [_upsert(parent, 1_000), _upsert(pid("r1"), 1_100, thread_ext_id=parent)])
    with db.reader() as conn:
        assert chan_subs.message_cat(conn, _msg_id(db, pid("r1"))).thread_ext == parent
        assert chan_subs.message_cat(conn, _msg_id(db, parent)).thread_ext is None  # родитель — не ответ


@pytest.mark.parametrize(
    "garbage",
    [
        "hub_thread(1) и текст",  # произвольная строка
        "A" * 26,  # заглавные
        "a" * 25,
        "a" * 27,
        "a" * 25 + "-",
        "а" * 26,  # кириллица
        "٣" * 26,  # юникод-цифры
    ],
)
def test_message_cat_drops_garbage_thread_parent(db, garbage):
    account_id = _account(db)
    _apply(db, account_id, [_upsert(pid("r1"), 1_100, thread_ext_id=garbage)])
    with db.reader() as conn:
        assert chan_subs.message_cat(conn, _msg_id(db, pid("r1"))).thread_ext is None


def test_sub_wait_accepts_mattermost_thread_parent_only_by_fullmatch():
    parent = pid("p1")
    assert sub_wait._cat_fields({"cat": "thread", "thread_ext": parent}) == {  # noqa: SLF001
        "cat": "thread",
        "thread_ext": parent,
    }
    assert sub_wait._cat_fields({"thread_ext": "1700000000.123456"})["thread_ext"] == (  # noqa: SLF001
        "1700000000.123456"
    )
    for garbage in (parent + "\n", parent + "x", parent[:-1], parent.upper(), "x" * 26 + "\nкоманда"):
        assert "thread_ext" not in sub_wait._cat_fields({"thread_ext": garbage})  # noqa: SLF001


# --- D. Источник `mattermost` в списках модулей ------------------------------------------------------------


def _own_text_message(db, account_id, *, text="моё"):
    _apply(db, account_id, [_upsert(pid("mine"), 1_000, is_me=True, text=text)])
    return _msg_id(db, pid("mine"))


def test_sources_lists_include_mattermost():
    assert "mattermost" in media.MEDIA_SOURCES
    assert "mattermost" in msg_ops.SOURCES
    assert "mattermost" in reactions.SOURCES
    assert "mattermost" in read_ack.SOURCES


def test_msg_ops_edit_mattermost_limit_16383_chars(db):
    account_id = _account(db)
    message_id = _own_text_message(db, account_id)

    ok = db.write_sync(lambda conn: msg_ops.enqueue_edit(conn, message_id, "я" * 16_383, 2_000))
    assert ok.status == "queued"
    with db.reader() as conn:
        op, text = conn.execute(
            "SELECT op, text FROM work.msg_op WHERE message_id = ?", (message_id,)
        ).fetchone()
    assert op == "edit" and len(text) == 16_383

    with pytest.raises(msg_ops.InvalidText) as info:
        db.write_sync(lambda conn: msg_ops.enqueue_edit(conn, message_id, "я" * 16_384, 2_001))
    assert (info.value.code, info.value.limit) == ("too_long", 16_383)


def test_msg_ops_mattermost_limit_counts_chars_not_utf16(db):
    """16383 символа вне BMP — это 32766 единиц UTF-16: у Mattermost лимит в символах."""
    account_id = _account(db)
    message_id = _own_text_message(db, account_id)
    ok = db.write_sync(lambda conn: msg_ops.enqueue_edit(conn, message_id, "😀" * 16_383, 2_000))
    assert ok.status == "queued"
    assert msg_ops.text_limit("mattermost", "text") == (16_383, False)
    assert msg_ops.text_limit("mattermost", "file") == (16_383, False)
    assert msg_ops.text_limit("slack", "text") == (4_000, False)  # Slack — как раньше


def test_msg_ops_delete_mattermost_accepted(db):
    account_id = _account(db)
    message_id = _own_text_message(db, account_id)
    channel_id = db.write_sync(lambda conn: msg_ops.enqueue_delete(conn, [message_id], 2_000))
    assert channel_id == _channel_id(db)


def _ops(db, message_id):
    with db.reader() as conn:
        row = conn.execute(
            "SELECT ops_json FROM work.reaction_ack WHERE message_id = ?", (message_id,)
        ).fetchone()
    return json.loads(row[0]) if row is not None else None


def test_reactions_mattermost_several_own_and_named(db):
    """Как Slack: вторая своя реакция на то же сообщение принята и первую не заменяет; `:name:` принято."""
    account_id = _account(db)
    mine = [{"emoji": "👍", "count": 1, "mine": True}]
    _apply(db, account_id, [_upsert(pid("m1"), 1_000)])
    _apply(db, account_id, [{"type": "reaction", "channel": CH, "ext_id": pid("m1"), "reactions": mine}])
    message_id = _msg_id(db, pid("m1"))

    first = db.write_sync(lambda conn: reactions.enqueue(conn, message_id, "🔥", True, 2_000))
    second = db.write_sync(lambda conn: reactions.enqueue(conn, message_id, "🎉", True, 2_001))
    named = db.write_sync(lambda conn: reactions.enqueue(conn, message_id, ":party_parrot:", True, 2_002))
    assert (first.status, second.status, named.status) == ("queued", "queued", "queued")
    assert _ops(db, message_id) == [["🔥", True], ["🎉", True], [":party_parrot:", True]]

    (item,) = db.write_sync(lambda conn: reactions.lease(conn, account_id=account_id, now_ms=60_000))
    assert item["ops"] == [["🔥", True], ["🎉", True], [":party_parrot:", True]]
    # «Текущие мои» коннектору шлются только у источников с одной реакцией или своим учётом (Telegram, почта).
    assert "current_mine" not in item


def test_reactions_mattermost_not_limited_to_telegram_set(db):
    account_id = _account(db)
    _apply(db, account_id, [_upsert(pid("m1"), 1_000)])
    message_id = _msg_id(db, pid("m1"))
    assert "🧩" not in reactions.TELEGRAM_FREE_REACTIONS
    result = db.write_sync(lambda conn: reactions.enqueue(conn, message_id, "🧩", True, 2_000))
    assert result.status == "queued"
    assert reactions.normalize_emoji("mattermost", "❤️") == "❤️"  # U+FE0F не срезается (как у Slack)
    with pytest.raises(reactions.InvalidEmoji):
        reactions.normalize_emoji("mattermost", "не эмодзи")
    with pytest.raises(reactions.InvalidEmoji):
        reactions.normalize_emoji("telegram", ":party_parrot:")  # `:name:` — только у именованных


def test_read_ack_mattermost_target_is_not_thread_reply(db):
    """Как Slack: цель отметки в источнике — не ответ треда; своё сообщение ленты целью быть может."""
    account_id = _account(db)
    parent = pid("p1")
    _apply(
        db,
        account_id,
        [
            _upsert(parent, 1_000),
            _upsert(pid("r1"), 1_100, thread_ext_id=parent),
            _upsert(pid("r2"), 1_200, thread_ext_id=parent),
        ],
    )
    channel_id = _channel_id(db)
    queued = db.write_sync(lambda conn: read_ack.enqueue(conn, channel_id, _msg_id(db, pid("r2")), 2_000))
    assert queued == "queued"  # раньше — `unsupported`

    (item,) = db.write_sync(lambda conn: read_ack.lease(conn, account_id=account_id, now_ms=60_000))
    assert item["channel"]["ext_id"] == "c1"
    assert item["message"]["ext_id"] == parent  # не ответ треда
    assert item["message"]["ts"] == 1_000

    _apply(db, account_id, [_upsert(pid("mine"), 1_300, is_me=True)])
    db.write_sync(lambda conn: read_ack.enqueue(conn, channel_id, _msg_id(db, pid("mine")), 70_000))
    (item2,) = db.write_sync(lambda conn: read_ack.lease(conn, account_id=account_id, now_ms=200_000))
    assert item2["message"]["ext_id"] == pid("mine") and item2["message"]["is_me"] is True


def test_read_ack_dropped_when_source_position_by_time_is_not_behind(db):
    """Позиция источника временем `(ts, EXT_SORT_MAX, "")` уже покрывает цель UI — толкать нечего."""
    account_id = _three(db)
    channel_id = _channel_id(db)
    db.write_sync(lambda conn: read_ack.enqueue(conn, channel_id, _msg_id(db, pid("m2")), 20_000))
    assert _apply(db, account_id, [_read(2_000)]).applied == 1
    assert db.write_sync(lambda conn: read_ack.lease(conn, account_id=account_id, now_ms=60_000)) == []
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM work.read_ack").fetchone()[0] == 0


def test_media_rows_created_for_mattermost_like_slack(db):
    account_id = _account(db)
    db.write_sync(lambda conn: media.ensure_start_ts(conn, 1_000))
    attachments = [
        {"type": "photo", "name": "a.png", "size": 1_000, "mime": "image/png", "ref": {"id": pid("f1")}},
        {"type": "document", "name": "b.pdf", "size": 2_000, "mime": "application/pdf"},
    ]
    event = _upsert(pid("m1"), 5_000, attachments=attachments)
    event["kind"] = "file"
    _apply(db, account_id, [event])
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT md.idx, md.kind, md.status FROM work.media md JOIN work.message m "
            "ON m.id = md.message_id WHERE m.ext_id = ? ORDER BY md.idx",
            (pid("m1"),),
        ).fetchall()
    # Раньше строк не было вовсе (источник вне `MEDIA_SOURCES`); качается сразу, как у Slack.
    assert rows == [(0, "photo", "pending"), (1, "document", "pending")]
