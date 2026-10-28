"""Тесты 1-4 куска 1b (`docs/architecture.md`): `convert.py` — TL -> события ядра, ЧИСТЫЕ
функции, без сети. Объекты TL собираются руками (`telethon.tl.types`) через `helpers.py`.
"""

import json

from telethon.tl import types

from hub.connectors.telegram import convert

from . import helpers as h


def _ctx(
    channel: dict, *, channel_kind: str = "dm", entities: dict | None = None, me_id: int = h.ME_ID
) -> convert.ConvertContext:
    return convert.ConvertContext(
        channel=channel, channel_kind_for_sender=channel_kind, me_id=me_id, entities=entities or {}
    )


# --- Тест 1: kind/ext_id/own_id_space/sender/is_me/ts для разных типов диалогов --------------------------


def test_1b_01_dm_incoming_no_from_id_uses_peer_as_sender():
    other = h.make_user(7, first_name="Пётр")
    channel = convert.dialog_channel_ref(h.peer_of(other), other, me_id=h.ME_ID)
    assert channel == {"ext_id": "7", "kind": "dm", "title": "Пётр", "own_id_space": False, "attrs": None}

    msg = h.make_message(1, h.peer_of(other), message="привет", out=False, from_id=None)
    ev = convert.convert_message(msg, _ctx(channel, entities={7: other}))

    assert ev["sender"] == {"ext_id": "7", "name": "Пётр", "handle": None, "is_me": False}
    assert ev["is_me"] is False
    assert ev["ext_id"] == "1"
    assert ev["ts"] == int(msg.date.timestamp() * 1000)


def test_1b_01_dm_outgoing_no_from_id_is_me():
    other = h.make_user(7, first_name="Пётр")
    channel = convert.dialog_channel_ref(h.peer_of(other), other, me_id=h.ME_ID)
    msg = h.make_message(2, h.peer_of(other), message="ответ", out=True, from_id=None)
    ev = convert.convert_message(msg, _ctx(channel, entities={h.ME_ID: h.make_user(h.ME_ID, is_self=True)}))
    assert ev["is_me"] is True
    assert ev["sender"]["ext_id"] == str(h.ME_ID)
    assert ev["sender"]["is_me"] is True


def test_1b_01_group_kind_and_marked_ext_id():
    chat = h.make_chat(55, title="Группа")
    channel = convert.dialog_channel_ref(h.peer_of(chat), chat, me_id=h.ME_ID)
    assert channel["kind"] == "group"
    assert channel["ext_id"] == "-55"  # помеченный id обычного чата — отрицательный
    assert channel["own_id_space"] is False


def test_1b_01_megagroup_kind_group_own_id_space_true():
    mega = h.make_channel(100, title="Супергруппа", megagroup=True)
    channel = convert.dialog_channel_ref(h.peer_of(mega), mega, me_id=h.ME_ID)
    assert channel["kind"] == "group"
    assert channel["own_id_space"] is True
    assert channel["ext_id"] == str(-1000000000000 - 100)


def test_1b_01_broadcast_kind_channel():
    ch = h.make_channel(200, title="Канал", broadcast=True)
    channel = convert.dialog_channel_ref(h.peer_of(ch), ch, me_id=h.ME_ID)
    assert channel["kind"] == "channel"
    assert channel["own_id_space"] is True


def test_1b_01_favorites_dm_with_own_id_titled():
    me = h.make_user(h.ME_ID, first_name="владелец", is_self=True)
    channel = convert.dialog_channel_ref(h.peer_of(me), me, me_id=h.ME_ID)
    assert channel == {
        "ext_id": str(h.ME_ID),
        "kind": "dm",
        "title": "Избранное",
        "own_id_space": False,
        "attrs": None,
    }


# --- Тест 2: детерминизм ------------------------------------------------------------------------------


def test_1b_02_same_message_converts_to_byte_identical_dict():
    other = h.make_user(7, first_name="Пётр")
    channel = convert.dialog_channel_ref(h.peer_of(other), other, me_id=h.ME_ID)
    msg = h.make_message(1, h.peer_of(other), message="привет", from_id=None)
    ctx = _ctx(channel, entities={7: other})

    ev1 = convert.convert_message(msg, ctx)
    ev2 = convert.convert_message(msg, ctx)

    assert ev1 == ev2
    assert json.dumps(ev1, sort_keys=True) == json.dumps(ev2, sort_keys=True)


def test_1b_02_ingest_twice_is_noop_second_time(db):
    from hub.core.ingest import apply_batch

    account_id = db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.account (source, db) VALUES ('telegram', 'main')").lastrowid
        )
    )
    other = h.make_user(7, first_name="Пётр")
    channel = convert.dialog_channel_ref(h.peer_of(other), other, me_id=h.ME_ID)
    msg = h.make_message(1, h.peer_of(other), message="привет", from_id=None)
    ev = convert.convert_message(msg, _ctx(channel, entities={7: other}))
    event = {"type": "upsert", **ev}

    r1 = db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=[event], cursor=None, now_ms=1)
    )
    assert r1.applied == 1
    assert r1.dead == 0

    r2 = db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=[event], cursor=None, now_ms=2)
    )
    assert r2.applied == 0
    assert r2.noop == 1


# --- Тест 3: сущности ---------------------------------------------------------------------------------


def test_1b_03_bold_italic_code_pre_and_links_and_mentions():
    text = "AB CD EF GH t.me @user"
    entities = [
        types.MessageEntityBold(offset=0, length=2),
        types.MessageEntityItalic(offset=3, length=2),
        types.MessageEntityCode(offset=6, length=2),
        types.MessageEntityPre(offset=9, length=2, language=""),
        types.MessageEntityTextUrl(offset=12, length=4, url="https://example.com"),
        types.MessageEntityMention(offset=17, length=5),
    ]
    out = convert.convert_entities(entities, text)
    types_seen = [e["type"] for e in out]
    assert types_seen == ["bold", "italic", "code", "pre", "link", "mention"]
    link = next(e for e in out if e["type"] == "link")
    assert link["url"] == "https://example.com"


def test_1b_03_unknown_entity_types_are_dropped():
    text = "#hash /cmd"
    entities = [
        types.MessageEntityHashtag(offset=0, length=5),
        types.MessageEntityBotCommand(offset=6, length=4),
    ]
    assert convert.convert_entities(entities, text) == []


def test_1b_03_astral_emoji_before_url_slices_correctly_by_utf16():
    # 🚀 вне BMP — суррогатная пара (2 UTF-16 code unit, 1 кодовая точка Python). url начинается СРАЗУ после.
    text = "🚀 https://x.io"
    url_offset_utf16 = 3  # 2 (суррогатная пара) + 1 (пробел)
    url_length_utf16 = len("https://x.io")
    entities = [types.MessageEntityUrl(offset=url_offset_utf16, length=url_length_utf16)]

    out = convert.convert_entities(entities, text)
    assert len(out) == 1
    assert out[0]["url"] == "https://x.io"

    # Наивный срез по индексам Python-строки съехал бы (эмодзи — 1 индекс Python, но 2 UTF-16 unit).
    naive = text[url_offset_utf16 : url_offset_utf16 + url_length_utf16]
    assert naive != "https://x.io"


# --- Тест 4: медиа --------------------------------------------------------------------------------------


def test_1b_04_photo_with_caption_is_file_kind_text_is_caption_no_bytes():
    other = h.make_user(7)
    channel = convert.dialog_channel_ref(h.peer_of(other), other, me_id=h.ME_ID)
    media = types.MessageMediaPhoto(photo=types.PhotoEmpty(id=1), spoiler=False)
    msg = h.make_message(3, h.peer_of(other), message="подпись", media=media, from_id=None)
    ev = convert.convert_message(msg, _ctx(channel, entities={7: other}))

    assert ev["kind"] == "file"
    assert ev["text"] == "подпись"
    assert ev["attachments"] == [{"type": "photo"}]
    assert "id" not in json.dumps(ev["attachments"])  # никаких байтов/идентификаторов файла


def test_1b_04_link_preview_stays_text_kind():
    other = h.make_user(7)
    channel = convert.dialog_channel_ref(h.peer_of(other), other, me_id=h.ME_ID)
    webpage = types.MessageMediaWebPage(webpage=types.WebPageEmpty(id=1))
    msg = h.make_message(4, h.peer_of(other), message="смотри https://x.io", media=webpage, from_id=None)
    ev = convert.convert_message(msg, _ctx(channel, entities={7: other}))

    assert ev["kind"] == "text"
    assert ev["attachments"] is None
    assert ev["text"] == "смотри https://x.io"


def test_1b_04_source_url_public_private_topic_and_dm_none():
    # публичный канал
    pub = h.make_channel(1, title="Pub", broadcast=True, username="pubchan")
    ctx_pub = convert.ConvertContext(
        channel={}, channel_kind_for_sender="channel", me_id=h.ME_ID, entities={}, public_username="pubchan"
    )
    msg_pub = h.make_message(9, h.peer_of(pub), media=types.MessageMediaPhoto(photo=types.PhotoEmpty(id=1)))
    ev_pub = convert.convert_message(msg_pub, ctx_pub)
    assert ev_pub["attachments"][0]["source_url"] == "https://t.me/pubchan/9"

    # приватный канал/супергруппа (numeric id)
    ctx_priv = convert.ConvertContext(
        channel={}, channel_kind_for_sender="channel", me_id=h.ME_ID, entities={}, private_channel_num=555
    )
    msg_priv = h.make_message(
        9, types.PeerChannel(555), media=types.MessageMediaPhoto(photo=types.PhotoEmpty(id=1))
    )
    ev_priv = convert.convert_message(msg_priv, ctx_priv)
    assert ev_priv["attachments"][0]["source_url"] == "https://t.me/c/555/9"

    # тема форума: /c/<chat>/<topic>/<id>
    reply = h.make_reply(top_id=42, forum_topic=True)
    msg_topic = h.make_message(
        9, types.PeerChannel(555), media=types.MessageMediaPhoto(photo=types.PhotoEmpty(id=1)), reply_to=reply
    )
    ev_topic = convert.convert_message(msg_topic, ctx_priv)
    assert ev_topic["attachments"][0]["source_url"] == "https://t.me/c/555/42/9"

    # личка: ссылки нет
    other = h.make_user(7)
    ctx_dm = convert.ConvertContext(
        channel={}, channel_kind_for_sender="dm", me_id=h.ME_ID, entities={7: other}
    )
    msg_dm = h.make_message(
        9, h.peer_of(other), media=types.MessageMediaPhoto(photo=types.PhotoEmpty(id=1)), from_id=None
    )
    ev_dm = convert.convert_message(msg_dm, ctx_dm)
    assert "source_url" not in ev_dm["attachments"][0]
