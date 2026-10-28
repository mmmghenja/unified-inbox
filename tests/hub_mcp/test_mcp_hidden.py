"""Ф8-4 (docs/architecture.md): MCP не показывает удалённое у себя в хабе (T2), пометка
«(ред., версий: N)» у правленого (C5, П12), родитель треда, удалённый у себя, — заглушкой без содержимого
(П6)."""

from mcp import Client

from hub.core import hiding, ingest
from hub.mcp_server import format as fmt

from .conftest import insert_account, make_token, text_of, write_profile


def _ev(ext_id, ts, text, **extra):
    event = {
        "type": "upsert",
        "channel": {"ext_id": "C1", "kind": "channel", "title": "general"},
        "ext_id": ext_id,
        "ts": ts,
        "sender": {"ext_id": "U1", "name": "Петя"},
        "is_me": False,
        "kind": "text",
        "text": text,
        "mentions_me": False,
    }
    event.update(extra)
    return event


def _ingest(db, account_id, events, now_ms):
    result = db.write_sync(
        lambda conn: ingest.apply_batch(
            conn, account_id=account_id, events=events, cursor=None, now_ms=now_ms
        )
    )
    assert result.dead == 0


def _ids(db):
    with db.reader() as conn:
        return dict(conn.execute("SELECT ext_id, id FROM main.message").fetchall())


async def test_mcp_read_hides_hidden_and_marks_edits(db, config_dir, make_server):
    acc = insert_account(db, source="slack")
    _ingest(db, acc, [_ev("1", 1000, "видно"), _ev("2", 2000, "тайнаслово")], 3000)
    _ingest(db, acc, [_ev("1", 1000, "видно-2", type="edit", edited_ts=4000)], 4000)
    _ingest(db, acc, [_ev("1", 1000, "видно-3", type="edit", edited_ts=5000)], 5000)
    ids = _ids(db)
    db.write_sync(lambda conn: hiding.hide_message(conn, ids["2"], now_ms=6000))
    with db.reader() as conn:
        channel_id = conn.execute("SELECT id FROM main.channel WHERE ext_id = 'C1'").fetchone()[0]
    token, token_id = make_token(db, scopes=("read",), project_ids=None, name="mcp:test")
    write_profile(config_dir, "test", token, token_id)
    server, _hub = make_server()
    async with Client(server) as client:
        text = text_of(await client.call_tool("hub_read", {"channel_id": channel_id}))
        assert "тайнаслово" not in text and f"[msg {ids['2']} " not in text
        assert "(ред., версий: 2)" in text and "видно-3" in text
        found = text_of(await client.call_tool("hub_search", {"query": "тайнаслово"}))
        assert f"msg {ids['2']}" not in found


def test_format_hidden_parent_stub_and_edit_marks():
    stub = {"id": 5, "ext_id": "10", "ts": 1000, "hidden": True, "purged": True, "text": None, "sender": None}
    (line,) = fmt.message_lines(stub)
    assert "(удалено у себя в хабе)" in line and "стёрт по сроку" not in line
    (line,) = fmt.message_lines({"id": 6, "ts": 1000, "text": "т", "edited_ts": 2000, "edit_count": 0})
    assert "(ред.)" in line and "версий" not in line
    (line,) = fmt.message_lines({"id": 7, "ts": 1000, "text": "т", "edited_ts": 2000, "edit_count": 3})
    assert "(ред., версий: 3)" in line
