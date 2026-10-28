"""Тест 5 (`lease`/`report` happy path, устаревший `lease_gen` -> 409, `temporary` на reconcile-лизе -> 409)
и тест 6 (`/outbox/wait`). Спецификация: docs/architecture.md.
"""

import asyncio

from .conftest import connector_headers, enqueue_outbox, insert_channel, make_connector_account


async def test_lease_report_happy_path_to_sent(internal_client, db) -> None:
    account_id, secret = make_connector_account(db)
    channel_id = insert_channel(db, account_id, ext_id="c1")
    enqueue_outbox(db, channel_id, text="hi")

    lease_r = await internal_client.post(
        "/internal/v1/outbox/lease", headers=connector_headers(secret), json={}
    )
    assert lease_r.status_code == 200
    (item,) = lease_r.json()
    assert item["mode"] == "send"

    report_r = await internal_client.post(
        "/internal/v1/outbox/report",
        headers=connector_headers(secret),
        json={
            "outbox_id": item["outbox_id"],
            "lease_gen": item["lease_gen"],
            "outcome": "sent",
            "ext_id": "ext-1",
        },
    )
    assert report_r.status_code == 200


async def test_report_with_stale_lease_gen_is_409(internal_client, db) -> None:
    account_id, secret = make_connector_account(db)
    channel_id = insert_channel(db, account_id, ext_id="c1")
    enqueue_outbox(db, channel_id, text="hi")

    lease_r = await internal_client.post(
        "/internal/v1/outbox/lease", headers=connector_headers(secret), json={}
    )
    (item,) = lease_r.json()

    r = await internal_client.post(
        "/internal/v1/outbox/report",
        headers=connector_headers(secret),
        json={
            "outbox_id": item["outbox_id"],
            "lease_gen": item["lease_gen"] + 1,  # устаревшее/неверное поколение
            "outcome": "sent",
            "ext_id": "ext-1",
        },
    )
    assert r.status_code == 409


async def test_temporary_on_reconcile_lease_is_409(internal_client, db, clock) -> None:
    account_id, secret = make_connector_account(db)  # не идемпотентный аккаунт -> истёкший лиз => reconcile
    channel_id = insert_channel(db, account_id, ext_id="c1")
    enqueue_outbox(db, channel_id, text="hi")

    first = await internal_client.post(
        "/internal/v1/outbox/lease", headers=connector_headers(secret), json={"lease_ms": 1_000}
    )
    (item,) = first.json()
    assert item["mode"] == "send"

    clock.advance_ms(2_000)  # лиз истёк по настенным часам

    second = await internal_client.post(
        "/internal/v1/outbox/lease", headers=connector_headers(secret), json={}
    )
    (reconciled,) = second.json()
    assert reconciled["mode"] == "reconcile"

    r = await internal_client.post(
        "/internal/v1/outbox/report",
        headers=connector_headers(secret),
        json={
            "outbox_id": reconciled["outbox_id"],
            "lease_gen": reconciled["lease_gen"],
            "outcome": "temporary",
        },
    )
    assert r.status_code == 409


# --- /outbox/wait (тест 6) -------------------------------------------------------------------------------


async def test_wait_returns_immediately_when_row_ready(internal_client, db) -> None:
    account_id, secret = make_connector_account(db)
    channel_id = insert_channel(db, account_id, ext_id="c1")
    enqueue_outbox(db, channel_id, text="hi")

    r = await internal_client.get(
        "/internal/v1/outbox/wait", headers=connector_headers(secret), params={"timeout_s": 5}
    )
    assert r.status_code == 200
    assert r.json() == {"ready": True}


async def test_wait_times_out_when_nothing_ready(internal_client, db) -> None:
    account_id, secret = make_connector_account(db)
    insert_channel(db, account_id, ext_id="c1")

    r = await internal_client.get(
        "/internal/v1/outbox/wait", headers=connector_headers(secret), params={"timeout_s": 0.05}
    )
    assert r.status_code == 200
    assert r.json() == {"ready": False}


async def test_wait_wakes_up_on_enqueue_from_another_request(internal_client, db) -> None:
    account_id, secret = make_connector_account(db)
    channel_id = insert_channel(db, account_id, ext_id="c1")

    wait_task = asyncio.ensure_future(
        internal_client.get(
            "/internal/v1/outbox/wait", headers=connector_headers(secret), params={"timeout_s": 5}
        )
    )
    await asyncio.sleep(0.05)
    assert not wait_task.done()

    enqueue_outbox(db, channel_id, text="hi")  # синхронная запись -> on_commit -> notifier.notify()

    r = await asyncio.wait_for(wait_task, timeout=2)
    assert r.status_code == 200
    assert r.json() == {"ready": True}


async def test_wait_not_woken_by_other_account(internal_client, db) -> None:
    account_a, secret_a = make_connector_account(db, source="tg")
    account_b, _secret_b = make_connector_account(db, source="slack")
    insert_channel(db, account_a, ext_id="a1")
    channel_b = insert_channel(db, account_b, ext_id="b1")

    wait_task = asyncio.ensure_future(
        internal_client.get(
            "/internal/v1/outbox/wait", headers=connector_headers(secret_a), params={"timeout_s": 0.2}
        )
    )
    await asyncio.sleep(0.02)
    enqueue_outbox(db, channel_b, text="not yours")  # будит Notifier, но не готовит A

    r = await asyncio.wait_for(wait_task, timeout=2)
    assert r.status_code == 200
    assert r.json() == {"ready": False}
