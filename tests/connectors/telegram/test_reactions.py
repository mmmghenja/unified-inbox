"""Ф10-3 (C1 + П1, П4, П5, тест E6): реакции из хаба в Telegram — `TelegramConnector.push_reaction` с
`FakeGateway` (без сети)."""

from __future__ import annotations

import pytest
from telethon import errors, utils
from telethon.tl import types

from hub.connectors.base import AuthRequired
from hub.connectors.telegram.connector import TelegramConnector

from ..media_fakes import FakeMediaCore
from . import helpers as h
from .helpers import FakeGateway

USER = h.make_user(42, first_name="Петя")
FORUM = h.make_channel(600, title="Форум", megagroup=True, forum=True)
OTHER = h.make_user(43, first_name="Вася")


def marked(entity) -> int:
    return utils.get_peer_id(h.peer_of(entity))


def setup():
    gw = FakeGateway()
    for entity in (USER, FORUM):
        gw.seed_peer(marked(entity), h.input_peer_of(entity))
    conn = TelegramConnector(FakeMediaCore().client(), gw, account_id=7)
    events: list[dict] = []
    conn.emit = events.append
    return gw, conn, events


def job(ops, *, channel_ext_id=None, kind="dm", msg="15", current_mine=None) -> dict:
    return {
        "message_id": 101,
        "channel_id": 12,
        "gen": 1,
        "lease_gen": 1,
        "channel": {"ext_id": channel_ext_id or str(marked(USER)), "kind": kind, "parent_ext_id": None},
        "message_ext_id": msg,
        "ops": ops,
        "current_mine": current_mine if current_mine is not None else [],
    }


def reactions_update(entity, msg_id, results, *, is_min=False):
    return types.UpdateMessageReactions(
        peer=h.peer_of(entity),
        msg_id=msg_id,
        reactions=types.MessageReactions(
            results=[
                types.ReactionCount(
                    reaction=types.ReactionEmoji(emoticon=e), count=c, chosen_order=0 if me else None
                )
                for e, c, me in results
            ],
            min=is_min,
        ),
    )


def updates(*items):
    return types.Updates(updates=list(items), users=[], chats=[], date=None, seq=0)


def read_result(entity, msg_id, results, *, is_min=False):
    """Ответ `GetMessagesReactionsRequest` — `Updates` с `UpdateMessageReactions`."""
    return updates(reactions_update(entity, msg_id, results, is_min=is_min))


# --- E6 + ревью: чтение текущего, проигрыш операций, отправка ---


async def test_e6_on_reads_current_sends_single_emoji_and_emits_truth_from_response():
    gw, conn, events = setup()
    gw.get_reactions_results.append(read_result(USER, 15, [("🔥", 2, False)]))
    gw.send_reaction_result = updates(reactions_update(USER, 15, [("👍", 1, True), ("🔥", 2, False)]))
    assert conn.reactions_enabled is True
    res = await conn.push_reaction(job([["👍", True]]))
    assert res.outcome == "done"
    assert gw.reaction_calls == [("get", marked(USER), 15), ("send", marked(USER), 15, ["👍"])]
    assert events == [
        {
            "type": "reaction",
            "channel": {"ext_id": str(marked(USER)), "kind": "dm"},
            "ext_id": "15",
            "reactions": [
                {"emoji": "👍", "count": 1, "me": True},
                {"emoji": "🔥", "count": 2, "me": False},
            ],
        }
    ]


async def test_review_chain_on_on_off_from_empty_ends_empty_no_send():
    """«👍 on, 🔥 on, 🔥 off» от пустого: {👍} -> {🔥} -> {} == текущему — SendReaction не нужен."""
    gw, conn, events = setup()
    gw.get_reactions_results.append(read_result(USER, 15, [("😢", 1, False)]))
    res = await conn.push_reaction(job([["👍", True], ["🔥", True], ["🔥", False]]))
    assert res == ("done", None, "nothing")
    assert [c[0] for c in gw.reaction_calls] == ["get"]
    assert events[0]["reactions"] == [{"emoji": "😢", "count": 1, "me": False}]  # событие из прочитанного


async def test_review_off_uses_source_truth_over_stale_current_mine():
    """«👍 off»: в источнике моя 👍, а `current_mine=[]` устарел — снять (`SendReaction([])`)."""
    gw, conn, events = setup()
    gw.get_reactions_results.append(read_result(USER, 15, [("👍", 1, True)]))
    gw.send_reaction_result = updates(reactions_update(USER, 15, []))
    res = await conn.push_reaction(job([["👍", False]], current_mine=[]))
    assert res.outcome == "done"
    assert [c for c in gw.reaction_calls if c[0] == "send"] == [("send", marked(USER), 15, [])]
    assert events[-1]["reactions"] == []  # снятая последняя реакция — пустой список, не «нечего слать»


async def test_review_on_already_mine_in_source_no_send():
    gw, conn, _events = setup()
    gw.get_reactions_results.append(read_result(USER, 15, [("❤", 1, True)]))
    res = await conn.push_reaction(job([["❤️", True]], current_mine=[]))
    assert res.outcome == "done"
    assert [c[0] for c in gw.reaction_calls] == ["get"]


async def test_review_off_other_emoji_keeps_mine_no_send():
    gw, conn, _events = setup()
    gw.get_reactions_results.append(read_result(USER, 15, [("🔥", 1, True)]))
    res = await conn.push_reaction(job([["👍", False]]))
    assert res == ("done", None, "nothing")
    assert [c[0] for c in gw.reaction_calls] == ["get"]


async def test_review_off_snimaet_only_when_state_is_exactly_that_emoji():
    """Несколько своих (Premium, вне куска): `off 🔥` при {👍, 🔥} — состояние не {🔥}, без изменений."""
    gw, conn, _events = setup()
    gw.get_reactions_results.append(read_result(USER, 15, [("👍", 1, True), ("🔥", 1, True)]))
    res = await conn.push_reaction(job([["🔥", False]]))
    assert res == ("done", None, "nothing")
    assert [c[0] for c in gw.reaction_calls] == ["get"]


async def test_review_on_replaces_mine_and_vs16_stripped():
    gw, conn, _events = setup()
    gw.get_reactions_results.append(read_result(USER, 15, [("🔥", 1, True)]))
    await conn.push_reaction(job([["👍", True], ["❤️", True], ["😢", False]]))
    assert [c for c in gw.reaction_calls if c[0] == "send"] == [("send", marked(USER), 15, ["❤"])]


async def test_review_min_or_missing_read_falls_back_to_current_mine():
    gw, conn, events = setup()
    gw.get_reactions_results.append(read_result(USER, 15, [("👍", 1, True)], is_min=True))
    gw.send_reaction_result = updates(reactions_update(USER, 15, []))
    await conn.push_reaction(job([["👍", False]], current_mine=["👍"]))
    assert [c for c in gw.reaction_calls if c[0] == "send"] == [("send", marked(USER), 15, [])]

    gw2, conn2, events2 = setup()
    # прочиталось, но апдейт чужого сообщения — данных нет; `current_mine` пуст — снимать нечего
    gw2.get_reactions_results.append(read_result(USER, 16, [("👍", 1, True)]))
    res = await conn2.push_reaction(job([["👍", False]], current_mine=[]))
    assert res == ("done", None, "nothing")
    assert [c[0] for c in gw2.reaction_calls] == ["get"] and events2 == []


async def test_review_flood_wait_on_read_is_retry_without_send():
    gw, conn, events = setup()
    gw.get_reactions_faults.append(errors.FloodWaitError(request=None, capture=9))
    res = await conn.push_reaction(job([["👍", True]]))
    assert res.outcome == "retry" and res.retry_after_s == 9
    assert [c[0] for c in gw.reaction_calls] == ["get"] and events == []


async def test_e6_topic_uses_chat_peer():
    gw, conn, events = setup()
    chat = str(marked(FORUM))
    gw.get_reactions_results.append(read_result(FORUM, 321, []))
    gw.send_reaction_result = updates(reactions_update(FORUM, 321, [("👍", 1, True)]))
    res = await conn.push_reaction(job([["👍", True]], channel_ext_id=f"{chat}:7", kind="topic", msg="321"))
    assert res.outcome == "done"
    assert gw.reaction_calls == [("get", marked(FORUM), 321), ("send", marked(FORUM), 321, ["👍"])]
    assert events[0]["channel"] == {"ext_id": f"{chat}:7", "kind": "topic"}


# --- П4: правда после отправки ---


async def test_p4_no_update_in_response_reads_reactions_again():
    gw, conn, events = setup()
    gw.get_reactions_results.append(read_result(USER, 15, []))
    # в ответе — апдейт ЧУЖОГО сообщения и чужого чата: не наш
    gw.send_reaction_result = updates(
        reactions_update(USER, 16, [("😢", 1, True)]), reactions_update(OTHER, 15, [("😢", 1, True)])
    )
    gw.get_reactions_results.append(read_result(USER, 15, [("👍", 3, True)]))
    await conn.push_reaction(job([["👍", True]]))
    assert [c[0] for c in gw.reaction_calls] == ["get", "send", "get"]
    assert events[0]["reactions"] == [{"emoji": "👍", "count": 3, "me": True}]


async def test_p4_min_everywhere_no_event():
    gw, conn, events = setup()
    gw.get_reactions_results.append(read_result(USER, 15, []))
    gw.send_reaction_result = updates(reactions_update(USER, 15, [("👍", 1, True)], is_min=True))
    gw.get_reactions_results.append(
        types.UpdateShort(update=reactions_update(USER, 15, [("👍", 1, True)], is_min=True), date=None)
    )
    res = await conn.push_reaction(job([["👍", True]]))
    assert res.outcome == "done"
    assert [c[0] for c in gw.reaction_calls] == ["get", "send", "get"]
    assert events == []


async def test_p4_reread_failure_after_send_is_still_done_without_event():
    gw, conn, events = setup()
    real_get = gw.get_reactions
    calls = {"n": 0}

    async def get_reactions(peer, msg_id):
        calls["n"] += 1
        if calls["n"] == 2:  # перечитывание после отправки
            raise errors.FloodWaitError(request=None, capture=3)
        return await real_get(peer, msg_id)

    gw.get_reactions = get_reactions
    gw.get_reactions_results.append(read_result(USER, 15, []))
    res = await conn.push_reaction(job([["👍", True]]))
    assert res.outcome == "done"
    assert calls["n"] == 2 and gw.reaction_calls[-1][0] == "send"
    assert events == []


# --- П5: ошибки по образцу push_read ---


async def test_e6_flood_wait_is_retry_with_seconds():
    gw, conn, events = setup()
    gw.reaction_faults.append(errors.FloodWaitError(request=None, capture=7))
    res = await conn.push_reaction(job([["👍", True]]))
    assert res.outcome == "retry" and res.retry_after_s == 7
    assert events == []


@pytest.mark.parametrize(
    "exc",
    [
        errors.ReactionInvalidError(request=None),
        errors.ReactionsTooManyError(request=None),
        errors.MessageIdInvalidError(request=None),
        errors.MsgIdInvalidError(request=None),
        errors.ChatWriteForbiddenError(request=None),
        errors.UserBannedInChannelError(request=None),
    ],
)
async def test_e6_permanent_rpc_errors_drop_with_class(exc, caplog):
    gw, conn, events = setup()
    gw.reaction_faults.append(exc)
    with caplog.at_level("INFO"):
        res = await conn.push_reaction(job([["👍", True]]))
    assert res == ("drop", None, type(exc).__name__)
    assert events == []
    assert "👍" not in caplog.text and type(exc).__name__ in caplog.text


async def test_p5_auth_lost_raises_auth_required():
    gw, conn, _events = setup()
    gw.reaction_faults.append(errors.AuthKeyUnregisteredError(request=None))
    with pytest.raises(AuthRequired):
        await conn.push_reaction(job([["👍", True]]))


async def test_p5_network_is_retry():
    gw, conn, _events = setup()
    gw.reaction_faults.append(ConnectionError("down"))
    res = await conn.push_reaction(job([["👍", True]]))
    assert res.outcome == "retry" and res.reason == "ConnectionError"


async def test_p5_cache_miss_is_retry_without_send():
    gw, conn, _events = setup()
    gw.forget_peer(marked(USER))
    res = await conn.push_reaction(job([["👍", True]]))
    assert res.outcome == "retry" and res.reason == "peer_unresolved"
    assert gw.reaction_calls == [] and gw.iter_dialogs_calls == 0


@pytest.mark.parametrize("ch, msg", [("abc", "5"), ("42", "x"), ("42", "0"), ("42:x", "5")])
async def test_bad_ids_drop(ch, msg):
    gw, conn, _events = setup()
    res = await conn.push_reaction(job([["👍", True]], channel_ext_id=ch, msg=msg))
    assert res.outcome == "drop" and res.reason == "bad_ref"
    assert gw.reaction_calls == []
