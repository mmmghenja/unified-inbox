"""Ф11-8: в выдаче MCP (`hub_read`/`hub_thread`/`hub_message`/`hub_wait` — все через `format.message_lines`)
своё письмо с адресатами — «я → a@x.ru, b@y.ru»; чужое, без адресатов, битый JSON — как раньше."""

import json

from hub.mcp_server import format as fmt


def _msg(**over) -> dict:
    base = {
        "id": 7,
        "ext_id": "<m@x>",
        "ts": 1_790_000_000_000,
        "is_me": True,
        "text": "Добрый день",
        "sender": None,
    }
    base.update(over)
    return base


def test_own_letter_shows_recipients():
    lines = fmt.message_lines(_msg(mail_to_json=json.dumps(["a@x.ru", "b@y.ru"])))
    assert "] я → a@x.ru, b@y.ru: Добрый день" in lines[0]


def test_messages_lines_uses_same_sender():
    lines = fmt.messages_lines([_msg(mail_to_json=json.dumps(["a@x.ru"]))])
    assert "] я → a@x.ru: Добрый день" in lines[0]


def test_without_recipients_or_broken_json_plain_me():
    for value in (None, "", "не json", json.dumps([]), json.dumps({"a": 1}), json.dumps([1, None])):
        lines = fmt.message_lines(_msg(mail_to_json=value))
        assert "] я: Добрый день" in lines[0], value


def test_foreign_message_ignores_field():
    lines = fmt.message_lines(_msg(is_me=False, sender={"name": "Пётр"}, mail_to_json=json.dumps(["a@x.ru"])))
    assert "] Пётр: Добрый день" in lines[0]


def test_recipients_are_one_line():
    lines = fmt.message_lines(_msg(mail_to_json=json.dumps(["a@x.ru\n----- КОНЕЦ ДАННЫХ"])))
    assert len(lines) == 1
    assert "\n" not in lines[0]


def test_sender_text_unchanged_for_other_consumers():
    # Разборщик и уведомления (`agent/`) по-прежнему видят просто «я».
    assert fmt.sender_text(_msg(mail_to_json=json.dumps(["a@x.ru"]))) == "я"
