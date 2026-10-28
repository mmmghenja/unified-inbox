"""Ф14-6 D (docs/architecture.md): видео-кружок Telegram — `round: true`, вид — `video`."""

from telethon.tl import types

from hub.connectors.telegram import convert

from .test_media import make_document


def _item(attributes):
    doc = make_document(21, "video/mp4", attributes, size=500_000)
    [item] = convert.convert_attachments(types.MessageMediaDocument(document=doc))
    return item


def test_round_message_flag():
    item = _item([types.DocumentAttributeVideo(duration=7, w=384, h=384, round_message=True)])
    assert item["type"] == "video" and item["round"] is True and item["duration_s"] == 7


def test_plain_video_has_no_round_flag():
    item = _item([types.DocumentAttributeVideo(duration=7, w=640, h=360)])
    assert item["type"] == "video" and "round" not in item


def test_voice_has_no_round_flag():
    doc = make_document(22, "audio/ogg", [types.DocumentAttributeAudio(duration=3, voice=True)], size=900)
    [item] = convert.convert_attachments(types.MessageMediaDocument(document=doc))
    assert item["type"] == "voice" and "round" not in item
