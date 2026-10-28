"""Тесты 7 и 8 куска 1a фазы 1 (`docs/architecture.md`): `GET /channels/heads`,
`status='ok'` не снимает `limited`, `hub account resume` снимает и чистит `flood_until`, `lease()`/
`has_leasable` пусты при `limited`/`auth_required`. Обвязка — `tests/internal/conftest.py`."""

from hub.core.outbox import enqueue

from .conftest import admin_headers, connector_headers, insert_api_token_row, make_connector_account


def _insert_channel(db, account_id, *, ext_id, kind="dm", parent_channel_id=None, own_id_space=False):
    import json

    meta = json.dumps({"own_id_space": own_id_space})
    return db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.channel (account_id, ext_id, kind, parent_channel_id, meta_json, map_source) "
            "VALUES (?, ?, ?, ?, ?, 'auto')",
            (account_id, ext_id, kind, parent_channel_id, meta),
        ).lastrowid
    )


def _insert_message(db, channel_id, *, ext_id, ts, is_me=0):
    from hub.core.db import next_seq

    def _fn(conn):
        seq = next_seq(conn)
        conn.execute(
            "INSERT INTO main.message (seq, first_seq, account_id, channel_id, ext_id, ext_sort, ts, is_me, "
            "kind, text) VALUES (?, ?, (SELECT account_id FROM main.channel WHERE id = ?), ?, ?, ?, ?, ?, "
            "'text', 'x')",
            (seq, seq, channel_id, channel_id, ext_id, int(ext_id) if ext_id.isdigit() else 0, ts, is_me),
        )

    db.write_sync(_fn)


# --- Тест 7: GET /channels/heads ----------------------------------------------------------------------


async def test_1a_07_channels_heads_ignores_outbox_placeholder_and_numeric_order(internal_client, db) -> None:
    account_id, secret = make_connector_account(db)
    channel_id = _insert_channel(db, account_id, ext_id="c1")
    _insert_message(db, channel_id, ext_id="99", ts=1000)
    _insert_message(db, channel_id, ext_id="100", ts=1001)

    api_token_id = insert_api_token_row(db)
    db.write_sync(
        lambda conn: enqueue(
            conn,
            channel_id=channel_id,
            text="черновик",
            origin="ui",
            api_token_id=api_token_id,
            idempotency_key="k1",
            now_ms=2000,
        )
    )

    r = await internal_client.get("/internal/v1/channels/heads", headers=connector_headers(secret))
    assert r.status_code == 200, r.text
    heads = r.json()
    assert len(heads) == 1
    assert heads[0]["ext_id"] == "c1"
    assert heads[0]["max_ext_sort"] == 100  # заготовка outbox (ext_id=NULL) не в счёт, числовой порядок


async def test_1a_07_channels_heads_hides_other_account(internal_client, db) -> None:
    account_id, secret = make_connector_account(db)
    other_id, _other_secret = make_connector_account(db)
    _insert_channel(db, account_id, ext_id="mine")
    _insert_channel(db, other_id, ext_id="not-mine")

    r = await internal_client.get("/internal/v1/channels/heads", headers=connector_headers(secret))
    assert r.status_code == 200, r.text
    ext_ids = {h["ext_id"] for h in r.json()}
    assert ext_ids == {"mine"}


async def test_1a_07_channels_heads_parent_ext_id_and_own_id_space(internal_client, db) -> None:
    account_id, secret = make_connector_account(db)
    forum_id = _insert_channel(db, account_id, ext_id="forum", kind="group")
    _insert_channel(
        db, account_id, ext_id="forum:1", kind="topic", parent_channel_id=forum_id, own_id_space=True
    )

    r = await internal_client.get("/internal/v1/channels/heads", headers=connector_headers(secret))
    by_ext = {h["ext_id"]: h for h in r.json()}
    assert by_ext["forum:1"]["parent_ext_id"] == "forum"
    assert by_ext["forum:1"]["own_id_space"] is True
    assert by_ext["forum"]["parent_ext_id"] is None


# --- Тест 8: status='ok' не снимает limited; resume снимает; lease/has_leasable пусты -------------------


async def test_1a_08_ok_status_does_not_clear_limited_but_updates_heartbeat(
    internal_client, db, clock
) -> None:
    account_id, secret = make_connector_account(db, flood_until=None)
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.account SET status = 'limited' WHERE id = ?", (account_id,))
    )

    r = await internal_client.post(
        "/internal/v1/account/status", headers=connector_headers(secret), json={"status": "ok"}
    )
    assert r.status_code == 200, r.text

    with db.reader() as conn:
        status, last_ok_ts = conn.execute(
            "SELECT status, last_ok_ts FROM main.account WHERE id = ?", (account_id,)
        ).fetchone()
    assert status == "limited"
    assert last_ok_ts is not None  # пульс всё равно обновился


async def test_1a_08_admin_resume_clears_limited_and_flood_until(internal_client, db) -> None:
    account_id, _secret = make_connector_account(db, flood_until=999_999)
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.account SET status = 'limited' WHERE id = ?", (account_id,))
    )

    r = await internal_client.post(
        f"/internal/v1/admin/accounts/{account_id}/resume", headers=admin_headers()
    )
    assert r.status_code == 200, r.text

    with db.reader() as conn:
        status, flood_until = conn.execute(
            "SELECT status, flood_until FROM main.account WHERE id = ?", (account_id,)
        ).fetchone()
    assert status == "ok"
    assert flood_until is None

    with db.reader() as conn:
        audit = conn.execute(
            "SELECT action FROM main.audit WHERE action = 'account_resume'"
        ).fetchone()
    assert audit is not None


async def test_1a_08_resume_unknown_account_is_404(internal_client) -> None:
    r = await internal_client.post("/internal/v1/admin/accounts/999999/resume", headers=admin_headers())
    assert r.status_code == 404


async def test_1a_08_lease_and_has_leasable_empty_when_limited_or_auth_required(internal_client, db) -> None:
    for status in ("limited", "auth_required"):
        account_id, secret = make_connector_account(db)
        channel_id = _insert_channel(db, account_id, ext_id=f"c-{status}")
        api_token_id = insert_api_token_row(db)
        db.write_sync(
            lambda conn, channel_id=channel_id, api_token_id=api_token_id: enqueue(
                conn,
                channel_id=channel_id,
                text="hi",
                origin="ui",
                api_token_id=api_token_id,
                idempotency_key="k",
                now_ms=1000,
            )
        )
        db.write_sync(
            lambda conn, account_id=account_id, status=status: conn.execute(
                "UPDATE main.account SET status = ? WHERE id = ?", (status, account_id)
            )
        )

        r = await internal_client.post(
            "/internal/v1/outbox/lease", headers=connector_headers(secret), json={}
        )
        assert r.status_code == 200, r.text
        assert r.json() == []

        r2 = await internal_client.get(
            "/internal/v1/outbox/wait", headers=connector_headers(secret), params={"timeout_s": 0}
        )
        assert r2.status_code == 200, r2.text
        assert r2.json() == {"ready": False}
