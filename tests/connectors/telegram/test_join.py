"""Ф17-1 (docs/architecture.md: вступление в группы и каналы —
`TelegramConnector.push_join`/`after_join` и стартовая загрузка беседы после вступления, с `FakeGateway`
(без сети)."""

from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from telethon import errors, utils
from telethon.tl import types

from hub.connectors.base import AuthRequired
from hub.connectors.join import JoinResult
from hub.connectors.telegram.connector import TelegramConnector

from . import helpers as h
from .helpers import FakeGateway

_FAKE_NOW_S = 1_700_000_000.0
_DAY_MS = 24 * 60 * 60 * 1000


def _channel(id_: int, *, left=True, broadcast=True, forum=False, join_request=False, title="Канал"):
    ch = h.make_channel(id_, title=title, broadcast=broadcast, megagroup=not broadcast, forum=forum)
    ch.left = left
    ch.join_request = join_request
    return ch


def _marked(entity) -> str:
    return str(utils.get_peer_id(entity))


class _Client:
    """Ядро для сверки: известные беседы (`heads`) и пометки стартовой загрузки."""

    def __init__(self, heads=None):
        self.heads = heads or []
        self.activity_calls = []

    async def get_cursor(self):  # Ф42-1: курсор аккаунта (дыры догона) — пусто
        return None

    async def push_events(self, events, cursor=None):
        return {"ok": True}

    async def channels_heads(self):
        return list(self.heads)

    async def channels_window(self, *, ext_id, limit=30):
        return []

    async def set_activity(self, *, backfill):
        self.activity_calls.append(backfill)
        return {"ok": True}

    async def set_status(self, status, *, last_error=None):
        return {"ok": True}


def _setup(heads=None):
    gw = FakeGateway()
    client = _Client(heads)
    conn = TelegramConnector(client, gw, account_id=1, wall=lambda: _FAKE_NOW_S)
    conn._me_id = gw.me.id  # noqa: SLF001
    conn._sleep = _no_sleep  # noqa: SLF001 — джиттер стартовой загрузки
    return gw, conn, client


async def _no_sleep(_seconds):
    return None


def _item(step, target, *, kind="username", ext_id=None):
    return {
        "id": 5,
        "lease_gen": 1,
        "step": step,
        "target_kind": kind,
        "target": target,
        "ext_id": ext_id,
        "project_id": 3,
    }


# --- шаг peek -----------------------------------------------------------------------------------------------


async def test_peek_username_channel_with_full_info():
    gw, conn, _ = _setup()
    ch = _channel(500, join_request=True)
    gw.add_username("news_chan", ch)
    gw.full_channels[utils.get_peer_id(ch)] = SimpleNamespace(
        full_chat=SimpleNamespace(participants_count=12_400, about="Новости рынка")
    )
    res = await conn.push_join(_item("peek", "news_chan"))
    assert res.outcome == "done"
    assert res.peek == {
        "title": "Канал",
        "kind": "broadcast",
        "participants": 12_400,
        "about": "Новости рынка",
        "request_needed": True,
        "member": False,
        "forum": False,
        "ext_id": _marked(ch),
    }
    assert gw.resolve_calls == ["news_chan"] and ("full", utils.get_peer_id(ch)) in gw.join_calls


async def test_peek_full_info_failure_is_without_it():
    gw, conn, _ = _setup()
    ch = _channel(501, broadcast=False, forum=True, left=False)
    gw.add_username("forum_grp", ch)
    gw.full_faults.append(errors.FloodWaitError(request=None, capture=30))
    res = await conn.push_join(_item("peek", "forum_grp"))
    assert res.outcome == "done"
    assert res.peek["kind"] == "megagroup" and res.peek["forum"] is True and res.peek["member"] is True
    assert res.peek["participants"] is None and res.peek["about"] is None


@pytest.mark.parametrize("bot", [False, True])
async def test_peek_user_or_bot_is_not_group(bot):
    gw, conn, _ = _setup()
    gw.add_username("some_person", h.make_user(42, first_name="Петя", bot=bot))
    assert await conn.push_join(_item("peek", "some_person")) == JoinResult("drop", reason="not_group")


async def test_peek_invite_chat_invite_and_paid():
    gw, conn, _ = _setup()
    gw.invites["AbCdEfGh12"] = types.ChatInvite(
        title="Закрытая группа",
        photo=types.PhotoEmpty(id=0),
        participants_count=85,
        color=0,
        megagroup=True,
        request_needed=True,
        about="О группе",
    )
    res = await conn.push_join(_item("peek", "AbCdEfGh12", kind="invite"))
    assert res.peek == {
        "title": "Закрытая группа",
        "kind": "megagroup",
        "participants": 85,
        "about": "О группе",
        "request_needed": True,
        "member": False,
        "forum": False,
        "ext_id": None,
    }
    gw.invites["PaidPaid12"] = types.ChatInvite(
        title="Платный",
        photo=types.PhotoEmpty(id=0),
        participants_count=5,
        color=0,
        broadcast=True,
        subscription_pricing=types.StarsSubscriptionPricing(period=2_592_000, amount=100),
    )
    res = await conn.push_join(_item("peek", "PaidPaid12", kind="invite"))
    assert res == JoinResult("drop", reason="paid")


async def test_peek_invite_already_and_peek():
    gw, conn, _ = _setup()
    already = _channel(600, left=False, title="Уже там")
    gw.invites["Already123"] = types.ChatInviteAlready(chat=already)
    gw.full_channels[utils.get_peer_id(already)] = SimpleNamespace(full_chat=SimpleNamespace(about=None))
    res = await conn.push_join(_item("peek", "Already123", kind="invite"))
    assert res.outcome == "done" and res.peek["member"] is True and res.peek["ext_id"] == _marked(already)
    peek_chat = _channel(601, left=True, title="Предпросмотр")
    gw.invites["PeekPeek12"] = types.ChatInvitePeek(chat=peek_chat, expires=h.ts(0))
    res = await conn.push_join(_item("peek", "PeekPeek12", kind="invite"))
    assert res.peek["member"] is False and res.peek["ext_id"] == _marked(peek_chat)


# --- шаг join -----------------------------------------------------------------------------------------------


async def test_join_username_uses_cached_entity():
    gw, conn, _ = _setup()
    ch = _channel(700, title="Новости")
    gw.add_username("news_chan", ch)
    gw.full_channels[utils.get_peer_id(ch)] = SimpleNamespace(full_chat=SimpleNamespace())
    await conn.push_join(_item("peek", "news_chan"))
    res = await conn.push_join(_item("join", "news_chan", ext_id=_marked(ch)))
    assert res == JoinResult("done", result="joined", ext_id=_marked(ch), title="Новости")
    assert gw.resolve_calls == ["news_chan"]  # только на peek
    assert ("join_channel", utils.get_peer_id(ch)) in gw.join_calls


async def test_join_username_cache_miss_resolves_again():
    gw, conn, _ = _setup()
    ch = _channel(701)
    gw.add_username("news_chan", ch)
    res = await conn.push_join(_item("join", "news_chan", ext_id=_marked(ch)))
    assert res.result == "joined" and res.ext_id == _marked(ch)
    assert gw.resolve_calls == ["news_chan"]


async def test_join_invite_chat_from_updates():
    gw, conn, _ = _setup()
    chat = _channel(702, broadcast=False, title="Группа")
    gw.invite_chats["AbCdEfGh12"] = chat
    res = await conn.push_join(_item("join", "AbCdEfGh12", kind="invite"))
    assert res == JoinResult("done", result="joined", ext_id=_marked(chat), title="Группа")


async def test_join_already_participant_and_request_sent():
    gw, conn, _ = _setup()
    gw.join_faults.append(errors.UserAlreadyParticipantError(request=None))
    res = await conn.push_join(_item("join", "AbCdEfGh12", kind="invite", ext_id="-100702"))
    assert res == JoinResult("done", result="already", ext_id="-100702")
    gw.join_faults.append(errors.InviteRequestSentError(request=None))
    res = await conn.push_join(_item("join", "AbCdEfGh12", kind="invite"))
    assert res == JoinResult("done", result="requested", ext_id=None)


async def test_join_invite_already_on_retry_takes_chat_from_check_invite():
    """Ревью Ф17-1 (находка 3): повтор шага по приглашению после вступления, отчёт о котором не дошёл, —
    `UserAlreadyParticipant`, ext_id у заявки нет: чат берётся из `CheckChatInvite` (`ChatInviteAlready`)."""
    gw, conn, _ = _setup()
    ch = _channel(950)
    gw.invites["AbCdEfGh12"] = types.ChatInviteAlready(chat=ch)
    gw.join_faults.append(errors.UserAlreadyParticipantError(request=None))
    res = await conn.push_join(_item("join", "AbCdEfGh12", kind="invite"))
    assert res == JoinResult("done", result="already", ext_id=str(utils.get_peer_id(ch)))
    # Приглашение не отдало чат (не участник или сбой) — `already` без ext_id, как раньше.
    gw.join_faults.append(errors.UserAlreadyParticipantError(request=None))
    res = await conn.push_join(_item("join", "Unknown123", kind="invite"))
    assert res == JoinResult("done", result="already", ext_id=None)


@pytest.mark.parametrize(
    "exc, expected",
    [
        (errors.FloodWaitError(request=None, capture=42), ("retry", 42.0, "FloodWait")),
        (ConnectionError("down"), ("retry", None, "ConnectionError")),
        (errors.ServerError(request=None, message="INTERNAL", code=500), ("retry", None, "ServerError")),
        (errors.UsernameNotOccupiedError(request=None), ("drop", None, "not_found")),
        (errors.UsernameInvalidError(request=None), ("drop", None, "not_found")),
        (errors.InviteHashExpiredError(request=None), ("drop", None, "invite_expired")),
        (errors.InviteHashInvalidError(request=None), ("drop", None, "invite_invalid")),
        (errors.InviteHashEmptyError(request=None), ("drop", None, "invite_invalid")),
        (errors.ChannelPrivateError(request=None), ("drop", None, "private")),
        (errors.ChannelInvalidError(request=None), ("drop", None, "private")),
        (errors.ChannelsTooMuchError(request=None), ("drop", None, "too_many_channels")),
        (errors.UserChannelsTooMuchError(request=None), ("drop", None, "too_many_channels")),
        (errors.UsersTooMuchError(request=None), ("drop", None, "group_full")),
        # Прочие: известный Telethon класс — его имя; незнакомый код — сам код (так Telethon строит ответ).
        (errors.ChatAdminRequiredError(request=None), ("drop", None, "ChatAdminRequiredError")),
        (
            errors.rpc_message_to_error(types.RpcError(400, "SOME_NEW_CODE"), request=None),
            ("drop", None, "SOME_NEW_CODE"),
        ),
    ],
)
async def test_error_classes(exc, expected):
    gw, conn, _ = _setup()
    gw.join_faults.append(exc)
    res = await conn.push_join(_item("join", "AbCdEfGh12", kind="invite"))
    assert (res.outcome, res.retry_after_s, res.reason) == expected


async def test_error_classes_on_resolve_and_auth_lost():
    gw, conn, _ = _setup()
    gw.resolve_faults.append(errors.UsernameNotOccupiedError(request=None))
    assert (await conn.push_join(_item("peek", "news_chan"))).reason == "not_found"
    gw.resolve_faults.append(errors.AuthKeyUnregisteredError(request=None))
    with pytest.raises(AuthRequired):
        await conn.push_join(_item("peek", "news_chan"))
    gw.join_faults.append(errors.AuthKeyUnregisteredError(request=None))
    with pytest.raises(AuthRequired):
        await conn.push_join(_item("join", "AbCdEfGh12", kind="invite"))


# --- после вступления: стартовая загрузка одной беседы (П4) -------------------------------------------------


async def _joined(gw, conn, ch, *, old=True):
    """Вступили в канал: история в «сервере» — 50 сообщений, последнее — 40 дней назад (вне окна 30 дней)."""
    date_ms = int(_FAKE_NOW_S * 1000) - (40 * _DAY_MS if old else 0)
    for i in range(1, 51):
        msg = h.make_message(i, h.peer_of(ch), message=f"m{i}", date_ms=date_ms)
        gw.add_message(utils.get_peer_id(ch), msg)
    gw.invite_chats["AbCdEfGh12"] = ch
    item = _item("join", "AbCdEfGh12", kind="invite")
    res = await conn.push_join(item)
    await conn.after_join(item, res)
    return res


async def test_after_join_loads_one_chat_outside_30_days():
    gw, conn, client = _setup()
    ch = _channel(800)
    emitted = []
    conn.emit = emitted.append
    await _joined(gw, conn, ch)
    reason = conn._sync_queue.get_nowait()  # noqa: SLF001
    assert reason == "join"
    await conn.sync(reason)
    upserts = [e for e in emitted if e.get("type") == "upsert" and e["channel"]["ext_id"] == _marked(ch)]
    assert len(upserts) == 30
    assert gw.iter_dialogs_calls == 0  # «join» — узкое действие, без полной сверки
    assert client.activity_calls == [True, False]
    # Повтор сверки — второй загрузки нет (очередь пуста).
    await conn.sync("join")
    assert len([c for c in gw.get_messages_calls if c[0] == utils.get_peer_id(ch)]) == 1


async def test_after_join_known_chat_not_loaded_and_drop_ignored():
    ch = _channel(801)
    gw, conn, _ = _setup(heads=[{"ext_id": _marked(ch), "kind": "channel", "max_ext_sort": 5}])
    await _joined(gw, conn, ch)
    await conn.sync("join")
    assert gw.get_messages_calls == []
    await conn.after_join(_item("join", "x"), JoinResult("drop", reason="private"))
    await conn.after_join(_item("join", "x"), JoinResult("done", result="requested", ext_id=_marked(ch)))
    assert conn._join_loads == {}  # noqa: SLF001


async def test_after_join_full_sync_does_not_load_twice():
    gw, conn, _ = _setup()
    ch = _channel(802)
    gw.add_dialog(h.make_dialog(ch, top_message=50, last_message_date_ms=int(_FAKE_NOW_S * 1000)))
    emitted = []
    conn.emit = emitted.append
    await _joined(gw, conn, ch, old=False)
    await conn.sync("scheduled")  # причины слились — `join` не главная
    assert len([c for c in gw.get_messages_calls if c[0] == utils.get_peer_id(ch)]) == 1
    assert gw.iter_dialogs_calls >= 1  # полная сверка прошла, но беседу второй раз не грузила


async def test_after_join_already_from_peek_queues_load():
    gw, conn, _ = _setup()
    ch = _channel(803, left=False)
    gw.add_username("news_chan", ch)
    item = _item("peek", "news_chan")
    res = await conn.push_join(item)
    assert res.peek["member"] is True
    await conn.after_join(item, res)
    assert list(conn._join_loads) == [_marked(ch)]  # noqa: SLF001


# --- ревью Ф17-1 (`.runs/critic-f17/`, находки 4 и 5) ---


async def test_floodwait_during_join_load_keeps_the_load():
    gw, conn, _ = _setup()
    ch = _channel(900)
    emitted = []
    conn.emit = emitted.append
    await _joined(gw, conn, ch)
    assert conn._sync_queue.get_nowait() == "join"  # noqa: SLF001

    real = gw.get_messages
    calls = {"n": 0}

    async def flaky(peer, *, min_id, limit):
        calls["n"] += 1
        if calls["n"] == 1:
            raise errors.FloodWaitError(request=None, capture=5)
        return await real(peer, min_id=min_id, limit=limit)

    gw.get_messages = flaky
    await conn.sync("join")  # FloodWait — сверка «пауза 5 с», не падение
    await conn.sync("join")  # следующая сверка — загрузка должна повториться
    await conn.sync("scheduled")
    upserts = [
        e
        for e in emitted
        if e.get("type") == "upsert" and e["channel"]["ext_id"] == str(utils.get_peer_id(ch))
    ]
    assert upserts, (
        "история беседы после вступления так и не загружена: очередь _join_loads очищена до загрузки"
    )


def test_join_can_win_over_clock():
    code = "from hub.connectors.base import _pick_main_sync_reason as p; print(p({'clock', 'join'}))"
    picks = set()
    for seed in range(12):
        env = {**os.environ, "PYTHONHASHSEED": str(seed)}
        out = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
        )
        picks.add(out.stdout.strip())
    assert picks == {"clock"}, picks


def test_joined_chat_prefers_chat_without_left():
    """Ревью Ф17-1 (не проверено вживую): приглашение в канал со связанной группой — в `Updates.chats` оба
    чата; берётся тот, где владелец теперь участник."""
    linked = _channel(961, left=True, broadcast=False)
    joined = _channel(962, left=False)
    updates = types.Updates(updates=[], users=[], chats=[linked, joined], date=h.ts(0), seq=0)
    assert TelegramConnector._joined_chat(updates, None) is joined
