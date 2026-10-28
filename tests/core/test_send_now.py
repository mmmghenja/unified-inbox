"""«Отправить сейчас» у подтверждённой строки в очереди (слова владельца, `outbox.release_now`): строка
графика или с `send_at` уходит при ближайшей выдаче — вне окна и мимо лимита; уже выданная — отказ; стоп
PeerFlood держит. Время и случайность — подменные."""

import datetime as dt
import json
import random
import zoneinfo

import pytest

from hub.core import outbox_queries
from hub.core.db import next_seq
from hub.core.outbox import NotWaiting, approve, enqueue, lease, release_now, report

MSK = zoneinfo.ZoneInfo("Europe/Moscow")
OWNER = "connector:1"


def msk(hour: int, minute: int = 0, *, day: int = 5) -> int:
    """Октябрь 2026 по времени хаба (05.10 — понедельник)."""
    return int(dt.datetime(2026, 10, day, hour, minute, tzinfo=MSK).timestamp() * 1000)


def _account(db, config=None) -> int:
    cfg = {"idempotent_send": True, **(config or {})}
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, config_json) VALUES ('telegram', 'main', ?)",
                (json.dumps(cfg),),
            ).lastrowid
        )
    )


def _token(db) -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.api_token (name, kind, hash) VALUES ('mcp:iip', 'interactive', 'x')"
            ).lastrowid
        )
    )


def _channel(db, account_id, ext_id) -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.channel (account_id, ext_id, kind, my_msg_count, first_seen_ts) "
                "VALUES (?, ?, 'dm', 1, 0)",
                (account_id, ext_id),
            ).lastrowid
        )
    )


def _inbound(db, account_id, channel_id, *, ts, ext_id="in1") -> None:
    def _fn(conn):
        conn.execute(
            "INSERT INTO main.message (seq, first_seq, account_id, channel_id, ext_id, ts, is_me, kind, "
            "text) VALUES (?, ?, ?, ?, ?, ?, 0, 'text', 'привет')",
            (seq := next_seq(conn), seq, account_id, channel_id, ext_id, ts),
        )

    db.write_sync(_fn)


def _draft(db, channel_id, token_id, *, now_ms, paced=False, send_at=None, cancel_if_reply=False) -> int:
    return db.write_sync(
        lambda conn: enqueue(
            conn,
            channel_id=channel_id,
            text="Добрый день!",
            origin="api",
            api_token_id=token_id,
            idempotency_key=None,
            now_ms=now_ms,
            paced=paced,
            send_at=send_at,
            cancel_if_reply=cancel_if_reply,
        )
    ).outbox_id


def _approve(db, outbox_id, *, now_ms):
    return db.write_sync(lambda conn: approve(conn, outbox_id, now_ms=now_ms, rng=random.Random(5)))


def _release(db, outbox_id, *, now_ms):
    db.write_sync(lambda conn: release_now(conn, outbox_id, now_ms=now_ms))


def _lease(db, account_id, *, now_ms):
    return db.write_sync(
        lambda conn: lease(conn, account_id=account_id, owner=OWNER, now_ms=now_ms, rng=random.Random(3))
    )


def _row(db, outbox_id) -> dict:
    cols = ("status", "paced", "pace_slot_ts", "send_at_ts", "not_before_ts", "cancel_if_reply", "by_hand")
    with db.reader() as conn:
        row = conn.execute(
            f"SELECT {', '.join(cols)} FROM main.outbox WHERE id = ?",  # noqa: S608
            (outbox_id,),
        ).fetchone()
    return dict(zip(cols, row, strict=True))


def _audit(db, action) -> list[tuple]:
    with db.reader() as conn:
        return conn.execute(
            "SELECT outbox_id, result, detail_json FROM main.audit WHERE action = ? ORDER BY id", (action,)
        ).fetchall()


def _seq(db, outbox_id) -> int:
    with db.reader() as conn:
        return conn.execute("SELECT seq FROM main.message WHERE outbox_id = ?", (outbox_id,)).fetchone()[0]


@pytest.fixture
def stand(db):
    account_id = _account(db, config={"pace_daily": 1})
    token_id = _token(db)
    channels = [_channel(db, account_id, f"u{i}") for i in range(4)]
    for channel_id in channels:
        _inbound(db, account_id, channel_id, ts=1)  # старые собеседники: стоп PeerFlood их не держит
    return {"account": account_id, "token": token_id, "channels": channels}


def test_paced_row_out_of_window_goes_now(db, stand):
    now = msk(21)  # вне окна 10–19: слот — завтра утром
    oid = _draft(db, stand["channels"][0], stand["token"], now_ms=now, paced=True)
    slot = _approve(db, oid, now_ms=now)
    assert slot >= msk(10, day=6)
    assert _lease(db, stand["account"], now_ms=now) == []
    before = _seq(db, oid)

    _release(db, oid, now_ms=now)
    row = _row(db, oid)
    assert row == {
        "status": "queued",
        "paced": 0,
        "pace_slot_ts": None,
        "send_at_ts": None,
        "not_before_ts": now,
        "cancel_if_reply": 0,
        "by_hand": 1,
    }
    assert _seq(db, oid) > before  # интерфейс узнаёт через /sync
    ((audit_oid, result, detail),) = _audit(db, "outbox_send_now")
    assert (audit_oid, result) == (oid, "queued")
    assert json.loads(detail) == {"paced": True, "send_at_ts": None, "not_before_ts": slot}
    with db.reader() as conn:
        assert outbox_queries.has_leasable(conn, account_id=stand["account"], now_ms=now)
    (item,) = _lease(db, stand["account"], now_ms=now)
    assert item.outbox_id == oid


def test_paced_row_past_daily_limit_goes_now(db, stand):
    first = _draft(db, stand["channels"][0], stand["token"], now_ms=msk(11), paced=True)
    _approve(db, first, now_ms=msk(11))
    (item,) = _lease(db, stand["account"], now_ms=_row(db, first)["not_before_ts"])
    db.write_sync(
        lambda conn: report(
            conn, outbox_id=first, owner=OWNER, lease_gen=item.lease_gen, outcome="sent", ext_id="e1",
            now_ms=msk(11, 5),
        )
    )  # fmt: skip
    second = _draft(db, stand["channels"][1], stand["token"], now_ms=msk(12), paced=True)
    assert _approve(db, second, now_ms=msk(12)) >= msk(10, day=6)  # лимит дня (1) исчерпан
    _release(db, second, now_ms=msk(12))
    (item,) = _lease(db, stand["account"], now_ms=msk(12))
    assert item.outbox_id == second


def test_send_at_row_with_cancel_if_reply_goes_now_and_reply_does_not_eat_it(db, stand):
    now = msk(9)
    oid = _draft(
        db, stand["channels"][0], stand["token"], now_ms=now, send_at="2026-10-05 22:00", cancel_if_reply=True
    )
    _approve(db, oid, now_ms=now)
    assert _row(db, oid)["send_at_ts"] == msk(22)
    _inbound(db, stand["account"], stand["channels"][0], ts=msk(9, 30), ext_id="in2")  # ответили после
    _release(db, oid, now_ms=msk(10))
    assert (_row(db, oid)["send_at_ts"], _row(db, oid)["cancel_if_reply"]) == (None, 0)
    (item,) = _lease(db, stand["account"], now_ms=msk(10))
    assert item.outbox_id == oid


def test_already_leased_or_sent_or_not_waiting_is_refused(db, stand):
    now = msk(12)
    plain = _draft(db, stand["channels"][0], stand["token"], now_ms=now)
    _approve(db, plain, now_ms=now)
    with pytest.raises(NotWaiting) as exc:
        _release(db, plain, now_ms=now)
    assert "не ждёт" in exc.value.reason

    (item,) = _lease(db, stand["account"], now_ms=now)
    with pytest.raises(NotWaiting) as exc:
        _release(db, plain, now_ms=now)
    assert exc.value.reason == "уже уходит"
    # вернулась в очередь после temporary — уже выдавалась коннектору: могла уйти
    db.write_sync(
        lambda conn: report(
            conn, outbox_id=plain, owner=OWNER, lease_gen=item.lease_gen, outcome="temporary",
            retry_after_ms=600_000, now_ms=now,
        )
    )  # fmt: skip
    assert _row(db, plain)["status"] == "queued"
    with pytest.raises(NotWaiting) as exc:
        _release(db, plain, now_ms=now)
    assert exc.value.reason == "уже уходит"

    pending = _draft(db, stand["channels"][1], stand["token"], now_ms=now, paced=True)
    with pytest.raises(NotWaiting) as exc:
        _release(db, pending, now_ms=now)
    assert exc.value.reason == "не в очереди"
    assert _audit(db, "outbox_send_now") == []


def test_sent_row_is_refused(db, stand):
    now = msk(12)
    oid = _draft(db, stand["channels"][0], stand["token"], now_ms=now)
    _approve(db, oid, now_ms=now)
    (item,) = _lease(db, stand["account"], now_ms=now)
    db.write_sync(
        lambda conn: report(
            conn, outbox_id=oid, owner=OWNER, lease_gen=item.lease_gen, outcome="sent", ext_id="e9",
            now_ms=now,
        )
    )  # fmt: skip
    with pytest.raises(NotWaiting) as exc:
        _release(db, oid, now_ms=now)
    assert exc.value.reason == "уже ушло"


def test_peer_flood_stop_still_holds_released_row(db):
    account_id = _account(db)
    token_id = _token(db)
    new_peer = _channel(db, account_id, "777")  # входящих нет — «новый человек»
    now = msk(21)
    oid = _draft(db, new_peer, token_id, now_ms=now, paced=True)
    _approve(db, oid, now_ms=now)
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.account SET peer_flood_ts = ? WHERE id = ?", (now, account_id))
    )
    _release(db, oid, now_ms=now)
    assert _lease(db, account_id, now_ms=now) == []
    assert _row(db, oid)["status"] == "queued"
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.account SET peer_flood_ts = NULL WHERE id = ?", (account_id,))
    )
    (item,) = _lease(db, account_id, now_ms=now)  # без стопа та же строка уходит сразу
    assert item.outbox_id == oid
