"""Ф66-5 (`docs/architecture.md`): `last_activity_ts`, `fresh_threads`/`fresh_thread_ext`
беседы и `last.thread_ext_id` в «Нужна реакция». Ответы приходят через настоящий ingest."""

from .conftest import csrf_headers, insert_account, insert_channel, login_via_code
from .test_f20_bots_personal import _ev, _ingest, _now

P = "1790917549.000100"
R1 = "1790917600.000200"
R2 = "1790917700.000300"


def _channel(db):
    acc = insert_account(db, source="slack")
    return acc, insert_channel(db, acc, ext_id="C9", kind="group")


async def _channel_json(client, ch):
    r = await client.get(f"/api/v1/channels/{ch}")
    assert r.status_code == 200, r.text
    return r.json()


async def test_foreign_reply_in_unsubscribed_thread_lifts_activity_not_last_msg(client, db, clock):
    acc, ch = _channel(db)
    now = _now(clock)
    await login_via_code(client, db, clock)
    _ingest(db, acc, [_ev("C9", P, now - 5000)], now - 5000)
    before = await _channel_json(client, ch)
    seq_before = (await client.get("/api/v1/sync", params={"since": 0})).json()["max_seq"]
    assert before["fresh_threads"] == 0 and before["fresh_thread_ext"] is None

    _ingest(db, acc, [_ev("C9", R1, now, thread=P)], now)
    after = await _channel_json(client, ch)
    assert after["last_activity_ts"] == now
    assert after["last_msg_ts"] == before["last_msg_ts"] != now  # last_msg_ts правилами не двигается
    assert after["unread"] == before["unread"] and after["unread_threads"] == 0  # метка, не непрочитанное
    assert after["fresh_threads"] == 1 and after["fresh_thread_ext"] == P

    sync = (await client.get("/api/v1/sync", params={"since": seq_before})).json()
    changed = [c for c in sync["channels"] if c["id"] == ch]
    assert changed, "ответ в треде должен двигать seq беседы"
    assert changed[0]["last_activity_ts"] == now
    assert changed[0]["fresh_thread_ext"] == P


async def test_opened_thread_is_not_fresh_and_latest_thread_wins(client, db, clock):
    acc, ch = _channel(db)
    now = _now(clock)
    cookie, csrf = await login_via_code(client, db, clock)
    other = "1790917800.000400"
    _ingest(
        db,
        acc,
        [
            _ev("C9", P, now - 9000),
            _ev("C9", other, now - 8000),
            _ev("C9", R1, now - 3000, thread=P),
            _ev("C9", R2, now - 2000, thread=other),
        ],
        now,
    )
    info = await _channel_json(client, ch)
    assert info["fresh_threads"] == 2 and info["fresh_thread_ext"] == other

    with db.reader() as conn:
        mid = conn.execute(
            "SELECT id FROM main.message WHERE channel_id = ? AND ext_id = ?", (ch, R2)
        ).fetchone()[0]
    r = await client.post(
        f"/api/v1/channels/{ch}/threads/{other}/read",
        json={"up_to_message_id": mid},
        headers=csrf_headers(cookie),
    )
    assert r.status_code == 200, r.text
    info = await _channel_json(client, ch)
    assert info["fresh_threads"] == 1 and info["fresh_thread_ext"] == P


async def test_my_reply_is_not_fresh_but_lifts_activity(client, db, clock):
    acc, ch = _channel(db)
    now = _now(clock)
    await login_via_code(client, db, clock)
    _ingest(db, acc, [_ev("C9", P, now - 5000), _ev("C9", R1, now, thread=P, is_me=True)], now)
    info = await _channel_json(client, ch)
    assert info["fresh_threads"] == 0
    assert info["last_activity_ts"] == now


async def test_home_react_last_carries_thread_ext_id(client, db, clock):
    acc, ch = _channel(db)
    now = _now(clock)
    await login_via_code(client, db, clock)
    events = [_ev("C9", P, now - 5000, mention=True), _ev("C9", R1, now, thread=P, mention=True)]
    _ingest(db, acc, events, now)
    r = await client.get("/api/v1/home/react")
    assert r.status_code == 200, r.text
    body = r.json()
    items = body["items"] if isinstance(body, dict) else body
    mine = [i for i in items if i["channel"] and i["channel"]["id"] == ch]
    assert mine and mine[0]["last"]["thread_ext_id"] == P


async def test_home_react_last_thread_ext_id_null_for_top_message(client, db, clock):
    acc, ch = _channel(db)
    now = _now(clock)
    await login_via_code(client, db, clock)
    _ingest(db, acc, [_ev("C9", P, now, mention=True)], now)
    body = (await client.get("/api/v1/home/react")).json()
    items = body["items"] if isinstance(body, dict) else body
    mine = [i for i in items if i["channel"] and i["channel"]["id"] == ch]
    assert mine and mine[0]["last"]["thread_ext_id"] is None


async def test_unsubscribed_reply_older_than_channel_read_is_not_fresh_subscribed_is(client, db, clock):
    """Головная 16.10: неподписанный тред со старым ответом (до прочтения беседы) — не «свежий»; подписанный
    тред с непрочитанным — цель клика, даже если ответ старше прочтения беседы."""
    acc, ch = _channel(db)
    now = _now(clock)
    await login_via_code(client, db, clock)
    _ingest(db, acc, [_ev("C9", P, now - 9000), _ev("C9", R1, now - 5000, thread=P)], now - 5000)
    read_sql = "UPDATE main.channel SET last_read_ts = ? WHERE id = ?"
    db.write_sync(lambda c: c.execute(read_sql, (now - 1000, ch)))
    info = await _channel_json(client, ch)
    assert info["fresh_threads"] == 0 and info["fresh_thread_ext"] is None
    sub_sql = "UPDATE main.thread SET subscribed = 1 WHERE channel_id = ? AND ext_id = ?"
    db.write_sync(lambda c: c.execute(sub_sql, (ch, P)))
    info = await _channel_json(client, ch)
    assert info["fresh_threads"] == 1 and info["fresh_thread_ext"] == P
