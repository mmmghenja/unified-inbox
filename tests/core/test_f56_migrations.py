"""Ф56-1а (docs/architecture.md): миграции 0078–0080 — очередь канала владельца
(`owner_item`), переключатель сессии хаба `via_agent`, привязка заявок к сессии хаба (`hub_session_id`)."""

import sqlite3
from pathlib import Path

import pytest

from hub.core import db as db_module

ROUND2 = Path(__file__).resolve().parents[1] / "connectors" / "telegram" / "test_critic_round2.py"


def _columns(db, table: str) -> dict[str, tuple]:
    with db.reader() as conn:
        return {r[1]: r for r in conn.execute(f"PRAGMA main.table_info({table})")}  # noqa: S608


def test_files_one_per_number_and_main_only():
    names = sorted(p.name for p in db_module._MIGRATIONS_DIR.glob("00[78][0-9]_*.sql"))
    assert "0078_main.sql" in names and "0079_main.sql" in names and "0080_main.sql" in names
    for number in ("0078", "0079", "0080"):
        assert [n for n in names if n.startswith(number)] == [f"{number}_main.sql"]


def test_owner_item_table(db):
    cols = _columns(db, "owner_item")
    assert list(cols) == [
        "id",
        "ref",
        "kind",
        "project_id",
        "session_id",
        "created_ts",
        "closed_ts",
        "closed_by",
        "closed_token_id",
        "hold_ts",
        "payload_json",
        "seq",
    ]
    assert cols["ref"][3] == 1 and cols["kind"][3] == 1 and cols["created_ts"][3] == 1 and cols["seq"][3] == 1
    with db.reader() as conn:
        indexes = {r[1] for r in conn.execute("PRAGMA main.index_list(owner_item)")}
    assert {"owner_item_ref", "owner_item_closed"} <= indexes

    def _insert(by):
        return lambda conn: conn.execute(
            "INSERT INTO main.owner_item (ref, kind, created_ts, closed_ts, closed_by, seq) "
            "VALUES (?, ?, 1, 2, ?, 1)",
            ("req:1", "permission", by),
        )

    for by in ("ui", "agent", "system", None):
        db.write_sync(_insert(by))
    with pytest.raises(sqlite3.IntegrityError):
        db.write_sync(_insert("bot"))


def test_via_agent_column(db):
    col = _columns(db, "hub_session")["via_agent"]
    assert col[3] == 1 and col[4] == "0"  # NOT NULL DEFAULT 0
    with db.reader() as conn:
        sql = conn.execute("SELECT sql FROM main.sqlite_master WHERE name = 'hub_session'").fetchone()[0]
    assert "via_agent IN (0, 1)" in sql


@pytest.mark.parametrize("table", ["join_request", "leave_request", "rule_request"])
def test_hub_session_id_columns(db, table):
    col = _columns(db, table)["hub_session_id"]
    assert col[2] == "INTEGER" and col[3] == 0 and col[4] is None


def test_round2_migration_test_drops_every_new_column():
    """Мина проекта: каждый новый столбец — строка `DROP COLUMN` в тесте «миграция с версии 4»."""
    text = ROUND2.read_text(encoding="utf-8")
    assert "ALTER TABLE hub_session DROP COLUMN via_agent" in text
    assert "DROP COLUMN hub_session_id" in text
    for table in ("join_request", "leave_request", "rule_request"):
        assert f'"{table}"' in text
