"""Ф8-7 (C1 + П5, тест 11): прочитанность из хаба в Telegram — `TelegramConnector.push_read` с `FakeGateway`
(без сети)."""

from __future__ import annotations

import pytest
from telethon import errors, utils

from hub.connectors.base import AuthRequired
from hub.connectors.telegram.connector import TelegramConnector

from ..media_fakes import FakeMediaCore
from . import helpers as h
from .helpers import FakeGateway

USER = h.make_user(42, first_name="Петя")
GROUP = h.make_chat(77, title="Малая группа")
SUPER = h.make_channel(500, title="Супергруппа", megagroup=True)
FORUM = h.make_channel(600, title="Форум", megagroup=True, forum=True)


def marked(entity) -> int:
    return utils.get_peer_id(h.peer_of(entity))


def setup():
    gw = FakeGateway()
    for entity in (USER, GROUP, SUPER, FORUM):
        gw.seed_peer(marked(entity), h.input_peer_of(entity))
    conn = TelegramConnector(FakeMediaCore().client(), gw, account_id=7)
    return gw, conn


def item(channel_ext_id: str, msg_ext_id: str, kind: str = "dm", parent: str | None = None) -> dict:
    return {
        "channel_id": 12,
        "gen": 1,
        "lease_gen": 1,
        "channel": {"ext_id": channel_ext_id, "kind": kind, "parent_ext_id": parent},
        "message": {"ext_id": msg_ext_id, "ts": 1_790_000_000_000, "ext_sort": 0, "thread_ext_id": None},
    }


async def test_11_chat_read_history_max_id():
    gw, conn = setup()
    assert conn.read_ack_enabled is True
    res = await conn.push_read(item(str(marked(USER)), "15"))
    assert res.outcome == "done"
    res = await conn.push_read(item(str(marked(GROUP)), "7", kind="group"))
    assert res.outcome == "done"
    assert gw.read_calls == [("history", marked(USER), 15), ("history", marked(GROUP), 7)]


async def test_11_supergroup_known_not_forum_reads_history():
    gw, conn = setup()
    conn._entities[marked(SUPER)] = SUPER
    res = await conn.push_read(item(str(marked(SUPER)), "99", kind="group"))
    assert res.outcome == "done"
    assert gw.read_calls == [("history", marked(SUPER), 99)]


@pytest.mark.parametrize("topic_id", [3, 1])
async def test_11_topic_uses_read_discussion_never_history(topic_id):
    gw, conn = setup()
    chat = str(marked(FORUM))
    res = await conn.push_read(item(f"{chat}:{topic_id}", "321", kind="topic", parent=chat))
    assert res.outcome == "done"
    assert gw.read_calls == [("discussion", marked(FORUM), topic_id, 321)]


async def test_p5_forum_parent_never_read_history():
    gw, conn = setup()
    conn._entities[marked(FORUM)] = FORUM
    res = await conn.push_read(item(str(marked(FORUM)), "321", kind="group"))
    assert res == ("drop", None, "forum_parent")
    assert gw.read_calls == []


async def test_p5_channel_entity_unknown_is_retry_without_call():
    gw, conn = setup()
    res = await conn.push_read(item(str(marked(SUPER)), "5", kind="group"))
    assert res.outcome == "retry" and res.reason == "entity_unknown"
    assert gw.read_calls == []


async def test_11_flood_wait_is_retry_with_seconds():
    gw, conn = setup()
    gw.read_faults.append(errors.FloodWaitError(request=None, capture=7))
    res = await conn.push_read(item(str(marked(USER)), "15"))
    assert res.outcome == "retry"
    assert res.retry_after_s == 7


async def test_p5_topic_rpc_error_is_drop_with_class_name():
    gw, conn = setup()
    chat = str(marked(FORUM))
    gw.read_faults.append(errors.ChannelPrivateError(request=None))
    res = await conn.push_read(item(f"{chat}:1", "5", kind="topic", parent=chat))
    assert res == ("drop", None, "ChannelPrivateError")


async def test_p5_auth_lost_raises_auth_required():
    gw, conn = setup()
    gw.read_faults.append(errors.AuthKeyUnregisteredError(request=None))
    with pytest.raises(AuthRequired):
        await conn.push_read(item(str(marked(USER)), "15"))


async def test_p5_network_is_retry():
    gw, conn = setup()
    gw.read_faults.append(ConnectionError("down"))
    res = await conn.push_read(item(str(marked(USER)), "15"))
    assert res.outcome == "retry" and res.reason == "ConnectionError"


async def test_p5_cache_miss_is_retry_without_dialogs_pass():
    gw, conn = setup()
    gw.forget_peer(marked(USER))
    res = await conn.push_read(item(str(marked(USER)), "15"))
    assert res.outcome == "retry" and res.reason == "peer_unresolved"
    assert gw.iter_dialogs_calls == 0
    assert gw.read_calls == []


@pytest.mark.parametrize("ch, msg", [("abc", "5"), ("42", "x"), ("42", "0"), ("42:x", "5")])
async def test_bad_ids_drop(ch, msg):
    gw, conn = setup()
    res = await conn.push_read(item(ch, msg))
    assert res.outcome == "drop"
    assert gw.read_calls == []
