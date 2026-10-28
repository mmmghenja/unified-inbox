"""`POST /outbox/{id}/send-now` — «Отправить сейчас» у подтверждённой строки в очереди:
только кука интерфейса, отказ «не ждёт» — 409 с причиной."""

import datetime as dt
import json
import zoneinfo

import pytest

from .conftest import csrf_headers, insert_channel, insert_project, login_via_code, make_bearer_token

MSK = zoneinfo.ZoneInfo("Europe/Moscow")
EVENING_S = dt.datetime(2026, 9, 28, 21, 0, tzinfo=MSK).timestamp()  # вне окна графика 10–19


@pytest.fixture
def stand(db, clock):
    clock.value = EVENING_S
    project = insert_project(db, name="Umbrella")
    account = db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, config_json) VALUES ('telegram', 'main', ?)",
                (json.dumps({"idempotent_send": True}),),
            ).lastrowid
        )
    )
    channels = [insert_channel(db, account, project_id=project, ext_id=f"u{i}") for i in range(2)]
    token = make_bearer_token(db, scopes=("read", "draft"), project_ids=[project], name="mcp:iip")
    return {"project": project, "account": account, "channels": channels, "token": token}


async def _paced_approved(client, db, clock, stand, channel_id):
    r = await client.post(
        "/api/v1/messages/send",
        json={
            "channel_id": channel_id,
            "text": "Напоминаю",
            "idempotency_key": f"k{channel_id}",
            "paced": True,
        },
        headers={"Authorization": f"Bearer {stand['token']}"},
    )
    assert r.status_code == 200, r.text
    outbox_id = r.json()["outbox_id"]
    cookie, _csrf = await login_via_code(client, db, clock)
    r = await client.post(f"/api/v1/outbox/{outbox_id}/approve", json={}, headers=csrf_headers(cookie))
    assert r.status_code == 200, r.text
    assert r.json()["not_before_ts"] > int(clock() * 1000)  # ждёт утра
    return outbox_id, cookie


def _row(db, outbox_id):
    with db.reader() as conn:
        return conn.execute(
            "SELECT status, paced, not_before_ts FROM main.outbox WHERE id = ?", (outbox_id,)
        ).fetchone()


async def test_send_now_by_cookie_and_second_click_is_409(client, db, clock, stand):
    outbox_id, cookie = await _paced_approved(client, db, clock, stand, stand["channels"][0])
    r = await client.post(f"/api/v1/outbox/{outbox_id}/send-now", headers=csrf_headers(cookie))
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "queued"}
    assert _row(db, outbox_id) == ("queued", 0, int(clock() * 1000))

    r = await client.post(f"/api/v1/outbox/{outbox_id}/send-now", headers=csrf_headers(cookie))
    assert r.status_code == 409
    assert r.json() == {"detail": "не ждёт — уходит обычным порядком", "error": "not_waiting"}


async def test_send_now_bearer_is_403_and_unknown_is_404(client, db, clock, stand):
    outbox_id, cookie = await _paced_approved(client, db, clock, stand, stand["channels"][0])
    r = await client.post(
        f"/api/v1/outbox/{outbox_id}/send-now", headers={"Authorization": f"Bearer {stand['token']}"}
    )
    assert r.status_code == 403
    assert _row(db, outbox_id)[1] == 1  # график не снят
    r = await client.post("/api/v1/outbox/999999/send-now", headers=csrf_headers(cookie))
    assert r.status_code == 404
