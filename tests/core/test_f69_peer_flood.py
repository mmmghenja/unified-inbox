"""Ф69-1: PeerFlood Telegram — стоп только первых сообщений новым людям
(docs/architecture.md, A–D, поправки П1–П9): признак «новый человек», исход отчёта,
выдача при стопе (`lease` = `has_leasable`), снятие."""

import datetime as dt
import json
import random
import zoneinfo

import pytest

from hub.core import admin, outbox_queries, peer_flood
from hub.core.db import next_seq
from hub.core.outbox import approve, enqueue, lease, report
from hub.watch import evaluate as watch_evaluate

OWNER = "connector:1"
MSK = zoneinfo.ZoneInfo("Europe/Moscow")
NOW = int(dt.datetime(2026, 10, 14, 12, 0, tzinfo=MSK).timestamp() * 1000)  # среда, внутри окна графика


def _account(db, *, source="telegram") -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, title, db, config_json) VALUES (?, 'Личный', 'main', ?)",
                (source, json.dumps({"idempotent_send": True})),
            ).lastrowid
        )
    )


def _token(db, kind="ui") -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.api_token (name, kind, hash) VALUES ('t', ?, 'x')", (kind,)
            ).lastrowid
        )
    )


def _channel(db, account_id, ext_id, *, kind="dm", meta=None) -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.channel (account_id, ext_id, kind, my_msg_count, first_seen_ts, meta_json) "
                "VALUES (?, ?, ?, 1, 0, ?)",
                (account_id, ext_id, kind, json.dumps(meta) if meta is not None else None),
            ).lastrowid
        )
    )


def _inbound(db, account_id, channel_id, *, kind="text", is_me=0, ext_id="in1") -> None:
    def _fn(conn):
        conn.execute(
            "INSERT INTO main.message (seq, first_seq, account_id, channel_id, ext_id, ts, is_me, kind, "
            "text) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?, 'привет')",
            (seq := next_seq(conn), seq, account_id, channel_id, ext_id, is_me, kind),
        )

    db.write_sync(_fn)


def _queued(db, channel_id, token_id, *, text="hi", now_ms=NOW) -> int:
    return db.write_sync(
        lambda conn: enqueue(
            conn,
            channel_id=channel_id,
            text=text,
            origin="ui",
            api_token_id=token_id,
            idempotency_key=None,
            now_ms=now_ms,
        )
    ).outbox_id


def _lease(db, account_id, *, now_ms=NOW):
    return db.write_sync(
        lambda conn: lease(conn, account_id=account_id, owner=OWNER, now_ms=now_ms, rng=random.Random(3))
    )


def _has(db, account_id, *, now_ms=NOW) -> bool:
    with db.reader() as conn:
        return outbox_queries.has_leasable(conn, account_id=account_id, now_ms=now_ms)


def _report(db, item, outcome, *, error=None, now_ms=NOW, **kw) -> None:
    db.write_sync(
        lambda conn: report(
            conn,
            outbox_id=item.outbox_id,
            owner=OWNER,
            lease_gen=item.lease_gen,
            outcome=outcome,
            error=error,
            now_ms=now_ms,
            **kw,
        )
    )


def _acc(db, account_id) -> tuple:
    with db.reader() as conn:
        return conn.execute(
            "SELECT status, last_error, peer_flood_ts, flood_until FROM main.account WHERE id = ?",
            (account_id,),
        ).fetchone()


def _row(db, outbox_id) -> tuple:
    with db.reader() as conn:
        return conn.execute(
            "SELECT status, error, lease_gen, not_before_ts FROM main.outbox WHERE id = ?", (outbox_id,)
        ).fetchone()


def _set_stop(db, account_id, ts=NOW) -> None:
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.account SET peer_flood_ts = ? WHERE id = ?", (ts, account_id))
    )


@pytest.fixture
def stand(db):
    account_id = _account(db)
    token_id = _token(db)
    new_dm = _channel(db, account_id, "101")
    old_dm = _channel(db, account_id, "102")
    _inbound(db, account_id, old_dm)
    group = _channel(db, account_id, "-103", kind="group")
    return {"account": account_id, "token": token_id, "new": new_dm, "old": old_dm, "group": group}


# --- A. Признак «новый человек» -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ext_id", "kind", "meta", "inbound", "expected"),
    [
        ("new:abc", "dm", {"attrs": {"new_to": {"kind": "tg_username", "username": "petr"}}}, None, True),
        ("201", "dm", None, None, True),  # личка без входящих
        ("202", "dm", None, ("text", 0), False),  # человек писал
        ("203", "dm", None, ("event", 0), True),  # служебное — не «писал»
        ("204", "dm", None, ("text", 1), True),  # только мои
        ("-205", "group", None, None, False),
        ("206", "dm", {"attrs": {"tg_type": "bot"}}, None, False),
        (
            "new:bot",
            "dm",
            {"attrs": {"new_to": {"kind": "tg_username", "username": "shop_bot"}}},
            None,
            False,
        ),
        ("207", "dm", {"attrs": {"contact": True}}, None, True),  # контакт без входящих — новый (ревью)
    ],
)
def test_a_is_new_peer(db, ext_id, kind, meta, inbound, expected):
    account_id = _account(db)
    channel_id = _channel(db, account_id, ext_id, kind=kind, meta=meta)
    if inbound is not None:
        _inbound(db, account_id, channel_id, kind=inbound[0], is_me=inbound[1])
    with db.reader() as conn:
        assert peer_flood.is_new_peer(conn, "main", channel_id) is expected


def test_a_not_telegram_is_never_new_peer(db):
    account_id = _account(db, source="whatsapp")
    channel_id = _channel(db, account_id, "301")
    with db.reader() as conn:
        assert peer_flood.is_new_peer(conn, "main", channel_id) is False


# --- B. Исход PeerFlood -------------------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", ["permanent", "temporary"])  # П3: новый и старый коннектор
def test_b_new_peer_fails_row_and_stops_only_new_people(db, stand, outcome):
    oid = _queued(db, stand["new"], stand["token"])
    (item,) = _lease(db, stand["account"])
    _report(db, item, outcome, error="PeerFlood", now_ms=NOW + 5)
    status, error, _gen, _nb = _row(db, oid)
    assert (status, error) == ("failed", peer_flood.FAILED_TEXT)
    assert _acc(db, stand["account"]) == ("ok", None, NOW + 5, None)
    with db.reader() as conn:
        audit = conn.execute(
            "SELECT result, outbox_id, channel_id FROM main.audit WHERE action = 'peer_flood'"
        ).fetchall()
    assert audit == [("new_peers", oid, stand["new"])]


@pytest.mark.parametrize("outcome", ["permanent", "temporary"])
def test_b_old_peer_fails_row_and_limits_account(db, stand, outcome):
    oid = _queued(db, stand["old"], stand["token"])
    (item,) = _lease(db, stand["account"])
    _report(db, item, outcome, error="PeerFlood")
    assert _row(db, oid)[:2] == ("failed", peer_flood.FAILED_TEXT)
    assert _acc(db, stand["account"]) == ("limited", "PeerFlood", None, None)
    with db.reader() as conn:
        assert conn.execute("SELECT result FROM main.audit WHERE action = 'peer_flood'").fetchone() == (
            "account",
        )


def test_b_second_peer_flood_keeps_first_stop_time(db, stand):
    _set_stop(db, stand["account"], NOW - 1000)
    db.write_sync(
        lambda conn: peer_flood.apply_report(
            conn, "main", account_id=stand["account"], channel_id=stand["new"], now_ms=NOW
        )
    )
    assert _acc(db, stand["account"])[2] == NOW - 1000


def test_b_other_temporary_errors_unchanged(db, stand):
    oid = _queued(db, stand["new"], stand["token"])
    (item,) = _lease(db, stand["account"])
    _report(db, item, "temporary", error="network")
    assert _row(db, oid)[0] == "queued"
    assert _acc(db, stand["account"])[2] is None


# --- C. Выдача при стопе (П1) -------------------------------------------------------------------------------


def test_c_lease_holds_new_people_and_gives_the_rest(db, stand):
    held = _queued(db, stand["new"], stand["token"])
    old = _queued(db, stand["old"], stand["token"])
    group = _queued(db, stand["group"], stand["token"])
    _set_stop(db, stand["account"])
    before = _row(db, held)
    assert _has(db, stand["account"]) is True
    items = _lease(db, stand["account"])
    assert sorted(i.outbox_id for i in items) == sorted([old, group])
    assert _row(db, held) == before  # не тронута: `queued`, поколение то же
    assert _has(db, stand["account"]) is False  # П8: в очереди только задержанные
    assert _lease(db, stand["account"]) == []


def test_c_without_stop_new_people_go(db, stand):
    held = _queued(db, stand["new"], stand["token"])
    assert [i.outbox_id for i in _lease(db, stand["account"])] == [held]


def test_c_has_leasable_matches_lease_only_held(db, stand):
    _queued(db, stand["new"], stand["token"])
    _set_stop(db, stand["account"])
    assert _has(db, stand["account"]) is False
    assert _lease(db, stand["account"]) == []


def test_c_paced_held_row_gets_no_new_slot_and_no_audit(db, stand):
    """П8: задержанная строка графика не получает новый слот, аудит и сдвиг `seq` при повторных `lease`."""
    api_token = _token(db, kind="interactive")
    created = db.write_sync(
        lambda conn: enqueue(
            conn, channel_id=stand["new"], text="касание", origin="api", api_token_id=api_token,
            idempotency_key=None, now_ms=NOW, paced=True,
        )
    )  # fmt: skip
    db.write_sync(lambda conn: approve(conn, created.outbox_id, now_ms=NOW, rng=random.Random(1)))
    _set_stop(db, stand["account"])

    def _snapshot():
        with db.reader() as conn:
            row = conn.execute(
                "SELECT status, not_before_ts, pace_slot_ts, lease_gen FROM main.outbox WHERE id = ?",
                (created.outbox_id,),
            ).fetchone()
            seq = conn.execute(
                "SELECT seq FROM main.message WHERE outbox_id = ?", (created.outbox_id,)
            ).fetchone()
            audit = conn.execute(
                "SELECT COUNT(*) FROM main.audit WHERE action LIKE 'outbox_pace%'"
            ).fetchone()
        return row, seq, audit

    first = _snapshot()
    for step in range(3):
        # 20–22 ч — вне окна 10–19: без стопа строка графика получала бы новый слот на каждой выдаче
        assert _lease(db, stand["account"], now_ms=NOW + 3_600_000 * (8 + step)) == []
    assert _snapshot() == first
    assert first[0][0] == "queued"


def test_c_expired_lease_branch_unchanged(db, stand):
    """П1: ветку истёкшего лиза стоп не трогает — `has_leasable` и `lease` согласны (нет горячего цикла)."""
    oid = _queued(db, stand["new"], stand["token"])
    (item,) = _lease(db, stand["account"])
    _set_stop(db, stand["account"])
    later = NOW + 10 * 60_000
    assert _has(db, stand["account"], now_ms=later) is True
    (again,) = _lease(db, stand["account"], now_ms=later)
    assert again.outbox_id == oid and again.lease_gen == item.lease_gen + 1


# --- D. Снятие стопа ----------------------------------------------------------------------------------------


def test_d_resume_account_clears_both_and_no_loop(db, stand):
    failed = _queued(db, stand["old"], stand["token"])
    (item,) = _lease(db, stand["account"])
    _report(db, item, "permanent", error="PeerFlood")
    _set_stop(db, stand["account"])
    assert db.write_sync(lambda conn: admin.resume_account(conn, stand["account"])) is True
    assert _acc(db, stand["account"])[:3] == ("ok", "PeerFlood", None)
    assert _lease(db, stand["account"]) == []  # петли нет: поймавшая строка `failed`
    assert _row(db, failed)[0] == "failed"


def test_d_resume_ui_clears_only_peer_flood(db, stand):
    rid = stand["account"]
    assert db.write_sync(lambda conn: peer_flood.resume_ui(conn, rid)) == []
    assert db.write_sync(lambda conn: peer_flood.resume_ui(conn, 99999)) is None
    _set_stop(db, rid)
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.account SET status = 'limited', last_error = 'PeerFlood' WHERE id = ?", (rid,)
        )
    )
    assert db.write_sync(lambda conn: peer_flood.resume_ui(conn, rid)) == ["peer_flood", "limited"]
    assert _acc(db, rid) == ("ok", None, None, None)


@pytest.mark.parametrize(
    ("status", "last_error"), [("auth_required", "AuthKeyUnregistered"), ("limited", "x")]
)
def test_d_resume_ui_leaves_other_statuses_and_flood_until(db, stand, status, last_error):
    rid = stand["account"]
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.account SET status = ?, last_error = ?, flood_until = ? WHERE id = ?",
            (status, last_error, NOW + 1000, rid),
        )
    )
    assert db.write_sync(lambda conn: peer_flood.resume_ui(conn, rid)) == []
    assert _acc(db, rid) == (status, last_error, None, NOW + 1000)


def test_d_stop_and_resume_bump_held_rows_seq(db, stand):
    held = _queued(db, stand["new"], stand["token"])
    other = _queued(db, stand["group"], stand["token"])

    def _seqs():
        with db.reader() as conn:
            return {
                oid: conn.execute("SELECT seq FROM main.message WHERE outbox_id = ?", (oid,)).fetchone()[0]
                for oid in (held, other)
            }

    start = _seqs()
    db.write_sync(
        lambda conn: peer_flood.apply_report(
            conn, "main", account_id=stand["account"], channel_id=stand["new"], now_ms=NOW
        )
    )
    after_stop = _seqs()
    assert after_stop[held] > start[held] and after_stop[other] == start[other]
    db.write_sync(lambda conn: peer_flood.resume_ui(conn, stand["account"]))
    assert _seqs()[held] > after_stop[held]


def _incoming(db, stand, ext_id, *, is_me=False, channel_ext="101", channel_kind="dm"):
    from hub.core.ingest import apply_batch  # noqa: PLC0415

    event = {
        "type": "upsert",
        "channel": {"ext_id": channel_ext, "kind": channel_kind},
        "ext_id": ext_id,
        "ts": 5000,
        "is_me": is_me,
        "kind": "text",
        "text": "привет",
        "mentions_me": False,
        "thread_ext_id": None,
        "thread_broadcast": False,
    }
    return db.write_sync(
        lambda conn: apply_batch(conn, account_id=stand["account"], events=[event], cursor=None, now_ms=NOW)
    )


def _seq_of(db, outbox_id) -> int:
    with db.reader() as conn:
        return conn.execute("SELECT seq FROM main.message WHERE outbox_id = ?", (outbox_id,)).fetchone()[0]


def test_f66_c_inbound_from_new_peer_bumps_held_rows_under_stop(db, stand):
    """Ф66-3 C: человек написал первым при стопе — строка исходящего получает новый seq, бейдж снят."""
    held = _queued(db, stand["new"], stand["token"])
    other = _queued(db, stand["group"], stand["token"])
    _set_stop(db, stand["account"])
    start_held, start_other = _seq_of(db, held), _seq_of(db, other)
    with db.reader() as conn:
        assert peer_flood.held_of(conn, held) == peer_flood.HELD
    _incoming(db, stand, "in-new")
    assert _seq_of(db, held) > start_held
    assert _seq_of(db, other) == start_other
    with db.reader() as conn:
        assert peer_flood.held_of(conn, held) is None


def test_f66_c_inbound_without_stop_or_own_message_keeps_seq(db, stand):
    held = _queued(db, stand["new"], stand["token"])
    start = _seq_of(db, held)
    _incoming(db, stand, "in-a")  # стопа нет
    assert _seq_of(db, held) == start
    other_new = _queued(db, stand["new"], stand["token"], text="ещё")
    # Стоп стоит, но уже старая беседа и мои сообщения — сдвига нет.
    _set_stop(db, stand["account"])
    start2 = _seq_of(db, other_new)
    _incoming(db, stand, "in-b", is_me=True, channel_ext="102")
    _incoming(db, stand, "in-c", channel_ext="102")
    assert _seq_of(db, other_new) == start2


# --- E. Видно владельцу ------------------------------------------------------------------------------------------


def test_e_held_of(db, stand):
    held = _queued(db, stand["new"], stand["token"])
    old = _queued(db, stand["old"], stand["token"])
    with db.reader() as conn:
        assert peer_flood.held_of(conn, held) is None
    _set_stop(db, stand["account"])
    with db.reader() as conn:
        assert peer_flood.held_of(conn, held) == "peer_flood"
        assert peer_flood.held_of(conn, old) is None


def test_e_watch_problem():
    snapshot = {
        "accounts": [
            {"id": 1, "source": "telegram", "title": "Личный", "status": "ok", "peer_flood_ts": NOW},
            {"id": 2, "source": "telegram", "title": "Второй", "status": "ok", "peer_flood_ts": None},
        ]
    }
    problems = {p.key: p.text for p in watch_evaluate(snapshot, now_ms=NOW)}
    assert problems == {"account:1:peer_flood": "telegram Личный: стоп первых сообщений новым людям"}
