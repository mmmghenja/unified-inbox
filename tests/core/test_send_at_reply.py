"""Ф30-2: догон ко времени не уходит, если в беседе ответили (`cancel_if_reply`,
`docs/architecture.md` и 8, поправки П1–П4).
Время подменное."""

import datetime as dt
import json
import random
import zoneinfo

import pytest

from hub.core import db as db_module
from hub.core import outbox_queries
from hub.core.db import Db
from hub.core.ingest import apply_batch
from hub.core.outbox import (
    PACE_STALE_ERROR,
    SEND_AT_REPLIED_ERROR,
    Approved,
    OutboxError,
    approve,
    approve_mode,
    enqueue,
    lease,
    report,
    resolve_unknown,
)

MSK = zoneinfo.ZoneInfo("Europe/Moscow")
OWNER = "connector:1"
HOUR_MS = 60 * 60 * 1000
MIN_MS = 60 * 1000


def msk(hour: int, minute: int = 0, *, day: int = 2) -> int:
    return int(dt.datetime(2026, 10, day, hour, minute, tzinfo=MSK).timestamp() * 1000)


NOW = msk(9)
APPROVED = NOW + HOUR_MS  # владелец подтвердил догон
AT = msk(11, day=5)  # «догон партнёру 05.10 в 11:00»


def _slack_ts(ms: int) -> str:
    """`ext_id` вида ts Slack: отчёт об отправке ставит `ts` сообщения из него (как у настоящего Slack)."""
    return f"{ms // 1000}.{ms % 1000:03d}000"


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
                "INSERT INTO main.api_token (name, kind, hash) VALUES ('mcp:iip', 'interactive', 'x')"
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


def _draft(db, channel_id, token_id, *, send_at="2026-10-05 11:00", cancel_if_reply=True, now_ms=NOW,
           text="Добрый день! Напоминаю про договор", paced=False, origin="api"):  # fmt: skip
    return db.write_sync(
        lambda conn: enqueue(
            conn,
            channel_id=channel_id,
            text=text,
            origin=origin,
            api_token_id=token_id,
            idempotency_key=None,
            now_ms=now_ms,
            paced=paced,
            send_at=send_at,
            cancel_if_reply=cancel_if_reply,
        )
    )


def _approve(db, outbox_id, *, now_ms=APPROVED, send_now=False):
    return db.write_sync(
        lambda conn: approve_mode(conn, outbox_id, now_ms=now_ms, rng=random.Random(7), send_now=send_now)
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


def _incoming(db, account_id, channel_ext, *, ts, ext_id, is_me=False):
    """Сообщение источника в беседе: собеседника или владельца с телефона (`is_me`, без строки outbox)."""
    db.write_sync(
        lambda conn: apply_batch(
            conn,
            account_id=account_id,
            cursor=None,
            now_ms=ts,
            events=[
                {
                    "type": "upsert",
                    "channel": {"ext_id": channel_ext, "kind": "dm"},
                    "ext_id": ext_id,
                    "ts": ts,
                    "is_me": is_me,
                    "kind": "text",
                    "text": "Спасибо, подписали",
                    "mentions_me": False,
                }
            ],
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


def _row(db, outbox_id, schema="main") -> dict:
    cols = ("status", "error", "send_at_ts", "cancel_if_reply", "not_before_ts")
    with db.reader() as conn:
        row = conn.execute(
            f"SELECT {', '.join(cols)} FROM {schema}.outbox WHERE id = ?",  # noqa: S608
            (outbox_id,),
        ).fetchone()
    return dict(zip(cols, row, strict=True))


def _audit(db, action) -> list[tuple]:
    with db.reader() as conn:
        return conn.execute(
            "SELECT outbox_id, result FROM main.audit WHERE action = ? ORDER BY id", (action,)
        ).fetchall()


def _has_leasable(db, account_id, now_ms) -> bool:
    with db.reader() as conn:
        return outbox_queries.has_leasable(conn, account_id=account_id, now_ms=now_ms)


@pytest.fixture
def stand(db):
    account_id = _account(db)
    return {"account": account_id, "token": _token(db), "channel": _channel(db, account_id, "p1")}


# --- B1. cancel_if_reply только с send_at; сохраняется ------------------------------------------------------


def test_b1_cancel_if_reply_without_send_at_is_refused(db, stand):
    with pytest.raises(OutboxError, match="cancel_if_reply — только вместе с send_at"):
        _draft(db, stand["channel"], stand["token"], send_at=None)
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.outbox").fetchone()[0] == 0


def test_b1_cancel_if_reply_with_send_at_is_stored(db, stand):
    flagged = _draft(db, stand["channel"], stand["token"])
    plain = _draft(db, stand["channel"], stand["token"], cancel_if_reply=False)
    assert _row(db, flagged.outbox_id)["cancel_if_reply"] == 1
    assert _row(db, plain.outbox_id)["cancel_if_reply"] == 0


# --- B2. Ответ после подтверждения — догон не уходит --------------------------------------------------------


@pytest.mark.parametrize("is_me", [False, True], ids=["partner", "owner_phone"])
def test_b2_reply_after_approve_fails_not_leased(db, stand, is_me):
    created = _draft(db, stand["channel"], stand["token"])
    _approve(db, created.outbox_id)
    _incoming(db, stand["account"], "p1", ts=APPROVED + MIN_MS, ext_id="r1", is_me=is_me)
    assert _has_leasable(db, stand["account"], AT)  # `lease` закроет строку
    assert _lease(db, stand["account"], now_ms=AT) == []
    row = _row(db, created.outbox_id)
    assert (row["status"], row["error"]) == ("failed", SEND_AT_REPLIED_ERROR)
    assert _audit(db, "outbox_send_at_replied") == [(created.outbox_id, "failed")]
    assert _audit(db, "outbox_pace_stale") == []
    assert not _has_leasable(db, stand["account"], AT)


def test_b2_no_reply_sends_at_time(db, stand):
    created = _draft(db, stand["channel"], stand["token"])
    _approve(db, created.outbox_id)
    assert _lease(db, stand["account"], now_ms=AT - 1) == []
    (item,) = _lease(db, stand["account"], now_ms=AT)
    assert (item.outbox_id, item.mode) == (created.outbox_id, "send")


def test_b2_reply_before_approve_does_not_stop(db, stand):
    created = _draft(db, stand["channel"], stand["token"])
    _incoming(db, stand["account"], "p1", ts=NOW + MIN_MS, ext_id="r0")  # до подтверждения
    _approve(db, created.outbox_id)
    (item,) = _lease(db, stand["account"], now_ms=AT)
    assert item.outbox_id == created.outbox_id


def test_b2_service_event_is_not_a_reply(db, stand):
    created = _draft(db, stand["channel"], stand["token"])
    _approve(db, created.outbox_id)
    db.write_sync(
        lambda conn: apply_batch(
            conn,
            account_id=stand["account"],
            cursor=None,
            now_ms=APPROVED + MIN_MS,
            events=[
                {
                    "type": "upsert",
                    "channel": {"ext_id": "p1", "kind": "dm"},
                    "ext_id": "ev1",
                    "ts": APPROVED + MIN_MS,
                    "is_me": False,
                    "kind": "event",
                    "text": "собеседник сменил фото",
                    "mentions_me": False,
                }
            ],
        )
    )
    (item,) = _lease(db, stand["account"], now_ms=AT)
    assert item.outbox_id == created.outbox_id


# --- П1. Свои письма хаба от сессий (origin='api') — не ответ -----------------------------------------------


@pytest.mark.parametrize("letter_first", [True, False])
def test_p1_own_api_letter_sent_after_approve_does_not_stop(db, stand, letter_first):
    """Письмо сейчас + догон через 3 дня, подтверждены подряд; письмо ушло после подтверждения догона (ts
    сообщения — из ответа источника) — догон всё равно уходит к сроку."""
    channel = stand["channel"]
    if letter_first:
        letter = _draft(db, channel, stand["token"], send_at=None, cancel_if_reply=False, text="Письмо")
        follow = _draft(db, channel, stand["token"])
    else:
        follow = _draft(db, channel, stand["token"])
        letter = _draft(db, channel, stand["token"], send_at=None, cancel_if_reply=False, text="Письмо")
    _approve(db, follow.outbox_id)
    _approve(db, letter.outbox_id, now_ms=APPROVED + 1)
    sent_at = APPROVED + MIN_MS
    (item,) = _lease(db, stand["account"], now_ms=sent_at)
    assert item.outbox_id == letter.outbox_id
    _report(db, item, "sent", now_ms=sent_at, ext_id=_slack_ts(sent_at))
    with db.reader() as conn:
        (msg_ts,) = conn.execute(
            "SELECT ts FROM main.message WHERE outbox_id = ?", (letter.outbox_id,)
        ).fetchone()
    assert msg_ts == sent_at  # письмо в беседе — после подтверждения догона
    (item,) = _lease(db, stand["account"], now_ms=AT)
    assert item.outbox_id == follow.outbox_id


def test_p1_own_api_letter_enqueued_after_approve_does_not_stop(db, stand):
    """Telegram: `ts` своего сообщения — время черновика; письмо положено и ушло после подтверждения
    догона."""
    follow = _draft(db, stand["channel"], stand["token"])
    _approve(db, follow.outbox_id)
    letter = _draft(db, stand["channel"], stand["token"], send_at=None, cancel_if_reply=False,
                    text="Письмо", now_ms=APPROVED + MIN_MS)  # fmt: skip
    _approve(db, letter.outbox_id, now_ms=APPROVED + 2 * MIN_MS)
    (item,) = _lease(db, stand["account"], now_ms=APPROVED + 2 * MIN_MS)
    _report(db, item, "sent", now_ms=APPROVED + 2 * MIN_MS, ext_id="tg-777")
    (item,) = _lease(db, stand["account"], now_ms=AT)
    assert item.outbox_id == follow.outbox_id


def test_p1_paced_still_counts_own_api_letter(db):
    """График (`paced`) — без изменений: своё письмо хаба после подтверждения касания его отменяет, как
    раньше."""
    account_id = _account(db)
    token_id = _token(db)
    channel = _channel(db, account_id, "u1")
    start = msk(12)
    touch = _draft(db, channel, token_id, send_at=None, cancel_if_reply=False, paced=True, now_ms=start)
    db.write_sync(lambda conn: approve(conn, touch.outbox_id, now_ms=start, rng=random.Random(1)))
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.outbox SET not_before_ts = ?, pace_slot_ts = ? WHERE id = ?",
            (start + HOUR_MS, start + HOUR_MS, touch.outbox_id),
        )
    )
    letter = _draft(db, channel, token_id, send_at=None, cancel_if_reply=False, text="Письмо",
                    now_ms=start + MIN_MS)  # fmt: skip
    _approve(db, letter.outbox_id, now_ms=start + MIN_MS)
    (item,) = _lease(db, account_id, now_ms=start + MIN_MS)
    _report(db, item, "sent", now_ms=start + MIN_MS, ext_id="tg-1")
    assert _lease(db, account_id, now_ms=start + HOUR_MS) == []
    row = _row(db, touch.outbox_id)
    assert (row["status"], row["error"]) == ("failed", PACE_STALE_ERROR)


# --- B3. Без флажка — уходит несмотря на ответ (регресс Ф30-1) ----------------------------------------------


def test_b3_without_flag_post_goes_despite_reply(db, stand):
    created = _draft(db, stand["channel"], stand["token"], cancel_if_reply=False)
    _approve(db, created.outbox_id)
    _incoming(db, stand["account"], "p1", ts=APPROVED + MIN_MS, ext_id="r1")
    (item,) = _lease(db, stand["account"], now_ms=AT)
    assert item.outbox_id == created.outbox_id
    assert _audit(db, "outbox_send_at_replied") == []


# --- B4 + П3. Уже выдававшаяся: failed после temporary, unknown только при maybe_sent ---------------------


def test_b4_after_temporary_with_reply_fails(db, stand):
    created = _draft(db, stand["channel"], stand["token"])
    _approve(db, created.outbox_id)
    (item,) = _lease(db, stand["account"], now_ms=AT)
    _report(db, item, "temporary", now_ms=AT, error="сеть")
    _incoming(db, stand["account"], "p1", ts=AT + 1, ext_id="r1")
    retry_at = _row(db, created.outbox_id)["not_before_ts"]
    assert _lease(db, stand["account"], now_ms=retry_at) == []
    row = _row(db, created.outbox_id)
    assert (row["status"], row["error"]) == ("failed", SEND_AT_REPLIED_ERROR)


def test_b4_after_maybe_sent_with_reply_is_unknown(db, stand):
    created = _draft(db, stand["channel"], stand["token"])
    _approve(db, created.outbox_id)
    _lease(db, stand["account"], now_ms=AT)  # лиз 60 с, отчёта нет
    (item,) = _lease(db, stand["account"], now_ms=AT + 10 * MIN_MS)
    assert item.maybe_sent is True
    _report(db, item, "temporary", now_ms=AT + 10 * MIN_MS, error="сеть")
    _incoming(db, stand["account"], "p1", ts=AT + 11 * MIN_MS, ext_id="r1")
    retry_at = _row(db, created.outbox_id)["not_before_ts"]
    assert _lease(db, stand["account"], now_ms=retry_at) == []
    row = _row(db, created.outbox_id)
    assert (row["status"], row["error"]) == ("unknown", SEND_AT_REPLIED_ERROR)
    assert _audit(db, "outbox_send_at_replied") == [(created.outbox_id, "unknown")]


def test_b4_ui_reply_in_queue_counts(db, stand):
    """Ответ владельца из UI, ещё не ушедший (строка `origin='ui'` в очереди), — тоже ответ, как у графика."""
    created = _draft(db, stand["channel"], stand["token"])
    _approve(db, created.outbox_id)
    reply = _ui_reply(db, stand["channel"], stand["token"], APPROVED + MIN_MS)
    items = _lease(db, stand["account"], now_ms=AT)
    assert [i.outbox_id for i in items] == [reply.outbox_id]
    row = _row(db, created.outbox_id)
    assert (row["status"], row["error"]) == ("failed", SEND_AT_REPLIED_ERROR)


# --- П2. Проверка после FIFO --------------------------------------------------------------------------------


def test_p2_reply_check_after_fifo(db, stand):
    """Беседу держит более ранняя `unknown` — догон с ответом не трогается (решится, когда беседа
    свободна)."""
    channel = stand["channel"]
    blocker = _ui_reply(db, channel, stand["token"], NOW)
    (item,) = _lease(db, stand["account"], now_ms=NOW)
    _report(db, item, "unknown", now_ms=NOW)
    created = _draft(db, channel, stand["token"])
    _approve(db, created.outbox_id)
    _incoming(db, stand["account"], "p1", ts=APPROVED + MIN_MS, ext_id="r1")
    assert not _has_leasable(db, stand["account"], AT)
    assert _lease(db, stand["account"], now_ms=AT) == []
    assert _row(db, created.outbox_id)["status"] == "queued"
    assert _audit(db, "outbox_send_at_replied") == []
    db.write_sync(lambda conn: resolve_unknown(conn, blocker.outbox_id, decision="not_sent", now_ms=AT + 1))
    assert _has_leasable(db, stand["account"], AT + 2)
    assert _lease(db, stand["account"], now_ms=AT + 2) == []
    assert _row(db, created.outbox_id)["status"] == "failed"


# --- B5. send_now / retry — обычная отправка без проверки ---------------------------------------------------


def test_b5_send_now_ignores_reply(db, stand):
    created = _draft(db, stand["channel"], stand["token"])
    assert _approve(db, created.outbox_id, send_now=True) == Approved(None, None, None)
    _incoming(db, stand["account"], "p1", ts=APPROVED + MIN_MS, ext_id="r1")
    (item,) = _lease(db, stand["account"], now_ms=APPROVED + MIN_MS)
    assert item.outbox_id == created.outbox_id
    assert _row(db, created.outbox_id)["cancel_if_reply"] == 1  # флажок не сбрасывается, но не действует


def test_b5_retry_of_unknown_ignores_reply(db, stand):
    created = _draft(db, stand["channel"], stand["token"])
    _approve(db, created.outbox_id)
    _lease(db, stand["account"], now_ms=AT)
    (item,) = _lease(db, stand["account"], now_ms=AT + 10 * MIN_MS)
    _report(db, item, "temporary", now_ms=AT + 10 * MIN_MS, error="сеть")
    _incoming(db, stand["account"], "p1", ts=AT + 11 * MIN_MS, ext_id="r1")
    retry_at = _row(db, created.outbox_id)["not_before_ts"]
    _lease(db, stand["account"], now_ms=retry_at)
    assert _row(db, created.outbox_id)["status"] == "unknown"
    db.write_sync(
        lambda conn: resolve_unknown(conn, created.outbox_id, decision="retry", now_ms=retry_at + 1)
    )
    (item,) = _lease(db, stand["account"], now_ms=retry_at + 1)
    assert item.outbox_id == created.outbox_id


# --- B8. Миграция 0045 на копии БД, обе схемы ---------------------------------------------------------------


def test_b8_migration_0045_on_previous_version_both_files(tmp_path, monkeypatch):
    original = db_module._migrations

    def _up_to_44(*, main: bool):
        return {v: files for v, files in original(main=main).items() if v <= 44}

    monkeypatch.setattr(db_module, "_migrations", _up_to_44)
    old = Db(tmp_path)
    old.start()
    old_ids = {}
    try:
        token_id = _token(old)
        for schema in ("main", "work"):
            account_id = _account(old, db_name=schema)
            channel_id = _channel(old, account_id, "c1", db_name=schema)
            old_ids[schema] = _draft(old, channel_id, token_id, cancel_if_reply=False).outbox_id
        with old.reader() as conn:
            for schema in ("main", "work"):
                cols = {r[1] for r in conn.execute(f"PRAGMA {schema}.table_info(outbox)")}
                assert "send_at_ts" in cols and "cancel_if_reply" not in cols
    finally:
        old.close()

    monkeypatch.setattr(db_module, "_migrations", original)
    new = Db(tmp_path)
    new.start()
    try:
        with new.reader() as conn:
            for schema in ("main", "work"):
                assert conn.execute(f"PRAGMA {schema}.user_version").fetchone()[0] >= 45
        for schema in ("main", "work"):
            row = _row(new, old_ids[schema], schema)
            assert (row["status"], row["cancel_if_reply"], row["send_at_ts"]) == (
                "pending_approval",
                0,
                int(dt.datetime(2026, 10, 5, 11, 0, tzinfo=MSK).timestamp() * 1000),
            )
    finally:
        new.close()


# --- Ф32-2 п.6б + П9: своё служебное событие — не ответ для `cancel_if_reply`; график прежний --------------


def _own_event(db, account_id, channel_ext, ts):
    """Служебное событие владельца с телефона (например, закрепил сообщение): `is_me`, `kind='event'`."""
    db.write_sync(
        lambda conn: apply_batch(
            conn,
            account_id=account_id,
            cursor=None,
            now_ms=ts,
            events=[
                {
                    "type": "upsert",
                    "channel": {"ext_id": channel_ext, "kind": "dm"},
                    "ext_id": f"pin{ts}",
                    "ts": ts,
                    "is_me": True,
                    "kind": "event",
                    "text": "закрепил сообщение",
                    "mentions_me": False,
                }
            ],
        )
    )


def test_f322_own_service_event_is_not_a_reply(db, stand):
    created = _draft(db, stand["channel"], stand["token"])
    _approve(db, created.outbox_id)
    _own_event(db, stand["account"], "p1", APPROVED + MIN_MS)
    (item,) = _lease(db, stand["account"], now_ms=AT)
    assert item.outbox_id == created.outbox_id
    assert _audit(db, "outbox_send_at_replied") == []


def test_f322_paced_still_counts_own_service_event(db):
    """График (`skip_api=False`) не менялся: своё служебное событие после подтверждения касания — устарело."""
    account_id = _account(db)
    token_id = _token(db)
    channel = _channel(db, account_id, "u1")
    start = msk(12)
    touch = _draft(db, channel, token_id, send_at=None, cancel_if_reply=False, paced=True, now_ms=start)
    db.write_sync(lambda conn: approve(conn, touch.outbox_id, now_ms=start, rng=random.Random(1)))
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.outbox SET not_before_ts = ?, pace_slot_ts = ? WHERE id = ?",
            (start + HOUR_MS, start + HOUR_MS, touch.outbox_id),
        )
    )
    _own_event(db, account_id, "u1", start + MIN_MS)
    assert _lease(db, account_id, now_ms=start + HOUR_MS) == []
    row = _row(db, touch.outbox_id)
    assert (row["status"], row["error"]) == ("failed", PACE_STALE_ERROR)
