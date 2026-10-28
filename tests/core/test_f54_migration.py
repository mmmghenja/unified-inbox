"""Ф54-1 (docs/architecture.md): миграция `0077_main.sql` — таблица
заявок «нужен человек» только в основной базе; с прежней версии (76) по обоим файлам; повторный прогон файла
ничего не ломает (`IF NOT EXISTS`); ограничения столбцов."""

import sqlite3
from pathlib import Path

import pytest

from hub.core import db as db_module
from hub.core.db import Db

MIGRATION = Path(db_module.__file__).parent / "migrations" / "0077_main.sql"
COLUMNS = {
    "id", "project_id", "kind", "site", "url", "doing", "need", "profile", "tab", "shot_sha256", "shot_size",
    "shot_mime", "shot_purged_ts", "expires_ts", "status", "decided_ts", "decided_by", "decided_token_id",
    "author_sub_id", "author_project_id", "author_role_key", "author_role_no", "session_json", "token_id",
    "signaled_ts", "signal_sub_id", "created_ts", "seq",
    "decided_note",  # 0115 (Ф93-2)
}  # fmt: skip


def _tables(conn, schema: str) -> set[str]:
    rows = conn.execute(f"SELECT name FROM {schema}.sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def test_migration_0077_from_previous_version_both_files(tmp_path, monkeypatch):
    original = db_module._migrations

    def _up_to_76(*, main: bool):
        return {v: files for v, files in original(main=main).items() if v <= 76}

    monkeypatch.setattr(db_module, "_migrations", _up_to_76)
    old = Db(tmp_path)
    old.start()
    try:
        project_id = old.write_sync(
            lambda conn: conn.execute("INSERT INTO main.project (name) VALUES ('Hooli')").lastrowid
        )
        with old.reader() as conn:
            assert conn.execute("PRAGMA main.user_version").fetchone()[0] == 76
            before_work = conn.execute("PRAGMA work.user_version").fetchone()[0]
            assert "human_request" not in _tables(conn, "main")
    finally:
        old.close()

    monkeypatch.setattr(db_module, "_migrations", original)
    new = Db(tmp_path)
    new.start()
    try:
        with new.reader() as conn:
            assert conn.execute("PRAGMA main.user_version").fetchone()[0] >= 77
            assert conn.execute("PRAGMA work.user_version").fetchone()[0] >= before_work
            assert "human_request" in _tables(conn, "main")
            assert "human_request" not in _tables(conn, "work")  # заявки рабочих проектов — тоже в основной
            assert {r[1] for r in conn.execute("PRAGMA main.table_info(human_request)")} == COLUMNS
            indexes = {r[1] for r in conn.execute("PRAGMA main.index_list(human_request)")}
            assert {"human_request_seq", "human_request_open", "human_request_author"} <= indexes
            assert conn.execute("SELECT name FROM main.project WHERE id = ?", (project_id,)).fetchone()[0]

        def _insert(conn, **over):
            row = {"project_id": project_id, "kind": "captcha", "need": "Пройти", "status": "pending",
                   "created_ts": 1, "seq": 1, **over}  # fmt: skip
            names = ", ".join(row)
            marks = ", ".join("?" for _ in row)
            return conn.execute(
                f"INSERT INTO main.human_request ({names}) VALUES ({marks})",  # noqa: S608
                list(row.values()),
            ).lastrowid

        assert new.write_sync(_insert) == 1
        for bad in (
            {"kind": "robot"},
            {"status": "approved"},
            {"decided_by": "session"},
            {"project_id": 0},
            {"need": None},
        ):
            with pytest.raises(sqlite3.IntegrityError):
                new.write_sync(lambda conn, bad=bad: _insert(conn, **bad))
        for by in ("ui", "agent", "bot"):
            new.write_sync(lambda conn, by=by: _insert(conn, status="done", decided_by=by))
        # Повторный прогон файла миграции (ядро упало между файлом и записью версии) — без ошибок и потерь.
        lines = MIGRATION.read_text(encoding="utf-8").splitlines()
        script = "\n".join(line for line in lines if not line.startswith("--"))
        statements = [s.strip() for s in script.split(";") if s.strip()]
        assert len(statements) == 6 and all("IF NOT EXISTS" in s for s in statements)

        def _again(conn):
            for statement in statements:
                conn.execute(statement)

        new.write_sync(_again)
        with new.reader() as conn:
            assert conn.execute("SELECT COUNT(*) FROM main.human_request").fetchone()[0] == 4
    finally:
        new.close()


def test_one_file_for_number_0077():
    names = sorted(p.name for p in MIGRATION.parent.glob("0077_*"))
    assert names == ["0077_main.sql"]
