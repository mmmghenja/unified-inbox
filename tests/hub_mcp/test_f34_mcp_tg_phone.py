"""Ф34-3 A4 (`docs/architecture.md`): `hub_draft_new` по номеру телефона — описание
инструмента, ответ и выдачи MCP без номера (маска в названии). Ядро — в процессе, MCP-клиент — в памяти."""

import json

from mcp import Client

from hub.mcp_server.server import _channel_line

from .conftest import insert_project, text_of
from .test_mcp_server import _call, _profile

TAIL = "9000000001"
MASK = "+7 ••• ••• 00 01"


async def test_draft_new_by_phone_no_number_in_outputs(db, config_dir, make_server):
    project = insert_project(db, name="Umbrella")
    tg = db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, config_json) VALUES ('telegram', 'main', ?)",
                (json.dumps({"tg_phone_daily": 1}),),
            ).lastrowid
        )
    )
    _profile(db, config_dir, project_ids=[project], profile="iip")
    server, _hub = make_server("iip")
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        description = tools["hub_draft_new"].description
        assert 'to="+79000000000"' in description and "в сутки" in description
        assert "номер не найден" in description

        out = await _call(
            client,
            "hub_draft_new",
            project_id=project,
            account_id=tg,
            to="8 (900) 000-00-01",
            text="Здравствуйте!",
        )
        assert not out.is_error, text_of(out)
        assert "новая беседа #" in text_of(out) and TAIL not in text_of(out)

        again = await _call(
            client, "hub_draft_new", project_id=project, account_id=tg, to="+79000000001", text="ещё"
        )
        assert again.is_error and "уже есть черновик #" in text_of(again) and TAIL not in text_of(again)
        over = await _call(
            client, "hub_draft_new", project_id=project, account_id=tg, to="+79000000002", text="x"
        )
        assert over.is_error and "1 в сутки" in text_of(over) and "9000000002" not in text_of(over)
        bad = await _call(client, "hub_draft_new", project_id=project, account_id=tg, to="+7900", text="x")
        assert bad.is_error and "международном формате" in text_of(bad)

        for tool, args in (
            ("hub_channels", {"project_id": project}),
            ("hub_drafts", {}),
            ("hub_overview", {}),
        ):
            text = text_of(await _call(client, tool, **args))
            assert TAIL not in text, tool
        listing = text_of(await _call(client, "hub_channels", project_id=project))
        assert MASK in listing and "новая беседа (ждёт первого сообщения)" in listing
        assert MASK in text_of(await _call(client, "hub_drafts"))


def test_channel_line_for_phone_kind():
    channel = {
        "id": 12,
        "project_id": None,
        "source": "telegram",
        "kind": "dm",
        "title": MASK,
        "unread": 0,
        "last_msg_ts": None,
        "new_to": {"kind": "tg_phone"},
        "can_reply": True,
    }
    line = _channel_line(channel, unread=False)
    assert "новая беседа (ждёт первого сообщения)" in line and MASK in line
