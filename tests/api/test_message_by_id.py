"""Ф10-2 A3 (docs/architecture.md): `GET /api/v1/messages/{id}` — одно сообщение в форме
элемента ленты `/channels/{id}/messages`; права — как у ленты: чужой проект и несуществующее — 404, без скоупа
`read` — 403; скрытое у себя — заглушкой без содержимого; удалённое источником — с текстом и пометкой."""

from hub.core import hiding, media
from hub.core.ingest import apply_batch

from .conftest import (
    insert_account,
    insert_channel,
    insert_message,
    insert_project,
    login_via_code,
    make_bearer_token,
)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _setup(db):
    account_id = insert_account(db, source="telegram")
    project_id = insert_project(db, name="P")
    other_project = insert_project(db, name="O")
    channel_id = insert_channel(db, account_id, project_id=project_id, ext_id="c1", title="беседа")
    return account_id, project_id, other_project, channel_id


async def test_own_project_same_form_as_feed(client, db):
    account_id, project_id, _other, channel_id = _setup(db)
    db.write_sync(lambda conn: media.ensure_start_ts(conn, 0))
    event = {
        "type": "upsert",
        "channel": {"ext_id": "c1", "kind": "dm"},
        "ext_id": "5",
        "ts": 10_000,
        "is_me": False,
        "kind": "file",
        "text": "я" * 3160,
        "attachments": [{"type": "photo", "mime": "image/png", "name": "x.png"}],
        "mentions_me": False,
    }
    db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=[event], cursor=None, now_ms=10_000)
    )
    token = make_bearer_token(db, scopes=("read",), project_ids=[project_id])

    feed = await client.get(f"/api/v1/channels/{channel_id}/messages", headers=_auth(token))
    assert feed.status_code == 200
    (from_feed,) = feed.json()["messages"]
    r = await client.get(f"/api/v1/messages/{from_feed['id']}", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body == from_feed
    assert body["channel_id"] == channel_id and len(body["text"]) == 3160
    assert body["media"] and body["media"][0]["idx"] == 0


async def test_foreign_project_missing_and_scope(client, db):
    _account, project_id, other, channel_id = _setup(db)
    mid = insert_message(db, channel_id, ext_id="1", ts=1000, text="секрет")
    foreign = make_bearer_token(db, scopes=("read",), project_ids=[other], name="foreign")
    no_read = make_bearer_token(db, scopes=("draft",), project_ids=[project_id], name="noread")
    own = make_bearer_token(db, scopes=("read",), project_ids=[project_id], name="own")

    r = await client.get(f"/api/v1/messages/{mid}", headers=_auth(foreign))
    assert r.status_code == 404 and "секрет" not in r.text
    r = await client.get(f"/api/v1/messages/{mid}", headers=_auth(no_read))
    assert r.status_code == 403
    r = await client.get("/api/v1/messages/999999", headers=_auth(own))
    assert r.status_code == 404
    r = await client.get(f"/api/v1/messages/{mid}", headers=_auth(own))
    assert r.status_code == 200 and r.json()["text"] == "секрет"


async def test_hidden_is_stub_and_deleted_keeps_text(client, db, clock):
    _account, project_id, _other, channel_id = _setup(db)
    hidden_id = insert_message(db, channel_id, ext_id="1", ts=1000, text="тайнаслово")
    deleted_id = insert_message(db, channel_id, ext_id="2", ts=2000, text="было и сплыло", deleted=1)
    db.write_sync(lambda conn: hiding.hide_message(conn, hidden_id, now_ms=3000))
    token = make_bearer_token(db, scopes=("read",), project_ids=[project_id])

    r = await client.get(f"/api/v1/messages/{hidden_id}", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["hidden"] is True and body["text"] is None and body["sender"] is None
    assert "тайнаслово" not in r.text

    r = await client.get(f"/api/v1/messages/{deleted_id}", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["deleted"] is True and r.json()["text"] == "было и сплыло"

    # Лента: скрытого нет, удалённое источником — с текстом (та же форма).
    feed = (await client.get(f"/api/v1/channels/{channel_id}/messages", headers=_auth(token))).json()
    assert [m["id"] for m in feed["messages"]] == [deleted_id]
    assert feed["messages"][0] == r.json()

    await login_via_code(client, db, clock)
    r = await client.get(f"/api/v1/messages/{deleted_id}")
    assert r.status_code == 200 and r.json()["id"] == deleted_id
