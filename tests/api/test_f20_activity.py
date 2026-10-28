"""Ф20-3 (docs/architecture.md): «Владелец активен в хабе» в ядре.

`POST /presence/active` — только UI-сессия; `GET /internal/v1/activity` для коннектора (общее время на оба
приложения, выключатель `slack_status`); действия из UI (прочтение, отправка, реакция) обновляют время,
bearer — нет."""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import connector_auth
from hub.core.activity import Activity
from hub.core.app_internal import create_internal_app
from hub.core.app_public import create_public_app
from hub.core.connector_auth import hash_secret
from hub.core.feed import Notifier

from .conftest import (
    PORT,
    csrf_headers,
    insert_account,
    insert_channel,
    insert_message,
    login_via_code,
    make_bearer_token,
)


@pytest.fixture
def activity():
    return Activity()


@pytest.fixture
def app(db, clock, activity):
    return create_public_app(db, clock=clock, port=PORT, activity=activity)


@pytest.fixture
async def internal(db, clock, activity):
    app = create_internal_app(
        db, clock=clock, notifier=Notifier(), admin_key_hash=hash_secret("x"), activity=activity
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://hub-internal") as c:
        yield c


def _connector(db, *, config: dict | None = None) -> tuple[int, dict]:
    account_id = insert_account(db, source="slack")
    if config is not None:
        db.write_sync(
            lambda conn: conn.execute(
                "UPDATE main.account SET config_json = ? WHERE id = ?", (json.dumps(config), account_id)
            )
        )
    secret = connector_auth.generate_connector_secret()
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.connector_secret (account_id, hash, created_ts) VALUES (?, ?, 0)",
            (account_id, connector_auth.hash_secret(secret)),
        )
    )
    return account_id, {"Authorization": f"Bearer {secret}"}


async def test_presence_active_ui_only_and_internal_reads_it(client, internal, db, clock):
    _, headers = _connector(db)
    assert (await internal.get("/internal/v1/activity", headers=headers)).json()["active_ms"] is None

    wide = make_bearer_token(db, scopes=("read", "draft"), name="wide")
    r = await client.post(
        "/api/v1/presence/active", json={}, headers={"Authorization": f"Bearer {wide}", "Origin": "http://127.0.0.1:8787"}
    )
    assert r.status_code == 403
    assert (await internal.get("/internal/v1/activity", headers=headers)).json()["active_ms"] is None

    cookie, _ = await login_via_code(client, db, clock)
    r = await client.post("/api/v1/presence/active", json={}, headers=csrf_headers(cookie))
    assert r.status_code == 200
    body = (await internal.get("/internal/v1/activity", headers=headers)).json()
    assert body["active_ms"] == int(clock() * 1000)
    assert body["now_ms"] == int(clock() * 1000)
    assert body["enabled"] is True


async def test_internal_activity_needs_connector_secret(internal):
    assert (await internal.get("/internal/v1/activity")).status_code == 401


@pytest.mark.parametrize(
    "config, enabled",
    [
        (None, True),
        ({"slack_status": True}, True),
        ({"slack_status": False}, False),
        ({"read_ack": False}, True),
    ],
)
async def test_slack_status_switch(internal, db, config, enabled):
    _, headers = _connector(db, config=config)
    assert (await internal.get("/internal/v1/activity", headers=headers)).json()["enabled"] is enabled


async def test_ui_actions_touch_activity_bearer_does_not(client, db, clock, activity):
    acc = insert_account(db, source="telegram")
    ch = insert_channel(db, acc, ext_id="C1", kind="group")
    m = insert_message(db, ch, ext_id="1", ts=1000)
    cookie, _ = await login_via_code(client, db, clock)
    h = csrf_headers(cookie)
    assert activity.last_ms is None

    # прочтение
    r = await client.post(f"/api/v1/channels/{ch}/read", json={"up_to_message_id": m}, headers=h)
    assert r.status_code == 200, r.text
    assert activity.last_ms == int(clock() * 1000)

    # отправка
    clock.advance_ms(5_000)
    r = await client.post(
        "/api/v1/messages/send", json={"channel_id": ch, "text": "привет", "idempotency_key": "k1"}, headers=h
    )
    assert r.status_code == 200, r.text
    assert activity.last_ms == int(clock() * 1000)

    # реакция (ответ источника не важен: касание — по самому клику владельца из UI)
    clock.advance_ms(5_000)
    await client.post(f"/api/v1/messages/{m}/reactions", json={"emoji": "👍", "on": True}, headers=h)
    assert activity.last_ms == int(clock() * 1000)


async def test_bearer_send_does_not_touch_activity(client, db, activity):
    acc = insert_account(db, source="telegram")
    ch = insert_channel(db, acc, ext_id="C1", kind="group")
    token = make_bearer_token(db, scopes=("read", "draft"), name="draft-only")
    r = await client.post(
        "/api/v1/messages/send",
        json={"channel_id": ch, "text": "черновик", "idempotency_key": "k2"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert activity.last_ms is None
