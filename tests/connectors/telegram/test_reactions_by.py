"""Ф23-2: `reactions_list` Telegram — имена поставивших (`by`, `by_more`) из `recent_reactions` и кэша
сущностей, без запросов в Telegram."""

from telethon.tl import types

from hub.connectors.telegram import convert
from hub.connectors.telegram.connector import TelegramConnector

from . import helpers as h


def _recent(user_id: int, emoji: str = "👍", *, my: bool = False) -> types.MessagePeerReaction:
    from datetime import UTC, datetime

    return types.MessagePeerReaction(
        reaction=types.ReactionEmoji(emoji), peer_id=types.PeerUser(user_id), date=datetime.now(UTC), my=my
    )


def _reactions(
    count: int, recent: list | None, *, chosen: bool = False, emoji: str = "👍", min_: bool = False
):
    return types.MessageReactions(
        results=[
            types.ReactionCount(
                reaction=types.ReactionEmoji(emoji), count=count, chosen_order=0 if chosen else None
            )
        ],
        recent_reactions=recent,
        min=min_,
    )


def test_f23_group_names_from_entities_in_order_without_me():
    ents = {7: h.make_user(7, first_name="Пётр"), 8: h.make_user(8, first_name="Анна", last_name="К")}
    out = convert.reactions_list(
        _reactions(3, [_recent(8), _recent(h.ME_ID, my=True), _recent(7)], chosen=True),
        entities=ents,
        me_id=h.ME_ID,
    )
    assert out == [{"emoji": "👍", "count": 3, "me": True, "by": ["Анна К", "Пётр"]}]


def test_f23_group_unknown_entity_and_missing_recent_go_to_by_more():
    ents = {7: h.make_user(7, first_name="Пётр")}
    out = convert.reactions_list(_reactions(4, [_recent(7), _recent(99)]), entities=ents, me_id=h.ME_ID)
    assert out[0]["by"] == ["Пётр"] and out[0]["by_more"] == 3  # 99 без сущности + 2 без recent


def test_f23_recent_of_other_emoji_is_not_counted():
    ents = {7: h.make_user(7, first_name="Пётр")}
    out = convert.reactions_list(_reactions(1, [_recent(7, "🔥")]), entities=ents, me_id=h.ME_ID)
    assert out == [{"emoji": "👍", "count": 1, "me": False}]


def test_f23_dm_without_recent_is_the_partner():
    out = convert.reactions_list(_reactions(1, None), dm_name="Пётр", me_id=h.ME_ID)
    assert out == [{"emoji": "👍", "count": 1, "me": False, "by": ["Пётр"]}]
    # только моя реакция в личке — чужих нет
    mine = convert.reactions_list(_reactions(1, None, chosen=True), dm_name="Пётр", me_id=h.ME_ID)
    assert mine == [{"emoji": "👍", "count": 1, "me": True}]


def test_f23_no_recent_in_group_has_no_fields_and_min_is_none():
    assert convert.reactions_list(_reactions(2, None), entities={}, me_id=h.ME_ID) == [
        {"emoji": "👍", "count": 2, "me": False}
    ]
    assert convert.reactions_list(_reactions(2, [_recent(7)], min_=True), entities={}) is None


def test_f23_connector_helper_uses_cache_and_dm_partner():
    c = object.__new__(TelegramConnector)
    peer = h.make_user(7, first_name="Пётр")
    group = types.Chat(id=100, title="Группа", photo=None, participants_count=3, date=None, version=1)
    c._entities = {7: peer, -100: group}
    c._me_id = h.ME_ID
    dm = c._reactions_named(_reactions(1, None), 7)
    assert dm[0]["by"] == ["Пётр"]
    grp = c._reactions_named(_reactions(1, [_recent(7)]), -100)
    assert grp[0]["by"] == ["Пётр"]
    grp2 = c._reactions_named(_reactions(1, None), -100)
    assert "by" not in grp2[0]
