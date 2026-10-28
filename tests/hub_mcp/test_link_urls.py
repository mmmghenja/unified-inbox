"""Ссылки с подписью, не совпадающей с адресом, показываются сессиям вместе с адресом."""

import json

from hub.mcp_server import format as fmt

URL = "https://youtrack.chat.globex.example.com/articles/DEV-A-123/Very-long-article-name-with-many-words"


def _msg(text, entities):
    return {"id": 1, "ext_id": "1.2", "ts": 0, "text": text, "entities_json": json.dumps(entities)}


def _text(message, **kw):
    return "\n".join(fmt.message_lines(message, **kw))


def test_short_label_gets_full_url_slack_style():
    label = "youtrack.chat.globex.example.com/articles/…/…"
    # эмодзи до ссылки: смещения в единицах UTF-16
    text = f"😀 см. {label} тут"
    off = len("😀 см. ".encode("utf-16-le")) // 2
    out = _text(_msg(text, [{"type": "link", "offset": off, "length": len(label), "url": URL}]))
    assert f"{label} ({URL}) тут" in out


def test_same_label_no_duplicate():
    text = "youtrack.chat.globex.example.com/a"
    entity = {"type": "link", "offset": 0, "length": len(text), "url": "https://youtrack.chat.globex.example.com/a/"}
    out = _text(_msg(text, [entity]))
    assert "(" not in out.split(": ", 1)[1]


def test_telegram_text_url():
    out = _text(
        _msg("читай здесь", [{"type": "link", "offset": 6, "length": 5, "url": "https://t.example/x"}])
    )
    assert "читай здесь (https://t.example/x)" in out


def test_limits_count_added_urls():
    m = _msg("ab", [{"type": "link", "offset": 0, "length": 2, "url": URL}])
    assert fmt.text_limits([m]) == [fmt.MESSAGE_TEXT_MAX]
    assert fmt._text_units(m)[0] == len("ab") + len(f" ({URL})")


def test_bad_entities_ignored():
    assert _text(_msg("abc", [{"type": "link", "offset": 5, "length": 9, "url": URL}])).endswith("abc")
