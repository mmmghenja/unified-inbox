"""Тест 17 спеки (`docs/architecture.md`): `Db.on_commit`.

Стиль — как в test_db.py: `db` — фикстура из tests/conftest.py (tmp_path, Db.start()/close()).
"""

import sqlite3

import pytest


def _insert_channel(conn: sqlite3.Connection, ext_id: str = "c1") -> int:
    cur = conn.execute(
        "INSERT INTO main.channel (account_id, ext_id, kind, first_seen_ts) VALUES (1, ?, 'dm', 0)",
        (ext_id,),
    )
    return cur.lastrowid


def test_on_commit_called_after_successful_write(db):
    calls = []
    db.on_commit(lambda: calls.append(1))

    db.write_sync(lambda conn: _insert_channel(conn, "a"))
    assert calls == [1]

    db.write_sync(lambda conn: _insert_channel(conn, "b"))
    assert calls == [1, 1]


def test_on_commit_runs_after_commit_sees_committed_data(db):
    """Порядок COMMIT -> колбэки: колбэк, читающий БД своим `reader()`, уже видит закоммиченную строку."""
    seen = []

    def _callback() -> None:
        with db.reader() as conn:
            seen.append(conn.execute("SELECT COUNT(*) FROM main.channel").fetchone()[0])

    db.on_commit(_callback)
    db.write_sync(lambda conn: _insert_channel(conn, "x"))
    assert seen == [1]


def test_on_commit_not_called_on_rollback(db):
    calls = []
    db.on_commit(lambda: calls.append(1))

    def _boom(conn):
        _insert_channel(conn, "will-rollback")
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        db.write_sync(_boom)
    assert calls == []

    # Следующая успешная запись после отката всё же зовёт колбэк как обычно.
    db.write_sync(lambda conn: _insert_channel(conn, "after-boom"))
    assert calls == [1]


def test_on_commit_exception_does_not_break_write_or_next_write(db):
    def _boom_callback() -> None:
        raise RuntimeError("callback boom")

    calls = []
    db.on_commit(_boom_callback)  # регистрируем ПЕРЕД рабочим колбэком — он не должен его заблокировать
    db.on_commit(lambda: calls.append(1))

    result = db.write_sync(lambda conn: _insert_channel(conn, "y"))
    assert result  # успешная запись не превращается в ошибку из-за упавшего колбэка
    assert calls == [1]  # остальные колбэки всё равно вызваны

    result2 = db.write_sync(lambda conn: _insert_channel(conn, "z"))
    assert result2  # следующая запись после упавшего колбэка работает как обычно
    assert calls == [1, 1]
