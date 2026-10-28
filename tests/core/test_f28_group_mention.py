"""Ф28-2 (`docs/architecture.md`): упоминание моей группы Slack (`mentions_group_only`) —
упоминание для меня (категория `mention`, сам тег непрочитан, поднимает беседу), но тред НЕ подписывает и
устаревший снимок источника (Ф28-1) не сбрасывает. Личный тег — подписывает, как раньше.

Ядро — `ingest.apply_batch` напрямую (как `test_f28_thread_sub_stale.py`); work-аккаунт Slack."""

import sqlite3

from hub.core import chan_subs
from hub.core.db import Db
from hub.core.ingest import apply_batch
from hub.core.threads import message_unread_sql

P = "1790917549.148089"  # родитель
R1 = "1790917600.000100"  # чужой ответ
R_TAG = "1790920680.000200"  # тег моей группы
R_NEXT = "1790921220.000400"  # следующий чужой ответ без тега


def _account(db):
    return db.write_sync(
        lambda conn: conn.execute("INSERT INTO main.account (source, db) VALUES ('slack', 'work')").lastrowid
    )


def _up(ext_id, ts, *, thread_ext_id=None, mentions_me=False, **kw):
    event = {
        "type": "upsert",
        "channel": {"ext_id": "C1", "kind": "group"},
        "ext_id": ext_id,
        "ts": ts,
        "is_me": False,
        "kind": "text",
        "text": kw.pop("text", "текст"),
        "mentions_me": mentions_me,
        "thread_ext_id": thread_ext_id,
    }
    event.update(kw)
    return event


def _apply(db, account_id, events, now_ms=1000):
    result = db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=events, cursor=None, now_ms=now_ms)
    )
    assert result.dead == 0, result


def _thread(db):
    """`(subscribed, source_subscribed)` единственного треда."""
    with db.reader() as conn:
        return tuple(conn.execute("SELECT subscribed, source_subscribed FROM work.thread").fetchone())


def _unread(db, ext_id):
    with db.reader() as conn:
        return conn.execute(
            f"SELECT {message_unread_sql('m', 'ch', 't')} FROM work.message m "  # noqa: S608
            "JOIN work.channel ch ON ch.id = m.channel_id "
            "LEFT JOIN work.thread t ON t.channel_id = m.channel_id AND t.ext_id = m.thread_ext_id "
            "WHERE m.ext_id = ?",
            (ext_id,),
        ).fetchone()[0]


def _stale_snapshot(db, account_id):
    """Родитель, чужой ответ и снимок сверки «не подписан» (Ф28-1)."""
    _apply(db, account_id, [_up(P, 1000), _up(R1, 1001, thread_ext_id=P)])
    snapshot = _up(P, 1000, thread={"reply_count": 1, "latest_reply_ext_id": R1, "subscribed": False})
    _apply(db, account_id, [snapshot], now_ms=1002)
    assert _thread(db) == (0, 0)


def _group(ext_id, ts, **kw):
    return _up(ext_id, ts, mentions_me=True, mentions_group_only=True, **kw)


def _row(db, ext_id):
    with db.reader() as conn:
        return conn.execute(
            "SELECT id, mentions_me, mentions_broadcast, mentions_group FROM work.message WHERE ext_id = ?",
            (ext_id,),
        ).fetchone()


def _cat(db, ext_id):
    with db.reader() as conn:
        return chan_subs.message_cat(conn, _row(db, ext_id)[0]).cat


def _last_msg_ts(db):
    with db.reader() as conn:
        return conn.execute("SELECT last_msg_ts FROM work.channel WHERE ext_id = 'C1'").fetchone()[0]


def test_group_tag_in_foreign_thread_is_mention_but_does_not_subscribe(db):
    account_id = _account(db)
    _apply(db, account_id, [_up(P, 1000), _up(R1, 1001, thread_ext_id=P)])
    assert _thread(db) == (0, None)
    _apply(db, account_id, [_group(R_TAG, 1003, thread_ext_id=P)], now_ms=1003)
    assert _row(db, R_TAG)[1:] == (1, 0, 1)
    assert _cat(db, R_TAG) == "mention"
    assert _unread(db, R_TAG) == 1  # сам тег группы непрочитан
    assert _last_msg_ts(db) == 1003  # и поднимает беседу
    assert _thread(db) == (0, None)  # но тред не подписан
    _apply(db, account_id, [_up(R_NEXT, 1004, thread_ext_id=P)], now_ms=1004)
    assert _thread(db) == (0, None)
    assert _cat(db, R_NEXT) == "other"
    assert _unread(db, R_NEXT) == 0
    assert _last_msg_ts(db) == 1003


def test_group_tag_in_parent_does_not_subscribe(db):
    account_id = _account(db)
    _apply(db, account_id, [_group(P, 1000), _up(R1, 1001, thread_ext_id=P)])
    assert _thread(db) == (0, None)
    assert _cat(db, R1) == "other"
    assert _unread(db, R1) == 0
    assert _last_msg_ts(db) == 1000


def test_personal_tag_still_subscribes(db):
    account_id = _account(db)
    _apply(db, account_id, [_up(P, 1000), _up(R_TAG, 1001, thread_ext_id=P, mentions_me=True)])
    assert _row(db, R_TAG)[1:] == (1, 0, 0)
    assert _thread(db) == (1, None)
    _apply(db, account_id, [_up(R_NEXT, 1002, thread_ext_id=P)], now_ms=1002)
    assert _cat(db, R_NEXT) == "thread"
    assert _unread(db, R_NEXT) == 1


def test_personal_parent_tag_still_subscribes(db):
    account_id = _account(db)
    _apply(db, account_id, [_up(P, 1000, mentions_me=True), _up(R1, 1001, thread_ext_id=P)])
    assert _thread(db) == (1, None)


def test_group_tag_after_stale_snapshot_keeps_snapshot(db):
    """Ф28-1: тег группы — не свидетельство подписки, снимок Slack «не подписан» остаётся."""
    account_id = _account(db)
    _stale_snapshot(db, account_id)
    _apply(db, account_id, [_group(R_TAG, 1003, thread_ext_id=P)], now_ms=1003)
    assert _thread(db) == (0, 0)
    assert _unread(db, R_TAG) == 1  # сам тег — всё равно непрочитан


def test_edit_adds_group_tag_then_replaces_with_personal(db):
    account_id = _account(db)
    _stale_snapshot(db, account_id)
    group = _group(R1, 1001, thread_ext_id=P, text="@developers глянь", edited_ts=1005)
    _apply(db, account_id, [group], now_ms=1005)
    assert _row(db, R1)[1:] == (1, 0, 1)
    assert _thread(db) == (0, 0)  # правка с тегом группы снимок не сбрасывает и тред не подписывает
    personal = _up(R1, 1001, thread_ext_id=P, mentions_me=True, text="@владелец глянь", edited_ts=1006)
    _apply(db, account_id, [personal], now_ms=1006)
    assert _row(db, R1)[1:] == (1, 0, 0)
    assert _thread(db) == (1, None)


def test_group_and_broadcast_flags_together_count_as_broadcast(db):
    """Страховка ядра: оба флага (коннектор так не шлёт) — только широковещательное, `mentions_group = 0`."""
    account_id = _account(db)
    ev = _up(P, 1000, mentions_me=True, mentions_broadcast_only=True, mentions_group_only=True)
    _apply(db, account_id, [ev])
    assert _row(db, P)[1:] == (1, 1, 0)


def test_migration_0042_column_in_both_files(tmp_path):
    db = Db(tmp_path)
    db.start()
    db.close()
    db.start()  # повторный старт — без ошибок
    db.close()
    for path in (db.main_path, db.work_path):
        conn = sqlite3.connect(path)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] >= 42
            cols = {row[1]: row for row in conn.execute("PRAGMA table_info(message)")}
            assert cols["mentions_group"][3] == 1 and cols["mentions_group"][4] == "0"  # NOT NULL DEFAULT 0
        finally:
            conn.close()
