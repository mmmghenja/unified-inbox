"""`GET /outbox?status=` — очереди, ждущие человека (кусок 6: кликабельные счётчики верхней полосы)."""

from .conftest import (
    csrf_headers,
    insert_account,
    insert_channel,
    insert_project,
    login_via_code,
    make_bearer_token,
)


async def _send(client, token, channel_id, text, key):
    r = await client.post(
        "/api/v1/messages/send",
        json={"channel_id": channel_id, "text": text, "idempotency_key": key},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending_approval"
    return r.json()["outbox_id"]


async def test_outbox_list_covers_both_files_and_respects_project_scope(client, db, clock):
    project_a = insert_project(db, name="A")
    project_b = insert_project(db, name="B")
    acc_main = insert_account(db)
    acc_work = insert_account(db, source="slack", db_name="work")
    ch_a = insert_channel(
        db, acc_main, project_id=project_a, my_msg_count=0, title="<img src=x onerror=alert(1)>"
    )
    ch_b = insert_channel(db, acc_work, project_id=project_b, my_msg_count=0)

    sender = make_bearer_token(db, scopes=("read", "send"), name="sender")
    id_a = await _send(client, sender, ch_a, "в проект A", "k-a")
    clock.advance_ms(1000)
    id_b = await _send(client, sender, ch_b, "в проект B", "k-b")

    # bearer проекта A видит только свою запись — ни id, ни текста из проекта B
    token_a = make_bearer_token(db, scopes=("read",), project_ids=[project_a], name="only-a")
    r = await client.get(
        "/api/v1/outbox",
        params={"status": "pending_approval"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert r.status_code == 200, r.text
    assert [item["id"] for item in r.json()] == [id_a]
    assert "проект B" not in r.text

    # UI-сессия видит обе, новые сверху; название канала — побайтно, как лежит
    client.headers.pop("Authorization", None)
    await login_via_code(client, db, clock)
    r = await client.get("/api/v1/outbox", params={"status": "pending_approval"})
    assert [item["id"] for item in r.json()] == [id_b, id_a]
    assert r.json()[1]["channel_title"] == "<img src=x onerror=alert(1)>"
    assert r.json()[1]["origin"] == "api"

    r = await client.get("/api/v1/outbox", params={"status": "unknown"})
    assert r.json() == []

    # /status: outbox_approval — только ждущие человека; outbox_pending включает ещё queued/sending
    ch_trusted = insert_channel(
        db, acc_main, project_id=project_a, my_msg_count=5, first_seen_ts=0, ext_id="t"
    )
    r = await client.post(
        "/api/v1/messages/send",
        json={"channel_id": ch_trusted, "text": "сразу в очередь", "idempotency_key": "k-ui"},
        headers=csrf_headers(client.cookies.get("hub_session")),
    )
    assert r.json()["status"] == "queued", r.text
    status = (await client.get("/api/v1/status")).json()
    assert (status["outbox_approval"], status["outbox_pending"], status["outbox_unknown"]) == (2, 3, 0)


async def test_outbox_list_rejects_other_statuses_and_anonymous(client, db, clock):
    r = await client.get("/api/v1/outbox", params={"status": "pending_approval"})
    assert r.status_code == 401

    await login_via_code(client, db, clock)
    # `sent` списком отдаётся с Ф8-6 (A2: «Отправлено сегодня» на главной) — см. tests/api/test_home.py;
    # `queued` — с Ф12-2 (запланированные по графику) — см. tests/api/test_paced_send.py.
    for status in ("sending", "cancelled", "nope", ""):
        r = await client.get("/api/v1/outbox", params={"status": status})
        assert r.status_code == 422, status
    assert (await client.get("/api/v1/outbox")).status_code == 422
    assert (await client.get("/api/v1/outbox", params={"status": "failed", "limit": 0})).status_code == 422
