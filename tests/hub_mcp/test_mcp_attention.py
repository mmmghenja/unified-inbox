"""Ф8-3 E (сокращён поправкой П14): режим беседы по-русски — в строках `hub_channels` и в шапке `hub_read`;
`hub_inbox` не меняется. Спецификация: docs/architecture.md."""

from mcp import Client

from .conftest import insert_account, insert_channel, insert_message, insert_project, text_of
from .test_mcp_server import _call, _profile


async def test_f83_e_mode_in_channels_and_read_header_not_in_inbox(db, config_dir, make_server):
    pid = insert_project(db, name="Initech")
    acc = insert_account(db, source="telegram")
    group = insert_channel(db, acc, project_id=pid, title="фронт", kind="group", last_msg_ts=2000)
    dm = insert_channel(db, acc, project_id=pid, ext_id="d", title="Пётр", kind="dm", last_msg_ts=1000)
    insert_message(db, group, ext_id="1", ts=2000, text="сборка")
    insert_message(db, dm, ext_id="1", ts=1000, text="привет")
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.channel SET attention = 'react' WHERE id = ?", (group,))
    )
    _profile(db, config_dir)
    server, _hub = make_server()
    async with Client(server) as client:
        channels = text_of(await _call(client, "hub_channels", project_id=pid))
        line_group = next(line for line in channels.split("\n") if line.startswith(f"#{group} "))
        line_dm = next(line for line in channels.split("\n") if line.startswith(f"#{dm} "))
        assert line_group.endswith("режим: реагирую (вручную)")
        assert line_dm.endswith("режим: реагирую")
        read = text_of(await _call(client, "hub_read", channel_id=group))
        assert "режим: реагирую (вручную)" in read.split("\n")[0]
        inbox = text_of(await _call(client, "hub_inbox"))
        assert "режим:" not in inbox
