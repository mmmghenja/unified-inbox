"""Ф11-8: `mail_to_json` своего письма в публичном API — лента беседы, одно сообщение, `/sync`; после
«удалить у себя» поля нет (NULL в БД, пусто в выдаче)."""

import json

from hub.core import hiding
from hub.core.ingest import apply_batch

from .conftest import insert_account, insert_project, login_via_code

TO = ["alisa@client.example", "boss@client.example"]


def _ev(ext_id, ts, *, is_me, mail_to=None):
    event = {
        "type": "upsert",
        "channel": {"ext_id": "t1", "kind": "mail_thread", "title": "Тема"},
        "ext_id": ext_id,
        "ts": ts,
        "is_me": is_me,
        "kind": "text",
        "text": "письмо",
        "mentions_me": False,
    }
    if mail_to is not None:
        event["mail_to"] = mail_to
    return event


async def test_mail_to_in_public_api(client, db, clock):
    account_id = insert_account(db, source="mail")
    pid = insert_project(db, name="P")
    now = int(clock() * 1000)
    result = db.write_sync(
        lambda conn: apply_batch(
            conn,
            account_id=account_id,
            events=[
                _ev("<in@x>", now - 2000, is_me=False),
                _ev("<my@x>", now - 1000, is_me=True, mail_to=TO),
            ],
            cursor=None,
            now_ms=now,
        )
    )
    assert result.dead == 0
    with db.reader() as conn:
        cid = conn.execute("SELECT id FROM main.channel WHERE ext_id = 't1'").fetchone()[0]
        mid = conn.execute("SELECT id FROM main.message WHERE ext_id = '<my@x>'").fetchone()[0]
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET project_id = ?, map_source = 'manual' WHERE id = ?", (pid, cid)
        )
    )
    await login_via_code(client, db, clock)

    page = (await client.get(f"/api/v1/channels/{cid}/messages")).json()
    by_ext = {m["ext_id"]: m for m in page["messages"]}
    assert json.loads(by_ext["<my@x>"]["mail_to_json"]) == TO
    assert by_ext["<in@x>"]["mail_to_json"] is None

    single = (await client.get(f"/api/v1/messages/{mid}")).json()
    assert json.loads(single["mail_to_json"]) == TO

    sync = (await client.get("/api/v1/sync", params={"since": 0})).json()
    synced = {m["ext_id"]: m for m in sync["messages"]}
    assert json.loads(synced["<my@x>"]["mail_to_json"]) == TO

    db.write_sync(lambda conn: hiding.hide_message(conn, mid, now_ms=now))
    with db.reader() as conn:
        stored = conn.execute("SELECT mail_to_json FROM main.message WHERE id = ?", (mid,)).fetchone()[0]
    assert stored is None
    page = (await client.get(f"/api/v1/channels/{cid}/messages")).json()
    assert "<my@x>" not in {m["ext_id"] for m in page["messages"]}
