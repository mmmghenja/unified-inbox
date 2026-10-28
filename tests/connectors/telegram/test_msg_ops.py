"""Ф14-8 (C1 + П4, П6, тесты E8/E9): правка и «удалить у всех» своих сообщений в Telegram —
`TelegramConnector.push_msg_op` с `FakeGateway` (без сети) и сборка запросов настоящего `TgGateway` на
подменном клиенте."""

from __future__ import annotations

import pytest
from telethon import errors, utils
from telethon.tl import types
from telethon.tl.functions.messages import EditMessageRequest

from hub.connectors.base import AuthRequired
from hub.connectors.msg_ops import MsgOpResult
from hub.connectors.telegram import convert
from hub.connectors.telegram.connector import TelegramConnector
from hub.connectors.telegram.gateway import TgGateway

from ..media_fakes import FakeMediaCore
from . import helpers as h
from .helpers import FakeGateway

USER = h.make_user(42, first_name="Петя")
FORUM = h.make_channel(600, title="Форум", megagroup=True, forum=True)
NOW_MS = 1_700_000_500_000


def marked(entity) -> int:
    return utils.get_peer_id(h.peer_of(entity))


def setup():
    gw = FakeGateway()
    gw.now_ms = NOW_MS
    conn = TelegramConnector(FakeMediaCore().client(), gw, account_id=7)
    conn._me_id = h.ME_ID  # noqa: SLF001 — обычно ставит start_source()
    for entity in (USER, FORUM):
        gw.seed_peer(marked(entity), h.input_peer_of(entity))
        conn._entities[marked(entity)] = entity  # noqa: SLF001
    events: list[dict] = []
    conn.emit = events.append
    return gw, conn, events


def own(gw, entity, msg_id, *, text="было", media=None, reply_to=None, entities=None):
    msg = h.make_message(
        msg_id, h.peer_of(entity), message=text, out=True, media=media, reply_to=reply_to, entities=entities
    )
    gw.add_message(marked(entity), msg)
    return msg


def job(op="edit", *, text="стало", channel_ext_id=None, kind="dm", msg="15", message_kind="text") -> dict:
    item = {
        "message_id": 101,
        "gen": 1,
        "lease_gen": 1,
        "op": op,
        "channel": {"ext_id": channel_ext_id or str(marked(USER)), "kind": kind, "parent_ext_id": None},
        "message_ext_id": msg,
        "thread_ext_id": None,
        "message_kind": message_kind,
        "has_files": message_kind == "file",
    }
    if op == "edit":
        item["text"] = text
    return item


# --- E8: правка ---


async def test_e8_edit_calls_gateway_rereads_and_emits_edit_with_edited_ts():
    gw, conn, events = setup()
    own(gw, USER, 15)
    result = await conn.push_msg_op(job())
    assert result == MsgOpResult("done")
    assert gw.msg_op_calls == [("edit", marked(USER), 15, "стало")]
    assert gw.by_ids_calls == [(marked(USER), [15])]
    (event,) = [e for e in events if e["type"] == "edit"]
    assert event["ext_id"] == "15" and event["text"] == "стало" and event["is_me"] is True
    assert event["edited_ts"] == NOW_MS
    assert event["channel"]["ext_id"] == str(marked(USER))


async def test_review_edit_truth_uses_entities_of_reread_message_not_connector_cache():
    """Ревью Ф14-8: чата нет в кэше `_entities` коннектора (после рестарта его наполняет частичный проход
    сверки), сессия Telethon peer знает — правда о правке берёт чат из самого перечитанного сообщения (его
    `chat` Telethon разрешил из ответа `get_messages`), а не молча просит сверку со старым текстом в хабе."""
    gw, conn, events = setup()
    conn._entities.pop(marked(USER))  # noqa: SLF001
    msg = own(gw, USER, 15)
    msg._chat = USER  # noqa: SLF001 — так `get_messages` Telethon заполняет сообщение из `users`/`chats` ответа
    result = await conn.push_msg_op(job())
    assert result == MsgOpResult("done")
    (event,) = [e for e in events if e["type"] == "edit"]
    assert event["text"] == "стало" and event["channel"]["ext_id"] == str(marked(USER))


async def test_e8_topic_uses_chat_peer_and_event_lands_in_topic():
    gw, conn, events = setup()
    own(gw, FORUM, 31, reply_to=h.make_reply(top_id=7, msg_id=7, forum_topic=True))
    gw.forum_topics[(marked(FORUM), 7)] = h.make_forum_topic(marked(FORUM), 7, title="Тема")
    topic_ext = f"{marked(FORUM)}:7"
    result = await conn.push_msg_op(job(channel_ext_id=topic_ext, kind="topic", msg="31"))
    assert result.outcome == "done"
    assert gw.msg_op_calls == [("edit", marked(FORUM), 31, "стало")]
    (event,) = [e for e in events if e["type"] == "edit"]
    assert event["channel"]["ext_id"] == topic_ext


async def test_e8_not_modified_done_only_if_text_equal_p6():
    gw, conn, events = setup()
    own(gw, USER, 15, text="стало")
    # Telegram обрезает пробелы по краям — сравнение перечитанного текста без них
    gw.msg_op_faults.append(errors.MessageNotModifiedError(request=None))
    assert await conn.push_msg_op(job(text="стало ")) == MsgOpResult("done", detail="not_modified")
    assert [e["type"] for e in events] == ["edit"]

    gw2, conn2, events2 = setup()
    own(gw2, USER, 15, text="совсем другое")
    gw2.msg_op_faults.append(errors.MessageNotModifiedError(request=None))
    assert await conn2.push_msg_op(job()) == MsgOpResult("fail", code="other", detail="not_modified")
    assert events2 == []


async def test_e8_not_modified_reread_network_error_retry():
    gw, conn, events = setup()
    own(gw, USER, 15, text="стало")
    gw.by_ids_faults.append(ConnectionError("сеть"))
    result = await conn.push_msg_op(job())
    assert result.outcome == "retry" and events == []


@pytest.mark.parametrize(
    "exc, code",
    [
        (errors.MessageEditTimeExpiredError(request=None), "expired"),
        (errors.MessageAuthorRequiredError(request=None), "forbidden"),
        (errors.ChatWriteForbiddenError(request=None), "forbidden"),
        (errors.ChatAdminRequiredError(request=None), "forbidden"),
        (errors.MessageIdInvalidError(request=None), "not_found"),
        (errors.MessageTooLongError(request=None), "too_long"),
        (errors.MediaCaptionTooLongError(request=None), "too_long"),
        (errors.MessageEmptyError(request=None), "other"),
    ],
)
async def test_e8_edit_errors_map_to_codes(exc, code):
    gw, conn, events = setup()
    own(gw, USER, 15)
    gw.msg_op_faults.append(exc)
    result = await conn.push_msg_op(job())
    assert result == MsgOpResult("fail", code=code, detail=type(exc).__name__)
    assert events == []


async def test_e8_flood_wait_retry_with_seconds():
    gw, conn, _ = setup()
    own(gw, USER, 15)
    gw.msg_op_faults.append(errors.FloodWaitError(request=None, capture=7))
    result = await conn.push_msg_op(job())
    assert result.outcome == "retry" and result.retry_after_s == 7


async def test_e8_network_and_unresolved_peer_retry_auth_lost_raises():
    gw, conn, _ = setup()
    own(gw, USER, 15)
    gw.msg_op_faults.append(errors.ServerError(request=None, message="x", code=500))
    assert (await conn.push_msg_op(job())).outcome == "retry"
    gw.msg_op_faults.append(ConnectionError("сеть"))
    assert (await conn.push_msg_op(job())).outcome == "retry"
    gw.forget_peer(marked(USER))
    assert await conn.push_msg_op(job()) == MsgOpResult("retry", detail="peer_unresolved")
    gw.seed_peer(marked(USER), h.input_peer_of(USER))
    gw.msg_op_faults.append(errors.AuthKeyUnregisteredError(request=None))
    with pytest.raises(AuthRequired):
        await conn.push_msg_op(job())


async def test_e8_caption_1025_utf16_too_long_without_request():
    gw, conn, _ = setup()
    own(gw, USER, 15, media=types.MessageMediaPhoto(photo=types.PhotoEmpty(id=1)))
    result = await conn.push_msg_op(job(text="😀" * 512 + "a", message_kind="file"))  # 1025 UTF-16
    assert result.outcome == "fail" and result.code == "too_long"
    assert gw.msg_op_calls == [] and gw.get_input_entity_calls == []
    assert convert.utf16_len("😀" * 512) == 1024
    assert (await conn.push_msg_op(job(text="😀" * 512, message_kind="file"))).outcome == "done"
    # текст без медиа — лимит 4096
    result = await conn.push_msg_op(job(text="a" * 4097))
    assert result.code == "too_long"


async def test_e8_reread_failure_still_done_without_event():
    gw, conn, events = setup()
    own(gw, USER, 15)
    gw.by_ids_faults.append(ConnectionError("сеть"))
    assert await conn.push_msg_op(job()) == MsgOpResult("done")
    assert gw.msg_op_calls == [("edit", marked(USER), 15, "стало")]
    assert events == []


async def test_bad_ref_and_bad_op_fail_other():
    _gw, conn, _ = setup()
    assert (await conn.push_msg_op(job(msg="abc"))).code == "other"
    assert (await conn.push_msg_op(job(channel_ext_id="x:y"))).code == "other"
    assert (await conn.push_msg_op(job(op="nuke"))).detail == "bad_op"


# --- E9: удалить у всех ---


def _delete_events(events):
    return [e for e in events if e["type"] == "delete"]


async def test_e9_delete_pts_positive_emits_delete_done():
    gw, conn, events = setup()
    own(gw, USER, 15)
    assert await conn.push_msg_op(job("delete")) == MsgOpResult("done")
    assert gw.msg_op_calls == [("delete", marked(USER), [15])]
    assert _delete_events(events) == [
        {"type": "delete", "channel": {"ext_id": str(marked(USER)), "kind": "dm"}, "ext_ids": ["15"]}
    ]


async def test_e9_pts_zero_and_message_gone_is_done():
    gw, conn, events = setup()
    gw.delete_pts = 0  # сообщения уже нет — сервер ничего не удалил
    assert await conn.push_msg_op(job("delete")) == MsgOpResult("done")
    assert len(_delete_events(events)) == 1


async def test_e9_pts_zero_and_message_exists_forbidden():
    gw, conn, events = setup()
    own(gw, USER, 15)
    gw.delete_pts = 0
    result = await conn.push_msg_op(job("delete"))
    assert result.outcome == "fail" and result.code == "forbidden"
    assert events == []


async def test_e9_delete_forbidden_error():
    gw, conn, events = setup()
    own(gw, USER, 15)
    gw.msg_op_faults.append(errors.MessageDeleteForbiddenError(request=None))
    result = await conn.push_msg_op(job("delete"))
    assert result == MsgOpResult("fail", code="forbidden", detail="MessageDeleteForbiddenError")
    assert events == []


async def test_e9_p4_message_id_invalid_rereads_gone_done_exists_not_found():
    gw, conn, events = setup()
    gw.msg_op_faults.append(errors.MessageIdInvalidError(request=None))
    assert await conn.push_msg_op(job("delete")) == MsgOpResult("done")
    assert len(_delete_events(events)) == 1

    gw2, conn2, events2 = setup()
    own(gw2, USER, 15)
    gw2.msg_op_faults.append(errors.MessageIdInvalidError(request=None))
    result = await conn2.push_msg_op(job("delete"))
    assert result.outcome == "fail" and result.code == "not_found"
    assert events2 == []


async def test_e9_topic_delete_event_keeps_topic_channel():
    gw, conn, events = setup()
    own(gw, FORUM, 31)
    topic_ext = f"{marked(FORUM)}:7"
    assert (
        await conn.push_msg_op(job("delete", channel_ext_id=topic_ext, kind="topic", msg="31"))
    ).outcome == ("done")
    assert gw.msg_op_calls == [("delete", marked(FORUM), [31])]
    assert _delete_events(events)[0]["channel"] == {"ext_id": topic_ext, "kind": "topic"}


# --- шлюз: сборка запросов на подменном клиенте ---


class _FakeTelegramClient:
    def __init__(self, *args, **kwargs) -> None:
        self.requests: list = []
        self.delete_calls: list = []
        self.delete_result: object = [types.messages.AffectedMessages(pts=10, pts_count=1)]

    async def __call__(self, request):
        self.requests.append(request)
        return types.Updates(updates=[], users=[], chats=[], date=None, seq=0)

    async def delete_messages(self, entity, message_ids, *, revoke=True):
        self.delete_calls.append((entity, list(message_ids), revoke))
        return self.delete_result


def _tg_gateway(tmp_path):
    holder: dict = {}

    def factory(*a, **kw):
        holder["c"] = _FakeTelegramClient(*a, **kw)
        return holder["c"]

    gw = TgGateway(
        session_path=str(tmp_path / "s.session"),
        api_id=1,
        api_hash="x",
        proxy=None,
        device_model="t",
        system_version="t",
        app_version="t",
        client_factory=factory,
    )
    return gw, holder["c"]


async def test_gateway_edit_request_plain_text_no_webpage_false(tmp_path):
    gw, client = _tg_gateway(tmp_path)
    peer = types.InputPeerUser(42, 0)
    await gw.edit_message(peer, 15, "стало")
    (req,) = client.requests
    assert isinstance(req, EditMessageRequest)
    assert req.peer == peer and req.id == 15 and req.message == "стало"
    assert req.no_webpage is False and req.entities is None and req.media is None


async def test_gateway_delete_revoke_sums_pts_count(tmp_path):
    gw, client = _tg_gateway(tmp_path)
    peer = types.InputPeerChannel(600, 0)
    client.delete_result = [
        types.messages.AffectedMessages(pts=10, pts_count=1),
        types.messages.AffectedMessages(pts=11, pts_count=2),
    ]
    assert await gw.delete_messages(peer, [31, 32, 33]) == 3
    assert client.delete_calls == [(peer, [31, 32, 33], True)]
    client.delete_result = [types.messages.AffectedMessages(pts=10, pts_count=0)]
    assert await gw.delete_messages(peer, [31]) == 0
