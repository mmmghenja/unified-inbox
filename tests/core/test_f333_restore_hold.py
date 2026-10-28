"""Ф33-3 (К12): после восстановления из копии строки очереди, бывшие в пути, не уходят повторно — до решения
владельца они «могло уйти» (`unknown`). `outbox.hold_after_restore` и `core/restore.hold_outbox` (его зовёт
`hub backup --hold-outbox` из `scripts/hub-restore.sh`)."""

import fcntl
import itertools
import json
import os

import pytest

from hub.core import group_ops, presses, restore, runner
from hub.core.db import Db
from hub.core.outbox import (
    RESTORE_HOLD_ERROR,
    SEND_AT_REPLIED_ERROR,
    enqueue,
    hold_after_restore,
    lease,
    resolve_unknown,
)

NOW = 1_800_000_000_000


def _account(db, db_name="main") -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, config_json) VALUES ('telegram', ?, ?)",
                (db_name, json.dumps({"idempotent_send": True})),
            ).lastrowid
        )
    )


def _token(db) -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.api_token (name, kind, hash) VALUES ('ui', 'interactive', 'x')"
            ).lastrowid
        )
    )


def _channel(db, account_id, ext_id, db_name="main") -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute(
                f"INSERT INTO {db_name}.channel (account_id, ext_id, kind, my_msg_count, first_seen_ts) "  # noqa: S608
                "VALUES (?, ?, 'dm', 1, 0)",
                (account_id, ext_id),
            ).lastrowid
        )
    )


def _row(db, schema, status, *, account_id, channel_id, token_id, text) -> int:
    def fn(conn):
        res = enqueue(
            conn,
            channel_id=channel_id,
            text=text,
            origin="ui",
            api_token_id=token_id,
            idempotency_key=None,
            now_ms=NOW,
        )
        conn.execute(
            f"UPDATE {schema}.outbox SET status = ?, lease_owner = ?, lease_expires_ts = ? WHERE id = ?",  # noqa: S608
            (status, "connector:1" if status == "sending" else None, NOW + 1 if status == "sending" else None,
             res.outbox_id),
        )  # fmt: skip
        return res.outbox_id

    return db.write_sync(fn)


def _status(db, outbox_id, schema):
    with db.reader() as conn:
        return conn.execute(
            f"SELECT status, error, maybe_sent, lease_owner FROM {schema}.outbox WHERE id = ?",  # noqa: S608
            (outbox_id,),
        ).fetchone()


@pytest.fixture
def rows(db):
    token = _token(db)
    out = {}
    for schema in ("main", "work"):
        acc = _account(db, schema)
        ch = _channel(db, acc, f"peer-{schema}", schema)
        for status in ("queued", "sending", "unknown", "pending_approval", "sent", "failed"):
            out[(schema, status)] = (
                _row(db, schema, status, account_id=acc, channel_id=ch, token_id=token, text=status),
                acc,
            )
    return out


def test_hold_moves_in_flight_rows_to_unknown(db, rows) -> None:
    held = db.write_sync(lambda conn: hold_after_restore(conn, now_ms=NOW + 5))
    assert held == 4  # queued + sending в каждом из двух файлов

    for (schema, status), (outbox_id, _acc) in rows.items():
        got_status, error, maybe_sent, lease_owner = _status(db, outbox_id, schema)
        if status in ("queued", "sending"):
            assert got_status == "unknown"
            assert error == RESTORE_HOLD_ERROR
            assert maybe_sent == 1
            assert lease_owner is None
        else:
            assert got_status == status, (schema, status)
            assert error != RESTORE_HOLD_ERROR

    with db.reader() as conn:
        audits = conn.execute(
            "SELECT outbox_id, detail_json FROM main.audit WHERE action = 'outbox_restore_hold' ORDER BY id"
        ).fetchall()
    assert len(audits) == 4
    assert {json.loads(d)["from"] for _id, d in audits} == {"queued", "sending"}


def test_held_rows_are_not_leased_until_owner_decides(db, rows) -> None:
    db.write_sync(lambda conn: hold_after_restore(conn, now_ms=NOW + 5))
    for schema in ("main", "work"):
        outbox_id, acc = rows[(schema, "queued")]
        got = db.write_sync(
            lambda conn, acc=acc: lease(conn, account_id=acc, owner="connector:1", limit=10, now_ms=NOW + 10)
        )
        assert got == []

        assert (
            db.write_sync(
                lambda conn, oid=outbox_id: resolve_unknown(conn, oid, decision="not_sent", now_ms=NOW + 20)
            )
            == "failed"
        )


def test_hold_is_idempotent(db, rows) -> None:
    assert db.write_sync(lambda conn: hold_after_restore(conn, now_ms=NOW)) == 4
    assert db.write_sync(lambda conn: hold_after_restore(conn, now_ms=NOW)) == 0


DAY_MS = 24 * 3600 * 1000


def _follow_up(db, channel_id, token, status, send_at_ts) -> int:
    """Догон сессии (`cancel_if_reply`) со сроком `send_at_ts`: статус `status`, срок выдачи — срок."""

    def fn(conn):
        oid = enqueue(
            conn,
            channel_id=channel_id,
            text=f"{status}-{send_at_ts}",
            origin="api",
            api_token_id=token,
            idempotency_key=None,
            now_ms=NOW,
        ).outbox_id
        conn.execute(
            "UPDATE main.outbox SET status = ?, send_at_ts = ?, not_before_ts = ?, cancel_if_reply = 1 "
            "WHERE id = ?",
            (status, send_at_ts, send_at_ts, oid),
        )
        return oid

    return db.write_sync(fn)


def _lease_ids(db, acc, now_ms) -> list[int]:
    got = db.write_sync(lambda conn: lease(conn, account_id=acc, owner="c:1", now_ms=now_ms))
    return [x.outbox_id for x in got]


def test_hold_keeps_future_follow_up_and_holds_due_one(db) -> None:
    """Финальное ревью Ф33 (17.10): `queued` со сроком позже момента восстановления прежняя машина отправить
    не могла — остаётся `queued`, беседу не держит; срок прошёл (и `sending` со сроком) — «могло уйти»."""
    token = _token(db)
    acc = _account(db, "main")
    ch = _channel(db, acc, "peer", "main")
    restore_ts = NOW + 3600_000
    future = _follow_up(db, ch, token, "queued", NOW + 3 * DAY_MS)
    due = _follow_up(db, ch, token, "queued", restore_ts)  # срок ровно в момент восстановления — мог уйти
    sending = _follow_up(db, ch, token, "sending", NOW + 3 * DAY_MS)
    assert db.write_sync(lambda conn: hold_after_restore(conn, now_ms=restore_ts)) == 2
    assert _status(db, future, "main")[:3] == ("queued", None, 0)
    assert _status(db, due, "main")[0] == "unknown"
    assert _status(db, sending, "main")[0] == "unknown"
    for oid in (due, sending):
        db.write_sync(
            lambda conn, oid=oid: resolve_unknown(conn, oid, decision="not_sent", now_ms=restore_ts)
        )
    # новое сообщение владельца в ту же беседу уходит сразу — догон его не держит
    new = db.write_sync(
        lambda conn: (
            enqueue(
                conn,
                channel_id=ch,
                text="привет",
                origin="ui",
                api_token_id=token,
                idempotency_key=None,
                now_ms=restore_ts,
            ).outbox_id
        )
    )
    assert _lease_ids(db, acc, restore_ts + 1000) == [new]
    db.write_sync(lambda conn: conn.execute("UPDATE main.outbox SET status = 'sent' WHERE id = ?", (new,)))
    # в свой срок догон проходит обычные проверки: ответ владельца после подтверждения его отменяет
    assert _lease_ids(db, acc, NOW + 3 * DAY_MS) == []
    assert _status(db, future, "main")[:2] == ("failed", SEND_AT_REPLIED_ERROR)


def test_hold_keeps_future_follow_up_which_goes_at_its_time(db) -> None:
    token = _token(db)
    acc = _account(db, "main")
    ch = _channel(db, acc, "peer", "main")
    oid = _follow_up(db, ch, token, "queued", NOW + DAY_MS)
    assert db.write_sync(lambda conn: hold_after_restore(conn, now_ms=NOW + 3600_000)) == 0
    assert _lease_ids(db, acc, NOW + 3600_000) == []
    assert _lease_ids(db, acc, NOW + DAY_MS) == [oid]


def test_hold_outbox_on_restored_dir(tmp_path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    db = Db(data)
    db.start()
    try:
        token = _token(db)
        acc = _account(db)
        ch = _channel(db, acc, "peer")
        oid = _row(db, "main", "queued", account_id=acc, channel_id=ch, token_id=token, text="hi")
    finally:
        db.close()

    assert restore.hold_outbox(data, now_ms=NOW) == {"held": 1, "presses": 0, "group_ops": 0}

    db = Db(data)
    db.start()
    try:
        assert _status(db, oid, "main")[0] == "unknown"
    finally:
        db.close()


def test_hold_outbox_refuses_without_dbs(tmp_path) -> None:
    with pytest.raises(restore.RestoreError, match="нет файлов БД"):
        restore.hold_outbox(tmp_path, now_ms=NOW)
    assert list(tmp_path.iterdir()) == []  # пустых баз не заводит


def test_hold_outbox_refuses_when_core_lock_held(db, tmp_path) -> None:
    fd = os.open(tmp_path / runner.LOCK_NAME, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(restore.RestoreError, match="работает hub core"):
            restore.hold_outbox(tmp_path, now_ms=NOW)
    finally:
        os.close(fd)


def test_lock_name_matches_runner() -> None:
    assert restore.LOCK_NAME == runner.LOCK_NAME


# --- нажатия кнопок ботов и операции над группами WhatsApp -------------------------------------------------


_PRESS_COL = itertools.count()  # hash(status) случаен на процесс и давал коллизию UNIQUE


def _press(db, status, *, account_id, channel_id) -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.button_press (account_id, channel_id, message_id, ext_id, row, col, text, "
                "by, status, created_ts, updated_ts, lease_gen, lease_until, seq) "
                "VALUES (?, ?, 1, '100', 0, ?, 'OK', 'session', ?, ?, ?, ?, ?, 1)",
                (account_id, channel_id, next(_PRESS_COL), status, NOW, NOW,
                 1 if status == "leased" else 0, NOW + 40_000 if status == "leased" else None),
            ).lastrowid
        )
    )  # fmt: skip


def _press_row(db, press_id):
    with db.reader() as conn:
        return conn.execute(
            "SELECT status, reason, done_ts, lease_until FROM main.button_press WHERE id = ?", (press_id,)
        ).fetchone()


def test_presses_in_flight_become_unknown(db) -> None:
    acc = _account(db)
    ch = _channel(db, acc, "bot")
    ids = {s: _press(db, s, account_id=acc, channel_id=ch) for s in ("pending", "approved", "leased", "done")}

    assert db.write_sync(lambda conn: presses.hold_after_restore(conn, now_ms=NOW + 5)) == 2

    for status in ("approved", "leased"):
        assert _press_row(db, ids[status]) == ("unknown", presses.RESTORE_HOLD_REASON, NOW + 5, None)
    assert _press_row(db, ids["pending"])[0] == "pending"  # карточка владельца — как была
    assert _press_row(db, ids["done"])[0] == "done"
    with db.reader() as conn:
        audits = conn.execute(
            "SELECT detail_json FROM main.audit WHERE action = 'press_restore_hold'"
        ).fetchall()
    assert sorted(json.loads(d)["from"] for (d,) in audits) == ["approved", "leased"]
    assert db.write_sync(lambda conn: presses.hold_after_restore(conn, now_ms=NOW + 6)) == 0


def _op(db, kind, status, *, account_id, first_leased_ts=None, lease_gen=0, origin="session") -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.group_op (account_id, project_id, kind, title, participants_json, origin, "
                "status, auto, created_ts, decided_ts, updated_ts, lease_until_ms, lease_gen, "
                "first_leased_ts, seq) VALUES (?, NULL, ?, 'Клиенты', '[]', ?, ?, 1, ?, ?, ?, ?, ?, ?, 1)",
                (account_id, kind, origin, status, NOW - 100, NOW - 50 if status == "approved" else None, NOW,
                 NOW + 60_000 if first_leased_ts else None, lease_gen, first_leased_ts),
            ).lastrowid
        )
    )  # fmt: skip


def test_group_ops_approved_go_back_to_owner(db) -> None:
    acc = db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, config_json) VALUES ('whatsapp', 'main', '{}')"
            ).lastrowid
        )
    )
    never = _op(db, "create", "approved", account_id=acc)
    leased = _op(db, "create", "approved", account_id=acc, first_leased_ts=NOW - 10, lease_gen=2, origin="ui")
    add = _op(db, "add", "approved", account_id=acc)
    link = _op(db, "link", "approved", account_id=acc)
    pending = _op(db, "create", "pending", account_id=acc)
    done = _op(db, "create", "done", account_id=acc)

    assert db.write_sync(lambda conn: group_ops.hold_after_restore(conn, now_ms=NOW + 5)) == 3

    with db.reader() as conn:
        ops = {i: group_ops.get(conn, i) for i in (never, leased, add, link, pending, done)}
    for i in (never, leased, add):
        op = ops[i]
        assert (op.status, op.error, op.auto, op.decided_ts, op.lease_until_ms) == (
            "pending", group_ops.RESTORE_HOLD_ERROR, 0, None, None
        )
        assert group_ops.op_dict(op)["can_decline"] is True
    # create: шаг после нового «Да» — повтор со сверкой П1 от момента прежнего решения (или первой выдачи).
    assert (ops[never].lease_gen, ops[never].first_leased_ts) == (1, NOW - 50)
    assert (ops[leased].lease_gen, ops[leased].first_leased_ts) == (2, NOW - 10)
    assert (ops[add].lease_gen, ops[add].first_leased_ts) == (0, None)
    assert ops[link].status == "approved"  # чтение — повторяется как есть
    assert ops[pending].error is None and ops[done].status == "done"

    # Новое «Да»: пометка снята, задание create идёт повтором — коннектор сначала ищет уже созданную группу.
    db.write_sync(lambda conn: group_ops.decide(conn, never, "approve", now_ms=NOW + 10))
    with db.reader() as conn:
        assert group_ops.get(conn, never).error is None
    db.write_sync(lambda conn: conn.execute("UPDATE main.group_op SET status = 'done' WHERE id = ?", (link,)))
    jobs = db.write_sync(lambda conn: group_ops.lease(conn, account_id=acc, now_ms=NOW + 10_000_000))
    assert [j["id"] for j in jobs] == [never]
    job = jobs[0]
    assert job["lease_gen"] == 2 and job["first_leased_ts"] == NOW - 50
