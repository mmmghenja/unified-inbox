"""Ф17-2 (docs/architecture.md и тест E2, поправки П1, П4, П5): выход из групп и
каналов — `TelegramConnector.push_leave` по всем исходам и фильтр сообщений чатов, где владелец не участник
(живые апдейты и сверка), с `FakeGateway` (без сети)."""

from __future__ import annotations

import logging

import pytest
from telethon import errors, utils
from telethon.tl import types

from hub.connectors.base import AuthRequired
from hub.connectors.join import JoinResult
from hub.connectors.telegram.connector import TelegramConnector
from hub.connectors.telegram.gateway import NewMessageGE

from . import helpers as h
from .helpers import FakeGateway

_FAKE_NOW_S = 1_700_000_000.0


def _channel(id_: int, *, left=False, broadcast=True, forum=False, title="Канал"):
    ch = h.make_channel(id_, title=title, broadcast=broadcast, megagroup=not broadcast, forum=forum)
    ch.left = left
    return ch


def _chat(id_: int, *, left=False):
    chat = h.make_chat(id_, title="Группа")
    chat.left = left
    return chat


def _marked(entity) -> str:
    return str(utils.get_peer_id(entity))


class _Client:
    """Ядро для сверки: известные беседы (`heads`)."""

    def __init__(self, heads=None):
        self.heads = heads or []

    async def get_cursor(self):  # Ф42-1: курсор аккаунта (дыры догона) — пусто
        return None

    async def push_events(self, events, cursor=None):
        return {"ok": True}

    async def channels_heads(self):
        return list(self.heads)

    async def channels_window(self, *, ext_id, limit=30):
        return []

    async def set_activity(self, *, backfill):
        return {"ok": True}

    async def set_status(self, status, *, last_error=None):
        return {"ok": True}


async def _no_sleep(_seconds):
    return None


def _setup(heads=None):
    gw = FakeGateway()
    client = _Client(heads)
    conn = TelegramConnector(client, gw, account_id=1, wall=lambda: _FAKE_NOW_S)
    conn._me_id = gw.me.id  # noqa: SLF001
    conn._sleep = _no_sleep  # noqa: SLF001
    return gw, conn


def _item(ext_id, *, chat_kind="channel"):
    return {
        "id": 7,
        "lease_gen": 1,
        "step": "leave",
        "ext_id": ext_id,
        "chat_kind": chat_kind,
        "project_id": 3,
    }


def _dialog(gw, entity):
    gw.add_dialog(h.make_dialog(entity, top_message=10, last_message_date_ms=int(_FAKE_NOW_S * 1000)))


# --- push_leave ---------------------------------------------------------------------------------------------


async def test_leave_channel_calls_leave_channel_and_marks_left():
    gw, conn = _setup()
    ch = _channel(500)
    _dialog(gw, ch)
    conn._entities[int(_marked(ch))] = ch  # noqa: SLF001
    conn._topic_titles[_marked(ch)] = {5: "Тема"}  # noqa: SLF001
    res = await conn.push_leave(_item(_marked(ch)))
    assert res == JoinResult("done", result="left")
    assert gw.leave_calls == [("get_entity", int(_marked(ch))), ("leave_channel", int(_marked(ch)))]
    # Кэш: чат помечен «не участник» — живые апдейты после выхода в приём не идут; темы забыты.
    assert conn._entities[int(_marked(ch))].left is True  # noqa: SLF001
    assert _marked(ch) not in conn._topic_titles  # noqa: SLF001


async def test_leave_forum_supergroup_leaves_whole_chat():
    gw, conn = _setup()
    forum = _channel(501, broadcast=False, forum=True)
    _dialog(gw, forum)
    res = await conn.push_leave(_item(_marked(forum), chat_kind="group"))
    assert res == JoinResult("done", result="left")
    assert gw.leave_calls[-1] == ("leave_channel", int(_marked(forum)))


async def test_leave_basic_group_calls_delete_chat_user():
    gw, conn = _setup()
    chat = _chat(42)
    _dialog(gw, chat)
    res = await conn.push_leave(_item(_marked(chat), chat_kind="group"))
    assert res == JoinResult("done", result="left")
    assert gw.leave_calls == [("get_entity", -42), ("leave_chat", 42)]


async def test_leave_not_in_dialogs_is_not_member_without_call():
    gw, conn = _setup()
    res = await conn.push_leave(_item("-1009999"))
    assert res == JoinResult("done", result="not_member")
    assert gw.iter_dialogs_calls == 1 and gw.leave_calls == []


@pytest.mark.parametrize(
    "fresh",
    [
        _chat(43, left=True),  # П4: повтор после потерянного отчёта у basic-группы
        types.ChatForbidden(id=43, title="Группа"),
    ],
)
async def test_leave_fresh_entity_left_is_not_member_without_call(fresh):
    gw, conn = _setup()
    chat = _chat(43)
    _dialog(gw, chat)
    gw.fresh_entities[-43] = fresh
    res = await conn.push_leave(_item("-43", chat_kind="group"))
    assert res == JoinResult("done", result="not_member")
    assert gw.leave_calls == [("get_entity", -43)]


async def test_leave_fresh_entity_channel_private_is_not_member():
    gw, conn = _setup()
    ch = _channel(502)
    _dialog(gw, ch)
    gw.entity_faults.append(errors.ChannelPrivateError(request=None))
    res = await conn.push_leave(_item(_marked(ch)))
    assert res == JoinResult("done", result="not_member")
    assert [c[0] for c in gw.leave_calls] == ["get_entity"]


@pytest.mark.parametrize(
    "exc, expected",
    [
        (errors.UserNotParticipantError(request=None), JoinResult("done", result="not_member")),
        (errors.ChannelPrivateError(request=None), JoinResult("done", result="not_member")),
        (errors.UserCreatorError(request=None), JoinResult("drop", reason="creator")),
        (errors.ChannelInvalidError(request=None), JoinResult("drop", reason="not_found")),
        (errors.ChatIdInvalidError(request=None), JoinResult("drop", reason="not_found")),
        (errors.PeerIdInvalidError(request=None), JoinResult("drop", reason="not_found")),
        (errors.ChatAdminRequiredError(request=None), JoinResult("drop", reason="forbidden")),
        (ConnectionError("сеть"), JoinResult("retry", reason="ConnectionError")),
        (errors.ChatWriteForbiddenError(request=None), JoinResult("drop", reason="ChatWriteForbiddenError")),
    ],
)
async def test_leave_error_classes(exc, expected):
    gw, conn = _setup()
    ch = _channel(503)
    _dialog(gw, ch)
    gw.leave_faults.append(exc)
    assert await conn.push_leave(_item(_marked(ch))) == expected


async def test_leave_floodwait_retry_after_and_auth_lost():
    gw, conn = _setup()
    ch = _channel(504)
    _dialog(gw, ch)
    gw.leave_faults.append(errors.FloodWaitError(request=None, capture=42))
    assert await conn.push_leave(_item(_marked(ch))) == JoinResult(
        "retry", retry_after_s=42.0, reason="FloodWait"
    )
    gw.leave_faults.append(errors.AuthKeyUnregisteredError(request=None))
    with pytest.raises(AuthRequired):
        await conn.push_leave(_item(_marked(ch)))


async def test_leave_bad_items_dropped():
    _gw, conn = _setup()
    assert await conn.push_leave({**_item("-100"), "step": "join"}) == JoinResult("drop", reason="bad_item")
    assert await conn.push_leave(_item("не число")) == JoinResult("drop", reason="bad_item")
    gw, conn = _setup()
    user = h.make_user(77, first_name="Петя")
    _dialog(gw, user)
    assert await conn.push_leave(_item("77")) == JoinResult("drop", reason="not_group")


async def test_leave_enabled_on_telegram_connector():
    assert TelegramConnector.leave_enabled is True


# --- П1: сообщения чатов, где владелец не участник, в приём не идут ---------------------------------------------


@pytest.mark.parametrize(
    "entity, accepted",
    [
        (_channel(600, left=False), True),
        (_channel(600, left=True), False),
        (types.ChannelForbidden(id=600, access_hash=0, title="Канал"), False),
    ],
)
async def test_live_message_from_left_channel_dropped(entity, accepted, caplog):
    gw, conn = _setup()
    emitted = []
    conn.emit = emitted.append
    marked = utils.get_peer_id(types.PeerChannel(600))
    msg = h.make_message(1, types.PeerChannel(600), message="пост", from_id=None)
    with caplog.at_level(logging.INFO, logger="hub.connectors.telegram.connector"):
        for i in (1, 2):
            msg = h.make_message(i, types.PeerChannel(600), message="пост", from_id=None)
            await conn._handle_gateway_event(NewMessageGE(message=msg, entities={marked: entity}))  # noqa: SLF001
    upserts = [e for e in emitted if e.get("type") == "upsert"]
    assert len(upserts) == (2 if accepted else 0)
    lines = [r.getMessage() for r in caplog.records if "не участник" in r.getMessage()]
    assert lines == (
        [] if accepted else [f"connector: chat={marked} — владелец не участник, сообщения чата не принимаются"]
    )
    assert "Канал" not in " ".join(lines)


async def test_live_message_entity_from_update_beats_cache():
    """Сущность из апдейта свежее кэша: вступил снова — кэш `left`, апдейт уже без `left` — сообщение идёт."""
    gw, conn = _setup()
    emitted = []
    conn.emit = emitted.append
    marked = utils.get_peer_id(types.PeerChannel(601))
    conn._entities[marked] = _channel(601, left=True)  # noqa: SLF001
    msg = h.make_message(1, types.PeerChannel(601), message="пост", from_id=None)
    await conn._handle_gateway_event(NewMessageGE(message=msg, entities={marked: _channel(601)}))  # noqa: SLF001
    assert [e["type"] for e in emitted] == ["upsert"]
    # И наоборот: апдейт без сущности чата — решает кэш.
    conn._entities[marked] = _channel(601, left=True)  # noqa: SLF001
    msg = h.make_message(2, types.PeerChannel(601), message="пост", from_id=None)
    await conn._handle_gateway_event(NewMessageGE(message=msg, entities={}))  # noqa: SLF001
    assert [e["type"] for e in emitted] == ["upsert"]


async def test_live_message_after_leave_dropped_until_rejoin():
    gw, conn = _setup()
    emitted = []
    conn.emit = emitted.append
    ch = _channel(602)
    _dialog(gw, ch)
    marked = int(_marked(ch))
    conn._entities[marked] = ch  # noqa: SLF001
    await conn.push_leave(_item(_marked(ch)))
    msg = h.make_message(1, types.PeerChannel(602), message="пост", from_id=None)
    await conn._handle_gateway_event(NewMessageGE(message=msg, entities={}))  # noqa: SLF001
    assert emitted == []


async def test_leave_basic_group_keeps_cache_readded_live_message_accepted():
    """Ревью 17-2a: после выхода из ОБЫЧНОЙ группы кэш не помечается `left` — владельца добавили обратно, короткий
    апдейт группы (без сущности чата, решает кэш) принимается сразу, а не после ближайшей сверки."""
    gw, conn = _setup(heads=[{"ext_id": "-46", "kind": "group", "max_ext_sort": 1}])
    emitted = []
    conn.emit = emitted.append
    chat = _chat(46)
    _dialog(gw, chat)
    assert await conn.push_leave(_item("-46", chat_kind="group")) == JoinResult("done", result="left")
    assert conn._entities[-46].left is False  # noqa: SLF001
    msg = h.make_message(5, types.PeerChat(46), message="снова здесь")
    await conn._handle_gateway_event(NewMessageGE(message=msg, entities={}))  # noqa: SLF001
    assert [e["type"] for e in emitted if e.get("type") == "upsert"] == ["upsert"]


async def test_sync_skips_left_basic_group_dialog():
    """П5: диалог обычной группы после выхода остаётся в списке — сверка его не догоняет и не грузит."""
    left = _chat(44, left=True)
    member = _chat(45)
    gw, conn = _setup(heads=[{"ext_id": "-44", "kind": "group", "max_ext_sort": 1}])
    emitted = []
    conn.emit = emitted.append
    for chat in (left, member):
        _dialog(gw, chat)
        for i in range(1, 4):
            gw.add_message(utils.get_peer_id(chat), h.make_message(i, h.peer_of(chat), message=f"m{i}"))
    await conn.sync("scheduled")
    fetched = {c[0] for c in gw.get_messages_calls}
    assert -44 not in fetched and -45 in fetched
    # Сообщений и маркеров прочтения по чату нет (атрибуты папок — событие `channel` — не сообщения).
    assert not [
        e
        for e in emitted
        if e["type"] in ("upsert", "read") and (e.get("channel") or {}).get("ext_id") == "-44"
    ]
    assert [e["type"] for e in emitted if (e.get("channel") or {}).get("ext_id") == "-45"].count(
        "upsert"
    ) == 3
