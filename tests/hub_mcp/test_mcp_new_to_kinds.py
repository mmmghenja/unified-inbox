"""Ф14-2 E (`docs/architecture.md`): строка беседы в выдаче MCP помечает беседу-заготовку
«новая беседа» при любом виде адресата, включая новые `tg_user`/`slack_users`;
адресат в выдачу не попадает."""

import pytest

from hub.mcp_server.server import _channel_line


@pytest.mark.parametrize(
    "new_to",
    [
        {"kind": "tg_username", "username": "petr"},
        {"kind": "tg_user", "user_id": 555, "name": "Пётр", "username": None},
        {"kind": "slack_users", "users": ["U0AAA", "U0BBB"], "names": ["Аня", "Борис"]},
    ],
)
def test_new_chat_line_for_all_kinds(new_to):
    channel = {
        "id": 12,
        "project_id": None,
        "source": "telegram",
        "kind": "dm",
        "title": "Заготовка",
        "unread": 0,
        "last_msg_ts": None,
        "new_to": new_to,
        "can_reply": True,
    }
    line = _channel_line(channel, unread=False)
    assert "новая беседа (ждёт первого сообщения)" in line
    assert "555" not in line and "U0AAA" not in line
