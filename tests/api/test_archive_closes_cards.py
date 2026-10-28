"""Ф11-9c (H1, docs/architecture.md): беседа уходит в архив (UI `PATCH /channels/{id}` с
`archived: true` или админская ручка `hub channel archive`) -> её открытые карточки разборщика (`new`/`seen`)
-> `done` в той же транзакции; `done`/`dismissed` не трогаются; возврат из архива карточки не открывает."""

import json

from httpx import ASGITransport, AsyncClient

from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.feed import Notifier

from .conftest import (
    csrf_headers,
    insert_account,
    insert_channel,
    insert_message,
    insert_project,
    login_via_code,
)

ADMIN_SECRET = ADMIN_PREFIX + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHI"


def _card(db, channel_id, message_id, status):
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.agent_item (channel_id, label, summary, message_ids_json, "
                "last_message_id, covered_seq, status, provider, model, created_ts, updated_ts) "
                "VALUES (?, 'reply', 's', ?, ?, 0, ?, 'rules', '-', 1, 1)",
                (channel_id, json.dumps([message_id]), message_id, status),
            ).lastrowid
        )
    )


def _statuses(db):
    with db.reader() as conn:
        return dict(conn.execute("SELECT id, status FROM main.agent_item").fetchall())


def _archived(db, channel_id):
    with db.reader() as conn:
        return conn.execute("SELECT archived FROM main.channel WHERE id = ?", (channel_id,)).fetchone()[0]


def _world(db):
    project = insert_project(db, name="Initech")
    account = insert_account(db, source="mail")
    old = insert_channel(
        db, account, project_id=project, ext_id="t-old", kind="mail_thread", title="[Jira] (A-1)"
    )
    other = insert_channel(db, account, project_id=project, ext_id="t-other", kind="mail_thread")
    m_old = insert_message(db, old, ext_id="<m1@x>", ts=1000)
    m_other = insert_message(db, other, ext_id="<m2@x>", ts=1000)
    cards = {
        "new": _card(db, old, m_old, "new"),
        "seen": _card(db, old, m_old, "seen"),
        "done": _card(db, old, m_old, "done"),
        "dismissed": _card(db, old, m_old, "dismissed"),
        "other": _card(db, other, m_other, "new"),
    }
    return old, other, cards


async def test_ui_archive_closes_open_cards_and_undo_keeps_them_closed(client, db, clock):
    old, _other, cards = _world(db)
    cookie, _csrf = await login_via_code(client, db, clock)

    r = await client.patch(f"/api/v1/channels/{old}", json={"muted": True}, headers=csrf_headers(cookie))
    assert r.status_code == 200
    assert _statuses(db)[cards["new"]] == "new"  # не архив — карточки не трогаются

    r = await client.patch(f"/api/v1/channels/{old}", json={"archived": True}, headers=csrf_headers(cookie))
    assert r.status_code == 200, r.text
    assert _archived(db, old) == 1
    statuses = _statuses(db)
    assert statuses[cards["new"]] == "done"
    assert statuses[cards["seen"]] == "done"
    assert statuses[cards["done"]] == "done"
    assert statuses[cards["dismissed"]] == "dismissed"
    assert statuses[cards["other"]] == "new"  # карточка другой беседы — на месте

    r = await client.patch(f"/api/v1/channels/{old}", json={"archived": False}, headers=csrf_headers(cookie))
    assert r.status_code == 200
    assert _archived(db, old) == 0
    assert _statuses(db) == statuses  # возврат из архива карточки не открывает


async def test_admin_archive_reports_closed_cards(db, clock):
    old, _other, cards = _world(db)
    notifier = Notifier()
    db.on_commit(notifier.notify)
    internal = create_internal_app(
        db, clock=clock, notifier=notifier, admin_key_hash=hash_secret(ADMIN_SECRET)
    )
    auth = {"Authorization": f"Bearer {ADMIN_SECRET}"}
    async with AsyncClient(transport=ASGITransport(app=internal), base_url="http://hub-internal") as admin:
        r = await admin.patch(f"/internal/v1/admin/channels/{old}", json={"archived": True}, headers=auth)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == old and body["archived"] is True and body["closed_cards"] == 2
        assert _statuses(db)[cards["new"]] == "done" and _statuses(db)[cards["seen"]] == "done"
        assert _statuses(db)[cards["other"]] == "new"

        r = await admin.patch(f"/internal/v1/admin/channels/{old}", json={"archived": False}, headers=auth)
        assert r.status_code == 200
        assert r.json()["archived"] is False and r.json()["closed_cards"] == 0
        assert _statuses(db)[cards["new"]] == "done"

        r = await admin.patch("/internal/v1/admin/channels/999999", json={"archived": True}, headers=auth)
        assert r.status_code == 404
        r = await admin.patch(f"/internal/v1/admin/channels/{old}", json={"archived": None}, headers=auth)
        assert r.status_code == 422
        r = await admin.patch(f"/internal/v1/admin/channels/{old}", json={"archived": "yes"}, headers=auth)
        assert r.status_code == 422
        r = await admin.patch(f"/internal/v1/admin/channels/{old}", json={"archived": True})
        assert r.status_code == 401
    with db.reader() as conn:
        audit = conn.execute(
            "SELECT detail_json FROM main.audit WHERE action = 'channel_update' ORDER BY id"
        ).fetchall()
    assert [json.loads(a[0]) for a in audit] == [{"archived": True}, {"archived": False}]
