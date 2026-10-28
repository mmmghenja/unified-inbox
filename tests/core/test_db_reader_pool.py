"""Пул читателей `Db.reader()` (19.10): соединение переиспользуется, а снимок всегда свежий."""

import sqlite3
import threading

from hub.core import db as db_module


def _add(db, schema: str, ext_id: str) -> None:
    db.write_sync(
        lambda conn: conn.execute(
            f"INSERT INTO {schema}.channel (account_id, ext_id, kind, first_seen_ts) VALUES (1, ?, 'dm', 0)",
            (ext_id,),
        )
    )


def _count(conn, schema: str = "main") -> int:
    return conn.execute(f"SELECT count(*) FROM {schema}.channel").fetchone()[0]


def test_reader_reuses_connection(db):
    with db.reader() as first:
        pass
    with db.reader() as second:
        assert second is first
        assert second.execute("PRAGMA query_only").fetchone()[0] == 1
        assert second.execute("SELECT 1 FROM work.sqlite_master LIMIT 1").fetchall() is not None


def test_live_unfinished_cursor_does_not_freeze_snapshot(db):
    for i in range(3):
        _add(db, "main", f"c{i}")
        _add(db, "work", f"w{i}")
    with db.reader() as conn:
        held = conn.execute("SELECT ext_id FROM main.channel")  # не дочитан и жив после выхода
        held.fetchone()
        held_work = conn.cursor()
        held_work.execute("SELECT ext_id FROM work.channel")
        held_work.fetchone()
    _add(db, "main", "new")
    _add(db, "work", "new-w")
    with db.reader() as again:
        assert again is conn
        assert _count(again, "main") == 4
        assert _count(again, "work") == 4
    del held, held_work


def test_forgotten_transaction_is_rolled_back(db):
    with db.reader() as conn:
        conn.execute("BEGIN")
        _count(conn)
    _add(db, "main", "after")
    with db.reader() as again:
        assert not again.in_transaction
        assert _count(again) == 1


def test_exception_inside_reader_still_returns_fresh_connection(db):
    try:
        with db.reader() as conn:
            conn.execute("SELECT ext_id FROM main.channel").fetchone()
            raise LookupError
    except LookupError:
        pass
    _add(db, "main", "x")
    with db.reader() as again:
        assert again is conn
        assert _count(again) == 1


def test_concurrent_readers_get_distinct_connections(db):
    barrier = threading.Barrier(3)
    seen: list[int] = []
    lock = threading.Lock()

    def work() -> None:
        with db.reader() as conn:
            barrier.wait(timeout=10)
            _count(conn)
            with lock:
                seen.append(id(conn))
            barrier.wait(timeout=10)

    threads = [threading.Thread(target=work) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert len(set(seen)) == 3


def test_pool_is_bounded_and_closed_with_db(db, monkeypatch):
    monkeypatch.setattr(db_module, "_READER_POOL_MAX", 2)
    with db.reader() as a, db.reader() as b, db.reader() as c:
        pass
    assert len(db._readers) == 2
    pooled = list(db._readers)
    db.close()
    assert db._readers == []
    for conn in pooled:
        try:
            conn.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            continue
        raise AssertionError("соединение пула не закрыто")
    del a, b, c
