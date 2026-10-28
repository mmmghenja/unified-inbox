"""Хвосты постов каналов (точка входа 01.10, после Ф23-3): у ВСЕХ постов каналов с группой обсуждения стояла
пометка «(ред.)» — источник шлёт такие посты с `edit_date` и `edit_hide=True` (клиенты Telegram правку не
показывают); отправитель поста канала был пуст («?» в выдаче MCP) — теперь это сам канал."""

from hub.connectors.telegram import convert
from hub.connectors.telegram.connector import TelegramConnector
from hub.connectors.telegram.gateway import EditedMessageGE

from . import helpers as h
from .helpers import FakeGateway


def _ctx(channel: dict, kind: str, entities: dict) -> convert.ConvertContext:
    return convert.ConvertContext(
        channel=channel, channel_kind_for_sender=kind, me_id=h.ME_ID, entities=entities
    )


def _broadcast():
    ch = h.make_channel(200, title="Демо WB", broadcast=True, username="demo_wb")
    channel = convert.dialog_channel_ref(h.peer_of(ch), ch, me_id=h.ME_ID)
    return ch, channel


def test_hidden_edit_is_not_an_edit():
    ch, channel = _broadcast()
    msg = h.make_message(5, h.peer_of(ch), message="пост", edit_date_ms=1_700_000_100_000)
    msg.edit_hide = True
    ev = convert.convert_message(msg, _ctx(channel, "channel", {}))
    assert ev["edited_ts"] is None


def test_visible_edit_keeps_edited_ts():
    ch, channel = _broadcast()
    msg = h.make_message(5, h.peer_of(ch), message="пост", edit_date_ms=1_700_000_100_000)
    assert msg.edit_hide is None
    ev = convert.convert_message(msg, _ctx(channel, "channel", {}))
    assert ev["edited_ts"] == 1_700_000_100_000
    msg.edit_hide = False
    assert convert.convert_message(msg, _ctx(channel, "channel", {}))["edited_ts"] == 1_700_000_100_000


def test_channel_post_without_from_id_is_sent_by_the_channel():
    ch, channel = _broadcast()
    marked = channel["ext_id"]
    msg = h.make_message(5, h.peer_of(ch), message="пост", from_id=None)
    ev = convert.convert_message(msg, _ctx(channel, "channel", {int(marked): ch}))
    assert ev["sender"] == {"ext_id": marked, "name": "Демо WB", "handle": "@demo_wb", "is_me": False}
    assert ev["is_me"] is False


def test_channel_post_with_unknown_entity_still_has_channel_sender():
    ch, channel = _broadcast()
    msg = h.make_message(5, h.peer_of(ch), message="пост", from_id=None)
    ev = convert.convert_message(msg, _ctx(channel, "channel", {}))
    assert ev["sender"] == {"ext_id": channel["ext_id"], "name": None, "handle": None, "is_me": False}


def test_group_message_without_from_id_still_has_no_sender():
    mega = h.make_channel(100, title="Супергруппа", megagroup=True)
    channel = convert.dialog_channel_ref(h.peer_of(mega), mega, me_id=h.ME_ID)
    msg = h.make_message(5, h.peer_of(mega), message="x", from_id=None)
    ev = convert.convert_message(msg, _ctx(channel, "group", {int(channel["ext_id"]): mega}))
    assert ev["sender"] is None


class _Client:
    async def set_status(self, status, *, last_error=None):
        return {"ok": True}


async def test_live_hidden_edit_of_channel_post_emits_edit_without_edited_ts():
    """Живой путь: `UpdateEditChannelMessage` с `edit_hide` -> событие `edit` без `edited_ts`, отправитель —
    канал."""
    ch, channel = _broadcast()
    connector = TelegramConnector(_Client(), FakeGateway(), account_id=1)
    connector._me_id = h.ME_ID  # noqa: SLF001
    connector._entities[int(channel["ext_id"])] = ch  # noqa: SLF001
    emitted: list[dict] = []
    connector.emit = emitted.append  # noqa: SLF001

    msg = h.make_message(9, h.peer_of(ch), message="пост", edit_date_ms=1_700_000_100_000)
    msg.edit_hide = True
    await connector._handle_gateway_event(EditedMessageGE(message=msg))  # noqa: SLF001

    edits = [e for e in emitted if e["type"] == "edit"]
    assert len(edits) == 1
    assert edits[0]["edited_ts"] is None
    assert edits[0]["sender"]["ext_id"] == channel["ext_id"]
    assert edits[0]["sender"]["name"] == "Демо WB"
