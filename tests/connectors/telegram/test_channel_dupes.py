"""Ф17-3 (`docs/architecture.md`): автопересылка поста канала в группе обсуждения помечается
(`forward.auto`), история вступления — прочитанной, предпросмотр показывает связанный чат."""

from types import SimpleNamespace

from telethon import utils
from telethon.tl import types

from hub.connectors.telegram import convert

from . import helpers as h
from .test_join import _channel, _item, _joined, _marked, _setup

_CH_ID = 500


def _group_ctx(channel_entity, group):
    chan_ext = convert.dialog_channel_ref(h.peer_of(group), group, me_id=h.ME_ID)
    ctx = convert.ConvertContext(
        channel=chan_ext,
        channel_kind_for_sender="group",
        me_id=h.ME_ID,
        entities={utils.get_peer_id(channel_entity): channel_entity},
    )
    return ctx


def _fwd(**kw):
    base = dict(date=h.ts(1_700_000_000_000), from_id=types.PeerChannel(_CH_ID), from_name=None)
    base.update(kw)
    return types.MessageFwdHeader(**base)


def _forward_of(msg, chan, group):
    return convert.convert_message(msg, _group_ctx(chan, group))["forward"]


def test_autoforward_is_marked_with_channel_post_and_name():
    chan = h.make_channel(_CH_ID, title="DEMO", broadcast=True, megagroup=False)
    group = h.make_channel(501, title="Чат", broadcast=False, megagroup=True)
    fwd = _fwd(channel_post=77, saved_from_peer=types.PeerChannel(_CH_ID), saved_from_msg_id=77)
    msg = h.make_message(5, h.peer_of(group), message="пост", from_id=types.PeerChannel(_CH_ID), fwd_from=fwd)
    out = _forward_of(msg, chan, group)
    assert out["auto"] is True and out["channel_post"] == 77
    assert out["from_ext_id"] == str(utils.get_peer_id(types.PeerChannel(_CH_ID)))
    assert out["from_name"] == "DEMO"


def test_manual_forward_by_person_is_not_auto():
    chan = h.make_channel(_CH_ID, title="DEMO", broadcast=True, megagroup=False)
    group = h.make_channel(501, title="Чат", broadcast=False, megagroup=True)
    fwd = _fwd(channel_post=77)  # человек переслал пост: saved_from_* нет
    msg = h.make_message(5, h.peer_of(group), message="пост", from_id=types.PeerUser(9), fwd_from=fwd)
    out = _forward_of(msg, chan, group)
    assert "auto" not in out and "channel_post" not in out
    # даже с saved_from_*, но from_id сообщения — человек
    fwd2 = _fwd(channel_post=77, saved_from_peer=types.PeerChannel(_CH_ID), saved_from_msg_id=77)
    msg2 = h.make_message(6, h.peer_of(group), message="пост", from_id=types.PeerUser(9), fwd_from=fwd2)
    assert "auto" not in _forward_of(msg2, chan, group)


def test_forward_from_private_chat_is_not_auto():
    chan = h.make_channel(_CH_ID, title="DEMO", broadcast=True, megagroup=False)
    group = h.make_channel(501, title="Чат", broadcast=False, megagroup=True)
    fwd = _fwd(from_id=types.PeerUser(9), from_name=None)
    msg = h.make_message(5, h.peer_of(group), message="x", from_id=types.PeerUser(9), fwd_from=fwd)
    assert "auto" not in _forward_of(msg, chan, group)


async def test_join_load_marks_newest_loaded_as_read():
    gw, conn, _ = _setup()
    ch = _channel(800)
    emitted = []
    conn.emit = emitted.append
    await _joined(gw, conn, ch)
    await conn.sync(conn._sync_queue.get_nowait())  # noqa: SLF001
    reads = [e for e in emitted if e.get("type") == "read"]
    assert len(reads) == 1
    assert reads[0]["channel"]["ext_id"] == _marked(ch) and reads[0]["up_to_ext_id"] == "50"


async def test_peek_channel_with_linked_chat():
    gw, conn, _ = _setup()
    ch = _channel(_CH_ID, left=False)
    group = h.make_channel(501, title="Чат DEMO", broadcast=False, megagroup=True)
    group.username = "demo_chat"
    gw.add_username("news_chan", ch)
    gw.full_channels[utils.get_peer_id(ch)] = SimpleNamespace(
        full_chat=SimpleNamespace(participants_count=5, about="x", linked_chat_id=501), chats=[ch, group]
    )
    res = await conn.push_join(_item("peek", "news_chan"))
    assert res.peek["linked"] == {"title": "Чат DEMO", "username": "demo_chat", "kind": "discussion"}


async def test_peek_group_linked_is_channel_and_absent_without_link():
    gw, conn, _ = _setup()
    grp = _channel(502, left=False, broadcast=False)
    chan = h.make_channel(_CH_ID, title="DEMO", broadcast=True, megagroup=False)
    gw.add_username("chat_grp", grp)
    gw.full_channels[utils.get_peer_id(grp)] = SimpleNamespace(
        full_chat=SimpleNamespace(participants_count=5, about=None, linked_chat_id=_CH_ID), chats=[grp, chan]
    )
    res = await conn.push_join(_item("peek", "chat_grp"))
    assert res.peek["linked"] == {"title": "DEMO", "username": None, "kind": "channel"}
    # без связи — поля нет
    gw.full_channels[utils.get_peer_id(grp)] = SimpleNamespace(
        full_chat=SimpleNamespace(participants_count=5, about=None, linked_chat_id=None), chats=[grp]
    )
    res = await conn.push_join(_item("peek", "chat_grp"))
    assert "linked" not in res.peek
    # сущности связанного чата нет в full.chats — поля нет
    gw.full_channels[utils.get_peer_id(grp)] = SimpleNamespace(
        full_chat=SimpleNamespace(participants_count=5, about=None, linked_chat_id=_CH_ID), chats=[grp]
    )
    assert "linked" not in (await conn.push_join(_item("peek", "chat_grp"))).peek
