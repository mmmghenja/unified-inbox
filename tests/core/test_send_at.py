"""Ф30-1: отправка к точному времени (`send_at`) и прогрев суточного лимита графика
(`docs/architecture.md` и 6, поправки П1, П2, П4, П6, П7, П9).
Время и случайность — подменные."""

import datetime as dt
import json
import random
import zoneinfo

import pytest

from hub.core import db as db_module
from hub.core import outbox_queries, pace
from hub.core.db import Db
from hub.core.ingest import apply_batch
from hub.core.outbox import (
    SEND_AT_GRACE_MS,
    SEND_AT_LATE_ERROR,
    Approved,
    OutboxError,
    SendAtPassed,
    approve,
    approve_mode,
    cancel,
    enqueue,
    lease,
    report,
    resolve_unknown,
)

MSK = zoneinfo.ZoneInfo("Europe/Moscow")
OWNER = "connector:1"
HOUR_MS = 60 * 60 * 1000
MIN_MS = 60 * 1000


def msk(hour: int, minute: int = 0, *, day: int = 2, month: int = 10) -> int:
    """Октябрь 2026 по времени хаба -> мс эпохи (02.10 — пятница)."""
    return int(dt.datetime(2026, month, day, hour, minute, tzinfo=MSK).timestamp() * 1000)


NOW = msk(9)
AT = msk(10, day=5)  # «к 10:00 в день вебинара»


def _account(db, *, config=None, db_name="main", idempotent=True) -> int:
    cfg = {"idempotent_send": True} if idempotent else {}
    cfg.update(config or {})
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, config_json) VALUES ('telegram', ?, ?)",
                (db_name, json.dumps(cfg)),
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


def _channel(db, account_id, ext_id, db_name="main") -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute(
                f"INSERT INTO {db_name}.channel (account_id, ext_id, kind, my_msg_count, first_seen_ts) "  # noqa: S608
                "VALUES (?, ?, 'group', 1, 0)",
                (account_id, ext_id),
            ).lastrowid
        )
    )


def _draft(db, channel_id, token_id, *, send_at="2026-10-05 10:00", now_ms=NOW, paced=False, origin="api"):
    return db.write_sync(
        lambda conn: enqueue(
            conn,
            channel_id=channel_id,
            text="Через час начинаем вебинар",
            origin=origin,
            api_token_id=token_id,
            idempotency_key=None,
            now_ms=now_ms,
            paced=paced,
            send_at=send_at,
        )
    )


def _ui_reply(db, channel_id, token_id, now_ms):
    return db.write_sync(
        lambda conn: enqueue(
            conn,
            channel_id=channel_id,
            text="Ответ владельца",
            origin="ui",
            api_token_id=token_id,
            idempotency_key=None,
            now_ms=now_ms,
        )
    )


def _approve(db, outbox_id, *, now_ms, send_now=False, text=None):
    return db.write_sync(
        lambda conn: approve_mode(
            conn, outbox_id, text=text, now_ms=now_ms, rng=random.Random(7), send_now=send_now
        )
    )


def _lease(db, account_id, *, now_ms):
    return db.write_sync(
        lambda conn: lease(conn, account_id=account_id, owner=OWNER, now_ms=now_ms, rng=random.Random(3))
    )


def _report(db, item, outcome, *, now_ms, **kw):
    db.write_sync(
        lambda conn: report(
            conn,
            outbox_id=item.outbox_id,
            owner=OWNER,
            lease_gen=item.lease_gen,
            outcome=outcome,
            now_ms=now_ms,
            **kw,
        )
    )


def _row(db, outbox_id, schema="main") -> dict:
    cols = ("status", "not_before_ts", "send_at_ts", "pace_slot_ts", "paced", "error", "text")
    with db.reader() as conn:
        row = conn.execute(
            f"SELECT {', '.join(cols)} FROM {schema}.outbox WHERE id = ?",  # noqa: S608
            (outbox_id,),
        ).fetchone()
    return dict(zip(cols, row, strict=True))


def _audit(db, action) -> list[tuple]:
    with db.reader() as conn:
        return conn.execute(
            "SELECT outbox_id, result, detail_json FROM main.audit WHERE action = ? ORDER BY id", (action,)
        ).fetchall()


def _has_leasable(db, account_id, now_ms) -> bool:
    with db.reader() as conn:
        return outbox_queries.has_leasable(conn, account_id=account_id, now_ms=now_ms)


@pytest.fixture
def stand(db):
    account_id = _account(db)
    token_id = _token(db)
    channels = [_channel(db, account_id, f"g{i}") for i in range(4)]
    return {"account": account_id, "token": token_id, "channels": channels}


# --- D1. enqueue -------------------------------------------------------------------------------------------


def test_d1_send_at_from_ui_is_refused(db, stand):
    with pytest.raises(OutboxError, match="send_at"):
        _draft(db, stand["channels"][0], stand["token"], origin="ui")


def test_d1_send_at_with_paced_is_refused(db, stand):
    with pytest.raises(OutboxError, match="paced"):
        _draft(db, stand["channels"][0], stand["token"], paced=True)


@pytest.mark.parametrize(
    "value",
    [
        "2026-10-02 08:00",  # прошлое
        "2026-10-02 09:00",  # «сейчас» — раньше now + 1 мин
        "2026-11-02 09:01",  # дальше 30 дней
        "завтра в 10",
        "2026-10-05",  # без времени
        "2026-13-05 10:00",  # нет такого месяца
        "",
    ],
)
def test_d1_bad_send_at_is_refused_and_nothing_written(db, stand, value):
    with pytest.raises(OutboxError, match="send_at"):
        _draft(db, stand["channels"][0], stand["token"], send_at=value)
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.outbox").fetchone()[0] == 0


def test_d1_bounds_are_inclusive(db, stand):
    near = _draft(db, stand["channels"][0], stand["token"], send_at="2026-10-02 09:01")
    far = _draft(db, stand["channels"][1], stand["token"], send_at="2026-11-01 09:00")
    assert _row(db, near.outbox_id)["send_at_ts"] == NOW + MIN_MS
    assert _row(db, far.outbox_id)["send_at_ts"] == NOW + 30 * 24 * HOUR_MS


@pytest.mark.parametrize(
    "value", ["2026-10-05 10:00", "2026-10-05T10:00", "2026-10-05T10:00:00+03:00", "2026-10-05T07:00Z"]
)
def test_d1_msk_and_iso_with_offset_give_same_ts(db, stand, value):
    created = _draft(db, stand["channels"][0], stand["token"], send_at=value)
    row = _row(db, created.outbox_id)
    assert (row["status"], row["send_at_ts"], row["paced"]) == ("pending_approval", AT, 0)
    assert created.paced is False


def test_d1_local_time_is_account_pace_tz(db):
    account_id = _account(db, config={"pace_tz": "Asia/Yekaterinburg"})
    channel_id = _channel(db, account_id, "ekb")
    created = _draft(db, channel_id, _token(db), send_at="2026-10-05 10:00")
    assert _row(db, created.outbox_id)["send_at_ts"] == AT - 2 * HOUR_MS  # Екатеринбург = МСК+2


def test_d1_work_schema(db):
    account_id = _account(db, db_name="work")
    channel_id = _channel(db, account_id, "w", db_name="work")
    created = _draft(db, channel_id, _token(db))
    assert _row(db, created.outbox_id, "work")["send_at_ts"] == AT
    approved = _approve(db, created.outbox_id, now_ms=NOW)
    assert approved == Approved(AT, "send_at", AT)
    assert _row(db, created.outbox_id, "work")["not_before_ts"] == AT


def test_p6_draft_channel_with_send_at_is_refused(db, stand):
    """П6: беседа-заготовка ставит `paced` сама — `send_at` к ней нельзя, даже без входного `paced`."""
    channel_id = _channel(db, stand["account"], "new:tg:abc")
    with pytest.raises(OutboxError, match="send_at"):
        _draft(db, channel_id, stand["token"])


# --- D2. approve -------------------------------------------------------------------------------------------


def test_d2_approve_before_time_queues_at_send_at(db, stand):
    created = _draft(db, stand["channels"][0], stand["token"])
    approved = _approve(db, created.outbox_id, now_ms=NOW)
    assert approved == Approved(AT, "send_at", AT)
    row = _row(db, created.outbox_id)
    assert (row["status"], row["not_before_ts"], row["pace_slot_ts"]) == ("queued", AT, None)
    ((oid, result, detail),) = _audit(db, "outbox_approve")
    assert (oid, result) == (created.outbox_id, "queued")
    assert json.loads(detail) == {"not_before_ts": AT, "send_at_ts": AT, "send_now": False}


def test_d2_approve_wrapper_returns_not_before_ts(db, stand):
    created = _draft(db, stand["channels"][0], stand["token"])
    assert db.write_sync(lambda conn: approve(conn, created.outbox_id, now_ms=NOW)) == AT


@pytest.mark.parametrize("late_ms", [0, MIN_MS, SEND_AT_GRACE_MS])
def test_d2_approve_within_grace_goes_now(db, stand, late_ms):
    created = _draft(db, stand["channels"][0], stand["token"])
    now = AT + late_ms
    assert _approve(db, created.outbox_id, now_ms=now) == Approved(now, "send_at", AT)
    assert _row(db, created.outbox_id)["not_before_ts"] == now
    assert [i.outbox_id for i in _lease(db, stand["account"], now_ms=now)] == [created.outbox_id]


def test_d2_approve_after_grace_is_send_at_passed_and_changes_nothing(db, stand):
    created = _draft(db, stand["channels"][0], stand["token"])
    before = _row(db, created.outbox_id)
    with pytest.raises(SendAtPassed) as info:
        _approve(db, created.outbox_id, now_ms=AT + SEND_AT_GRACE_MS + 1, text="правка")
    assert info.value.send_at_ts == AT
    assert _row(db, created.outbox_id) == before
    assert _audit(db, "outbox_approve") == []


def test_d2_send_now_is_plain_send(db, stand):
    created = _draft(db, stand["channels"][0], stand["token"])
    now = AT + 5 * HOUR_MS
    assert _approve(db, created.outbox_id, now_ms=now, send_now=True) == Approved(None, None, None)
    row = _row(db, created.outbox_id)
    assert (row["status"], row["send_at_ts"], row["not_before_ts"]) == ("queued", None, NOW)
    ((_oid, _result, detail),) = _audit(db, "outbox_approve")
    assert json.loads(detail) == {"send_at_ts": AT, "send_now": True}
    assert [i.outbox_id for i in _lease(db, stand["account"], now_ms=now)] == [created.outbox_id]


def test_d2_send_now_before_time_also_sends_now(db, stand):
    created = _draft(db, stand["channels"][0], stand["token"])
    _approve(db, created.outbox_id, now_ms=NOW, send_now=True)
    assert [i.outbox_id for i in _lease(db, stand["account"], now_ms=NOW)] == [created.outbox_id]


def test_d2_send_now_without_send_at_has_no_effect(db, stand):
    created = _draft(db, stand["channels"][0], stand["token"], send_at=None)
    assert _approve(db, created.outbox_id, now_ms=NOW, send_now=True) == Approved(None, None, None)
    ((_oid, _result, detail),) = _audit(db, "outbox_approve")
    assert detail is None


def test_d2_cancel_queued_send_at(db, stand):
    created = _draft(db, stand["channels"][0], stand["token"])
    _approve(db, created.outbox_id, now_ms=NOW)
    assert db.write_sync(lambda conn: cancel(conn, created.outbox_id, now_ms=NOW + 1)) == "cancelled"
    assert _lease(db, stand["account"], now_ms=AT) == []


# --- D3. lease ---------------------------------------------------------------------------------------------


def test_d3_not_leased_before_send_at_leased_at_it(db, stand):
    created = _draft(db, stand["channels"][0], stand["token"])
    _approve(db, created.outbox_id, now_ms=NOW)
    assert not _has_leasable(db, stand["account"], AT - 1)
    assert _lease(db, stand["account"], now_ms=AT - 1) == []
    assert _has_leasable(db, stand["account"], AT)
    (item,) = _lease(db, stand["account"], now_ms=AT)
    assert (item.outbox_id, item.mode) == (created.outbox_id, "send")


def test_d3_leased_inside_grace(db, stand):
    created = _draft(db, stand["channels"][0], stand["token"])
    _approve(db, created.outbox_id, now_ms=NOW)
    (item,) = _lease(db, stand["account"], now_ms=AT + SEND_AT_GRACE_MS)
    assert item.outbox_id == created.outbox_id


def test_d3_late_never_leased_fails(db, stand):
    created = _draft(db, stand["channels"][0], stand["token"])
    _approve(db, created.outbox_id, now_ms=NOW)
    late = AT + SEND_AT_GRACE_MS + 1
    assert _has_leasable(db, stand["account"], late)
    assert _lease(db, stand["account"], now_ms=late) == []
    row = _row(db, created.outbox_id)
    assert (row["status"], row["error"]) == ("failed", SEND_AT_LATE_ERROR)
    assert _audit(db, "outbox_send_at_late") == [(created.outbox_id, "failed", None)]
    assert not _has_leasable(db, stand["account"], late)


def test_d3_late_after_temporary_fails_and_frees_channel(db, stand):
    """Ревью Ф30-1: после `temporary` коннектор сказал «не ушло» (`maybe_sent=0`) — опоздание -> `failed`,
    а не `unknown`: беседа не держится, ответ владельца в неё уходит."""
    channel_id = stand["channels"][0]
    created = _draft(db, channel_id, stand["token"])
    _approve(db, created.outbox_id, now_ms=NOW)
    (item,) = _lease(db, stand["account"], now_ms=AT)
    _report(db, item, "temporary", now_ms=AT, error="сеть")
    assert _row(db, created.outbox_id)["status"] == "queued"
    late = AT + SEND_AT_GRACE_MS + 1
    assert _lease(db, stand["account"], now_ms=late) == []
    row = _row(db, created.outbox_id)
    assert (row["status"], row["error"]) == ("failed", SEND_AT_LATE_ERROR)
    reply = _ui_reply(db, channel_id, stand["token"], late + 1)
    (item,) = _lease(db, stand["account"], now_ms=late + 1)
    assert item.outbox_id == reply.outbox_id


def test_d3_late_after_maybe_sent_is_unknown(db, stand):
    """Лиз истекал без отчёта (`maybe_sent=1`), потом `temporary` и опоздание — могло уйти -> `unknown`."""
    created = _draft(db, stand["channels"][0], stand["token"])
    _approve(db, created.outbox_id, now_ms=NOW)
    _lease(db, stand["account"], now_ms=AT)  # лиз 60 с, отчёта нет
    (item,) = _lease(db, stand["account"], now_ms=AT + HOUR_MS)
    assert item.maybe_sent is True
    _report(db, item, "temporary", now_ms=AT + HOUR_MS, error="сеть")
    late = AT + SEND_AT_GRACE_MS + 1
    assert _lease(db, stand["account"], now_ms=late) == []
    row = _row(db, created.outbox_id)
    assert (row["status"], row["error"]) == ("unknown", SEND_AT_LATE_ERROR)


def test_d3_late_check_comes_before_fifo(db, stand):
    """Решение 3/A4: опоздавшая строка закрывается и тогда, когда беседу держит более ранняя (`unknown`)."""
    channel = stand["channels"][0]
    blocker = _ui_reply(db, channel, stand["token"], NOW)
    (item,) = _lease(db, stand["account"], now_ms=NOW)
    _report(db, item, "unknown", now_ms=NOW)
    assert _row(db, blocker.outbox_id)["status"] == "unknown"
    created = _draft(db, channel, stand["token"])
    _approve(db, created.outbox_id, now_ms=NOW)
    assert _lease(db, stand["account"], now_ms=AT) == []  # держит `unknown`
    late = AT + SEND_AT_GRACE_MS + 1
    assert _has_leasable(db, stand["account"], late)
    _lease(db, stand["account"], now_ms=late)
    assert _row(db, created.outbox_id)["status"] == "failed"


def test_d3_window_and_daily_limit_do_not_hold_send_at(db):
    account_id = _account(db, config={"pace_window": "10:00-19:00", "pace_daily": 1})
    token_id = _token(db)
    paced_channel, post_channel = _channel(db, account_id, "p"), _channel(db, account_id, "c")
    paced = _draft(db, paced_channel, token_id, send_at=None, paced=True, now_ms=msk(12, day=5))
    db.write_sync(lambda conn: approve(conn, paced.outbox_id, now_ms=msk(12, day=5), rng=random.Random(1)))
    slot = _row(db, paced.outbox_id)["not_before_ts"]  # 24.10: и первая — через случайную паузу
    (item,) = _lease(db, account_id, now_ms=slot)
    _report(db, item, "sent", now_ms=slot, ext_id="e1")  # лимит дня исчерпан
    post = _draft(db, post_channel, token_id, send_at="2026-10-05 22:00", now_ms=msk(12, day=5))
    _approve(db, post.outbox_id, now_ms=msk(12, day=5))
    (item,) = _lease(db, account_id, now_ms=msk(22, day=5))  # ночь, вне окна, лимит исчерпан
    assert item.outbox_id == post.outbox_id
    assert _row(db, post.outbox_id)["not_before_ts"] == msk(22, day=5)


def _incoming(db, account_id, channel_ext, *, ts, ext_id="m1"):
    db.write_sync(
        lambda conn: apply_batch(
            conn,
            account_id=account_id,
            cursor=None,
            now_ms=ts,
            events=[
                {
                    "type": "upsert",
                    "channel": {"ext_id": channel_ext, "kind": "group"},
                    "ext_id": ext_id,
                    "ts": ts,
                    "is_me": False,
                    "kind": "text",
                    "text": "Будет запись?",
                    "mentions_me": False,
                }
            ],
        )
    )


def test_d3_reply_in_group_after_approve_does_not_stop_post(db, stand):
    """`_touch_stale` — только для графика: ответ в группе после подтверждения пост не отменяет."""
    created = _draft(db, stand["channels"][0], stand["token"])
    _approve(db, created.outbox_id, now_ms=NOW)
    _incoming(db, stand["account"], "g0", ts=NOW + HOUR_MS)
    (item,) = _lease(db, stand["account"], now_ms=AT)
    assert item.outbox_id == created.outbox_id
    assert _audit(db, "outbox_pace_stale") == []


def test_p1_future_send_at_does_not_hold_reply(db, stand):
    """П1: пост ждёт своего времени — ответ владельца из UI в ту же беседу уходит сразу."""
    channel = stand["channels"][0]
    post = _draft(db, channel, stand["token"])
    _approve(db, post.outbox_id, now_ms=NOW)
    reply = _ui_reply(db, channel, stand["token"], NOW + MIN_MS)
    assert _has_leasable(db, stand["account"], NOW + MIN_MS)
    (item,) = _lease(db, stand["account"], now_ms=NOW + MIN_MS)
    assert item.outbox_id == reply.outbox_id
    assert _row(db, post.outbox_id)["status"] == "queued"


def test_p1_send_at_in_backoff_holds_fifo(db, stand):
    """П1: время поста наступило, а он в бэкоффе после temporary — порядок держит (по `send_at_ts`, не по
    `not_before_ts`)."""
    channel = stand["channels"][0]
    post = _draft(db, channel, stand["token"])
    _approve(db, post.outbox_id, now_ms=NOW)
    (item,) = _lease(db, stand["account"], now_ms=AT)
    _report(db, item, "temporary", now_ms=AT, error="сеть")
    assert _row(db, post.outbox_id)["not_before_ts"] > AT
    reply = _ui_reply(db, channel, stand["token"], AT + 1)
    assert not _has_leasable(db, stand["account"], AT + 1)
    assert _lease(db, stand["account"], now_ms=AT + 1) == []
    later = _row(db, post.outbox_id)["not_before_ts"]
    items = _lease(db, stand["account"], now_ms=later)
    assert [i.outbox_id for i in items] == [post.outbox_id]
    assert _row(db, reply.outbox_id)["status"] == "queued"


def test_p1_pending_send_at_draft_still_holds_like_before(db, stand):
    """Черновик (`pending_approval`) беседу и раньше не держал — регресс: UI-ответ уходит."""
    channel = stand["channels"][0]
    _draft(db, channel, stand["token"])
    reply = _ui_reply(db, channel, stand["token"], NOW)
    assert [i.outbox_id for i in _lease(db, stand["account"], now_ms=NOW)] == [reply.outbox_id]


# --- П2. resolve_unknown(retry) обнуляет send_at_ts -------------------------------------------------------


def test_p2_retry_of_late_unknown_sends(db, stand):
    created = _draft(db, stand["channels"][0], stand["token"])
    _approve(db, created.outbox_id, now_ms=NOW)
    _lease(db, stand["account"], now_ms=AT)  # лиз истёк без отчёта -> maybe_sent
    late = AT + SEND_AT_GRACE_MS + 1
    _lease(db, stand["account"], now_ms=late)
    assert _row(db, created.outbox_id)["status"] == "unknown"
    db.write_sync(lambda conn: resolve_unknown(conn, created.outbox_id, decision="retry", now_ms=late + 1))
    assert _row(db, created.outbox_id)["send_at_ts"] is None
    (item,) = _lease(db, stand["account"], now_ms=late + 1)
    assert item.outbox_id == created.outbox_id
    assert _row(db, created.outbox_id)["status"] == "sending"


# --- П4. Истёкший лиз опоздавшей строки — не повторная отправка, а unknown ---------------------------------


def test_p4_expired_lease_late_idempotent_is_unknown(db, stand):
    created = _draft(db, stand["channels"][0], stand["token"])
    _approve(db, created.outbox_id, now_ms=NOW)
    (item,) = _lease(db, stand["account"], now_ms=AT)  # лиз 60 с, отчёта нет
    late = AT + SEND_AT_GRACE_MS + 1
    assert _lease(db, stand["account"], now_ms=late) == []
    row = _row(db, created.outbox_id)
    assert (row["status"], row["error"]) == ("unknown", SEND_AT_LATE_ERROR)
    assert _audit(db, "outbox_send_at_late") == [(created.outbox_id, "unknown", None)]


def test_p4_expired_lease_inside_grace_is_resent(db, stand):
    created = _draft(db, stand["channels"][0], stand["token"])
    _approve(db, created.outbox_id, now_ms=NOW)
    _lease(db, stand["account"], now_ms=AT)
    (item,) = _lease(db, stand["account"], now_ms=AT + HOUR_MS)
    assert (item.outbox_id, item.mode, item.maybe_sent) == (created.outbox_id, "send", True)


def test_p4_non_idempotent_late_still_reconciles(db):
    account_id = _account(db, idempotent=False)
    channel_id = _channel(db, account_id, "s")
    created = _draft(db, channel_id, _token(db))
    _approve(db, created.outbox_id, now_ms=NOW)
    _lease(db, account_id, now_ms=AT)
    (item,) = _lease(db, account_id, now_ms=AT + SEND_AT_GRACE_MS + 1)
    assert (item.outbox_id, item.mode) == (created.outbox_id, "reconcile")


# --- D4 + П9. Прогрев --------------------------------------------------------------------------------------


def _settings(db, config: dict) -> pace.Settings:
    account_id = _account(db, config=config)
    with db.reader() as conn:
        return pace.settings_for(conn, account_id)


WARM = {"pace_daily": 40, "pace_warmup_from": "2026-10-02", "pace_warmup_start": 5, "pace_warmup_days": 10}


def test_d4_warmup_curve(db):
    s = _settings(db, WARM)
    day = dt.date(2026, 10, 2)
    assert s.daily_for(day - dt.timedelta(days=1)) == 5
    assert s.daily_for(day) == 5
    assert s.daily_for(day + dt.timedelta(days=1)) == 8
    assert s.daily_for(day + dt.timedelta(days=5)) == 22
    assert s.daily_for(day + dt.timedelta(days=10)) == 40
    assert s.daily_for(day + dt.timedelta(days=30)) == 40
    assert s.warmup_active(day) and not s.warmup_active(day + dt.timedelta(days=10))


def test_d4_without_from_no_warmup(db):
    s = _settings(db, {"pace_daily": 40, "pace_warmup_start": 5, "pace_warmup_days": 10})
    assert s.warmup is None and s.daily_for(dt.date(2026, 10, 2)) == 40


@pytest.mark.parametrize(
    ("extra", "expected", "warns"),
    [
        ({"pace_warmup_from": "вчера"}, None, True),  # кривой from — прогрев выключен
        ({"pace_warmup_from": "2026-02-30"}, None, True),
        ({"pace_warmup_from": 20261002}, None, True),
        ({"pace_warmup_start": "abc"}, pace.Warmup(dt.date(2026, 10, 2), 5, 10), True),
        ({"pace_warmup_start": 0}, pace.Warmup(dt.date(2026, 10, 2), 5, 10), True),
        ({"pace_warmup_start": 100}, pace.Warmup(dt.date(2026, 10, 2), 40, 10), False),  # обрезан до daily
        ({"pace_warmup_days": 0}, pace.Warmup(dt.date(2026, 10, 2), 5, 14), True),
        ({"pace_warmup_days": 500}, pace.Warmup(dt.date(2026, 10, 2), 5, 14), True),
        ({"pace_warmup_days": "x"}, pace.Warmup(dt.date(2026, 10, 2), 5, 14), True),
    ],
)
def test_p9_bad_warmup_values_go_cautious(db, extra, expected, warns, caplog, monkeypatch):
    monkeypatch.setattr(pace, "_warned", set())
    s = _settings(db, {**WARM, **extra})
    assert s.warmup == expected
    assert ("pace_warmup" in caplog.text) is warns


def test_p9_bad_start_with_small_daily(db):
    s = _settings(db, {**WARM, "pace_daily": 3, "pace_warmup_start": "x"})
    assert s.warmup.start == 3 and s.daily_for(dt.date(2026, 10, 2)) == 3


def test_d4_next_slot_takes_tomorrow_with_its_limit(db):
    """Сегодня 5 занято — следующий слот завтра, где лимит 8; девятый завтрашний — послезавтра."""
    account_id = _account(db, config={**WARM, "pace_window": "10:00-19:00"})
    token_id = _token(db)
    channels = [_channel(db, account_id, f"u{i}") for i in range(15)]
    slots = []
    for i, channel in enumerate(channels[:14]):
        created = _draft(db, channel, token_id, send_at=None, paced=True, now_ms=msk(10, i))
        slot = db.write_sync(
            lambda conn, oid=created.outbox_id, i=i: approve(
                conn, oid, now_ms=msk(10, i), rng=random.Random(i)
            )
        )
        slots.append(dt.datetime.fromtimestamp(slot / 1000, MSK).date())
    assert slots[:5] == [dt.date(2026, 10, 2)] * 5
    assert slots[5:13] == [dt.date(2026, 10, 3)] * 8
    assert slots[13] == dt.date(2026, 10, 4)
    with db.reader() as conn:
        assert pace.today(conn, account_id, msk(12)) == (5, 5)
        assert pace.today(conn, account_id, msk(12, day=3)) == (8, 8)
        assert pace.warmup_status(conn, account_id, msk(12)) == {
            "from": "2026-10-02",
            "start": 5,
            "days": 10,
            "target": 40,
        }
        assert pace.warmup_status(conn, account_id, msk(12, day=12)) is None


@pytest.mark.parametrize(("config", "leased"), [(WARM, False), ({"pace_daily": 40}, True)])
def test_d4_lease_daily_reason_uses_warmup(db, config, leased):
    """П2 Ф12-2 при выдаче — тоже по лимиту дня с прогревом: сегодня (02.10) прогрев даёт 1, без него — 40."""
    account_id = _account(db, config={**config, "pace_warmup_start": 1})
    token_id = _token(db)
    first, second = _channel(db, account_id, "a"), _channel(db, account_id, "b")
    sent = _draft(db, first, token_id, send_at=None, paced=True, now_ms=msk(12))
    db.write_sync(lambda conn: approve(conn, sent.outbox_id, now_ms=msk(12), rng=random.Random(1)))
    slot = _row(db, sent.outbox_id)["not_before_ts"]  # 24.10: и первая — через случайную паузу
    (item,) = _lease(db, account_id, now_ms=slot)
    _report(db, item, "sent", now_ms=slot, ext_id="x1")
    touch = _draft(db, second, token_id, send_at=None, paced=True, now_ms=msk(12))
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.outbox SET status = 'queued', not_before_ts = ?, pace_slot_ts = ? WHERE id = ?",
            (msk(12, 30), msk(12, 30), touch.outbox_id),
        )
    )
    items = _lease(db, account_id, now_ms=msk(12, 30))
    assert [i.outbox_id for i in items] == ([touch.outbox_id] if leased else [])
    if not leased:
        (detail,) = [json.loads(d) for _o, _r, d in _audit(db, "outbox_pace_reslot")]
        assert detail["reason"] == "daily"
        assert dt.datetime.fromtimestamp(detail["not_before_ts"] / 1000, MSK).date() == dt.date(2026, 10, 3)


# --- D6. Миграция 0043 на копии БД, обе схемы --------------------------------------------------------------


def test_d6_migration_0043_on_previous_version_both_files(tmp_path, monkeypatch):
    original = db_module._migrations

    def _up_to_42(*, main: bool):
        return {v: files for v, files in original(main=main).items() if v <= 42}

    monkeypatch.setattr(db_module, "_migrations", _up_to_42)
    old = Db(tmp_path)
    old.start()
    old_ids = {}
    try:
        token_id = _token(old)
        for schema in ("main", "work"):
            account_id = _account(old, db_name=schema)
            channel_id = _channel(old, account_id, "c1", db_name=schema)
            created = _draft(old, channel_id, token_id, send_at=None)
            old_ids[schema] = created.outbox_id
        with old.reader() as conn:
            for schema in ("main", "work"):
                assert conn.execute(f"PRAGMA {schema}.user_version").fetchone()[0] == 42
                cols = {r[1] for r in conn.execute(f"PRAGMA {schema}.table_info(outbox)")}
                assert "send_at_ts" not in cols
    finally:
        old.close()

    monkeypatch.setattr(db_module, "_migrations", original)
    new = Db(tmp_path)
    new.start()
    try:
        with new.reader() as conn:
            for schema in ("main", "work"):
                assert conn.execute(f"PRAGMA {schema}.user_version").fetchone()[0] >= 43
        for schema in ("main", "work"):
            row = _row(new, old_ids[schema], schema)
            assert (row["status"], row["send_at_ts"], row["text"]) == (
                "pending_approval",
                None,
                "Через час начинаем вебинар",
            )
            # Старая строка подтверждается как раньше.
            assert _approve(new, old_ids[schema], now_ms=NOW) == Approved(None, None, None)
    finally:
        new.close()
