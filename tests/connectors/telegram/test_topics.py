"""Тест 5 куска 1b: топики форума. Часть — чистые функции `convert.py` (без сети), часть — `TelegramConnector`
с `FakeGateway` (промах кэша названий тянет `GetForumTopicsByIDRequest`, это уже не «чистая функция»)."""

from telethon import utils
from telethon.tl import types

from hub.connectors.telegram import convert
from hub.connectors.telegram.connector import TelegramConnector

from . import helpers as h
from .helpers import FakeGateway


def _client_stub():
    class _Client:
        def __init__(self):
            self.statuses = []

        async def set_status(self, status, *, last_error=None):
            self.statuses.append(status)

    return _Client()


# --- часть 1: чистые функции ------------------------------------------------------------------------------


def test_1b_05_message_in_topic_and_general_and_reply_within_topic():
    # Обычный пост в теме (id=42): reply_to_top_id=42, reply_to_msg_id тоже 42 (не настоящий ответ).
    reply_post = h.make_reply(top_id=42, msg_id=42, forum_topic=True)
    assert convert.message_topic_id(_msg_with(reply_post)) == 42
    assert convert.message_reply_to_ext_id(_msg_with(reply_post), topic_id=42) is None

    # Настоящий ответ внутри темы: reply_to_msg_id указывает на ДРУГОЕ сообщение, не на корень.
    reply_answer = h.make_reply(top_id=42, msg_id=100, forum_topic=True)
    assert convert.message_topic_id(_msg_with(reply_answer)) == 42
    assert convert.message_reply_to_ext_id(_msg_with(reply_answer), topic_id=42) == "100"

    # Нет reply_to_top_id и reply_to_msg_id -> General (1).
    reply_general = h.make_reply(forum_topic=True)
    assert convert.message_topic_id(_msg_with(reply_general)) == convert.GENERAL_TOPIC_ID == 1

    # Не форум вовсе (forum_topic=False) -> None, сообщение остаётся в родительском канале.
    reply_plain = h.make_reply(msg_id=5, forum_topic=False)
    assert convert.message_topic_id(_msg_with(reply_plain)) is None
    assert convert.message_reply_to_ext_id(_msg_with(reply_plain), topic_id=None) == "5"


def _msg_with(reply):
    return h.make_message(1, types.PeerChannel(1), reply_to=reply, from_id=None)


def test_1b_05_topic_channel_ref_shape():
    ref = convert.topic_channel_ref("-1001", 42, "Обсуждение")
    assert ref == {
        "ext_id": "-1001:42",
        "kind": "topic",
        "title": "Обсуждение",
        "parent_ext_id": "-1001",
        "own_id_space": True,
    }


# --- часть 2: коннектор + FakeGateway — промах кэша названий тянет сеть -----------------------------------


async def test_1b_05_topic_title_cache_miss_fetches_and_fallback_on_failure():
    gw = FakeGateway()
    forum = h.make_channel(1, title="Форум", megagroup=True, forum=True)
    gw.add_dialog(h.make_dialog(forum, top_message=1))
    gw.forum_topics[(utils.get_peer_id(h.peer_of(forum)), 42)] = h.make_forum_topic(1, 42, title="Уже знаем")

    connector = TelegramConnector(_client_stub(), gw, account_id=1)
    connector._me_id = h.ME_ID  # noqa: SLF001 — тест внутреннего состояния, обычно ставит start_source()
    connector._entities[int(convert.peer_ext_id(h.peer_of(forum)))] = forum  # noqa: SLF001

    chat_ext = convert.peer_ext_id(h.peer_of(forum))
    title = await connector._topic_title(chat_ext, h.peer_of(forum), 42)  # noqa: SLF001
    assert title == "Уже знаем"
    assert gw.get_forum_topics_calls == [(int(chat_ext), (42,))]

    # Второй раз — из кэша, сеть не трогается снова.
    title2 = await connector._topic_title(chat_ext, h.peer_of(forum), 42)  # noqa: SLF001
    assert title2 == "Уже знаем"
    assert len(gw.get_forum_topics_calls) == 1

    # Неизвестная тема (не в forum_topics) -> провал -> "Тема <id>".
    title3 = await connector._topic_title(chat_ext, h.peer_of(forum), 77)  # noqa: SLF001
    assert title3 == "Тема 77"


async def test_1b_05_general_topic_is_default_title_without_network_call():
    gw = FakeGateway()
    forum = h.make_channel(1, title="Форум", megagroup=True, forum=True)
    connector = TelegramConnector(_client_stub(), gw, account_id=1)
    connector._me_id = h.ME_ID  # noqa: SLF001

    chat_ext = convert.peer_ext_id(h.peer_of(forum))
    title = await connector._topic_title(chat_ext, h.peer_of(forum), convert.GENERAL_TOPIC_ID)  # noqa: SLF001
    assert title == "General"
    assert gw.get_forum_topics_calls == []
