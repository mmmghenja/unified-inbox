"""Ф23-3 (docs/architecture.md): просмотры постов
каналов в коннекторе Telegram — `views`/`forwards` в событии поста канала (`convert_message`), элементы
события `views` (`views_items`) и часовой фоновый проход (`_views_pass`/`_views_loop`): только беседы-каналы
ядра (не архив, последний пост моложе 72 ч), где владелец участник; история до 100 под замком сверки; посты моложе
72 ч, пачки ≤ 100; FloodWait и сеть проход откладывают, коннектор не падает; в журнале — без текстов и
названий."""

import asyncio
import logging

import pytest
from telethon import errors
from telethon.tl import types

from hub.connectors.telegram import connector as connector_mod
from hub.connectors.telegram import convert
from hub.connectors.telegram.connector import TelegramConnector

from . import helpers as h
from .conftest import create_tg_account, make_core_client
from .helpers import FakeGateway

NOW_MS = 1_800_000_000_000
HOUR_MS = 60 * 60 * 1000
SECRET_TITLE = "Секретный канал 7f3a"
SECRET_TEXT = "секретный текст поста 91bc"


def _channel(id_: int, *, left: bool = False, title: str = SECRET_TITLE) -> types.Channel:
    ch = h.make_channel(id_, title=title, broadcast=True)
    ch.left = left
    return ch


def _marked(entity) -> int:
    return int(convert.peer_ext_id(h.peer_of(entity)))


def _post(id_: int, entity, *, age_ms: int, views: int | None, forwards: int | None = None):
    msg = h.make_message(id_, h.peer_of(entity), message=SECRET_TEXT, date_ms=NOW_MS - age_ms)
    msg.views = views
    msg.forwards = forwards
    return msg


def _ctx(kind: str) -> convert.ConvertContext:
    return convert.ConvertContext(
        channel={"ext_id": "-1001", "kind": kind, "title": "x", "own_id_space": True, "attrs": None},
        channel_kind_for_sender=kind,
        me_id=h.ME_ID,
        entities={},
    )


# --- конвертация ------------------------------------------------------------------------------------------


def test_convert_channel_post_carries_views_and_forwards():
    ch = _channel(1001)
    out = convert.convert_message(_post(5, ch, age_ms=0, views=1234, forwards=5), _ctx("channel"))
    assert (out["views"], out["forwards"]) == (1234, 5)
    out = convert.convert_message(_post(6, ch, age_ms=0, views=10, forwards=None), _ctx("channel"))
    assert (out["views"], out["forwards"]) == (10, None)


def test_convert_no_views_keys_without_counter_or_outside_channel():
    ch = _channel(1001)
    assert "views" not in convert.convert_message(_post(5, ch, age_ms=0, views=None), _ctx("channel"))
    # группа (мегагруппа, обсуждение) со счётчиком — не пост канала
    group_post = _post(5, ch, age_ms=0, views=99)
    out = convert.convert_message(group_post, _ctx("group"))
    assert "views" not in out and "forwards" not in out
    service = h.make_message(
        7, h.peer_of(ch), action=types.MessageActionChatEditTitle(title="t"), date_ms=NOW_MS
    )
    assert "views" not in convert.convert_message(service, _ctx("channel"))


def test_views_items_only_fresh_posts_with_counter_ascending():
    ch = _channel(1001)
    since = NOW_MS - 72 * HOUR_MS
    msgs = [
        _post(9, ch, age_ms=HOUR_MS, views=30, forwards=2),
        _post(3, ch, age_ms=80 * HOUR_MS, views=500),  # старше 72 ч
        _post(7, ch, age_ms=2 * HOUR_MS, views=None),  # без счётчика
        _post(8, ch, age_ms=71 * HOUR_MS, views=40),
    ]
    assert convert.views_items(msgs, since_ms=since) == [
        {"ext_id": "8", "views": 40, "forwards": None},
        {"ext_id": "9", "views": 30, "forwards": 2},
    ]


# --- фоновый проход: подделки -----------------------------------------------------------------------------


class _HeadsClient:
    def __init__(self, heads):
        self.heads = heads
        self.heads_calls = 0

    async def channels_heads(self):
        self.heads_calls += 1
        if isinstance(self.heads, Exception):
            raise self.heads
        return list(self.heads)


class _Gw(FakeGateway):
    """`FakeGateway` с очередью сбоев истории и проверкой «под замком сверки»."""

    def __init__(self):
        super().__init__()
        self.faults: dict[int, Exception] = {}
        self.locked_seen: list[bool] = []
        self.connector: TelegramConnector | None = None
        self.ignore_limit = False

    async def get_messages(self, peer, *, min_id, limit):
        marked = (
            int(convert.peer_ext_id(peer))
            if not isinstance(peer, types.InputPeerChannel)
            else -(1_000_000_000_000 + peer.channel_id)
        )
        assert self.connector is not None
        self.locked_seen.append(self.connector._sync_lock.locked())  # noqa: SLF001
        if marked in self.faults:
            self.get_messages_calls.append((marked, min_id, limit))
            raise self.faults.pop(marked)
        if self.ignore_limit:
            self.get_messages_calls.append((marked, min_id, limit))
            msgs = sorted(self.history.get(marked, []), key=lambda m: m.id, reverse=True)
            return msgs, dict(self.entities)
        return await super().get_messages(peer, min_id=min_id, limit=limit)


def _head(entity, *, kind="channel", last_age_ms=HOUR_MS, archived=False):
    return {
        "ext_id": str(_marked(entity)),
        "kind": kind,
        "parent_ext_id": None,
        "own_id_space": True,
        "max_ext_sort": 1,
        "max_ext_id": "1",
        "last_msg_ts": None if last_age_ms is None else NOW_MS - last_age_ms,
        "archived": archived,
    }


def _connector(heads, gw: _Gw):
    sleeps: list[float] = []

    async def _sleep(s):
        sleeps.append(s)

    conn = TelegramConnector(_HeadsClient(heads), gw, account_id=1, wall=lambda: NOW_MS / 1000, sleep=_sleep)
    conn._me_id = h.ME_ID  # noqa: SLF001
    emitted: list[dict] = []
    conn.emit = emitted.append
    gw.connector = conn
    return conn, emitted, sleeps


def _seed(gw: _Gw, entity, posts) -> None:
    gw.seed_peer(_marked(entity), h.input_peer_of(entity))
    gw.entities[_marked(entity)] = entity
    for p in posts:
        gw.add_message(_marked(entity), p)


async def test_pass_only_member_channels_with_fresh_posts():
    gw = _Gw()
    target = _channel(1001)
    other = _channel(1002)
    old = _channel(1003)
    archived = _channel(1004)
    group = h.make_channel(1005, megagroup=True)
    left_cached = _channel(1006, left=True)
    left_in_reply = _channel(1007)
    not_in_session = _channel(1008)
    _seed(
        gw,
        target,
        [
            _post(1, target, age_ms=100 * HOUR_MS, views=1),
            _post(2, target, age_ms=HOUR_MS, views=1234, forwards=5),
        ],
    )
    _seed(gw, other, [_post(10, other, age_ms=3 * HOUR_MS, views=77)])
    for e in (old, archived, group, left_cached):
        _seed(gw, e, [_post(1, e, age_ms=HOUR_MS, views=9)])
    _seed(gw, left_in_reply, [_post(1, left_in_reply, age_ms=HOUR_MS, views=9)])
    gw.entities[_marked(left_in_reply)] = _channel(1007, left=True)  # ответ истории: владелец уже не участник
    heads = [
        _head(left_in_reply),  # первым: кэш коннектора о нём ещё ничего не знает
        _head(target),
        _head(old, last_age_ms=73 * HOUR_MS),
        _head(archived, archived=True),
        _head(group, kind="group"),
        _head(left_cached),
        _head(not_in_session),
        _head(other, last_age_ms=3 * HOUR_MS),
        _head(_channel(1009), last_age_ms=None),
    ]
    conn, emitted, sleeps = _connector(heads, gw)
    conn._entities[_marked(left_cached)] = left_cached  # noqa: SLF001

    delay = await conn._views_pass()  # noqa: SLF001

    assert delay == connector_mod._VIEWS_INTERVAL_S  # noqa: SLF001
    requested = [c[0] for c in gw.get_messages_calls]
    assert requested == [_marked(left_in_reply), _marked(target), _marked(other)]
    assert all(c[1] is None and c[2] == 100 for c in gw.get_messages_calls)
    assert gw.iter_dialogs_calls == 0  # промах кэша сессии — без прохода диалогов
    assert gw.locked_seen == [True, True, True]  # история — под замком сверки
    assert emitted == [
        {
            "type": "views",
            "channel": {"ext_id": str(_marked(target)), "kind": "channel"},
            "ts": NOW_MS,
            "items": [{"ext_id": "2", "views": 1234, "forwards": 5}],
        },
        {
            "type": "views",
            "channel": {"ext_id": str(_marked(other)), "kind": "channel"},
            "ts": NOW_MS,
            "items": [{"ext_id": "10", "views": 77, "forwards": None}],
        },
    ]
    # пауза между запрошенными каналами (~2 с), перед первым — нет
    assert sleeps == [2.0, 2.0]


async def test_pass_splits_items_by_100():
    gw = _Gw()
    gw.ignore_limit = True
    ch = _channel(1001)
    _seed(gw, ch, [_post(i, ch, age_ms=HOUR_MS, views=i) for i in range(1, 151)])
    conn, emitted, _ = _connector([_head(ch)], gw)
    await conn._views_pass()  # noqa: SLF001
    assert [len(e["items"]) for e in emitted] == [100, 50]
    assert emitted[0]["items"][0]["ext_id"] == "1" and emitted[1]["items"][-1]["ext_id"] == "150"


async def test_pass_flood_wait_postpones_without_crash_and_logs_no_text(caplog):
    gw = _Gw()
    a, b = _channel(1001), _channel(1002)
    _seed(gw, a, [_post(1, a, age_ms=HOUR_MS, views=5)])
    _seed(gw, b, [_post(1, b, age_ms=HOUR_MS, views=6)])
    gw.faults[_marked(a)] = errors.FloodWaitError(request=None, capture=900)
    conn, emitted, _ = _connector([_head(a), _head(b)], gw)
    with caplog.at_level(logging.DEBUG):
        delay = await conn._views_pass()  # noqa: SLF001
    assert delay == 900
    assert [c[0] for c in gw.get_messages_calls] == [_marked(a)]  # проход прерван, b — в следующий раз
    assert emitted == []
    assert str(_marked(a)) in caplog.text and "FloodWaitError" in caplog.text
    assert SECRET_TITLE not in caplog.text and SECRET_TEXT not in caplog.text

    # короткий FloodWait — не раньше общего повтора
    gw.faults[_marked(a)] = errors.FloodWaitError(request=None, capture=3)
    assert await conn._views_pass() == connector_mod._VIEWS_RETRY_S  # noqa: SLF001


async def test_pass_network_error_postpones_other_error_skips_channel(caplog):
    gw = _Gw()
    a, b = _channel(1001), _channel(1002)
    _seed(gw, a, [_post(1, a, age_ms=HOUR_MS, views=5)])
    _seed(gw, b, [_post(1, b, age_ms=HOUR_MS, views=6)])
    conn, emitted, _ = _connector([_head(a), _head(b)], gw)

    gw.faults[_marked(a)] = ConnectionError("down")
    assert await conn._views_pass() == connector_mod._VIEWS_RETRY_S  # noqa: SLF001
    assert emitted == []

    gw.faults[_marked(a)] = errors.ChannelPrivateError(request=None)
    with caplog.at_level(logging.WARNING):
        assert await conn._views_pass() == connector_mod._VIEWS_INTERVAL_S  # noqa: SLF001
    assert [e["channel"]["ext_id"] for e in emitted] == [str(_marked(b))]
    assert "ChannelPrivateError" in caplog.text and SECRET_TITLE not in caplog.text


async def test_loop_first_pass_after_delay_and_survives_core_failure():
    gw = _Gw()
    conn, _emitted, sleeps = _connector(RuntimeError("core down"), gw)
    calls = {"n": 0}

    async def _sleep(s):
        sleeps.append(s)
        calls["n"] += 1
        if calls["n"] >= 3:
            raise asyncio.CancelledError

    conn._sleep = _sleep  # noqa: SLF001
    with pytest.raises(asyncio.CancelledError):
        await conn._views_loop()  # noqa: SLF001
    assert sleeps == [
        connector_mod._VIEWS_FIRST_DELAY_S,  # noqa: SLF001
        connector_mod._VIEWS_RETRY_S,  # noqa: SLF001
        connector_mod._VIEWS_RETRY_S,  # noqa: SLF001
    ]
    assert conn._client.heads_calls == 2  # noqa: SLF001


async def test_views_task_only_when_enabled():
    gw = FakeGateway()

    async def _never(_s):
        await asyncio.Event().wait()

    off = TelegramConnector(_HeadsClient([]), gw, account_id=1, sleep=_never)
    on = TelegramConnector(_HeadsClient([]), gw, account_id=1, sleep=_never, views_refresh=True)
    for c in (off, on):
        c._check_session_alert = _noop  # noqa: SLF001
        await c.start_source()
    try:
        assert off._views_task is None  # noqa: SLF001
        assert on._views_task is not None and not on._views_task.done()  # noqa: SLF001
    finally:
        for c in (off, on):
            await c.stop_source()
    assert on._views_task is None  # noqa: SLF001


async def _noop():
    return None


# --- сквозь настоящее ядро --------------------------------------------------------------------------------


async def test_pass_through_real_core(admin_client, internal_transport, db):
    account_id, secret = await create_tg_account(admin_client)
    client = make_core_client(internal_transport, secret)
    gw = _Gw()
    ch = _channel(1001)
    marked = _marked(ch)
    post = _post(2, ch, age_ms=HOUR_MS, views=100, forwards=1)
    _seed(gw, ch, [post])

    conn = TelegramConnector(client, gw, account_id=account_id, wall=lambda: NOW_MS / 1000, sleep=_noop_sleep)
    conn._me_id = h.ME_ID  # noqa: SLF001
    gw.connector = conn
    # пост пришёл обычным приёмом — со счётчиком
    ctx = convert.ConvertContext(
        channel=convert.dialog_channel_ref(h.peer_of(ch), ch, me_id=h.ME_ID),
        channel_kind_for_sender="channel",
        me_id=h.ME_ID,
        entities={},
    )
    r = await client.push_events([{"type": "upsert", **convert.convert_message(post, ctx)}])
    assert r["applied"] == 1 and r["dead"] == 0

    post.views = 250
    emitted: list[dict] = []
    conn.emit = emitted.append
    await conn._views_pass()  # noqa: SLF001
    assert len(emitted) == 1
    r = await client.push_events(emitted)
    assert (r["applied"], r["dead"]) == (1, 0)
    with db.reader() as c:
        views_json = c.execute(
            "SELECT m.views_json FROM main.message m JOIN main.channel ch ON ch.id = m.channel_id "
            "WHERE ch.ext_id = ? AND m.ext_id = '2'",
            (str(marked),),
        ).fetchone()[0]
    assert views_json == f'{{"views":250,"forwards":1,"ts":{NOW_MS}}}'


async def _noop_sleep(_s):
    return None
