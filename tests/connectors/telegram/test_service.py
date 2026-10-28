"""Тест 6 куска 1b: служебные сообщения (`MessageService`) — вход/выход, закрепление, смена названия,
миграция, создание темы. Чистые функции `convert.py`, без сети."""

from telethon.tl import types

from hub.connectors.telegram import convert

from . import helpers as h


def _service(action, **kwargs):
    other = h.make_user(7, first_name="Пётр")
    return h.make_message(1, h.peer_of(other), action=action, from_id=None, **kwargs)


def test_1b_06_join_leave_pin_texts():
    join = _service(types.MessageActionChatAddUser(users=[7]))
    assert convert.service_text(join.action) == "вход"

    leave = _service(types.MessageActionChatDeleteUser(user_id=7))
    assert convert.service_text(leave.action) == "выход"

    pin = _service(types.MessageActionPinMessage())
    assert convert.service_text(pin.action) == "закрепление"


def test_1b_06_unknown_action_falls_back_to_generic_text():
    action = types.MessageActionHistoryClear()
    assert convert.service_text(action) == "служебное событие"


def test_1b_06_service_message_converts_with_event_kind_and_no_media():
    other = h.make_user(7, first_name="Пётр")
    channel = convert.dialog_channel_ref(h.peer_of(other), other, me_id=h.ME_ID)
    msg = _service(types.MessageActionPinMessage())
    ctx = convert.ConvertContext(
        channel=channel, channel_kind_for_sender="dm", me_id=h.ME_ID, entities={7: other}
    )

    ev = convert.convert_message(msg, ctx)
    assert ev["kind"] == "event"
    assert ev["text"] == "закрепление"
    assert ev["attachments"] is None
    assert ev["entities"] is None


def test_1b_06_title_change_renames_channel_via_normal_upsert(db):
    """`MessageActionChatEditTitle` -> событие `channel` с новым заголовком (спека): раз коннектор кладёт
    новый `title` прямо в `ChannelRef` того же upsert, ядро переименовывает канал тем же путём, что и любую
    смену title (раздел «Идентификаторы»)."""
    from hub.core.ingest import apply_batch

    account_id = db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.account (source, db) VALUES ('telegram', 'main')").lastrowid
        )
    )
    first = {
        "type": "upsert",
        "channel": {"ext_id": "c1", "kind": "group", "title": "Старое имя"},
        "ext_id": "1",
        "ts": 1000,
        "is_me": False,
        "kind": "text",
        "text": "hi",
        "mentions_me": False,
    }
    db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=[first], cursor=None, now_ms=1000)
    )

    rename = {
        "type": "upsert",
        "channel": {"ext_id": "c1", "kind": "group", "title": "Новое имя"},
        "ext_id": "2",
        "ts": 2000,
        "is_me": False,
        "kind": "event",
        "text": "смена названия",
        "mentions_me": False,
    }
    db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=[rename], cursor=None, now_ms=2000)
    )

    with db.reader() as conn:
        title, history = conn.execute(
            "SELECT title, title_history_json FROM main.channel WHERE ext_id='c1'"
        ).fetchone()
    assert title == "Новое имя"
    assert "Старое имя" in history


def test_1b_06_migrate_action_is_recognized_and_builds_channel_migrate_payload():
    new_channel_entity = h.make_channel(555, title="Супергруппа", megagroup=True)
    action = types.MessageActionChannelMigrateFrom(title="Группа", chat_id=123)
    msg = h.make_message(1, types.PeerChannel(555), action=action)

    assert convert.is_migrate_action(action) is True
    new_ref = convert.dialog_channel_ref(h.peer_of(new_channel_entity), new_channel_entity, me_id=h.ME_ID)
    payload = convert.build_channel_migrate_event(msg, new_channel=new_ref)
    assert payload["old_ext_id"] == "-123"  # помеченный id обычной группы
    assert payload["channel"] == new_ref


def test_1b_06_topic_create_service_text():
    action = types.MessageActionTopicCreate(title="Новая тема", icon_color=0)
    assert convert.service_text(action) == "создание темы"
