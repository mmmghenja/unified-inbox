"""Ф12-2: график отправки черновиков «по графику» — слот при подтверждении (решение 3), проверка графика при
выдаче (П2), запланированное не держит беседу и устаревшее касание не уходит (П1)
(`docs/architecture.md`, 8 и поправки). Время и случайность — подменные."""

import datetime as dt
import json
import logging
import random
import zoneinfo

import pytest

from hub.core import outbox_queries, pace
from hub.core.db import Db
from hub.core.ingest import apply_batch
from hub.core.outbox import PACE_STALE_ERROR, approve, cancel, enqueue, lease, report

MSK = zoneinfo.ZoneInfo("Europe/Moscow")
GAP_MIN_MS = pace.DEFAULT_GAP_MIN_S * 1000
GAP_MAX_MS = pace.DEFAULT_GAP_MAX_S * 1000
OWNER = "connector:1"


def msk(hour: int, minute: int = 0, *, day: int = 28) -> int:
    """Сентябрь 2026 по времени хаба -> мс эпохи (28.09 — понедельник)."""
    return int(dt.datetime(2026, 9, day, hour, minute, tzinfo=MSK).timestamp() * 1000)


def local(ts_ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts_ms / 1000, MSK)


class MaxRng:
    """Всегда верхняя граница — худший случай для «≤ gap_max»."""

    def randint(self, a: int, b: int) -> int:
        return b


class MinRng:
    def randint(self, a: int, b: int) -> int:
        return a


def _account(db, *, source="telegram", config=None, db_name="main") -> int:
    cfg = {"idempotent_send": True, **(config or {})}
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, config_json) VALUES (?, ?, ?)",
                (source, db_name, json.dumps(cfg)),
            ).lastrowid
        )
    )


def _token(db) -> int:
    return db.write_sync(
        lambda conn: (
            (
                conn.execute(
                    "INSERT INTO main.api_token (name, kind, hash) VALUES ('mcp:iip', 'interactive', 'x')"
                )
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


def _draft(db, channel_id, token_id, *, paced=True, now_ms, text="Добрый день! Напоминаю о подписи."):
    return db.write_sync(
        lambda conn: enqueue(
            conn,
            channel_id=channel_id,
            text=text,
            origin="api",
            api_token_id=token_id,
            idempotency_key=None,
            now_ms=now_ms,
            paced=paced,
        )
    )


def _approve(db, outbox_id, *, now_ms, rng=None):
    return db.write_sync(lambda conn: approve(conn, outbox_id, now_ms=now_ms, rng=rng or random.Random(7)))


def _lease(db, account_id, *, now_ms, rng=None):
    return db.write_sync(
        lambda conn: lease(
            conn, account_id=account_id, owner=OWNER, now_ms=now_ms, rng=rng or random.Random(3)
        )
    )


def _sent(db, item, *, now_ms, ext_id=None):
    db.write_sync(
        lambda conn: report(
            conn,
            outbox_id=item.outbox_id,
            owner=OWNER,
            lease_gen=item.lease_gen,
            outcome="sent",
            ext_id=ext_id or f"e{item.outbox_id}",
            now_ms=now_ms,
        )
    )


def _row(db, outbox_id) -> dict:
    with db.reader() as conn:
        row = conn.execute(
            "SELECT status, not_before_ts, pace_slot_ts, paced, error, sent_ts FROM main.outbox WHERE id = ?",
            (outbox_id,),
        ).fetchone()
    return dict(
        zip(("status", "not_before_ts", "pace_slot_ts", "paced", "error", "sent_ts"), row, strict=True)
    )


def _has_leasable(db, account_id, now_ms) -> bool:
    with db.reader() as conn:
        return outbox_queries.has_leasable(conn, account_id=account_id, now_ms=now_ms)


@pytest.fixture
def stand(db):
    account_id = _account(db)
    token_id = _token(db)
    channels = [_channel(db, account_id, f"u{i}") for i in range(8)]
    return {"account": account_id, "token": token_id, "channels": channels}


def _approved(db, stand, n, *, now_ms, rng=None):
    ids = []
    for i in range(n):
        created = _draft(db, stand["channels"][i], stand["token"], now_ms=now_ms)
        _approve(db, created.outbox_id, now_ms=now_ms, rng=rng)
        ids.append(created.outbox_id)
    return ids


# --- 1. Пустая очередь, в окне: первый слот — сейчас, второй — через случайный промежуток ------------------


def test_1_first_slot_now_second_after_random_gap(db, stand):
    now = msk(12)
    first, second = _approved(db, stand, 2, now_ms=now, rng=random.Random(11))
    s1 = _row(db, first)
    # Слова владельца: и первая — не мгновенно, а через случайную паузу `[0, gap_min]` от «сейчас».
    assert s1["status"] == "queued" and s1["not_before_ts"] == s1["pace_slot_ts"]
    assert now <= s1["not_before_ts"] <= now + GAP_MIN_MS
    s2 = _row(db, second)["not_before_ts"]
    assert s1["not_before_ts"] + GAP_MIN_MS <= s2 <= s1["not_before_ts"] + GAP_MAX_MS
    assert _row(db, second)["pace_slot_ts"] == s2


def test_1_first_slot_random_pause_bounds(db, stand):
    """Слова владельца: предыдущего слота нет — пауза `[0, gap_min]` от «сейчас» (обе границы)."""
    now = msk(12)
    (low,) = _approved(db, stand, 1, now_ms=now, rng=MinRng())
    assert _row(db, low)["not_before_ts"] == now
    account_id = _account(db)
    other = {"account": account_id, "token": stand["token"], "channels": [_channel(db, account_id, "z0")]}
    (high,) = _approved(db, other, 1, now_ms=now, rng=MaxRng())
    assert _row(db, high)["not_before_ts"] == now + GAP_MIN_MS


def test_1_previous_slot_long_ago_random_pause_from_now(db, stand):
    """Предыдущий слот давно (`prev + gap <= now`) — та же случайная пауза от «сейчас», не мгновенно."""
    (first,) = _approved(db, stand, 1, now_ms=msk(11), rng=MinRng())
    (item,) = _lease(db, stand["account"], now_ms=msk(11))
    _sent(db, item, now_ms=msk(11))
    second = _draft(db, stand["channels"][1], stand["token"], now_ms=msk(13))
    _approve(db, second.outbox_id, now_ms=msk(13), rng=MaxRng())
    assert _row(db, second.outbox_id)["not_before_ts"] == msk(13) + GAP_MIN_MS
    assert _row(db, first)["status"] == "sent"


def test_1_slot_uses_rng_bounds(db, stand):
    now = msk(12)
    ids_min = _approved(db, stand, 2, now_ms=now, rng=MinRng())
    assert _row(db, ids_min[1])["not_before_ts"] == now + GAP_MIN_MS
    third = _draft(db, stand["channels"][5], stand["token"], now_ms=now)
    _approve(db, third.outbox_id, now_ms=now, rng=MaxRng())
    assert _row(db, third.outbox_id)["not_before_ts"] == now + GAP_MIN_MS + GAP_MAX_MS


# --- 2. Вне окна (8:00 и 20:00 МСК) -> начало ближайшего окна + смещение ≤ gap_max ------------------------


@pytest.mark.parametrize(("now", "window_start"), [(msk(8), msk(10)), (msk(20), msk(10, day=29))])
def test_2_outside_window_goes_to_next_window_start(db, stand, now, window_start):
    (oid,) = _approved(db, stand, 1, now_ms=now, rng=MaxRng())
    assert _row(db, oid)["not_before_ts"] == window_start + GAP_MAX_MS
    (oid2,) = [_draft(db, stand["channels"][3], stand["token"], now_ms=now).outbox_id]
    _approve(db, oid2, now_ms=now, rng=MinRng())
    assert _row(db, oid2)["not_before_ts"] == window_start + GAP_MAX_MS + GAP_MIN_MS


def test_2_window_end_is_exclusive_and_offset_stays_inside(db):
    account_id = _account(db, config={"pace_window": "10:00-10:05"})
    token_id = _token(db)
    channel_id = _channel(db, account_id, "x")
    created = _draft(db, channel_id, token_id, now_ms=msk(10, 5))
    _approve(db, created.outbox_id, now_ms=msk(10, 5), rng=MaxRng())
    slot = _row(db, created.outbox_id)["not_before_ts"]
    # 10:05 — уже вне окна; смещение 720 с длиннее окна — слот всё равно внутри окна завтра.
    assert msk(10, day=29) <= slot < msk(10, 5, day=29)


# --- 3. Лимит: pace_daily=3, четыре подтверждения -> четвёртое — завтра в окне ----------------------------


def test_3_daily_limit_moves_fourth_to_tomorrow(db):
    account_id = _account(db, config={"pace_daily": 3})
    token_id = _token(db)
    stand = {
        "account": account_id,
        "token": token_id,
        "channels": [_channel(db, account_id, f"u{i}") for i in range(4)],
    }
    ids = _approved(db, stand, 4, now_ms=msk(12), rng=MinRng())
    slots = [_row(db, i)["not_before_ts"] for i in ids]
    assert [local(s).date() for s in slots[:3]] == [dt.date(2026, 9, 28)] * 3
    assert slots[3] == msk(10, day=29)  # MinRng: смещение 0 от начала окна
    assert slots == sorted(slots)


def test_3_sent_today_counts_by_sent_ts(db):
    account_id = _account(db, config={"pace_daily": 1})
    token_id = _token(db)
    stand = {
        "account": account_id,
        "token": token_id,
        "channels": [_channel(db, account_id, f"u{i}") for i in range(2)],
    }
    (first,) = _approved(db, stand, 1, now_ms=msk(11), rng=MinRng())
    (item,) = _lease(db, account_id, now_ms=msk(11))
    _sent(db, item, now_ms=msk(11, 1))
    second = _draft(db, stand["channels"][1], token_id, now_ms=msk(12))
    _approve(db, second.outbox_id, now_ms=msk(12), rng=MinRng())
    assert _row(db, second.outbox_id)["not_before_ts"] == msk(10, day=29)
    assert _row(db, first)["status"] == "sent"


# --- 4. Рестарт: слоты — из БД ----------------------------------------------------------------------------


def test_4_restart_reads_slots_from_db(tmp_path):
    first_db = Db(tmp_path)
    first_db.start()
    try:
        account_id = _account(first_db)
        token_id = _token(first_db)
        ch = [_channel(first_db, account_id, f"u{i}") for i in range(2)]
        a = _draft(first_db, ch[0], token_id, now_ms=msk(12))
        _approve(first_db, a.outbox_id, now_ms=msk(12), rng=MaxRng())
        last = _row(first_db, a.outbox_id)["not_before_ts"]
    finally:
        first_db.close()
    second_db = Db(tmp_path)
    second_db.start()
    try:
        b = _draft(second_db, ch[1], token_id, now_ms=msk(12, 1))
        _approve(second_db, b.outbox_id, now_ms=msk(12, 1), rng=MinRng())
        assert _row(second_db, b.outbox_id)["not_before_ts"] == last + GAP_MIN_MS
    finally:
        second_db.close()


# --- 5. approve без paced — not_before_ts не меняется (регресс) --------------------------------------------


def test_5_approve_without_paced_keeps_not_before(db, stand):
    created = _draft(db, stand["channels"][0], stand["token"], paced=False, now_ms=msk(3))
    assert _approve(db, created.outbox_id, now_ms=msk(4)) is None
    row = _row(db, created.outbox_id)
    assert (row["status"], row["not_before_ts"], row["pace_slot_ts"], row["paced"]) == (
        "queued",
        msk(3),
        None,
        0,
    )
    (item,) = _lease(db, stand["account"], now_ms=msk(4))  # ночью — обычный черновик уходит сразу
    assert item.outbox_id == created.outbox_id


def test_5_paced_only_for_api_drafts(db, stand):
    with pytest.raises(Exception, match="по графику"):
        db.write_sync(
            lambda conn: enqueue(
                conn,
                channel_id=stand["channels"][0],
                text="x",
                origin="ui",
                api_token_id=stand["token"],
                idempotency_key=None,
                now_ms=msk(12),
                paced=True,
            )
        )


# --- 7. lease не выдаёт раньше слота; cancel запланированного -> cancelled ---------------------------------


def test_7_lease_waits_for_slot_then_cancel(db, stand):
    now = msk(12)
    first, second = _approved(db, stand, 2, now_ms=now, rng=MinRng())
    (item,) = _lease(db, stand["account"], now_ms=now)
    assert item.outbox_id == first
    _sent(db, item, now_ms=now + 1000)
    slot2 = _row(db, second)["not_before_ts"]
    assert _lease(db, stand["account"], now_ms=slot2 - 1) == []
    assert not _has_leasable(db, stand["account"], slot2 - 1)
    assert db.write_sync(lambda conn: cancel(conn, second, now_ms=slot2 - 1)) == "cancelled"
    assert _lease(db, stand["account"], now_ms=slot2 + 1) == []
    assert _row(db, second)["status"] == "cancelled"


def test_7_lease_gives_row_at_its_slot(db, stand):
    now = msk(12)
    first, second = _approved(db, stand, 2, now_ms=now, rng=MinRng())
    (item,) = _lease(db, stand["account"], now_ms=now)
    _sent(db, item, now_ms=now)
    slot2 = _row(db, second)["not_before_ts"]
    assert _has_leasable(db, stand["account"], slot2)
    (item2,) = _lease(db, stand["account"], now_ms=slot2)
    assert item2.outbox_id == second


# --- 8. Настройки аккаунта ---------------------------------------------------------------------------------


def test_8_account_settings_apply(db):
    account_id = _account(db, config={"pace_daily": 5, "pace_window": "11:00-18:00", "pace_gap_min_s": 60,
                                      "pace_gap_max_s": 60})  # fmt: skip
    with db.reader() as conn:
        s = pace.settings_for(conn, account_id)
    assert (s.start_min, s.end_min, s.daily, s.gap_min_s, s.gap_max_s) == (660, 1080, 5, 60, 60)
    token_id = _token(db)
    stand = {
        "account": account_id,
        "token": token_id,
        "channels": [_channel(db, account_id, f"u{i}") for i in range(6)],
    }
    ids = _approved(db, stand, 6, now_ms=msk(10), rng=MinRng())
    slots = [_row(db, i)["not_before_ts"] for i in ids]
    assert slots[:5] == [msk(11) + i * 60_000 for i in range(5)]  # 10:00 — вне окна 11–18
    assert slots[5] == msk(11, day=29)  # шестое — завтра: лимит 5


def test_8_string_values_from_cli_are_accepted(db):
    account_id = _account(db, config={"pace_daily": "4", "pace_tz": "Asia/Yekaterinburg"})
    with db.reader() as conn:
        s = pace.settings_for(conn, account_id)
    assert s.daily == 4 and str(s.tz) == "Asia/Yekaterinburg"


@pytest.mark.parametrize(
    "config",
    [
        {"pace_daily": 0},
        {"pace_daily": "много"},
        {"pace_window": "19:00-10:00"},
        {"pace_window": "25:00-26:00"},
        {"pace_tz": "Mars/Olympus"},
        {"pace_gap_min_s": 0, "pace_gap_max_s": 10},
        {"pace_gap_min_s": 900, "pace_gap_max_s": 100},
        {"pace_daily": True},
    ],
)
def test_8_bad_values_fall_back_to_defaults_with_warning(db, caplog, config):
    account_id = _account(db, config=config)
    with caplog.at_level(logging.WARNING, logger="hub.core.pace"), db.reader() as conn:
        s = pace.settings_for(conn, account_id)
    assert (s.start_min, s.end_min, s.gap_min_s, s.gap_max_s, s.daily, str(s.tz)) == (
        600,
        1140,
        240,
        720,
        20,
        "Europe/Moscow",
    )
    assert any("не годится" in r.getMessage() for r in caplog.records)


def test_8_mail_default_daily_is_15(db):
    account_id = _account(db, source="mail")
    with db.reader() as conn:
        assert pace.settings_for(conn, account_id).daily == 15


# --- П2: график проверяется при выдаче --------------------------------------------------------------------


def test_p2_woke_at_23_with_five_overdue_none_at_night_then_one_by_one(db, stand):
    ids = _approved(db, stand, 5, now_ms=msk(12), rng=random.Random(5))
    # Ноутбук уснул в 12:00, проснулся в 23:00: все пять слотов просрочены.
    assert _has_leasable(db, stand["account"], msk(23))
    assert _lease(db, stand["account"], now_ms=msk(23)) == []
    assert not _has_leasable(db, stand["account"], msk(23))
    slots = sorted(_row(db, i)["not_before_ts"] for i in ids)
    for s in slots:
        assert msk(10, day=29) <= s < msk(19, day=29)
    assert all(b - a >= GAP_MIN_MS for a, b in zip(slots, slots[1:], strict=False))
    assert all(_row(db, i)["pace_slot_ts"] == _row(db, i)["not_before_ts"] for i in ids)

    # Завтра: коннектор опрашивает раз в минуту — уходит по одному, с промежутками не меньше минимума.
    sent_at: list[int] = []
    now = msk(9, day=29)
    while now < msk(19, day=29) and len(sent_at) < 5:
        items = _lease(db, stand["account"], now_ms=now)
        assert len(items) <= 1
        for item in items:
            _sent(db, item, now_ms=now)
            sent_at.append(now)
        now += 60_000
    assert len(sent_at) == 5
    assert all(msk(10, day=29) <= t < msk(19, day=29) for t in sent_at)
    assert all(b - a >= GAP_MIN_MS for a, b in zip(sent_at, sent_at[1:], strict=False))


def test_p2_two_due_rows_in_one_lease_only_one_goes(db, stand):
    ids = _approved(db, stand, 2, now_ms=msk(12), rng=MinRng())
    later = msk(13)  # оба слота просрочены, но в окне
    items = _lease(db, stand["account"], now_ms=later, rng=MinRng())
    assert [i.outbox_id for i in items] == [ids[0]]
    # второй — не раньше, чем через минимальный промежуток после «сейчас» (первый ещё отправляется)
    assert _row(db, ids[1])["not_before_ts"] == later + GAP_MIN_MS


def test_p2_flood_moved_row_to_21_goes_tomorrow_in_window(db, stand):
    (oid,) = _approved(db, stand, 1, now_ms=msk(18, 50), rng=MinRng())
    (item,) = _lease(db, stand["account"], now_ms=msk(18, 50))
    db.write_sync(
        lambda conn: report(
            conn,
            outbox_id=oid,
            owner=OWNER,
            lease_gen=item.lease_gen,
            outcome="flood",
            retry_after_ms=msk(21) - msk(18, 50),
            now_ms=msk(18, 50),
        )
    )
    assert _row(db, oid)["not_before_ts"] == msk(21)
    assert _lease(db, stand["account"], now_ms=msk(21)) == []
    slot = _row(db, oid)["not_before_ts"]
    assert msk(10, day=29) <= slot < msk(19, day=29)
    (again,) = _lease(db, stand["account"], now_ms=slot)
    assert again.outbox_id == oid


def test_p2_gap_after_last_sent_is_enforced(db, stand):
    first, second = _approved(db, stand, 2, now_ms=msk(12), rng=MinRng())
    (item,) = _lease(db, stand["account"], now_ms=msk(12))
    # Первое ушло с опозданием (долгая отправка) — второе по своему слоту не должно уйти впритык.
    _sent(db, item, now_ms=msk(12, 3))
    assert _lease(db, stand["account"], now_ms=msk(12, 4), rng=MinRng()) == []
    assert _row(db, second)["not_before_ts"] == msk(12, 3) + GAP_MIN_MS


def test_p2_daily_limit_checked_at_lease(db):
    account_id = _account(db, config={"pace_daily": 2})
    token_id = _token(db)
    stand = {
        "account": account_id,
        "token": token_id,
        "channels": [_channel(db, account_id, f"u{i}") for i in range(3)],
    }
    ids = _approved(db, stand, 2, now_ms=msk(11), rng=MinRng())
    # Третье стоит в очереди на сегодня мимо лимита (лимит уменьшили уже после подтверждения) — выдача сверяет
    # лимит заново и переносит его на завтра.
    for oid in ids:
        (item,) = _lease(db, account_id, now_ms=_row(db, oid)["not_before_ts"])
        _sent(db, item, now_ms=_row(db, oid)["not_before_ts"])
    third = _draft(db, stand["channels"][2], token_id, now_ms=msk(12)).outbox_id
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.outbox SET status = 'queued', not_before_ts = ?, pace_slot_ts = ? WHERE id = ?",
            (msk(13), msk(13), third),
        )
    )
    assert _lease(db, account_id, now_ms=msk(13), rng=MinRng()) == []
    assert _row(db, third)["not_before_ts"] == msk(10, day=29)


# --- П1: запланированное не держит беседу, устаревшее касание не уходит -----------------------------------


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


def _incoming(db, account_id, channel_ext, *, ts, is_me=False, ext_id="m1", kind="text"):
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
                    "kind": kind,
                    "text": "Спасибо, уже не нужно",
                    "mentions_me": False,
                }
            ],
        )
    )


def test_p1_scheduled_touch_does_not_hold_reply(db, stand):
    first, touch = _approved(db, stand, 2, now_ms=msk(12), rng=MinRng())
    # касание `touch` — в беседе u1, слот 12:04; первое (u0) ещё не ушло — ответ владельца в u1 не ждёт никого
    reply = _ui_reply(db, stand["channels"][1], stand["token"], msk(12, 1))
    items = _lease(db, stand["account"], now_ms=msk(12, 1))
    assert {i.outbox_id for i in items} == {first, reply.outbox_id}
    assert _row(db, touch)["status"] == "queued"


def test_p1_overdue_touch_with_reply_in_queue_is_stale_and_reply_goes(db, stand):
    (touch,) = _approved(db, stand, 1, now_ms=msk(18, 59), rng=MinRng())
    reply = _ui_reply(db, stand["channels"][0], stand["token"], msk(22))
    items = _lease(db, stand["account"], now_ms=msk(22))
    assert [i.outbox_id for i in items] == [reply.outbox_id]
    row = _row(db, touch)
    assert (row["status"], row["error"]) == ("failed", PACE_STALE_ERROR)


def test_p1_reply_before_approval_does_not_make_touch_stale(db, stand):
    reply = _ui_reply(db, stand["channels"][0], stand["token"], msk(11))
    (touch,) = _approved(db, stand, 1, now_ms=msk(12))
    items = _lease(db, stand["account"], now_ms=msk(12))
    assert [i.outbox_id for i in items] == [reply.outbox_id]  # FIFO беседы: сначала ответ владельца
    assert _row(db, touch)["status"] == "queued"


@pytest.mark.parametrize("is_me", [False, True])
def test_p1_answer_after_approval_makes_touch_stale(db, stand, is_me):
    (touch,) = _approved(db, stand, 1, now_ms=msk(20))  # слот — завтра утром
    _incoming(db, stand["account"], "u0", ts=msk(21), is_me=is_me)
    slot = _row(db, touch)["not_before_ts"]
    assert _has_leasable(db, stand["account"], slot)
    assert _lease(db, stand["account"], now_ms=slot) == []
    row = _row(db, touch)
    assert (row["status"], row["error"]) == ("failed", PACE_STALE_ERROR)


def test_p1_service_event_after_approval_is_not_an_answer(db, stand):
    (touch,) = _approved(db, stand, 1, now_ms=msk(20))
    _incoming(db, stand["account"], "u0", ts=msk(21), kind="event")  # «вступил в группу», закреп и т.п.
    slot = _row(db, touch)["not_before_ts"]
    (item,) = _lease(db, stand["account"], now_ms=slot)
    assert item.outbox_id == touch


def test_p1_message_before_approval_is_not_stale(db, stand):
    _incoming(db, stand["account"], "u0", ts=msk(11))
    (touch,) = _approved(db, stand, 1, now_ms=msk(12), rng=MinRng())
    (item,) = _lease(db, stand["account"], now_ms=msk(12))
    assert item.outbox_id == touch


def test_p1_stale_after_attempt_is_unknown(db, stand):
    (touch,) = _approved(db, stand, 1, now_ms=msk(12), rng=MinRng())
    (item,) = _lease(db, stand["account"], now_ms=msk(12))
    db.write_sync(
        lambda conn: report(
            conn,
            outbox_id=touch,
            owner=OWNER,
            lease_gen=item.lease_gen,
            outcome="temporary",
            retry_after_ms=600_000,
            now_ms=msk(12),
        )
    )
    _incoming(db, stand["account"], "u0", ts=msk(12, 5))
    assert _lease(db, stand["account"], now_ms=msk(12, 11)) == []
    row = _row(db, touch)
    assert (row["status"], row["error"]) == ("unknown", PACE_STALE_ERROR)


def test_p1_work_schema(db):
    account_id = _account(db, db_name="work")
    token_id = _token(db)
    channel_id = _channel(db, account_id, "w0", db_name="work")
    created = _draft(db, channel_id, token_id, now_ms=msk(20))
    slot = _approve(db, created.outbox_id, now_ms=msk(20), rng=MinRng())
    assert slot == msk(10, day=29)
    (item,) = _lease(db, account_id, now_ms=slot)
    assert item.outbox_id == created.outbox_id


# --- A5: сколько по графику на сегодня ---------------------------------------------------------------------


def test_a5_today_counts(db, stand):
    _approved(db, stand, 3, now_ms=msk(18, 50), rng=MinRng())  # 18:50, 18:54, 18:58 — сегодня; ещё не ушли
    with db.reader() as conn:
        assert pace.today(conn, stand["account"], msk(18, 59)) == (3, 20)
        assert pace.today(conn, stand["account"], msk(10, day=29)) == (0, 20)


# --- Ревью Ф12-2 (code-reviewer): находки 1–3, тесты из `.runs/critic-f12-2/` ----------------------------


def _set_flood(db, account_id, until):
    db.write_sync(
        lambda c: c.execute("UPDATE main.account SET flood_until = ? WHERE id = ?", (until, account_id))
    )


def test_f1_queued_reply_of_owner_after_approval_touch_must_not_go_first(db, stand):
    """П1: владелец ответил в беседе ПОСЛЕ подтверждения касания, но его ответ ещё в очереди (flood/коннектор
    лежит/сон). Когда выдача возобновится, касание (меньший id, слот просрочен) уходит ПЕРВЫМ, ответ владельца ждёт
    за ним по FIFO: _touch_stale видит только сообщения с ext_id."""
    (touch,) = _approved(db, stand, 1, now_ms=msk(12), rng=MinRng())  # слот 12:00
    _set_flood(db, stand["account"], msk(12, 30))  # коннектор получил flood до 12:30
    _ui_reply(db, stand["channels"][0], stand["token"], msk(12, 10))
    items = _lease(db, stand["account"], now_ms=msk(12, 30))
    got = [i.outbox_id for i in items]
    assert touch not in got, f"устаревшее касание выдано до ответа владельца: {got}"
    assert _row(db, touch)["status"] == "failed"


def test_f1b_connector_down_same(db, stand):
    """То же без flood: коннектор выключен/ноутбук спал, слот 14:00, владелец ответил из UI в 14:30."""
    (touch,) = _approved(db, stand, 1, now_ms=msk(13, 58), rng=MinRng())
    reply = _ui_reply(db, stand["channels"][0], stand["token"], msk(14, 30))
    items = _lease(db, stand["account"], now_ms=msk(15))
    got = [i.outbox_id for i in items]
    assert got == [reply.outbox_id], f"выдано {got}, касание {touch}, ответ {reply.outbox_id}"


def test_f2_gap_reslot_jumps_behind_tomorrow_queue(db):
    """П2 «gap»: отправка первой строки заняла 5 с, вторая по слоту (ровно +gap_min) упирается в «gap» и
    переставляется ПОСЛЕ максимального слота аккаунта (завтрашнего), хотя сегодня место есть через 5 с."""
    account_id = _account(db, config={"pace_daily": 2})
    token_id = _token(db)
    chans = [_channel(db, account_id, f"u{i}") for i in range(3)]
    ids = []
    for ch in chans:
        created = _draft(db, ch, token_id, now_ms=msk(12))
        _approve(db, created.outbox_id, now_ms=msk(12), rng=MinRng())
        ids.append(created.outbox_id)
    assert _row(db, ids[1])["not_before_ts"] == msk(12, 4)
    assert _row(db, ids[2])["not_before_ts"] >= msk(10, day=29)  # третий — завтра (лимит 2)
    (item,) = _lease(db, account_id, now_ms=msk(12))
    _sent(db, item, now_ms=msk(12) + 5_000)  # отправка заняла 5 секунд
    assert _lease(db, account_id, now_ms=msk(12, 4), rng=MinRng()) == []
    new_slot = _row(db, ids[1])["not_before_ts"]
    assert new_slot < msk(19), f"вторая строка ушла на {local(new_slot)} вместо сегодня ~12:04:05"


def test_f3_expired_lease_of_touch_resent_outside_window(db, stand):
    """Лиз касания истёк (коннектор упал в 18:59), выдача в 23:00 идёт веткой просроченных лизов —
    мимо проверки графика: идемпотентный аккаунт (Telegram) получает режим send ночью."""
    (touch,) = _approved(db, stand, 1, now_ms=msk(18, 59), rng=MinRng())
    (item,) = _lease(db, stand["account"], now_ms=msk(18, 59))
    items = _lease(db, stand["account"], now_ms=msk(23))
    assert [(i.outbox_id, i.mode) for i in items if i.outbox_id == touch] == [], (
        "касание выдано на отправку в 23:00"
    )
