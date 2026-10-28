"""Ф41-2б-1а (docs/architecture.md): миграции 0057 и работа постоянной
сессии на уровне модулей ядра (без приложения)."""

import pytest

from hub.core import held, held_work, hub_sessions, subs
from hub.core.db import WORK_ID_BASE
from hub.core.security import Principal

AUDIT = {"actor": "ui", "api_token_id": None, "client_name": None, "request_id": "r"}
NOW = 1_700_000_000_000
UI = Principal(token_id=1, kind="ui", scopes=frozenset({"read"}), project_ids=None)


def _project(db, tmp_path, name="P") -> int:
    workdir = tmp_path / name
    workdir.mkdir(exist_ok=True)
    pid = db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.project (name, workdir) VALUES (?, ?)", (name, str(workdir))
            ).lastrowid
        )
    )
    db.write_sync(lambda conn: subs.set_project_subs(conn, pid, True, now_ms=0))
    return pid


def _create(db, project_id, **kw) -> held.HubSession:
    kw.setdefault("engine", "claude")
    return db.write_sync(
        lambda conn: hub_sessions.create(conn, project_id=project_id, now_ms=NOW, audit=AUDIT, **kw)
    )


def _get(db, sid) -> held.HubSession:
    with db.reader() as conn:
        return held.get(conn, sid)


def test_migration_tables_in_both_files_and_work_ids_seeded(db):
    with db.reader() as conn:
        for schema in ("main", "work"):
            names = {
                row[0]
                for row in conn.execute(
                    f"SELECT name FROM {schema}.sqlite_master WHERE type = 'table' AND name LIKE 'held_%'"
                )
            }
            # Ф41-3в: `held_file` — id текстом, в счётчиках `sqlite_sequence` её нет.
            assert names == {"held_event", "held_request", "held_say", "held_file"}
        seeded = dict(
            conn.execute("SELECT name, seq FROM work.sqlite_sequence WHERE name LIKE 'held_%'").fetchall()
        )
        assert seeded == dict.fromkeys(("held_event", "held_request", "held_say"), WORK_ID_BASE)
        columns = {row[1] for row in conn.execute("PRAGMA main.table_info(hub_session)")}
    assert {
        "seq",
        "last_event_id",
        "interrupt_requested_ts",
        "sleep_requested_ts",
        "last_turn_ts",
        "launch_fails",
        "failure",
        "store",
        "wait_reason",
    } <= columns


def test_event_kind_and_wait_reason_are_checked_by_schema(db, tmp_path):
    rec = _create(db, _project(db, tmp_path))
    with pytest.raises(Exception, match="CHECK"):
        db.write_sync(
            lambda conn: conn.execute(
                "INSERT INTO main.held_event (session_id, ts, kind, summary) VALUES (?, 0, 'go', '')",
                (rec.id,),
            )
        )
    with pytest.raises(Exception, match="CHECK"):
        db.write_sync(
            lambda conn: conn.execute("UPDATE main.hub_session SET wait_reason = 'x' WHERE id = ?", (rec.id,))
        )
    with pytest.raises(Exception, match="CHECK"):
        db.write_sync(
            lambda conn: conn.execute(
                "INSERT INTO main.held_say (session_id, text, priority, status, created_ts) "
                "VALUES (?, 'x', 'later', 'queued', 0)",
                (rec.id,),
            )
        )


def test_create_fixes_store_and_seq(db, tmp_path):
    rec = _create(db, _project(db, tmp_path))
    assert rec.store == "main" and rec.seq > 0 and rec.launch_fails == 0
    assert (rec.last_event_id, rec.failure, rec.wait_reason) == (None, None, None)


def test_force_asleep_clears_flags_expires_requests_keeps_queue(db, tmp_path):
    rec = _create(db, _project(db, tmp_path))

    def _prepare(conn):
        hub_sessions.set_state(conn, rec.id, state="running", proc_pid=10, proc_start=20, now_ms=NOW)
        held_work.say(conn, rec.id, text="жду", priority="next", now_ms=NOW, audit=AUDIT)
        request_id = held_work.create_request(
            conn,
            rec.id,
            kind="permission",
            tool="Bash",
            tool_input={"command": "ls"},
            reason=None,
            now_ms=NOW,
        )
        held_work.interrupt(conn, rec.id, now_ms=NOW, audit=AUDIT)
        held_work.sleep(conn, rec.id, now_ms=NOW, audit=AUDIT)  # снимает очередь
        held_work.say(conn, rec.id, text="после усыпления", priority="now", now_ms=NOW, audit=AUDIT)
        return request_id

    request_id = db.write_sync(_prepare)
    before = _get(db, rec.id)
    assert before.interrupt_requested_ts == NOW and before.sleep_requested_ts == NOW

    changed = db.write_sync(
        lambda conn: held_work.force_asleep(
            conn, held.get(conn, rec.id), now_ms=NOW + 5, note=held_work.NOTE_RECOVER
        )
    )
    after = _get(db, rec.id)
    assert changed is True and after.seq > before.seq
    assert (after.state, after.proc_pid, after.proc_start) == ("asleep", None, None)
    assert (after.interrupt_requested_ts, after.sleep_requested_ts) == (None, None)
    with db.reader() as conn:
        status = conn.execute("SELECT status FROM main.held_request WHERE id = ?", (request_id,)).fetchone()[
            0
        ]
        assert status == "expired"
        assert [s["text"] for s in held_work.queued_says(conn, after)] == ["после усыпления"]
        assert held_work.counts(conn, after) == (0, 1, 0)  # Ф48-2: третье — прерванных
    # Повтор — менять нечего.
    again = db.write_sync(
        lambda conn: held_work.force_asleep(
            conn, held.get(conn, rec.id), now_ms=NOW + 9, note=held_work.NOTE_RECOVER
        )
    )
    assert again is False and _get(db, rec.id).seq == after.seq


def test_set_state_same_state_does_not_move_seq(db, tmp_path):
    rec = _create(db, _project(db, tmp_path))
    db.write_sync(
        lambda conn: hub_sessions.set_state(
            conn, rec.id, state="idle", proc_pid=10, proc_start=20, now_ms=NOW
        )
    )
    seq = _get(db, rec.id).seq
    for step in range(1, 4):
        db.write_sync(lambda conn, s=step: hub_sessions.set_state(conn, rec.id, state="idle", now_ms=NOW + s))
    confirmed = _get(db, rec.id)
    assert confirmed.seq == seq and confirmed.host_seen_ts == NOW + 3 and confirmed.proc_pid == 10
    db.write_sync(lambda conn: hub_sessions.set_state(conn, rec.id, state="running", now_ms=NOW + 4))
    assert _get(db, rec.id).seq > seq


def test_sync_items_limit_and_scope(db, tmp_path):
    p1, p2 = _project(db, tmp_path, "A"), _project(db, tmp_path, "B")
    ids = [_create(db, p1).id for _ in range(3)] + [_create(db, p2).id]
    with db.reader() as conn:
        watermark = conn.execute("SELECT value FROM main.seq_counter WHERE id = 1").fetchone()[0]
        everything = held_work.sync_items(conn, UI, since=0, watermark=watermark, limit=100)
        capped = held_work.sync_items(conn, UI, since=0, watermark=watermark, limit=2)
        scoped = held_work.sync_items(
            conn,
            Principal(
                token_id=2, kind="interactive", scopes=frozenset({"read"}), project_ids=frozenset({p2})
            ),
            since=0,
            watermark=watermark,
            limit=100,
        )
        below = held_work.sync_items(conn, UI, since=0, watermark=everything[1][0], limit=100)
    assert [item[2]["id"] for item in everything] == ids
    assert [item[1] for item in everything] == ["hub_session"] * 4
    assert [item[2]["id"] for item in capped] == ids[:2]
    assert [item[2]["id"] for item in scoped] == ids[3:]
    assert "engine_session_id" not in scoped[0][2] and "engine_session_id" in everything[0][2]
    assert [item[2]["id"] for item in below] == ids[:2]  # выше водяного знака наружу ничего


def test_migration_0058_adds_usage_column_and_old_rows_have_no_usage(db, tmp_path):
    """Ф41-3 (2.3): столбец расхода; у записи без расхода `usage` — `None`, битое значение — тоже."""
    with db.reader() as conn:
        columns = {row[1]: row[2] for row in conn.execute("PRAGMA main.table_info(hub_session)")}
    assert columns["usage_json"] == "TEXT"
    rec = _create(db, _project(db, tmp_path))
    assert rec.usage_json is None and hub_sessions.usage_of(rec) is None
    for raw in ("не json", "[1]", ""):
        db.write_sync(
            lambda conn, raw=raw: conn.execute(
                "UPDATE main.hub_session SET usage_json = ? WHERE id = ?", (raw, rec.id)
            )
        )
        assert hub_sessions.usage_of(_get(db, rec.id)) is None
    db.write_sync(
        lambda conn: held_work.turn(
            conn, rec.id, {"state": "idle", "usage": {"context_tokens": 5, "turn_cost_usd": 0.5}}, now_ms=NOW
        )
    )
    assert hub_sessions.usage_of(_get(db, rec.id)) == {
        "context_tokens": 5,
        "context_limit": None,
        "cost_usd": 0.5,
        "updated_ts": NOW,
    }
