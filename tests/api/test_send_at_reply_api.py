"""Ф30-2 на публичном API: `cancel_if_reply` у `POST /messages/send` (только bearer и только с `send_at`),
выдача флажка в outbox и сообщениях (`docs/architecture.md`, B1)."""

import json

from .conftest import csrf_headers, login_via_code
from .test_send_at_api import AT, _bearer, stand  # noqa: F401 — фикстура стенда Ф30-1


async def _send(client, headers, channel_id, *, key, send_at="2026-10-05 10:00", cancel_if_reply=True):
    body: dict = {"channel_id": channel_id, "text": "Напоминаю про договор", "idempotency_key": key}
    if send_at is not None:
        body["send_at"] = send_at
    if cancel_if_reply is not None:
        body["cancel_if_reply"] = cancel_if_reply
    return await client.post("/api/v1/messages/send", json=body, headers=headers)


def _count(db) -> int:
    with db.reader() as conn:
        return conn.execute("SELECT COUNT(*) FROM main.outbox").fetchone()[0]


async def test_b1_without_send_at_is_422(client, db, stand):  # noqa: F811
    r = await _send(client, _bearer(stand["token"]), stand["channels"][0], key="a", send_at=None)
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "cancel_if_reply — только вместе с send_at"
    assert _count(db) == 0


async def test_b1_ui_cancel_if_reply_is_422(client, db, clock, stand):  # noqa: F811
    cookie, _csrf = await login_via_code(client, db, clock)
    r = await _send(client, csrf_headers(cookie), stand["channels"][0], key="ui", send_at=None)
    assert r.status_code == 422 and "cancel_if_reply" in r.text
    assert _count(db) == 0


async def test_b1_stored_and_visible_in_outputs(client, db, clock, stand):  # noqa: F811
    channel = stand["channels"][0]
    r = await _send(client, _bearer(stand["token"]), channel, key="f")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["status"], body["send_at_ts"], body["cancel_if_reply"]) == ("pending_approval", AT, True)
    oid = body["outbox_id"]
    with db.reader() as conn:
        (detail,) = conn.execute(
            "SELECT detail_json FROM main.audit WHERE action = 'message_send' AND outbox_id = ?", (oid,)
        ).fetchone()
    assert json.loads(detail)["cancel_if_reply"] is True

    plain = await _send(client, _bearer(stand["token"]), channel, key="p", cancel_if_reply=None)
    assert plain.json()["cancel_if_reply"] is False

    await login_via_code(client, db, clock)
    r = await client.get("/api/v1/outbox", params={"status": "pending_approval"})
    flags = {item["id"]: item["cancel_if_reply"] for item in r.json()}
    assert flags == {oid: True, plain.json()["outbox_id"]: False}
    r = await client.get(f"/api/v1/outbox/{oid}")
    assert r.json()["cancel_if_reply"] is True
    r = await client.get(f"/api/v1/channels/{channel}/messages")
    by_outbox = {m["outbox_id"]: m["outbox_cancel_if_reply"] for m in r.json()["messages"]}
    assert by_outbox == {oid: True, plain.json()["outbox_id"]: False}
