"""Ф20-1 A1, П5 (docs/architecture.md): признак бота у отправителя Telegram —
сущность `User` с `bot=True` (сущность бывает `None`/`Channel`) или служебный 777000. Ключ `is_bot` — только
когда истинен."""

from hub.connectors.telegram import convert

from . import helpers as h


def _ctx(channel: dict, *, channel_kind: str = "dm", entities: dict | None = None) -> convert.ConvertContext:
    return convert.ConvertContext(
        channel=channel, channel_kind_for_sender=channel_kind, me_id=h.ME_ID, entities=entities or {}
    )


def test_bot_user_in_dm_is_bot():
    bot = h.make_user(42, first_name="Бот ревью", bot=True)
    channel = convert.dialog_channel_ref(h.peer_of(bot), bot, me_id=h.ME_ID)
    msg = h.make_message(1, h.peer_of(bot), message="новый MR", from_id=None)
    ev = convert.convert_message(msg, _ctx(channel, entities={42: bot}))
    assert ev["sender"]["is_bot"] is True


def test_service_777000_is_bot_even_without_entity():
    service = h.make_user(777000, first_name="Telegram")
    channel = convert.dialog_channel_ref(h.peer_of(service), service, me_id=h.ME_ID)
    msg = h.make_message(2, h.peer_of(service), message="код входа", from_id=None)
    ev = convert.convert_message(msg, _ctx(channel))  # сущности отправителя нет
    assert ev["sender"]["ext_id"] == "777000" and ev["sender"]["is_bot"] is True


def test_plain_user_and_channel_sender_not_bot():
    other = h.make_user(7, first_name="Пётр")
    channel = convert.dialog_channel_ref(h.peer_of(other), other, me_id=h.ME_ID)
    msg = h.make_message(3, h.peer_of(other), message="привет", from_id=None)
    ev = convert.convert_message(msg, _ctx(channel, entities={7: other}))
    assert ev["sender"] == {"ext_id": "7", "name": "Пётр", "handle": None, "is_me": False}

    # Отправитель — канал (пост от имени канала в группе): сущность `Channel`, у неё нет `bot`.
    group = h.make_channel(100, title="Группа", megagroup=True)
    chan = h.make_channel(200, title="Канал", broadcast=True)
    gref = convert.dialog_channel_ref(h.peer_of(group), group, me_id=h.ME_ID)
    msg = h.make_message(4, h.peer_of(group), message="пост", from_id=h.peer_of(chan))
    ev = convert.convert_message(
        msg, _ctx(gref, channel_kind="group", entities={convert.utils.get_peer_id(h.peer_of(chan)): chan})
    )
    assert "is_bot" not in ev["sender"]
