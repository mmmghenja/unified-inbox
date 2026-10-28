"""Ф8-5 D (docs/architecture.md): в выдаче hub-mcp у вложения — тип, имя, размер и статус загрузки,
без ссылки; разборщик (без `media`) видит вложения как раньше."""

import json

from hub.mcp_server import format as fmt


def _message(**overrides):
    base = {
        "id": 7,
        "ext_id": "42",
        "ts": 1_700_000_000_000,
        "is_me": False,
        "sender": {"name": "Оля"},
        "kind": "file",
        "text": None,
        "attachments_json": json.dumps(
            [
                {"type": "photo", "name": "кот.jpg", "size": 2048, "source_url": "https://t.me/c/1/42"},
                {"type": "document", "name": "a.pdf", "size": 10},
                {"type": "location"},
            ]
        ),
        "media": [
            {"id": 1, "idx": 0, "kind": "photo", "status": "ready"},
            {"id": 2, "idx": 1, "kind": "document", "status": "deferred"},
        ],
    }
    base.update(overrides)
    return base


def test_d_message_lines_show_media_status_without_links():
    lines = fmt.message_lines(_message())
    text = "\n".join(lines)
    assert "photo «кот.jpg» 2 КБ [загружено]" in text
    assert "document «a.pdf» 10 Б [не загружено]" in text
    assert "location" in text
    assert "t.me" not in text and "/api/v1/media" not in text


def test_d_attachments_text_without_media_unchanged():
    msg = _message()
    assert fmt.attachments_text(msg["attachments_json"]) == (
        "вложения: photo «кот.jpg» 2 КБ; document «a.pdf» 10 Б; location"
    )
