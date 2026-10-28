"""Ф10-4 П12: сквозная отправка файла через fake-коннектор стенда — загрузка в ядро, отправка с `files`,
задание с полем `file`, эхо с вложением, одна видимая строка с готовой картинкой."""

import json

from hub.connectors.fake import FakeSource, FaultPlan

from .conftest import (
    create_account,
    csrf_headers,
    get_outbox,
    login_via_code,
    make_connector,
    new_idempotency_key,
    setup_channel,
    visible_rows,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


async def _upload_and_send(public_client, db, clock, channel_id, *, text):
    cookie, _csrf = await login_via_code(public_client, db, clock)
    r = await public_client.post(
        f"/api/v1/uploads?channel_id={channel_id}",
        content=PNG,
        headers={**csrf_headers(cookie), "X-Filename": "shot.png"},
    )
    assert r.status_code == 200, r.text
    upload_id = r.json()["upload_id"]
    r = await public_client.post(
        "/api/v1/messages/send",
        json={
            "channel_id": channel_id,
            "text": text,
            "idempotency_key": new_idempotency_key(),
            "files": [upload_id],
        },
        headers=csrf_headers(cookie),
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _check(public_client, source, *, channel_id, outbox_id, text):
    (delivery,) = source.deliveries
    assert delivery.text == text
    assert delivery.attachments == [
        {"type": "photo", "name": "shot.png", "size": len(PNG), "mime": "image/png"}
    ]
    rows = await visible_rows(public_client, channel_id)
    assert len(rows) == 1, rows
    (row,) = rows
    assert row["kind"] == "file"
    assert json.loads(row["attachments_json"])[0]["name"] == "shot.png"
    assert row["media"][0]["status"] == "ready"
    assert (await get_outbox(public_client, outbox_id))["status"] == "sent"
    r = await public_client.get(f"/api/v1/media/{row['media'][0]['id']}")
    assert r.status_code == 200
    assert r.content == PNG


async def test_send_file_without_caption_through_fake_connector(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_account(admin_client)
    channel_id = setup_channel(db, clock, account_id)
    result = await _upload_and_send(public_client, db, clock, channel_id, text="")

    source = FakeSource()
    connector = make_connector(internal_transport, secret=secret, source=source)
    assert await connector.step_outbox() == 1
    await connector.sync()
    await _check(public_client, source, channel_id=channel_id, outbox_id=result["outbox_id"], text="")


async def test_send_file_echo_before_report_through_fake_connector(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_account(admin_client)
    channel_id = setup_channel(db, clock, account_id)
    result = await _upload_and_send(public_client, db, clock, channel_id, text="смотри")

    source = FakeSource()
    plan = FaultPlan().queue_outbox("echo_before_report")
    connector = make_connector(internal_transport, secret=secret, source=source, plan=plan)
    assert await connector.step_outbox() == 1
    await connector.sync()
    await _check(public_client, source, channel_id=channel_id, outbox_id=result["outbox_id"], text="смотри")
