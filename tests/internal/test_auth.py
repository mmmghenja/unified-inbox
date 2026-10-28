"""Тест 2 (плохие сочетания секретов -> 401) и тест 3 (изоляция аккаунтов на внутреннем сокете).
Спецификация: docs/architecture.md, 5a.
"""

import pytest

from .conftest import (
    admin_headers,
    connector_headers,
    enqueue_outbox,
    insert_channel,
    make_connector_account,
)


@pytest.mark.parametrize(
    "headers",
    [
        {},  # без секрета
        {"Authorization": "Bearer garbage"},  # мусорный
        {"Authorization": "Bearer hub_notarealtoken0123456789"},  # bearer публичного API
    ],
)
async def test_connector_route_rejects_bad_auth(internal_client, headers) -> None:
    r = await internal_client.get("/internal/v1/cursor", headers=headers)
    assert r.status_code == 401


async def test_admin_secret_on_connector_route_rejected(internal_client, db) -> None:
    _account_id, _secret = make_connector_account(db)
    r = await internal_client.get("/internal/v1/cursor", headers=admin_headers())
    assert r.status_code == 401


async def test_connector_secret_on_admin_route_rejected(internal_client, db) -> None:
    _account_id, secret = make_connector_account(db)
    r = await internal_client.post("/internal/v1/admin/login-code", headers=connector_headers(secret))
    assert r.status_code == 401


async def test_public_bearer_prefix_on_admin_route_rejected(internal_client) -> None:
    r = await internal_client.post(
        "/internal/v1/admin/login-code", headers={"Authorization": "Bearer hub_0123456789"}
    )
    assert r.status_code == 401


async def test_valid_connector_secret_passes(internal_client, db) -> None:
    account_id, secret = make_connector_account(db)
    insert_channel(db, account_id, ext_id="c1")
    r = await internal_client.get("/internal/v1/cursor", headers=connector_headers(secret))
    assert r.status_code == 200
    assert r.json() is None


async def test_valid_admin_secret_passes(internal_client) -> None:
    r = await internal_client.post("/internal/v1/admin/login-code", headers=admin_headers())
    assert r.status_code == 200
    assert "code" in r.json()


# --- изоляция между аккаунтами (тест 3) ---------------------------------------------------------------


async def test_connector_a_cannot_write_event_to_account_b(internal_client, db) -> None:
    account_a, secret_a = make_connector_account(db, source="tg")
    account_b, _secret_b = make_connector_account(db, source="slack")
    insert_channel(db, account_a, ext_id="a1")
    insert_channel(db, account_b, ext_id="b1")

    # account_id в теле — лишнее поле, отвергается на уровне валидации, а не «применяется молча».
    r = await internal_client.post(
        "/internal/v1/events",
        headers=connector_headers(secret_a),
        json={"account_id": account_b, "events": []},
    )
    assert r.status_code == 422


async def test_connector_a_cannot_lease_account_b(internal_client, db) -> None:
    account_a, secret_a = make_connector_account(db, source="tg")
    account_b, _secret_b = make_connector_account(db, source="slack")
    channel_a = insert_channel(db, account_a, ext_id="a1")
    channel_b = insert_channel(db, account_b, ext_id="b1")
    enqueue_outbox(db, channel_a, text="own")
    outbox_b = enqueue_outbox(db, channel_b, text="theirs").outbox_id

    r = await internal_client.post("/internal/v1/outbox/lease", headers=connector_headers(secret_a), json={})
    assert r.status_code == 200
    leased_ids = {item["outbox_id"] for item in r.json()}
    assert outbox_b not in leased_ids
    assert leased_ids  # своё-то выдалось


async def test_connector_a_cannot_report_account_b_outbox(internal_client, db) -> None:
    account_a, secret_a = make_connector_account(db, source="tg")
    account_b, secret_b = make_connector_account(db, source="slack")
    channel_b = insert_channel(db, account_b, ext_id="b1")
    result_b = enqueue_outbox(db, channel_b, text="theirs")

    lease_r = await internal_client.post(
        "/internal/v1/outbox/lease", headers=connector_headers(secret_b), json={}
    )
    assert lease_r.status_code == 200
    lease_gen = lease_r.json()[0]["lease_gen"]

    r = await internal_client.post(
        "/internal/v1/outbox/report",
        headers=connector_headers(secret_a),
        json={
            "outbox_id": result_b.outbox_id,
            "lease_gen": lease_gen,
            "outcome": "sent",
            "ext_id": "e1",
        },
    )
    assert r.status_code == 404


async def test_connector_a_cannot_read_account_b_cursor(internal_client, db) -> None:
    account_a, secret_a = make_connector_account(db, source="tg")
    account_b, _secret_b = make_connector_account(db, source="slack")

    def _set_cursor(conn):
        conn.execute(
            "INSERT INTO main.cursor (account_id, value, ts, updated_ts) VALUES (?, 'b-cursor', 1, 1)",
            (account_b,),
        )

    db.write_sync(_set_cursor)

    r = await internal_client.get("/internal/v1/cursor", headers=connector_headers(secret_a))
    assert r.status_code == 200
    assert r.json() is None
