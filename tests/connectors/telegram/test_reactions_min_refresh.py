"""Ф18-2 (docs/architecture.md): живой `UpdateMessageReactions` с `min=True`
(супергруппы, каналы) -> очередь перечитывания `GetMessagesReactions` пачкой; подменённые часы."""

from __future__ import annotations

import asyncio

from telethon import errors, utils
from telethon.tl import types

from hub.connectors.telegram import connector as connector_mod
from hub.connectors.telegram.connector import TelegramConnector
from hub.connectors.telegram.gateway import ReactionsGE

from ..media_fakes import FakeMediaCore
from . import helpers as h
from .helpers import FakeGateway

FORUM = h.make_channel(600, title="Форум", megagroup=True, forum=True)
GROUP = h.make_channel(601, title="Группа", megagroup=True)


def marked(entity) -> int:
    return utils.get_peer_id(h.peer_of(entity))


def counts(*emojis, me=False):
    return [
        types.ReactionCount(reaction=types.ReactionEmoji(emoticon=e), count=1, chosen_order=0 if me else None)
        for e in emojis
    ]


def mr(*emojis, me=False, is_min=False):
    return types.MessageReactions(results=counts(*emojis, me=me), min=is_min)


def upd(entity, msg_id, reactions, top=None):
    return types.UpdateMessageReactions(
        peer=h.peer_of(entity), msg_id=msg_id, reactions=reactions, top_msg_id=top
    )


def answer(*items):
    return types.Updates(updates=list(items), users=[], chats=[], date=None, seq=0)


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps: list[float] = []

    def mono(self):
        return self.t

    async def sleep(self, s):
        self.sleeps.append(s)
        self.t += s
        await asyncio.sleep(0)


def setup(*entities):
    gw = FakeGateway()
    for entity in entities:
        gw.seed_peer(marked(entity), h.input_peer_of(entity))
    clock = Clock()
    conn = TelegramConnector(FakeMediaCore().client(), gw, account_id=7, mono=clock.mono, sleep=clock.sleep)
    conn._me_id = h.ME_ID  # noqa: SLF001
    events: list[dict] = []
    conn.emit = events.append
    return gw, conn, events, clock


async def live(conn, entity, msg_id, *, is_min=True, top=None):
    await conn._handle_gateway_event(  # noqa: SLF001
        ReactionsGE(
            peer_marked_id=marked(entity), msg_id=msg_id, reactions=mr("🤝", is_min=is_min), top_msg_id=top
        )
    )


async def drain(conn):
    task = conn._react_task  # noqa: SLF001
    if task is not None:
        await task


def many_calls(gw):
    return [c for c in gw.reaction_calls if c[0] == "get_many"]


async def test_min_live_update_one_batched_request_per_chat():
    gw, conn, events, clock = setup(FORUM)
    gw.get_reactions_results.append(
        answer(upd(FORUM, 5, mr("🤝", me=True), top=1), upd(FORUM, 6, mr("👍", me=False), top=1))
    )
    await live(conn, FORUM, 5, top=1)
    await live(conn, FORUM, 6, top=1)
    assert events == []  # сразу не шлётся
    await drain(conn)
    assert many_calls(gw) == [("get_many", marked(FORUM), [5, 6])]
    assert clock.sleeps[0] == connector_mod.REACT_REFRESH_WINDOW_S
    assert [e["ext_id"] for e in events] == ["5", "6"]
    assert all(e["type"] == "reaction" and e["channel"]["ext_id"] == f"{marked(FORUM)}:1" for e in events)
    assert events[0]["reactions"][0]["me"] is True
    assert not events[1]["reactions"][0].get("me")


async def test_topic_from_original_update_when_response_has_none():
    gw, conn, events, _ = setup(FORUM)
    gw.get_reactions_results.append(answer(upd(FORUM, 5, mr("🤝", me=True))))
    await live(conn, FORUM, 5, top=77)
    await drain(conn)
    assert events[0]["channel"] == {"ext_id": f"{marked(FORUM)}:77", "kind": "topic"}


async def test_no_top_goes_to_parent():
    gw, conn, events, _ = setup(GROUP)
    gw.get_reactions_results.append(answer(upd(GROUP, 5, mr("🤝", me=True))))
    await live(conn, GROUP, 5)
    await drain(conn)
    assert events[0]["channel"]["ext_id"] == str(marked(GROUP))
    assert events[0]["channel"]["kind"] != "topic"


async def test_duplicate_updates_collapse_to_one_id():
    gw, conn, events, _ = setup(GROUP)
    gw.get_reactions_results.append(answer(upd(GROUP, 5, mr("🤝", me=True))))
    await live(conn, GROUP, 5)
    await live(conn, GROUP, 5)
    await drain(conn)
    assert many_calls(gw) == [("get_many", marked(GROUP), [5])]
    assert len(events) == 1


async def test_per_minute_limit_25_chats():
    chats = [h.make_channel(700 + i, title=f"Г{i}", megagroup=True) for i in range(25)]
    gw, conn, events, clock = setup(*chats)
    for c in chats:
        gw.get_reactions_results.append(answer(upd(c, 5, mr("🤝", me=True))))
    for c in chats:
        await live(conn, c, 5)
    task = conn._react_task  # noqa: SLF001
    t0 = clock.t
    at_minute = None
    while not task.done():
        await asyncio.sleep(0)
        # первая точка, где часы дошли до t0 + 60 (конец лимитного сна) — запросов было ровно предел
        if at_minute is None and clock.t >= t0 + 60.0 - 1e-9:
            at_minute = len(many_calls(gw))
    assert len(many_calls(gw)) == 25
    assert at_minute == connector_mod.REACT_REFRESH_PER_MIN
    assert len(events) == 25


async def test_flood_wait_pauses_then_retries():
    gw, conn, events, clock = setup(GROUP)
    gw.get_reactions_faults.append(errors.FloodWaitError(request=None, capture=17))
    gw.get_reactions_results.append(answer(upd(GROUP, 5, mr("🤝", me=True))))
    await live(conn, GROUP, 5)
    await drain(conn)
    assert 17.0 in clock.sleeps
    assert len(many_calls(gw)) == 2
    assert len(events) == 1


async def test_other_error_drops_item():
    gw, conn, events, _ = setup(GROUP)
    gw.get_reactions_faults.append(errors.MessageIdInvalidError(request=None))
    await live(conn, GROUP, 5)
    await drain(conn)
    assert len(many_calls(gw)) == 1
    assert events == []
    assert conn._react_queued == 0  # noqa: SLF001


async def test_answer_still_min_gives_no_event():
    gw, conn, events, _ = setup(GROUP)
    gw.get_reactions_results.append(answer(upd(GROUP, 5, mr("🤝", is_min=True))))
    await live(conn, GROUP, 5)
    await drain(conn)
    assert len(many_calls(gw)) == 1
    assert events == []


async def test_no_entity_no_request():
    gw, conn, events, _ = setup()  # peer в кэше не засеян
    await live(conn, GROUP, 5)
    await drain(conn)
    assert many_calls(gw) == []
    assert events == []


async def test_full_update_is_emitted_without_request():
    gw, conn, events, _ = setup(GROUP)
    await live(conn, GROUP, 5, is_min=False)
    assert conn._react_task is None  # noqa: SLF001
    assert gw.reaction_calls == []
    assert len(events) == 1 and events[0]["reactions"][0]["emoji"] == "🤝"


async def test_queue_is_capped():
    gw, conn, _, _ = setup(GROUP)
    for i in range(connector_mod.REACT_REFRESH_QUEUE_MAX + 10):
        conn._enqueue_reaction_refresh(marked(GROUP), i + 1, None)  # noqa: SLF001
    assert conn._react_queued == connector_mod.REACT_REFRESH_QUEUE_MAX  # noqa: SLF001
    assert conn._react_dropped == 10  # noqa: SLF001
    conn._react_task.cancel()  # noqa: SLF001


async def test_chat_cooldown_between_refreshes():
    """Шумная группа: второе перечитывание того же чата — не раньше `REACT_REFRESH_CHAT_COOLDOWN_S` после
    первого (окно сбора не держит предел на одной группе)."""
    gw, conn, events, clock = setup(GROUP)
    gw.get_reactions_results.append(answer(upd(GROUP, 5, mr("🤝", me=True))))
    gw.get_reactions_results.append(answer(upd(GROUP, 6, mr("👍"))))
    await live(conn, GROUP, 5)
    await drain(conn)
    first = clock.t
    await live(conn, GROUP, 6)
    task = conn._react_task  # noqa: SLF001
    while not task.done():
        await asyncio.sleep(0)
        if len(many_calls(gw)) == 2:
            break
    assert len(many_calls(gw)) == 2
    assert clock.t - first >= connector_mod.REACT_REFRESH_CHAT_COOLDOWN_S - 1e-9
    await drain(conn)
    assert len(events) == 2
