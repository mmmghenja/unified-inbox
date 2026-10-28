"""Ф11-7 (docs/architecture.md: коннектор Telegram — прочтение МОИХ сообщений
собеседником. Живое `MessageRead(inbox=False)` (лички/группы — `UpdateReadHistoryOutbox`, супергруппы —
`UpdateReadChannelOutbox`) -> событие `peer_read`; сверка и стартовая загрузка — `read_outbox_max_id` диалога,
только если он больше уже отправленного; `inbox=True` — по-прежнему `read`."""

from telethon import events
from telethon.tl import types

from hub.connectors.telegram import convert
from hub.connectors.telegram.connector import TelegramConnector
from hub.connectors.telegram.gateway import PeerReadGE, ReadInboxGE, peer_read_ge

from . import helpers as h
from .conftest import create_tg_account, make_core_client
from .helpers import FakeGateway

_FAKE_NOW_S = 1_700_000_000.0  # совпадает с умолчаниями `helpers.make_dialog`/`make_message`


class _Client:
    async def set_status(self, status, *, last_error=None):
        return {"ok": True}


def _live_connector(*entities):
    connector = TelegramConnector(_Client(), FakeGateway(), account_id=1)
    connector._me_id = h.ME_ID  # noqa: SLF001
    for entity in entities:
        connector._entities[int(convert.peer_ext_id(h.peer_of(entity)))] = entity  # noqa: SLF001
    emitted: list[dict] = []
    connector.emit = lambda ev: emitted.append(ev)
    return connector, emitted


def _build(update, *, inbox: bool):
    """Как Telethon у `client.on(events.MessageRead(inbox=…))`: `build` + `filter` построителя."""
    builder = events.MessageRead(inbox=inbox)
    builder.resolved = (
        True  # фильтра по чатам нет — резолвить нечего (в клиенте это делает `_dispatch_update`)
    )
    ev = builder.build(update, None, h.ME_ID)
    if ev is None or builder.filter(ev) is None:
        return None
    return ev


# --- гейтвей: из апдейта Telethon в PeerReadGE --------------------------------------------------------------


def test_gateway_dm_outbox_read_becomes_peer_read_ge():
    upd = types.UpdateReadHistoryOutbox(peer=types.PeerUser(7), max_id=10, pts=1, pts_count=1)
    ev = _build(upd, inbox=False)
    assert ev is not None
    assert peer_read_ge(ev) == PeerReadGE(peer_marked_id=7, max_id=10)
    # Построитель прочтения МНОЙ этот апдейт не пропускает (регресс: `read` не шлётся за чужое прочтение).
    assert _build(upd, inbox=True) is None


def test_gateway_supergroup_channel_outbox_read_becomes_peer_read_ge():
    upd = types.UpdateReadChannelOutbox(channel_id=700, max_id=20)
    ev = _build(upd, inbox=False)
    assert ev is not None
    assert peer_read_ge(ev) == PeerReadGE(peer_marked_id=-1000000000700, max_id=20)


def test_gateway_inbox_and_contents_are_not_peer_read():
    inbox = types.UpdateReadHistoryInbox(
        peer=types.PeerUser(7), max_id=10, still_unread_count=0, pts=1, pts_count=1
    )
    ev = _build(inbox, inbox=True)
    assert ev is not None
    assert peer_read_ge(ev) is None  # прочтение мной — не peer_read
    assert _build(inbox, inbox=False) is None
    contents = types.UpdateChannelReadMessagesContents(channel_id=700, messages=[20])
    assert peer_read_ge(events.MessageRead(inbox=False).build(contents, None, 1)) is None


# --- коннектор: живое событие -------------------------------------------------------------------------------


async def test_live_dm_peer_read_emits_event_and_inbox_stays_read():
    other = h.make_user(7, first_name="Пётр")
    connector, emitted = _live_connector(other)

    await connector._handle_gateway_event(PeerReadGE(peer_marked_id=7, max_id=10))  # noqa: SLF001
    assert emitted[-1] == {
        "type": "peer_read",
        "channel": {"ext_id": "7", "kind": "dm"},
        "up_to_ext_id": "10",
    }

    await connector._handle_gateway_event(ReadInboxGE(peer_marked_id=7, max_id=12))  # noqa: SLF001
    assert emitted[-1] == {"type": "read", "channel": {"ext_id": "7", "kind": "dm"}, "up_to_ext_id": "12"}


async def test_live_supergroup_peer_read_emits_event_broadcast_and_forum_do_not():
    group = h.make_channel(700, title="Команда", megagroup=True)
    feed = h.make_channel(701, title="Лента", broadcast=True)
    forum = h.make_channel(702, title="Форум", megagroup=True, forum=True)
    connector, emitted = _live_connector(group, feed, forum)
    group_ext = convert.peer_ext_id(h.peer_of(group))

    await connector._handle_gateway_event(PeerReadGE(peer_marked_id=int(group_ext), max_id=20))  # noqa: SLF001
    assert emitted == [
        {"type": "peer_read", "channel": {"ext_id": group_ext, "kind": "group"}, "up_to_ext_id": "20"}
    ]
    for entity in (feed, forum):
        marked = int(convert.peer_ext_id(h.peer_of(entity)))
        await connector._handle_gateway_event(PeerReadGE(peer_marked_id=marked, max_id=5))  # noqa: SLF001
    assert len(emitted) == 1


# --- коннектор: сверка и стартовая загрузка по `read_outbox_max_id` -----------------------------------------


async def _sync_connector(admin_client, internal_transport):
    account_id, secret = await create_tg_account(admin_client)
    client = make_core_client(internal_transport, secret)
    gw = FakeGateway()
    connector = TelegramConnector(client, gw, account_id=account_id, wall=lambda: _FAKE_NOW_S)
    connector._me_id = gw.me.id  # noqa: SLF001
    return connector, gw, client


async def test_sync_known_chat_peer_read_only_when_greater(admin_client, internal_transport, db):
    other = h.make_user(7, first_name="Пётр")
    connector, gw, client = await _sync_connector(admin_client, internal_transport)
    mine = {
        "type": "upsert",
        "channel": {"ext_id": "7", "kind": "dm", "title": "Пётр"},
        "ext_id": "5",
        "ts": 1000,
        "is_me": True,
        "kind": "text",
        "text": "моё",
        "mentions_me": False,
    }
    assert (await client.push_events([mine]))["applied"] == 1
    dialog = h.make_dialog(other, top_message=5, read_outbox_max_id=5)
    gw.add_dialog(dialog)

    emitted: list[dict] = []
    connector.emit = lambda ev: emitted.append(ev)
    await connector.sync("start")
    peer_reads = [e for e in emitted if e["type"] == "peer_read"]
    assert peer_reads == [
        {"type": "peer_read", "channel": {"ext_id": "7", "kind": "dm"}, "up_to_ext_id": "5"}
    ]
    await client.push_events(emitted)
    with db.reader() as conn:
        assert conn.execute("SELECT peer_read_sort FROM main.channel WHERE ext_id = '7'").fetchone()[0] == 5

    # Равно известному — события нет.
    emitted.clear()
    await connector.sync("scheduled")
    assert [e for e in emitted if e["type"] == "peer_read"] == []

    # Меньше известного (живое уже сдвинуло дальше) — события нет.
    await connector._handle_gateway_event(PeerReadGE(peer_marked_id=7, max_id=9))  # noqa: SLF001
    emitted.clear()
    await connector.sync("scheduled")
    assert [e for e in emitted if e["type"] == "peer_read"] == []

    # Больше известного — событие.
    gw.dialogs[gw.dialogs.index(dialog)] = h.make_dialog(other, top_message=5, read_outbox_max_id=11)
    await connector.sync("scheduled")
    assert [e["up_to_ext_id"] for e in emitted if e["type"] == "peer_read"] == ["11"]


async def test_initial_load_sends_peer_read_after_messages(admin_client, internal_transport):
    other = h.make_user(9, first_name="Новый")
    connector, gw, _client = await _sync_connector(admin_client, internal_transport)
    gw.add_dialog(h.make_dialog(other, top_message=3, read_outbox_max_id=2))
    for i in range(1, 4):
        gw.add_message(9, h.make_message(i, h.peer_of(other), message=f"m{i}", from_id=None))

    emitted: list[dict] = []
    connector.emit = lambda ev: emitted.append(ev)
    await connector.sync("start")
    peer_reads = [e for e in emitted if e["type"] == "peer_read"]
    upserts = [e for e in emitted if e["type"] == "upsert"]
    assert [e["up_to_ext_id"] for e in peer_reads] == ["2"]
    assert emitted.index(peer_reads[0]) > emitted.index(upserts[-1])
