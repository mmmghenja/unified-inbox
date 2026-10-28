"""Ф69-1 сквозь: публичный API -> outbox -> `TelegramConnector` (`FakeGateway`) -> отчёт PeerFlood -> стоп
только новых людей; кнопка «Снять стоп» (`POST /api/v1/accounts/{id}/resume`, контракт П7). Стенд —
`tests/e2e/test_telegram_send.py`."""

from connectors.telegram.helpers import FakeGateway
from httpx import ASGITransport, AsyncClient
from telethon import errors

from hub.core import peer_flood
from hub.core.db import next_seq

from .conftest import (
    PUBLIC_BASE_URL,
    create_project,
    create_token,
    csrf_headers,
    get_outbox,
    login_via_code,
    send_ui,
)
from .test_telegram_send import channel_row, create_tg_account, make_tg_connector, seed_dm, seed_group


def _inbound(db, account_id, channel_id) -> None:
    def _fn(conn):
        seq = next_seq(conn)
        conn.execute(
            "INSERT INTO main.message (seq, first_seq, account_id, channel_id, ext_id, ts, is_me, kind, "
            "text) "
            "VALUES (?, ?, ?, ?, 'in1', 1, 0, 'text', 'привет')",
            (seq, seq, account_id, channel_id),
        )

    db.write_sync(_fn)


def _account(db, account_id) -> tuple:
    with db.reader() as conn:
        return conn.execute(
            "SELECT status, last_error, peer_flood_ts, flood_until FROM main.account WHERE id = ?",
            (account_id,),
        ).fetchone()


async def _resume(public_client, cookie, account_id):
    return await public_client.post(f"/api/v1/accounts/{account_id}/resume", headers=csrf_headers(cookie))


async def test_f69_new_peer_flood_stops_only_new_people_and_ui_resume(
    admin_client, public_client, public_app, internal_transport, db, clock
):
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    gateway = FakeGateway()
    new_ext = seed_dm(gateway, user_id=901)
    group_ext = seed_group(gateway, chat_id=902)
    new_dm = channel_row(db, account_id=account_id, project_id=project_id, ext_id=new_ext, kind="dm")
    group = channel_row(db, account_id=account_id, project_id=project_id, ext_id=group_ext, kind="group")
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    gateway.now_ms = int(clock.value * 1000)
    cookie, _csrf = await login_via_code(public_client, db, clock)

    first = await send_ui(public_client, cookie, channel_id=new_dm, text="первое новому")
    in_group = await send_ui(public_client, cookie, channel_id=group, text="в группу")
    second = await send_ui(public_client, cookie, channel_id=new_dm, text="второе новому")

    gateway.queue_send_faults(errors.PeerFloodError(request=None))
    assert await connector.step_outbox() == 1  # первая строка, остаток пачки не отправлялся
    assert gateway.send_calls == 1

    body = await get_outbox(public_client, first["outbox_id"])
    assert (body["status"], body["error"], body["held"]) == ("failed", peer_flood.FAILED_TEXT, None)
    status, last_error, stop_ts, flood_until = _account(db, account_id)
    assert (status, flood_until) == ("ok", None)  # П5/П8: остаток пачки не поставил `flood_until`
    assert stop_ts == int(clock.value * 1000)
    group_row = await get_outbox(public_client, in_group["outbox_id"])
    assert (group_row["status"], group_row["attempts"]) == ("queued", 0)  # попытка не потрачена

    # Строка нового человека держится стопом, видна причина; группа идёт после паузы остатка пачки.
    held = await get_outbox(public_client, second["outbox_id"])
    assert (held["status"], held["held"]) == ("queued", "peer_flood")
    r = await public_client.get(f"/api/v1/channels/{new_dm}/messages")
    marks = {m["outbox_id"]: m["outbox_held"] for m in r.json()["messages"]}
    assert marks[second["outbox_id"]] == "peer_flood" and marks[first["outbox_id"]] is None
    r = await public_client.get("/api/v1/status")
    (acc,) = [a for a in r.json()["accounts"] if a["id"] == account_id]
    assert acc["peer_flood_ts"] == stop_ts and acc["status"] == "ok"
    r = await public_client.get("/api/v1/outbox", params={"status": "queued"})
    listed = {row["id"]: row["held"] for row in r.json()}
    assert listed == {second["outbox_id"]: "peer_flood", in_group["outbox_id"]: None}

    clock.advance_ms(61_000)
    gateway.now_ms = int(clock.value * 1000)
    assert await connector.step_outbox() == 1
    assert (await get_outbox(public_client, in_group["outbox_id"]))["status"] == "sent"
    assert await connector.step_outbox() == 0  # второе новому — держится

    # «Снять стоп»: bearer — 403, нет аккаунта — 404, кука — 200 со снятым; повтор — 409.
    token = await create_token(admin_client, scopes=("read", "send"))
    async with AsyncClient(transport=ASGITransport(app=public_app), base_url=PUBLIC_BASE_URL) as bearer:
        r = await bearer.post(
            f"/api/v1/accounts/{account_id}/resume", headers={"Authorization": f"Bearer {token}"}
        )
    assert r.status_code == 403
    assert (await _resume(public_client, cookie, 99999)).status_code == 404
    r = await _resume(public_client, cookie, account_id)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "cleared": ["peer_flood"]}
    r = await _resume(public_client, cookie, account_id)
    assert (r.status_code, r.json()) == (409, {"detail": "nothing_to_resume"})
    with db.reader() as conn:
        audit = conn.execute(
            "SELECT actor, detail_json FROM main.audit WHERE action = 'account_resume'"
        ).fetchall()
    assert [a[0] for a in audit] == ["ui"]

    assert await connector.step_outbox() == 1
    assert (await get_outbox(public_client, second["outbox_id"]))["status"] == "sent"
    assert (await get_outbox(public_client, first["outbox_id"]))["status"] == "failed"  # петли нет
    assert gateway.send_calls == 3


async def test_f69_old_peer_flood_limits_account_and_ui_resume(
    admin_client, public_client, internal_transport, db, clock
):
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client)
    gateway = FakeGateway()
    chat_ext = seed_dm(gateway, user_id=911)
    channel_id = channel_row(db, account_id=account_id, project_id=project_id, ext_id=chat_ext, kind="dm")
    _inbound(db, account_id, channel_id)  # человек уже писал
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    gateway.now_ms = int(clock.value * 1000)
    cookie, _csrf = await login_via_code(public_client, db, clock)

    first = await send_ui(public_client, cookie, channel_id=channel_id, text="ответ")
    gateway.queue_send_faults(errors.PeerFloodError(request=None))
    await connector.step_outbox()
    assert (await get_outbox(public_client, first["outbox_id"]))["status"] == "failed"
    assert _account(db, account_id)[:3] == ("limited", "PeerFlood", None)

    second = await send_ui(public_client, cookie, channel_id=channel_id, text="ещё ответ")
    assert await connector.step_outbox() == 0  # весь аккаунт стоит
    r = await _resume(public_client, cookie, account_id)
    assert (r.status_code, r.json()) == (200, {"ok": True, "cleared": ["limited"]})
    assert _account(db, account_id)[0] == "ok"
    assert await connector.step_outbox() == 1
    assert (await get_outbox(public_client, second["outbox_id"]))["status"] == "sent"
    assert gateway.send_calls == 2


async def test_f69_ui_resume_keeps_auth_required_and_flood_until(admin_client, public_client, db, clock):
    account_id, _secret = await create_tg_account(admin_client)
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.account SET status = 'auth_required', last_error = 'x', flood_until = 123 "
            "WHERE id = ?",
            (account_id,),
        )
    )
    cookie, _csrf = await login_via_code(public_client, db, clock)
    r = await _resume(public_client, cookie, account_id)
    assert r.status_code == 409
    assert _account(db, account_id) == ("auth_required", "x", None, 123)
