"""Ф29-1 (docs/architecture.md–C4, тест D3): поиск и чтение без вступления —
`TelegramConnector.run_lookup` с `FakeGateway` (без сети): отбор и порядок результатов поиска, кэш 10 мин;
поля сообщений канала и группы, служебные пропущены, альбом одним сообщением, `next_before`, обрезка текста;
классы ошибок."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from telethon import errors, utils
from telethon.tl import types

from hub.connectors.base import AuthRequired
from hub.connectors.lookup import LookupResult
from hub.connectors.telegram.connector import TelegramConnector

from . import helpers as h
from .helpers import FakeGateway

_T = 1_700_000_000_000


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _setup():
    gw = FakeGateway()
    clock = _Clock()
    conn = TelegramConnector(object(), gw, account_id=1, wall=lambda: _T / 1000, mono=clock)
    conn._me_id = gw.me.id  # noqa: SLF001
    return gw, conn, clock


def _channel(id_, *, title="Канал", broadcast=True, username=None, left=True, count=None, **flags):
    ch = h.make_channel(id_, title=title, broadcast=broadcast, megagroup=not broadcast, username=username)
    ch.left = left
    ch.participants_count = count
    for key, value in flags.items():
        setattr(ch, key, value)
    return ch


def _found(*, mine=(), results=(), chats=(), users=()):
    return types.contacts.Found(
        my_results=list(mine), results=list(results), chats=list(chats), users=list(users)
    )


async def _search(conn, query="продавцы", limit=10):
    return await conn.run_lookup({"id": 1, "kind": "search", "params": {"query": query, "limit": limit}})


async def _peek(conn, username="some_chan", limit=20, before=None):
    return await conn.run_lookup(
        {"id": 2, "kind": "peek", "params": {"username": username, "limit": limit, "before": before}}
    )


# --- поиск ------------------------------------------------------------------------------------------------


async def test_search_filters_and_orders_my_results_first():
    gw, conn, _ = _setup()
    a = _channel(501, title="Мои продавцы", broadcast=False, username="my_sellers", left=False, count=40)
    b = _channel(502, title="Продавцы WB", username=None, count=None, verified=True, join_request=True)
    b.usernames = [types.Username("old_wb", active=False), types.Username("sellers_wb", active=True)]
    restricted = _channel(503, title="Запрещён", username="bad", restricted=True)
    scam = _channel(504, title="Скам", username="scam_chan", scam=True)
    small_mine = h.make_chat(601, title="Малая своя")
    small_other = h.make_chat(602, title="Малая чужая")
    person = h.make_user(42, first_name="Продавец", username="seller_person")
    bot = h.make_user(43, first_name="Бот", username="seller_bot", bot=True)
    gw.search_found["продавцы"] = _found(
        mine=[types.PeerChannel(501), types.PeerChat(601)],
        results=[
            types.PeerUser(42),
            types.PeerUser(43),
            types.PeerChannel(503),
            types.PeerChat(602),
            types.PeerChannel(502),
            types.PeerChannel(501),
            types.PeerChannel(504),
        ],
        chats=[a, b, restricted, scam, small_mine, small_other],
        users=[person, bot],
    )
    res = await _search(conn, limit=10)
    assert res.outcome == "ok"
    rows = res.payload["results"]
    assert [r["title"] for r in rows] == ["Мои продавцы", "Малая своя", "Продавцы WB", "Скам"]
    assert rows[0] == {
        "ext_id": str(utils.get_peer_id(a)),
        "username": "my_sellers",
        "title": "Мои продавцы",
        "kind": "megagroup",
        "participants": 40,
        "member": True,
        "verified": False,
        "scam": False,
        "fake": False,
        "join_request": False,
    }
    assert rows[1]["kind"] == "group" and rows[1]["username"] is None
    assert rows[2]["username"] == "sellers_wb" and rows[2]["kind"] == "broadcast"
    assert rows[2]["participants"] is None and rows[2]["member"] is False
    assert rows[2]["verified"] is True and rows[2]["join_request"] is True
    assert rows[3]["scam"] is True
    assert gw.search_calls == [("продавцы", 20)]
    # Сущности — в общий кэш.
    assert conn._entities[utils.get_peer_id(b)] is b  # noqa: SLF001


async def test_search_limit_cut_and_fetch_cap():
    gw, conn, _ = _setup()
    chans = [_channel(700 + i, title=f"К{i}", username=f"k{i}") for i in range(30)]
    gw.search_found["клиенты"] = _found(results=[types.PeerChannel(700 + i) for i in range(30)], chats=chans)
    res = await _search(conn, "клиенты", limit=3)
    assert [r["title"] for r in res.payload["results"]] == ["К0", "К1", "К2"]
    await _search(conn, "клиенты", limit=20)
    assert gw.search_calls == [("клиенты", 6), ("клиенты", 40)]


async def test_search_cache_10_minutes():
    gw, conn, clock = _setup()
    gw.search_found["ПВЗ"] = _found(results=[types.PeerChannel(800)], chats=[_channel(800, username="pvz")])
    first = await _search(conn, "ПВЗ")
    clock.now += 599
    second = await _search(conn, "ПВЗ")
    assert first == second and len(gw.search_calls) == 1
    # Ключ — строка без регистра.
    gw.search_found["пвз"] = gw.search_found["ПВЗ"]
    await _search(conn, "пвз")
    assert len(gw.search_calls) == 1
    clock.now += 2
    await _search(conn, "ПВЗ")
    assert len(gw.search_calls) == 2


# --- чтение -----------------------------------------------------------------------------------------------


def _post(id_, peer, **kw):
    attrs = {k: kw.pop(k) for k in ("views", "forwards", "post_author", "replies") if k in kw}
    msg = h.make_message(id_, peer, **kw)
    for key, value in attrs.items():
        setattr(msg, key, value)
    return msg


def _reactions(*counts):
    return types.MessageReactions(
        results=[types.ReactionCount(reaction=types.ReactionEmoji("👍"), count=c) for c in counts]
    )


async def test_peek_channel_fields_service_skipped_next_before_and_caches():
    gw, conn, clock = _setup()
    ch = _channel(900, title="Новости рынка", username="news_chan", count=100)
    gw.add_username("news_chan", ch)
    marked = utils.get_peer_id(ch)
    gw.full_channels[marked] = SimpleNamespace(
        full_chat=SimpleNamespace(participants_count=12_400, about="о" * 400)
    )
    peer = types.PeerChannel(900)
    gw.history[marked] = [
        _post(10, peer, message="старый пост", date_ms=_T - 3000, views=50),
        h.make_message(11, peer, action=types.MessageActionPinMessage(), date_ms=_T - 2000),
        _post(
            12,
            peer,
            message="x" * 2500,
            date_ms=_T - 1000,
            views=1200,
            forwards=3,
            post_author="Редактор",
            replies=types.MessageReplies(replies=5, replies_pts=0),
            reactions=_reactions(7, 2),
            media=types.MessageMediaPhoto(photo=types.PhotoEmpty(id=1)),
            fwd_from=types.MessageFwdHeader(date=h.ts(_T), from_name="Другой канал"),
        ),
    ]
    res = await _peek(conn, "news_chan", limit=3)
    assert res.outcome == "ok"
    p = res.payload
    assert p["chat"] == {
        "ext_id": str(marked),
        "username": "news_chan",
        "title": "Новости рынка",
        "kind": "broadcast",
        "participants": 12_400,
        "member": False,
        "about": "о" * 300,
    }
    assert [m["id"] for m in p["messages"]] == [12, 10]
    top = p["messages"][0]
    assert top["author"] == "Редактор" and top["fwd"] == "Другой канал"
    assert (top["views"], top["forwards"], top["replies"], top["reactions"]) == (1200, 3, 5, 9)
    assert top["media"] == ["photo"] and top["ts"] == _T - 1000
    assert top["text"] == "x" * 2000 + "…"
    assert p["messages"][1]["author"] == "Новости рынка" and p["messages"][1]["reactions"] is None
    # Telegram отдал ровно limit (служебное тоже) — дальше есть.
    assert p["next_before"] == 10
    assert gw.history_calls == [(marked, 0, 3)]
    # Второй раз: сущность и полная информация — из кэша, листаем назад.
    clock.now += 60
    res = await _peek(conn, "NEWS_CHAN", limit=3, before=12)
    assert gw.resolve_calls == ["news_chan"]
    assert [c for c in gw.join_calls if c[0] == "full"] == [("full", marked)]
    assert gw.history_calls[-1] == (marked, 12, 3)
    assert [m["id"] for m in res.payload["messages"]] == [10] and res.payload["next_before"] is None


async def test_peek_group_authors_album_and_no_phone():
    gw, conn, _ = _setup()
    grp = _channel(950, title="Чат продавцов", broadcast=False, username="sellers_chat", left=False)
    gw.add_username("sellers_chat", grp)
    marked = utils.get_peer_id(grp)
    petya = h.make_user(77, first_name="Петя", last_name="Иванов")
    petya.phone = "79000001122"
    gw.entities[utils.get_peer_id(petya)] = petya
    gw.full_channels[marked] = SimpleNamespace(full_chat=SimpleNamespace(participants_count=None, about=""))
    peer = types.PeerChannel(950)
    gw.history[marked] = [
        _post(
            20, peer, message="", from_id=types.PeerUser(77), grouped_id=5, media=types.MessageMediaPhoto()
        ),
        _post(
            21,
            peer,
            message="подпись альбома",
            from_id=types.PeerUser(77),
            grouped_id=5,
            media=types.MessageMediaDocument(document=types.DocumentEmpty(id=1)),
            reactions=_reactions(1),
        ),
        _post(22, peer, message="от имени чата", from_id=types.PeerChannel(950)),
        _post(23, peer, message="без отправителя"),
    ]
    res = await _peek(conn, "sellers_chat", limit=10)
    msgs = res.payload["messages"]
    assert res.payload["chat"]["kind"] == "megagroup" and res.payload["chat"]["member"] is True
    assert res.payload["chat"]["about"] is None and res.payload["next_before"] is None
    assert [m["id"] for m in msgs] == [23, 22, 20]
    assert msgs[0]["author"] == "Чат продавцов" and msgs[1]["author"] == "Чат продавцов"
    album = msgs[2]
    assert album["author"] == "Петя Иванов" and album["album"] == 2
    assert album["text"] == "подпись альбома" and sorted(album["media"]) == ["document", "photo"]
    assert album["reactions"] == 1
    assert "7999" not in repr(res.payload)


async def test_peek_user_is_not_group_without_history():
    gw, conn, _ = _setup()
    gw.add_username("some_person", h.make_user(42, first_name="Петя", username="some_person"))
    assert await _peek(conn, "some_person") == LookupResult("error", reason="not_group")
    assert gw.history_calls == []


async def test_peek_known_entity_by_username_without_resolve():
    gw, conn, _ = _setup()
    ch = _channel(990, username="Known_Chan", left=False)
    conn._entities[utils.get_peer_id(ch)] = ch  # noqa: SLF001 — из диалогов сверки
    res = await _peek(conn, "known_chan")
    assert res.outcome == "ok" and gw.resolve_calls == []


# --- классы ошибок (C4) -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        (errors.UsernameNotOccupiedError(request=None), LookupResult("error", reason="not_found")),
        (errors.UsernameInvalidError(request=None), LookupResult("error", reason="not_found")),
        (errors.ChannelPrivateError(request=None), LookupResult("error", reason="private")),
        (
            errors.FloodWaitError(request=None, capture=33),
            LookupResult("error", reason="flood", retry_after_s=33.0),
        ),
        (OSError("net"), LookupResult("error", reason="temporary")),
        (errors.ServerError(request=None, message="X", code=500), LookupResult("error", reason="temporary")),
        (
            errors.RPCError(request=None, message="CHAT_WEIRD", code=400),
            LookupResult("error", reason="CHAT_WEIRD"),
        ),
    ],
)
async def test_peek_resolve_errors(fault, expected):
    gw, conn, _ = _setup()
    gw.resolve_faults.append(fault)
    assert await _peek(conn) == expected


async def test_history_private_and_search_errors():
    gw, conn, _ = _setup()
    ch = _channel(991, username="closed_one")
    gw.add_username("closed_one", ch)
    gw.history_faults.append(errors.ChannelInvalidError(request=None))
    assert await _peek(conn, "closed_one") == LookupResult("error", reason="private")
    gw.search_faults.append(errors.QueryTooShortError(request=None))
    assert await _search(conn, "abc") == LookupResult("error", reason="query_too_short")
    gw.search_faults.append(errors.FloodWaitError(request=None, capture=7))
    assert await _search(conn, "abd") == LookupResult("error", reason="flood", retry_after_s=7.0)


async def test_flood_on_full_channel_is_reported_not_swallowed():
    """Ревью Ф29-1: FloodWait от GetFullChannel внутри чтения — `flood` ядру (пауза аккаунта), а не тихое `ok`
    без описания, после которого следующее чтение снова дёргает Telegram посреди паузы."""
    gw, conn, _ = _setup()
    ch = _channel(992, username="flood_chan")
    gw.add_username("flood_chan", ch)
    gw.history[utils.get_peer_id(ch)] = []
    gw.full_faults.append(errors.FloodWaitError(request=None, capture=600))
    assert await _peek(conn, "flood_chan") == LookupResult("error", reason="flood", retry_after_s=600.0)


async def test_auth_lost_raises_auth_required():
    gw, conn, _ = _setup()
    gw.search_faults.append(errors.AuthKeyUnregisteredError(request=None))
    with pytest.raises(AuthRequired):
        await _search(conn)


# --- Ф32-2 п.1 + П6: особые паузы Telegram -----------------------------------------------------------------


@pytest.mark.parametrize("cls", [errors.FloodPremiumWaitError, errors.SlowModeWaitError])
async def test_f322_special_flood_classes_are_flood_with_seconds(cls):
    gw, conn, _ = _setup()
    gw.search_faults.append(cls(request=None, capture=42))
    assert await _search(conn, "abc") == LookupResult("error", reason="flood", retry_after_s=42.0)


@pytest.mark.parametrize(
    ("seconds", "expected"), [(0, 1.0), (-5, 1.0), (86_400, 86_400.0), (200_000, 86_400.0)]
)
async def test_f322_flood_seconds_clamped_to_core_range(seconds, expected):
    gw, conn, _ = _setup()
    gw.search_faults.append(errors.FloodWaitError(request=None, capture=seconds))
    assert await _search(conn, "abc") == LookupResult("error", reason="flood", retry_after_s=expected)


async def test_f322_flood_subclass_without_seconds_is_named_error_not_flood():
    gw, conn, _ = _setup()
    exc = errors.FrozenMethodInvalidError(request=None)
    assert isinstance(exc, errors.FloodError) and not hasattr(exc, "seconds")
    gw.search_faults.append(exc)
    assert await _search(conn, "abc") == LookupResult("error", reason="FrozenMethodInvalidError")


@pytest.mark.parametrize("cls", [errors.FloodPremiumWaitError, errors.SlowModeWaitError])
async def test_f322_full_channel_special_flood_reported(cls):
    """Ревью Ф32-2: особая пауза Telegram от `GetFullChannel` не глотается — `flood` с секундами."""
    gw, conn, _ = _setup()
    ch = _channel(992, username="flood_chan")
    gw.add_username("flood_chan", ch)
    gw.history[utils.get_peer_id(ch)] = []
    gw.full_faults.append(cls(request=None, capture=600))
    assert await _peek(conn, "flood_chan") == LookupResult("error", reason="flood", retry_after_s=600.0)


# --- 24.10: кто это по @имени (проверка адресата `hub_draft_new`) ---------------------------------------


async def _resolve(conn, username):
    return await conn.run_lookup({"id": 3, "kind": "resolve", "params": {"username": username}})


async def test_resolve_kinds_without_history():
    gw, conn, _ = _setup()
    gw.add_username("some_person", h.make_user(42, first_name="Петя", username="some_person"))
    gw.add_username("help_bot", h.make_user(43, first_name="Бот", username="help_bot", bot=True))
    gw.add_username("news_chan", _channel(990, username="news_chan"))
    gw.add_username("talk_group", _channel(991, username="talk_group", broadcast=False))
    assert await _resolve(conn, "some_person") == LookupResult("ok", payload={"kind": "user"})
    assert await _resolve(conn, "help_bot") == LookupResult("ok", payload={"kind": "bot"})
    assert await _resolve(conn, "news_chan") == LookupResult("ok", payload={"kind": "broadcast"})
    assert await _resolve(conn, "talk_group") == LookupResult("ok", payload={"kind": "megagroup"})
    assert gw.history_calls == []


async def test_resolve_uses_entity_cache_and_errors():
    gw, conn, _ = _setup()
    ch = _channel(992, username="Known_Chan", left=False)
    conn._entities[utils.get_peer_id(ch)] = ch  # noqa: SLF001 — из диалогов сверки
    assert await _resolve(conn, "known_chan") == LookupResult("ok", payload={"kind": "broadcast"})
    assert gw.resolve_calls == []
    gw.resolve_faults.append(errors.UsernameNotOccupiedError(request=None))
    assert await _resolve(conn, "nobody_here") == LookupResult("error", reason="not_found")
    gw.resolve_faults.append(errors.FloodWaitError(request=None, capture=40))
    assert await _resolve(conn, "nobody_two") == LookupResult("error", reason="flood", retry_after_s=40.0)
    assert await conn.run_lookup({"id": 4, "kind": "resolve", "params": {}}) == LookupResult(
        "error", reason="bad_item"
    )
