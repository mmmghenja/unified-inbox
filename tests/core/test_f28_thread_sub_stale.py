"""Ф28-1 (`docs/architecture.md`): тег меня и мой ответ подписывают тред Slack поверх
устаревшего снимка источника «не подписан» (`thread.source_subscribed = 0`); миграция 0041 чинит застрявшие.

Ядро — `ingest.apply_batch` и `outbox.report` напрямую (как `test_threads.py`); work-аккаунт Slack."""

import sqlite3
import time
from pathlib import Path

from hub.core import chan_subs
from hub.core import outbox as outbox_mod
from hub.core.db import Db, next_seq
from hub.core.ingest import apply_batch
from hub.core.threads import message_unread_sql

P = "1790917549.148089"  # родитель
R1 = "1790917600.000100"  # чужой ответ до снимка
R_TAG = "1790920680.000200"  # тег меня после снимка
R_ME = "1790921040.000300"  # мой ответ после снимка
R_NEXT = "1790921220.000400"  # следующий чужой ответ без тега


def _account(db):
    return db.write_sync(
        lambda conn: conn.execute("INSERT INTO main.account (source, db) VALUES ('slack', 'work')").lastrowid
    )


def _up(
    ext_id, ts, *, thread_ext_id=None, is_me=False, mentions_me=False, broadcast=False, thread=None, **kw
):
    event = {
        "type": "upsert",
        "channel": {"ext_id": "C1", "kind": "group"},
        "ext_id": ext_id,
        "ts": ts,
        "is_me": is_me,
        "kind": "text",
        "text": kw.pop("text", "текст"),
        "mentions_me": mentions_me or broadcast,
        "thread_ext_id": thread_ext_id,
    }
    if broadcast:
        event["mentions_broadcast_only"] = True
    if thread is not None:
        event["thread"] = thread
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


def _stale_snapshot(db, account_id, *, latest=R1):
    """Родитель, чужой ответ и снимок сверки «не подписан» (последний ответ снимка — `latest`)."""
    _apply(db, account_id, [_up(P, 1000), _up(R1, 1001, thread_ext_id=P)])
    snapshot = _up(P, 1000, thread={"reply_count": 1, "latest_reply_ext_id": latest, "subscribed": False})
    _apply(db, account_id, [snapshot], now_ms=1002)
    assert _thread(db) == (0, 0)


def test_tag_after_stale_snapshot_subscribes_thread(db):
    account_id = _account(db)
    _stale_snapshot(db, account_id)
    _apply(db, account_id, [_up(R_TAG, 1003, thread_ext_id=P, mentions_me=True)], now_ms=1003)
    assert _thread(db) == (1, None)
    _apply(db, account_id, [_up(R_NEXT, 1004, thread_ext_id=P)], now_ms=1004)
    assert _thread(db) == (1, None)
    assert _unread(db, R_NEXT) == 1  # ответ без тега — непрочитан в UI


def test_my_reply_after_stale_snapshot_subscribes_thread(db):
    account_id = _account(db)
    _stale_snapshot(db, account_id)
    _apply(db, account_id, [_up(R_ME, 1003, thread_ext_id=P, is_me=True)], now_ms=1003)
    assert _thread(db) == (1, None)
    _apply(db, account_id, [_up(R_NEXT, 1004, thread_ext_id=P)], now_ms=1004)
    assert _unread(db, R_NEXT) == 1


def test_my_reply_from_hub_report_subscribes_thread(db):
    """Ответ владельца из хаба: отчёт об отправке приходит раньше эха — сброс в `outbox._report_sent`."""
    account_id = _account(db)
    _stale_snapshot(db, account_id)
    token_id = db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.api_token (name, kind, hash) VALUES ('ui', 'ui', 'x')").lastrowid
        )
    )
    with db.reader() as conn:
        channel_id = conn.execute("SELECT id FROM work.channel WHERE ext_id = 'C1'").fetchone()[0]
    enq = db.write_sync(
        lambda conn: outbox_mod.enqueue(
            conn,
            channel_id=channel_id,
            text="мой ответ",
            thread_ext_id=P,
            origin="ui",
            api_token_id=token_id,
            idempotency_key="k28",
            now_ms=5_000,
        )
    )
    leases = db.write_sync(
        lambda conn: outbox_mod.lease(conn, account_id=account_id, owner="c", now_ms=5_100)
    )
    db.write_sync(
        lambda conn: outbox_mod.report(
            conn,
            outbox_id=enq.outbox_id,
            owner="c",
            lease_gen=leases[0].lease_gen,
            outcome="sent",
            ext_id=R_ME,
            now_ms=5_200,
        )
    )
    assert _thread(db) == (1, None)
    _apply(db, account_id, [_up(R_NEXT, 1790921220_000, thread_ext_id=P)], now_ms=5_300)
    assert _unread(db, R_NEXT) == 1


def test_snapshot_after_my_reply_unsubscribes(db):
    """Снимок «не подписан» ПОСЛЕ моего ответа (сверка позже) — уважаем Slack; повтор моего ответа сверкой
    (не вставка) снимок не сбрасывает."""
    account_id = _account(db)
    _apply(db, account_id, [_up(P, 1000), _up(R_ME, 1001, thread_ext_id=P, is_me=True)])
    assert _thread(db) == (1, None)
    snapshot = _up(P, 1000, thread={"reply_count": 1, "latest_reply_ext_id": R_ME, "subscribed": False})
    _apply(db, account_id, [snapshot], now_ms=1002)
    assert _thread(db) == (0, 0)
    _apply(db, account_id, [_up(R_ME, 1001, thread_ext_id=P, is_me=True)], now_ms=1003)  # повтор сверкой
    _apply(db, account_id, [_up(R_NEXT, 1004, thread_ext_id=P)], now_ms=1004)
    assert _thread(db) == (0, 0)
    assert _unread(db, R_NEXT) == 0


def test_catch_up_reply_known_to_snapshot_keeps_unsubscribed(db):
    """Отступление: догон треда кладёт родителя со снимком, затем ответы того же `conversations.replies`. Мой
    ответ, о котором снимок уже знал (не новее его последнего ответа), снимок не сбрасывает."""
    account_id = _account(db)
    _apply(db, account_id, [_up(P, 1000)])
    snapshot = _up(P, 1000, thread={"reply_count": 2, "latest_reply_ext_id": R_NEXT, "subscribed": False})
    _apply(
        db,
        account_id,
        [snapshot, _up(R_ME, 1001, thread_ext_id=P, is_me=True), _up(R_NEXT, 1002, thread_ext_id=P)],
        now_ms=1003,
    )
    assert _thread(db) == (0, 0)


def test_parent_update_before_tag_does_not_hide_tag_from_reset(db):
    """Живое обновление родителя (`latest_reply` = тег, без мнения о подписке) приходит раньше самого ответа с
    тегом: позиция снимка подписки (`source_sub_ext`) осталась на X — тег новее, снимок сбрасывается."""
    account_id = _account(db)
    _stale_snapshot(db, account_id)  # снимок `subscribed=false` на позиции R1
    parent_update = _up(P, 1000, thread={"reply_count": 2, "latest_reply_ext_id": R_TAG})
    _apply(db, account_id, [parent_update], now_ms=1003)
    assert _thread(db) == (0, 0)
    _apply(db, account_id, [_up(R_TAG, 1004, thread_ext_id=P, mentions_me=True)], now_ms=1004)
    assert _thread(db) == (1, None)
    _apply(db, account_id, [_up(R_NEXT, 1005, thread_ext_id=P)], now_ms=1005)
    assert _unread(db, R_NEXT) == 1
    with db.reader() as conn:
        message_id = conn.execute("SELECT id FROM work.message WHERE ext_id = ?", (R_NEXT,)).fetchone()[0]
        assert chan_subs.message_cat(conn, message_id).cat == "thread"


def test_snapshot_without_latest_reply_takes_position_from_core(db):
    """Снимок с `subscribed`, но без `latest_reply` — позиция = последний ответ, уже лежащий в ядре."""
    account_id = _account(db)
    _apply(
        db,
        account_id,
        [_up(P, 1000), _up(R1, 1001, thread_ext_id=P), _up(R_ME, 1002, thread_ext_id=P, is_me=True)],
    )
    _apply(db, account_id, [_up(P, 1000, thread={"reply_count": 2, "subscribed": False})], now_ms=1003)
    with db.reader() as conn:
        assert conn.execute("SELECT source_sub_ext FROM work.thread").fetchone()[0] == R_ME
    assert _thread(db) == (0, 0)


def test_broadcast_mention_does_not_drop_snapshot(db):
    account_id = _account(db)
    _stale_snapshot(db, account_id)
    _apply(db, account_id, [_up(R_TAG, 1003, thread_ext_id=P, broadcast=True)], now_ms=1003)
    assert _thread(db) == (0, 0)


def test_edit_adding_direct_mention_drops_snapshot(db):
    account_id = _account(db)
    _apply(db, account_id, [_up(P, 1000), _up(R1, 1001, thread_ext_id=P)])
    snapshot = _up(P, 1000, thread={"reply_count": 1, "latest_reply_ext_id": R1, "subscribed": False})
    _apply(db, account_id, [snapshot], now_ms=1002)
    assert _thread(db) == (0, 0)
    edit = _up(R1, 1001, thread_ext_id=P, mentions_me=True, text="@владелец глянь", edited_ts=1005)
    _apply(db, account_id, [edit], now_ms=1005)
    assert _thread(db) == (1, None)


# --- миграция 0041 ----------------------------------------------------------------------------------------


def _seed_stuck(db, *, cid_ext, my_reply_age_ms=None, mention_parent_age_ms=None):
    """Беседа с тредом, застрявшим на снимке «не подписан»; свидетельства — с заданным возрастом (мс)."""
    now = int(time.time() * 1000)

    def _fn(conn):
        account_id = conn.execute("SELECT id FROM main.account WHERE source = 'slack'").fetchone()[0]
        cid = conn.execute(
            "INSERT INTO work.channel (account_id, ext_id, kind) VALUES (?, ?, 'group')",
            (account_id, cid_ext),
        ).lastrowid
        rows = [(P, None, 0, int(mention_parent_age_ms is not None), now - (mention_parent_age_ms or 10**9))]
        if my_reply_age_ms is not None:
            rows.append((R_ME, P, 1, 0, now - my_reply_age_ms))
        rows.append((R_NEXT, P, 0, 0, now))
        for ext_id, thread_ext_id, is_me, mentions_me, ts in rows:
            seq = next_seq(conn)
            conn.execute(
                "INSERT INTO work.message (seq, first_seq, account_id, channel_id, ext_id, ts, "
                "thread_ext_id, is_me, mentions_me, kind, text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'text', 'x')",
                (seq, seq, account_id, cid, ext_id, ts, thread_ext_id, is_me, mentions_me),
            )
        conn.execute(
            "INSERT INTO work.thread (channel_id, ext_id, subscribed, source_subscribed, first_seen_ts) "
            "VALUES (?, ?, 0, 0, ?)",
            (cid, P, now),
        )
        return cid

    return db.write_sync(_fn)


def _rerun_0041(path: Path) -> None:
    sql = (Path(__file__).parents[2] / "src/hub/core/migrations/0041_shared.sql").read_text(encoding="utf-8")
    sql = sql[sql.index("UPDATE thread") :]  # колонку 0041 добавляет один раз раннер; повтор — только починка
    conn = sqlite3.connect(path)
    try:
        conn.executescript(sql)
    finally:
        conn.close()


def test_migration_0041_unsticks_recent_evidence_only(tmp_path):
    db = Db(tmp_path)
    db.start()
    try:
        _account(db)
        day = 24 * 60 * 60 * 1000
        fresh_reply = _seed_stuck(db, cid_ext="C-fresh", my_reply_age_ms=day)
        fresh_parent = _seed_stuck(db, cid_ext="C-parent", mention_parent_age_ms=day)
        no_evidence = _seed_stuck(db, cid_ext="C-none")
        old_reply = _seed_stuck(db, cid_ext="C-old", my_reply_age_ms=4 * day)
    finally:
        db.close()

    work_path = db.work_path
    _rerun_0041(work_path)
    _rerun_0041(work_path)  # повторный запуск — без ошибок, результат тот же

    conn = sqlite3.connect(work_path)
    try:
        got = dict(
            conn.execute("SELECT channel_id, subscribed || ':' || IFNULL(source_subscribed, '-') FROM thread")
        )
    finally:
        conn.close()
    assert got == {
        fresh_reply: "1:-",
        fresh_parent: "1:-",
        no_evidence: "0:0",
        old_reply: "0:0",
    }


def test_migration_0041_applies_to_main_file_too(tmp_path):
    """`_shared` — и в главном файле (Slack-аккаунт с `db = 'main'`): версия main после старта — 41+."""
    db = Db(tmp_path)
    db.start()
    db.close()
    for path in (db.main_path, db.work_path):
        conn = sqlite3.connect(path)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] >= 41
        finally:
            conn.close()
