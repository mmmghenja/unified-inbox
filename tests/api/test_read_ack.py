"""Ф8-7 (docs/architecture.md): очередь «прочитано в хабе -> прочитано в источнике» — раздел E,
пункты 1–10, и поправки П1 (действующая цель), П2 (`wait` без горячего цикла), П4 (паузы), П7 (по одному).

UI-отметка идёт через публичное приложение (`POST /channels/{id}/read` под cookie-сессией), выдача и отчёт —
через внутреннее (секрет коннектора) на той же БД и тех же часах."""

import asyncio
import json
import time

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import connector_auth
from hub.core import db as db_module
from hub.core import read_ack as read_ack_mod
from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.db import Db, db_of
from hub.core.feed import Notifier

from .conftest import csrf_headers, insert_account, login_via_code

API = "/api/v1"
INTERNAL = "/internal/v1"
ADMIN_SECRET = ADMIN_PREFIX + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHI"


# --- обвязка ------------------------------------------------------------------------------------------------


@pytest.fixture
def notifier() -> Notifier:
    return Notifier()


@pytest.fixture
def internal_app(db, clock, notifier):
    db.on_commit(notifier.notify)
    return create_internal_app(db, clock=clock, notifier=notifier, admin_key_hash=hash_secret(ADMIN_SECRET))


@pytest.fixture
async def ic(internal_app):
    async with AsyncClient(transport=ASGITransport(app=internal_app), base_url="http://hub-internal") as c:
        yield c


def _account(db, *, source="telegram", db_name="main", config=None, status="ok") -> tuple[int, dict]:
    def _fn(conn):
        return conn.execute(
            "INSERT INTO main.account (source, db, config_json, status) VALUES (?, ?, ?, ?)",
            (source, db_name, json.dumps(config) if config is not None else None, status),
        ).lastrowid

    account_id = db.write_sync(_fn)
    secret = connector_auth.generate_connector_secret()
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.connector_secret (account_id, hash, created_ts) VALUES (?, ?, 0)",
            (account_id, connector_auth.hash_secret(secret)),
        )
    )
    return account_id, {"Authorization": f"Bearer {secret}"}


def _set_account(db, account_id, **cols):
    sets = ", ".join(f"{k} = ?" for k in cols)
    db.write_sync(
        lambda conn: conn.execute(
            f"UPDATE main.account SET {sets} WHERE id = ?",  # noqa: S608 — имена колонок из теста
            (*cols.values(), account_id),
        )
    )


def _up(ch, ext_id, ts, *, kind="dm", is_me=False, thread=None, broadcast=False, parent=None):
    channel = {"ext_id": ch, "kind": kind}
    if parent is not None:
        channel["parent_ext_id"] = parent
        channel["parent"] = {"ext_id": parent, "kind": "group"}
    ev = {
        "type": "upsert",
        "channel": channel,
        "ext_id": ext_id,
        "ts": ts,
        "is_me": is_me,
        "kind": "text",
        "text": "t",
        "mentions_me": False,
    }
    if thread is not None:
        ev["thread_ext_id"] = thread
        ev["thread_broadcast"] = broadcast
    return ev


async def _events(ic, h, events):
    r = await ic.post(f"{INTERNAL}/events", headers=h, json={"events": events})
    assert r.status_code == 200 and r.json()["dead"] == 0, r.text


def _channel_id(db, ext_id, schema="main"):
    with db.reader() as conn:
        return conn.execute(
            f"SELECT id FROM {schema}.channel WHERE ext_id = ?",  # noqa: S608
            (ext_id,),
        ).fetchone()[0]


def _mid(db, channel_id, ext_id):
    schema = db_of(channel_id)
    with db.reader() as conn:
        return conn.execute(
            f"SELECT id FROM {schema}.message WHERE channel_id = ? AND ext_id = ?",  # noqa: S608
            (channel_id, ext_id),
        ).fetchone()[0]


def _row(db, channel_id):
    schema = db_of(channel_id)
    with db.reader() as conn:
        cur = conn.execute(f"SELECT * FROM {schema}.read_ack WHERE channel_id = ?", (channel_id,))  # noqa: S608
        row = cur.fetchone()
        if row is None:
            return None
        return dict(zip([c[0] for c in cur.description], row, strict=True))


def _now_ms(clock) -> int:
    return int(clock() * 1000)


async def _ui(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    return csrf_headers(cookie)


async def _mark(client, ui, channel_id, message_id):
    r = await client.post(
        f"{API}/channels/{channel_id}/read", json={"up_to_message_id": message_id}, headers=ui
    )
    assert r.status_code == 200, r.text
    return r.json()["changed"]


async def _lease(ic, h, limit=None):
    body = {} if limit is None else {"limit": limit}
    r = await ic.post(f"{INTERNAL}/read-ack/lease", headers=h, json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def _report(ic, h, channel_id, lease_gen, outcome, retry_after_ms=None, *, status=200):
    body = {"channel_id": channel_id, "lease_gen": lease_gen, "outcome": outcome}
    if retry_after_ms is not None:
        body["retry_after_ms"] = retry_after_ms
    r = await ic.post(f"{INTERNAL}/read-ack/report", headers=h, json=body)
    assert r.status_code == status, r.text
    return r.json()


def _audit_read_ack(db):
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT detail_json FROM main.audit WHERE action = 'channel_read' ORDER BY id"
        ).fetchall()
    return [json.loads(r[0]).get("read_ack") for r in rows]


async def _simple(db, ic, *, source="telegram", config=None, n=3, ch="c1", kind="dm", db_name="main"):
    """Аккаунт и канал с входящими `1..n` (ts = 1000 * i)."""
    acc, h = _account(db, source=source, config=config, db_name=db_name)
    await _events(ic, h, [_up(ch, str(i), 1000 * i, kind=kind) for i in range(1, n + 1)])
    cid = _channel_id(db, ch, db_name)
    return acc, h, cid


# --- E1: постановка только при сдвиге ---


async def test_e1_ui_mark_queues_only_on_advance(client, ic, db, clock):
    acc, h, ch = await _simple(db, ic)
    m1, m2 = _mid(db, ch, "1"), _mid(db, ch, "2")
    ui = await _ui(client, db, clock)
    t0 = _now_ms(clock)

    assert await _mark(client, ui, ch, m2) is True
    row = _row(db, ch)
    assert row["message_id"] == m2 and row["account_id"] == acc and row["gen"] == 1
    assert row["not_before_ms"] == t0 + 5000 and row["first_ms"] == t0
    assert _audit_read_ack(db) == ["queued"]

    assert await _lease(ic, h) == []  # задержка 5 с ещё не прошла
    clock.advance_ms(5000)
    (item,) = await _lease(ic, h)
    assert item == {
        "channel_id": ch,
        "gen": 1,
        "lease_gen": 1,
        "channel": {"ext_id": "c1", "kind": "dm", "parent_ext_id": None},
        "message": {"ext_id": "2", "ts": 2000, "ext_sort": 2, "thread_ext_id": None, "is_me": False},
    }
    assert (await _report(ic, h, ch, 1, "done"))["result"] == "deleted"
    assert _row(db, ch) is None

    # повтор той же отметки и отметка назад — маркер не сдвинулся, в очередь ничего
    assert await _mark(client, ui, ch, m2) is False
    assert await _mark(client, ui, ch, m1) is False
    assert _row(db, ch) is None
    assert _audit_read_ack(db) == ["queued", None, None]


async def test_e1_unsupported_source_not_queued(client, ic, db, clock):
    _acc, _h, ch = await _simple(db, ic, source="jira")
    ui = await _ui(client, db, clock)
    assert await _mark(client, ui, ch, _mid(db, ch, "2")) is True
    assert _row(db, ch) is None
    assert _audit_read_ack(db) == ["unsupported"]


# --- E2 + П2: выключатель аккаунта ---


async def test_e2_read_ack_false_not_queued_and_lease_empty(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic, config={"read_ack": False})
    ui = await _ui(client, db, clock)
    assert await _mark(client, ui, ch, _mid(db, ch, "2")) is True
    assert _row(db, ch) is None
    assert _audit_read_ack(db) == ["off"]
    clock.advance_ms(60_000)
    assert await _lease(ic, h) == []


async def test_p2_read_ack_false_old_row_wait_holds_timeout_and_lease_deletes(
    client, ic, db, clock, monkeypatch
):
    acc, h, ch = await _simple(db, ic)
    ui = await _ui(client, db, clock)
    await _mark(client, ui, ch, _mid(db, ch, "2"))
    clock.advance_ms(60_000)  # строка давно созрела
    _set_account(db, acc, config_json=json.dumps({"read_ack": False}))

    calls = []
    real = read_ack_mod.wait_state

    def _counting(conn, **kw):
        calls.append(1)
        return real(conn, **kw)

    monkeypatch.setattr(read_ack_mod, "wait_state", _counting)
    started = time.monotonic()
    r = await ic.get(f"{INTERNAL}/read-ack/wait", params={"timeout_s": 0.5}, headers=h)
    elapsed = time.monotonic() - started
    assert r.json() == {"ready": False}
    assert elapsed >= 0.45
    assert len(calls) <= 3  # горячего цикла нет

    assert await _lease(ic, h) == []
    assert _row(db, ch) is None  # П2: `read_ack=false` — `lease` удаляет строки аккаунта


async def test_p2_limited_account_wait_holds_timeout_row_kept(client, ic, db, clock, monkeypatch):
    acc, h, ch = await _simple(db, ic)
    ui = await _ui(client, db, clock)
    await _mark(client, ui, ch, _mid(db, ch, "2"))
    clock.advance_ms(60_000)
    _set_account(db, acc, status="limited")

    calls = []
    real = read_ack_mod.wait_state

    def _counting(conn, **kw):
        calls.append(1)
        return real(conn, **kw)

    monkeypatch.setattr(read_ack_mod, "wait_state", _counting)
    started = time.monotonic()
    r = await ic.get(f"{INTERNAL}/read-ack/wait", params={"timeout_s": 0.5}, headers=h)
    assert r.json() == {"ready": False}
    assert time.monotonic() - started >= 0.45
    assert len(calls) <= 3

    assert await _lease(ic, h) == []
    assert _row(db, ch) is not None  # строка ждёт, пока аккаунт снова `ok`
    _set_account(db, acc, status="ok")
    assert len(await _lease(ic, h)) == 1


# --- E3: тредовая отметка Slack не толкается ---


async def test_e3_slack_thread_mark_not_queued(client, ic, db, clock):
    _acc, h = _account(db, source="slack")
    parent, reply = "1790000000.000100", "1790000000.000200"
    await _events(
        ic,
        h,
        [
            _up("C1", parent, 1_790_000_000_000, kind="group"),
            _up("C1", reply, 1_790_000_000_001, kind="group", thread=parent),
        ],
    )
    ch = _channel_id(db, "C1")
    ui = await _ui(client, db, clock)
    r = await client.post(
        f"{API}/channels/{ch}/threads/{parent}/read",
        json={"up_to_message_id": _mid(db, ch, reply)},
        headers=ui,
    )
    assert r.status_code == 200, r.text
    assert _row(db, ch) is None


# --- E4: слияние отметок и скольжение задержки ---


async def test_e4_merge_and_sliding_delay_capped(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic, n=6)
    ui = await _ui(client, db, clock)
    t0 = _now_ms(clock)

    await _mark(client, ui, ch, _mid(db, ch, "1"))
    assert _row(db, ch)["not_before_ms"] == t0 + 5000
    clock.advance_ms(4000)
    await _mark(client, ui, ch, _mid(db, ch, "2"))
    assert _row(db, ch)["not_before_ms"] == t0 + 9000  # скользит
    clock.advance_ms(4000)
    await _mark(client, ui, ch, _mid(db, ch, "3"))
    clock.advance_ms(4000)
    await _mark(client, ui, ch, _mid(db, ch, "4"))
    clock.advance_ms(4000)  # t0 + 16 с
    await _mark(client, ui, ch, _mid(db, ch, "5"))
    row = _row(db, ch)
    assert row["not_before_ms"] == t0 + 20_000  # не дальше first_ms + 20 с
    assert row["gen"] == 5 and row["message_id"] == _mid(db, ch, "5") and row["first_ms"] == t0

    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.read_ack").fetchone()[0] == 1

    clock.advance_ms(4000)  # t0 + 20 с
    (item,) = await _lease(ic, h)
    assert item["message"]["ext_id"] == "5" and item["gen"] == 5
    assert _row(db, ch)["first_ms"] is None  # П4: сброс при выдаче


# --- E5: событие `read` источника ---


async def test_e5_source_read_beyond_target_drops_row(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    ui = await _ui(client, db, clock)
    await _mark(client, ui, ch, _mid(db, ch, "2"))
    await _events(ic, h, [{"type": "read", "channel": {"ext_id": "c1", "kind": "dm"}, "up_to_ext_id": "3"}])
    clock.advance_ms(5000)
    assert await _lease(ic, h) == []
    assert _row(db, ch) is None


async def test_e5_source_read_not_beyond_target_still_issued(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    ui = await _ui(client, db, clock)
    await _events(ic, h, [{"type": "read", "channel": {"ext_id": "c1", "kind": "dm"}, "up_to_ext_id": "1"}])
    await _mark(client, ui, ch, _mid(db, ch, "2"))
    # источник догнал ровно до цели — не «строго дальше», задание выдаётся
    await _events(ic, h, [{"type": "read", "channel": {"ext_id": "c1", "kind": "dm"}, "up_to_ext_id": "2"}])
    clock.advance_ms(5000)
    (item,) = await _lease(ic, h)
    assert item["message"]["ext_id"] == "2"


# --- E6: отчёт `done` при сменившейся цели ---


async def test_e6_done_with_changed_gen_keeps_new_target(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    ui = await _ui(client, db, clock)
    await _mark(client, ui, ch, _mid(db, ch, "1"))
    clock.advance_ms(5000)
    (item,) = await _lease(ic, h)
    t1 = _now_ms(clock)
    await _mark(client, ui, ch, _mid(db, ch, "3"))  # UI ушёл дальше, пока задание в работе
    assert (await _report(ic, h, ch, item["lease_gen"], "done"))["result"] == "released"
    row = _row(db, ch)
    assert row["message_id"] == _mid(db, ch, "3") and row["gen"] == 2 and row["lease_until_ms"] is None
    assert row["not_before_ms"] == t1 + 5000
    assert await _lease(ic, h) == []
    clock.advance_ms(5000)
    (item2,) = await _lease(ic, h)
    assert item2["message"]["ext_id"] == "3" and item2["gen"] == 2
    assert (await _report(ic, h, ch, item2["lease_gen"], "drop"))["result"] == "deleted"
    assert _row(db, ch) is None


# --- E7: повторы и пауза ---


async def test_e7_retry_backoff_grows_and_fifth_removes(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    ui = await _ui(client, db, clock)
    await _mark(client, ui, ch, _mid(db, ch, "2"))
    clock.advance_ms(5000)

    # 1-я неудача: источник велел ждать 100 с — больше бэкоффа 30 с, берётся он
    (item,) = await _lease(ic, h)
    now = _now_ms(clock)
    assert (await _report(ic, h, ch, item["lease_gen"], "retry", 100_000))["result"] == "paused"
    row = _row(db, ch)
    assert row["attempts"] == 1 and row["not_before_ms"] == now + 100_000 and row["lease_until_ms"] is None
    clock.advance_ms(99_000)
    assert await _lease(ic, h) == []
    clock.advance_ms(1000)

    for attempt, pause_ms in ((2, 60_000), (3, 120_000), (4, 240_000)):
        (item,) = await _lease(ic, h)
        now = _now_ms(clock)
        await _report(ic, h, ch, item["lease_gen"], "retry")
        row = _row(db, ch)
        assert row["attempts"] == attempt and row["not_before_ms"] == now + pause_ms
        clock.advance_ms(pause_ms - 1)
        assert await _lease(ic, h) == []
        clock.advance_ms(1)

    (item,) = await _lease(ic, h)
    assert (await _report(ic, h, ch, item["lease_gen"], "retry"))["result"] == "gave_up"
    assert _row(db, ch) is None


async def test_p4_flood_pause_survives_new_ui_mark(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    ui = await _ui(client, db, clock)
    await _mark(client, ui, ch, _mid(db, ch, "1"))
    clock.advance_ms(5000)
    (item,) = await _lease(ic, h)
    t_flood = _now_ms(clock)
    await _report(ic, h, ch, item["lease_gen"], "retry", 100_000)  # FloodWait 100 с

    clock.advance_ms(1000)
    await _mark(client, ui, ch, _mid(db, ch, "2"))  # новая отметка UI — пауза источника остаётся
    row = _row(db, ch)
    assert row["attempts"] == 0 and row["not_before_ms"] == t_flood + 100_000
    clock.advance_ms(98_999)
    assert await _lease(ic, h) == []
    clock.advance_ms(1)
    (item,) = await _lease(ic, h)
    assert item["message"]["ext_id"] == "2"


async def test_p4_retry_after_gen_change_does_not_count_attempt_but_pauses(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    ui = await _ui(client, db, clock)
    await _mark(client, ui, ch, _mid(db, ch, "1"))
    clock.advance_ms(5000)
    (item,) = await _lease(ic, h)
    await _mark(client, ui, ch, _mid(db, ch, "2"))  # новая цель, пока старая в работе
    now = _now_ms(clock)
    await _report(ic, h, ch, item["lease_gen"], "retry", 40_000)
    row = _row(db, ch)
    assert row["attempts"] == 0  # попытка была по старой цели
    assert row["pause_until_ms"] == now + 40_000 and row["not_before_ms"] == now + 40_000
    assert row["lease_until_ms"] is None and row["message_id"] == _mid(db, ch, "2")


# --- E8: чужой аккаунт, устаревший лиз, скрытое ---


async def test_e8_foreign_account_404_and_stale_lease_409(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    _other, h_other = _account(db, source="telegram")
    ui = await _ui(client, db, clock)
    await _mark(client, ui, ch, _mid(db, ch, "2"))
    clock.advance_ms(5000)
    assert await _lease(ic, h_other) == []
    (item,) = await _lease(ic, h)
    await _report(ic, h_other, ch, item["lease_gen"], "done", status=404)
    await _report(ic, h, 10**6, item["lease_gen"], "done", status=404)  # строки нет

    clock.advance_ms(60_000)  # лиз истёк — задание выдаётся снова
    (item2,) = await _lease(ic, h)
    assert item2["lease_gen"] == item["lease_gen"] + 1
    await _report(ic, h, ch, item["lease_gen"], "done", status=409)
    assert _row(db, ch) is not None
    await _report(ic, h, ch, item2["lease_gen"], "done")
    assert _row(db, ch) is None


async def test_e8_body_extra_fields_rejected(ic, db):
    _acc, h = _account(db)
    r = await ic.post(f"{INTERNAL}/read-ack/lease", headers=h, json={"limit": 1, "account_id": 9})
    assert r.status_code == 422
    r = await ic.post(
        f"{INTERNAL}/read-ack/report",
        headers=h,
        json={"channel_id": 1, "lease_gen": 1, "outcome": "sent"},
    )
    assert r.status_code == 422
    r = await ic.post(f"{INTERNAL}/read-ack/lease", json={})
    assert r.status_code == 401


async def test_e8_hidden_target_falls_back_hidden_channel_not_issued(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    ui = await _ui(client, db, clock)
    await _mark(client, ui, ch, _mid(db, ch, "3"))
    r = await client.delete(f"{API}/messages/{_mid(db, ch, '3')}", headers=ui)
    assert r.status_code == 204, r.text
    clock.advance_ms(5000)
    (item,) = await _lease(ic, h)
    assert item["message"]["ext_id"] == "2"  # скрытое сообщение не выдаётся
    await _report(ic, h, ch, item["lease_gen"], "done")

    _acc2, h2, ch2 = await _simple(db, ic, ch="c2")
    await _mark(client, ui, ch2, _mid(db, ch2, "2"))
    r = await client.delete(f"{API}/channels/{ch2}", headers=ui)
    assert r.status_code == 204, r.text
    clock.advance_ms(5000)
    assert await _lease(ic, h2) == []
    assert _row(db, ch2) is None


# --- П1: действующая цель ---


async def test_p1_draft_target_resolves_to_last_incoming(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic, n=1)
    ui = await _ui(client, db, clock)
    drafts = []
    for key in ("k1", "k2"):  # две заготовки подряд: более ранняя тоже не цель (у неё нет `ext_id`)
        r = await client.post(
            f"{API}/messages/send",
            json={"channel_id": ch, "text": "черновик", "idempotency_key": key},
            headers=ui,
        )
        assert r.status_code == 200, r.text
        with db.reader() as conn:
            drafts.append(
                conn.execute(
                    "SELECT id FROM main.message WHERE outbox_id = ?", (r.json()["outbox_id"],)
                ).fetchone()[0]
            )
        clock.advance_ms(1000)
    assert await _mark(client, ui, ch, drafts[-1]) is True
    clock.advance_ms(5000)
    (item,) = await _lease(ic, h)
    assert item["message"]["ext_id"] == "1" and item["message"]["ts"] == 1000


async def test_p1_only_draft_no_source_message_row_removed(client, ic, db, clock):
    _acc, h = _account(db)
    # канал с одним своим исходящим, которое ещё не ушло: входящих нет
    await _events(ic, h, [_up("c1", "1", 1000)])
    ch = _channel_id(db, "c1")
    ui = await _ui(client, db, clock)
    await client.delete(f"{API}/messages/{_mid(db, ch, '1')}", headers=ui)
    r = await client.post(
        f"{API}/messages/send", json={"channel_id": ch, "text": "x", "idempotency_key": "k2"}, headers=ui
    )
    with db.reader() as conn:
        draft = conn.execute(
            "SELECT id FROM main.message WHERE outbox_id = ?", (r.json()["outbox_id"],)
        ).fetchone()[0]
    await _mark(client, ui, ch, draft)
    clock.advance_ms(5000)
    assert await _lease(ic, h) == []
    assert _row(db, ch) is None


async def test_p1_mail_own_message_skipped(client, ic, db, clock):
    _acc, h = _account(db, source="mail", db_name="work")
    await _events(
        ic,
        h,
        [
            _up("777", "<in@x>", 1000, kind="mail_thread"),
            _up("777", "<own@x>", 5000, kind="mail_thread", is_me=True),
        ],
    )
    ch = _channel_id(db, "777", "work")
    ui = await _ui(client, db, clock)
    assert await _mark(client, ui, ch, _mid(db, ch, "<own@x>")) is True
    clock.advance_ms(5000)
    (item,) = await _lease(ic, h)
    assert item["channel_id"] == ch and ch >= db_module.WORK_ID_BASE
    assert item["message"]["ext_id"] == "<in@x>" and item["message"]["ts"] == 1000
    assert item["channel"] == {"ext_id": "777", "kind": "mail_thread", "parent_ext_id": None}


async def test_p1_telegram_own_message_is_valid_target(client, ic, db, clock):
    """Не почта: своё сообщение с `ext_id` — законная цель (у Telegram `max_id` по id сообщения)."""
    _acc, h = _account(db)
    await _events(ic, h, [_up("c1", "1", 1000), _up("c1", "2", 2000, is_me=True)])
    ch = _channel_id(db, "c1")
    ui = await _ui(client, db, clock)
    await _mark(client, ui, ch, _mid(db, ch, "2"))
    clock.advance_ms(5000)
    (item,) = await _lease(ic, h)
    assert item["message"]["ext_id"] == "2" and item["message"]["is_me"] is True


async def test_p1_slack_skips_non_broadcast_reply(client, ic, db, clock):
    _acc, h = _account(db, source="slack")
    # до часов хаба (FakeClock — 1_700_000_000 с): заготовка встаёт после них
    parent, reply = "1600000000.000100", "1600000000.000200"
    await _events(
        ic,
        h,
        [
            _up("C1", parent, 1_600_000_000_000, kind="group"),
            _up("C1", reply, 1_600_000_000_001, kind="group", thread=parent),
        ],
    )
    ch = _channel_id(db, "C1")
    ui = await _ui(client, db, clock)
    r = await client.post(
        f"{API}/messages/send", json={"channel_id": ch, "text": "x", "idempotency_key": "k3"}, headers=ui
    )
    with db.reader() as conn:
        draft = conn.execute(
            "SELECT id FROM main.message WHERE outbox_id = ?", (r.json()["outbox_id"],)
        ).fetchone()[0]
    await _mark(client, ui, ch, draft)
    clock.advance_ms(5000)
    (item,) = await _lease(ic, h)
    assert item["message"]["ext_id"] == parent and item["message"]["thread_ext_id"] in (None, parent)


async def test_topic_channel_carries_parent_ext_id(client, ic, db, clock):
    _acc, h = _account(db)
    await _events(ic, h, [_up("-100:5", "10", 1000, kind="topic", parent="-100")])
    ch = _channel_id(db, "-100:5")
    ui = await _ui(client, db, clock)
    await _mark(client, ui, ch, _mid(db, ch, "10"))
    clock.advance_ms(5000)
    (item,) = await _lease(ic, h)
    assert item["channel"] == {"ext_id": "-100:5", "kind": "topic", "parent_ext_id": "-100"}


# --- П7: по одному ------------------------------------------------------------------------------------------


async def test_p7_lease_one_by_default(client, ic, db, clock):
    acc, h = _account(db)
    await _events(ic, h, [_up("c1", "1", 1000), _up("c2", "1", 1000)])
    c1, c2 = _channel_id(db, "c1"), _channel_id(db, "c2")
    ui = await _ui(client, db, clock)
    await _mark(client, ui, c1, _mid(db, c1, "1"))
    await _mark(client, ui, c2, _mid(db, c2, "1"))
    clock.advance_ms(5000)
    first = await _lease(ic, h)
    assert len(first) == 1
    second = await _lease(ic, h)
    assert len(second) == 1 and second[0]["channel_id"] != first[0]["channel_id"]
    assert await _lease(ic, h) == []
    del acc


# --- E9: `wait` ---------------------------------------------------------------------------------------------


async def test_e9_wait_woken_by_enqueue(client, ic, db, clock, monkeypatch):
    _acc, h, ch = await _simple(db, ic)
    ui = await _ui(client, db, clock)
    # Без задержки строка готова сразу после постановки: пробудить `wait` может только уведомление коммита.
    monkeypatch.setattr(read_ack_mod, "DELAY_MS", 0)
    started = time.monotonic()
    task = asyncio.ensure_future(ic.get(f"{INTERNAL}/read-ack/wait", params={"timeout_s": 10}, headers=h))
    await asyncio.sleep(0.1)
    assert not task.done()
    await _mark(client, ui, ch, _mid(db, ch, "2"))
    r = await asyncio.wait_for(task, 5)
    assert r.json() == {"ready": True}
    assert time.monotonic() - started < 3


async def test_e9_wait_returns_near_not_before_not_long_poll_end(client, ic, db, clock):
    _acc, h, ch = await _simple(db, ic)
    ui = await _ui(client, db, clock)
    await _mark(client, ui, ch, _mid(db, ch, "2"))
    clock.advance_ms(4700)  # до срока 0,3 с

    async def _tick():
        await asyncio.sleep(0.1)
        clock.advance_ms(300)  # часы ядра дошли до срока, но никто ничего не пишет — уведомления нет

    started = time.monotonic()
    ticker = asyncio.ensure_future(_tick())
    r = await ic.get(f"{INTERNAL}/read-ack/wait", params={"timeout_s": 10}, headers=h)
    elapsed = time.monotonic() - started
    await ticker
    assert r.json() == {"ready": True}
    assert elapsed < 3


async def test_e9_wait_empty_times_out(ic, db):
    _acc, h = _account(db)
    started = time.monotonic()
    r = await ic.get(f"{INTERNAL}/read-ack/wait", params={"timeout_s": 0.3}, headers=h)
    assert r.json() == {"ready": False}
    assert time.monotonic() - started >= 0.25


# --- E10: миграция 0016 на копии БД предыдущей версии, обе схемы ---


def test_e10_migration_0016_on_previous_version_both_files(tmp_path, monkeypatch):
    original = db_module._migrations

    def _up_to_15(*, main: bool):
        return {v: files for v, files in original(main=main).items() if v <= 15}

    monkeypatch.setattr(db_module, "_migrations", _up_to_15)
    old = Db(tmp_path)
    old.start()
    try:
        for schema, source in (("main", "telegram"), ("work", "slack")):
            acc = insert_account(old, source=source, db_name=schema)

            def _fn(conn, schema=schema, acc=acc):
                cid = conn.execute(
                    f"INSERT INTO {schema}.channel (account_id, ext_id, kind, title, first_seen_ts) "  # noqa: S608
                    "VALUES (?, 'c1', 'dm', 'Вася', 0)",
                    (acc,),
                ).lastrowid
                conn.execute(
                    f"INSERT INTO {schema}.message (seq, first_seq, account_id, channel_id, ext_id, "  # noqa: S608
                    "ts, is_me, kind, text) VALUES (?, ?, ?, ?, '1', 1000, 0, 'text', 'привет')",
                    (1 if schema == "main" else 2, 1 if schema == "main" else 2, acc, cid),
                )

            old.write_sync(_fn)
        with old.reader() as conn:
            for schema in ("main", "work"):
                assert conn.execute(f"PRAGMA {schema}.user_version").fetchone()[0] == 15
                names = {r[0] for r in conn.execute(f"SELECT name FROM {schema}.sqlite_master")}  # noqa: S608
                assert "read_ack" not in names
    finally:
        old.close()

    monkeypatch.setattr(db_module, "_migrations", original)
    new = Db(tmp_path)
    new.start()
    try:
        with new.reader() as conn:
            for schema in ("main", "work"):
                assert conn.execute(f"PRAGMA {schema}.user_version").fetchone()[0] >= 16
                cols = {r[1]: r for r in conn.execute(f"PRAGMA {schema}.table_info(read_ack)")}
                assert set(cols) == {
                    "channel_id",
                    "account_id",
                    "message_id",
                    "gen",
                    "first_ms",
                    "not_before_ms",
                    "pause_until_ms",
                    "attempts",
                    "lease_until_ms",
                    "lease_gen",
                    "leased_gen",
                }
                assert cols["channel_id"][5] == 1  # первичный ключ
                assert cols["first_ms"][3] == 0  # NULL-able (П4)
                assert cols["pause_until_ms"][4] == "0"
                idx = {r[1] for r in conn.execute(f"PRAGMA {schema}.index_list(read_ack)")}
                assert "read_ack_account" in idx
                text = conn.execute(f"SELECT text FROM {schema}.message").fetchall()  # noqa: S608
                assert text == [("привет",)]
    finally:
        new.close()
