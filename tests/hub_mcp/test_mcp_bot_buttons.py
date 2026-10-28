"""Ф44-1a (docs/architecture.md): в выдаче MCP (`hub_read`/`hub_thread`/
`hub_message` — все через `format.message_lines`) под сообщением бота — строка «кнопки: [1.1] «…» · …»
после вложений; сырого callback в ней нет; шапка MCP называет тексты кнопок и адреса чужим вводом;
разборщику кнопки не даются."""

import inspect
import json

from hub.agent import dispatcher as agent_dispatcher
from hub.mcp_server import format as fmt
from hub.mcp_server import server as mcp_server

BUTTONS = {
    "kind": "inline",
    "rows": [
        [{"t": "Получить ключ", "k": "callback"}, {"t": "Сайт", "k": "url", "u": "https://example.com"}],
        [
            {"t": "Оплатить", "k": "other"},
            {"t": "Код", "k": "copy", "c": "PROMO"},
            {"t": "Удалить", "k": "callback", "pw": True},
        ],
    ],
}


def _msg(**over) -> dict:
    base = {"id": 7, "ext_id": "9980", "ts": 1_790_000_000_000, "is_me": False, "text": "Выбери"}
    base["sender"] = None
    base.update(over)
    return base


def test_buttons_line_after_attachments_before_reactions():
    lines = fmt.message_lines(
        _msg(
            buttons=BUTTONS,
            attachments_json=json.dumps([{"type": "photo"}]),
            reactions_json=json.dumps([{"emoji": "👍", "count": 3}]),
        )
    )
    assert lines[-1] == "    реакции: 👍×3"
    assert lines[-2] == (
        "    кнопки: [1.1] «Получить ключ» · [1.2] «Сайт» -> https://example.com · "
        "[2.1] «Оплатить» (только в Telegram) · [2.2] «Код» (копирует: PROMO) · "
        "[2.3] «Удалить» (требует пароль Telegram)"
    )
    assert len(lines) == 4 and "кнопки" not in lines[1]  # строка вложений — перед кнопками


def test_cut_and_foreign_text_is_one_line():
    text = fmt.buttons_text(
        {"kind": "inline", "rows": [[{"t": "стр1\nстр2\x00" + "я" * 300, "k": "callback"}]], "cut": True}
    )
    assert "\n" not in text and "\x00" not in text
    assert text.startswith("кнопки: [1.1] «стр1 стр2 ") and text.endswith("» · (часть кнопок не показана)")
    assert len(text) < 200
    assert fmt.buttons_text({"kind": "inline", "rows": [], "cut": True}) == (
        "кнопки: (часть кнопок не показана)"
    )


def test_no_buttons_no_line():
    for value in (None, {}, {"rows": "x"}, {"kind": "inline", "rows": []}, "x", []):
        lines = fmt.message_lines(_msg(buttons=value))
        assert not any("кнопки" in line for line in lines), value


def test_raw_callback_never_printed_even_if_core_leaked_it():
    leaked = {"kind": "inline", "rows": [[{"t": "Ключ", "k": "callback", "d": "U0VDUkVU"}]]}
    assert "U0VDUkVU" not in "\n".join(fmt.message_lines(_msg(buttons=leaked)))


def test_instructions_name_buttons_as_foreign_input():
    assert "кнопки: …" in mcp_server.INSTRUCTIONS
    assert "тоже чужой ввод" in mcp_server.INSTRUCTIONS


def test_triage_cards_have_no_buttons():
    # Ф57-1: промпта модели больше нет — карточка по правилам (пересказ разборщика) кнопок не несёт
    assert "buttons" not in inspect.getsource(agent_dispatcher)
