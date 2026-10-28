"""Ф14-6 E (docs/architecture.md): расшифровка голосового в выдаче сессиям — отдельными
строками под строкой «вложения:», внутри блока ДАННЫХ, в общем бюджете текста (Ф10-2)."""

import json

from hub.mcp_server import format as fmt


def _message(transcript, *, kind="voice", item_extra=None, text="", duration=42, mid=7):
    item = {"type": kind, "mime": "audio/ogg", "size": 2048, **(item_extra or {})}
    return {
        "id": mid,
        "ext_id": "100",
        "ts": 1_700_000_000_000,
        "is_me": False,
        "sender": {"name": "Вася"},
        "text": text,
        "attachments_json": json.dumps([item]),
        "media": [
            {
                "id": 55,
                "idx": 0,
                "kind": kind,
                "status": "ready",
                "duration_s": duration,
                "transcript": transcript,
            }
        ],
    }


def _done(text, **extra):
    return {"status": "done", "text": text, "lang": "ru", "problem": None, "manual": False, **extra}


def test_done_voice_line_under_attachments():
    lines = fmt.message_lines(_message(_done("первая строка\nвторая строка")))
    attach = next(i for i, line in enumerate(lines) if line.lstrip().startswith("вложения:"))
    assert lines[attach + 1] == "    голосовое 0:42, расшифровка (ru): первая строка"
    assert lines[attach + 2] == "    вторая строка"


def test_statuses_and_labels():
    def one(transcript, **kw):
        text = fmt.attachments_text(
            _message(transcript, **kw)["attachments_json"], _message(transcript, **kw)["media"]
        )
        return text.split("\n")[1:]

    assert one({"status": "queued"}) == ["голосовое 0:42, расшифровка идёт"]
    assert one({"status": "working"}) == ["голосовое 0:42, расшифровка идёт"]
    assert one({"status": "failed"}) == ["голосовое 0:42, расшифровать не удалось"]
    assert one(_done("", problem="пусто")) == ["голосовое 0:42, расшифровка: речи не слышно"]
    assert one(_done("ну вот", problem="мало слов")) == [
        "голосовое 0:42, расшифровка сомнительная (ru): ну вот"
    ]
    assert one(_done("hello", lang="en")) == ["голосовое 0:42, расшифровка (en): hello"]
    assert one(_done("x"), kind="video", item_extra={"round": True}, duration=5) == [
        "кружок 0:05, расшифровка (ru): x"
    ]
    assert one(_done("x"), kind="audio", item_extra={"clip": True}, duration=None) == [
        "аудиоклип, расшифровка (ru): x"
    ]
    assert one(None) == []


def test_without_media_as_before():
    """Разборщик пока зовёт без `media` (14-6b) — вывод прежний, одной строкой."""
    message = _message(_done("текст"))
    assert fmt.attachments_text(message["attachments_json"]) == "вложения: voice 2 КБ"


def test_inside_data_block():
    message = _message(_done("данные собеседника"))
    out = fmt.render(["шапка"], fmt.messages_lines([message]))
    lines = out.split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith("----- ДАННЫЕ"))
    end = next(i for i, line in enumerate(lines) if line.startswith("----- КОНЕЦ ДАННЫХ"))
    where = next(i for i, line in enumerate(lines) if "данные собеседника" in line)
    assert start < where < end


def test_long_transcript_clipped_with_hint_and_full_in_message():
    long_text = "слово " * 3000  # 18 000 символов
    message = _message(_done(long_text))
    lines = fmt.message_lines(message, text_limit=fmt.MESSAGE_TEXT_MAX)
    assert any("hub_message(message_id=7)" in line for line in lines)
    full = fmt.message_lines(message, text_limit=None)
    assert any(long_text.rstrip() in line for line in full)


def test_budget_counts_transcripts():
    """Ф10-2 бюджет: восемь голосовых по 8000 символов не влезают в 60 000 — старые укорачиваются."""
    messages = [_message(_done("а" * 9000), mid=i) for i in range(10)]
    limits = fmt.text_limits(messages)
    assert limits[0] == fmt.OLD_MESSAGE_TEXT_MAX
    assert limits[-1] == fmt.MESSAGE_TEXT_MAX
    shown = sum(min(9000, limit) for limit in limits)
    assert shown <= fmt.OUTPUT_TEXT_BUDGET
    # без расшифровок бюджет не тронут
    plain = [_message(None, mid=i) for i in range(10)]
    assert fmt.text_limits(plain) == [fmt.MESSAGE_TEXT_MAX] * 10
