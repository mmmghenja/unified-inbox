"""Ф44-1c (docs/architecture.md): `TelegramConnector.push_press` с `FakeGateway`
(без сети) — один вызов шлюза на нажатие и разбор исходов."""

from __future__ import annotations

import base64

import pytest
from telethon import errors, utils
from telethon.tl import types

from hub.connectors.base import AuthRequired
from hub.connectors.press import PressResult
from hub.connectors.telegram.connector import TelegramConnector
from hub.connectors.telegram.gateway import PressUnknown

from ..media_fakes import FakeMediaCore
from . import helpers as h
from .helpers import FakeGateway

BOT = h.make_user(777, first_name="Бот")
FORUM = h.make_channel(600, title="Форум", megagroup=True, forum=True)
DATA = b"\x00callback\xff"


def marked(entity) -> int:
    return utils.get_peer_id(h.peer_of(entity))


def setup():
    gw = FakeGateway()
    for entity in (BOT, FORUM):
        gw.seed_peer(marked(entity), h.input_peer_of(entity))
    return gw, TelegramConnector(FakeMediaCore().client(), gw, account_id=7)


def item(channel_ext_id: str | None = None, msg="500", data: bytes = DATA) -> dict:
    return {
        "id": 3,
        "lease_gen": 1,
        "channel_ext_id": channel_ext_id or str(marked(BOT)),
        "message_ext_id": msg,
        "data_b64": base64.b64encode(data).decode(),
    }


def _rpc(cls, **kw):
    return cls(request=None, **kw)


async def test_answer_fields_and_one_gateway_call():
    gw, conn = setup()
    assert conn.press_enabled is True
    gw.press_answer = types.messages.BotCallbackAnswer(
        cache_time=0, alert=True, message="Заказ принят", url="https://pay.example/1"
    )
    res = await conn.push_press(item())
    assert res == PressResult(
        "done", result={"message": "Заказ принят", "alert": True, "url": "https://pay.example/1"}
    )
    ((peer_id, msg_id, data, timeout_s),) = gw.press_calls
    assert (peer_id, msg_id, data) == (marked(BOT), 500, DATA) and 0 < timeout_s < 25
    # Пустой ответ бота — тоже `done`.
    gw.press_answer = None
    assert await conn.push_press(item()) == PressResult("done", result={})


async def test_topic_of_forum_presses_in_the_chat_itself():
    gw, conn = setup()
    chat = marked(FORUM)
    assert (await conn.push_press(item(f"{chat}:3"))).outcome == "done"
    assert gw.press_calls[0][:2] == (chat, 500)


@pytest.mark.parametrize(
    "fault",
    [_rpc(errors.TimedOutError, message="TIMEOUT"), _rpc(errors.BotResponseTimeoutError)],
)
async def test_bot_did_not_answer_is_done_with_timeout_and_one_call(fault):
    """П1: бот получил нажатие и не ответил на запрос — `done`, и шлюз вызван ровно один раз."""
    gw, conn = setup()
    gw.press_faults.append(fault)
    assert await conn.push_press(item()) == PressResult("done", result={"timeout": True})
    assert len(gw.press_calls) == 1


@pytest.mark.parametrize(
    "cls", [errors.MessageIdInvalidError, errors.DataInvalidError, errors.ButtonDataInvalidError]
)
async def test_stale_button_errors(cls):
    gw, conn = setup()
    gw.press_faults.append(_rpc(cls))
    assert await conn.push_press(item()) == PressResult("failed", reason="stale_button")
    assert len(gw.press_calls) == 1


async def test_flood_unknown_network_and_other_errors():
    gw, conn = setup()
    gw.press_faults.extend(
        [
            _rpc(errors.FloodWaitError, capture=40),
            PressUnknown("reconnected"),
            ConnectionError("нет связи"),
            _rpc(errors.ChatWriteForbiddenError),
        ]
    )
    assert await conn.push_press(item()) == PressResult("retry", reason="flood", retry_after_s=40.0)
    assert await conn.push_press(item()) == PressResult("unknown", reason="reconnected")
    assert await conn.push_press(item()) == PressResult("retry", reason="ConnectionError")
    assert await conn.push_press(item()) == PressResult("failed", reason="ChatWriteForbiddenError")
    assert len(gw.press_calls) == 4  # на каждое нажатие — один вызов, повторов внутри нет


async def test_auth_lost_raises_auth_required():
    gw, conn = setup()
    gw.press_faults.append(_rpc(errors.AuthKeyUnregisteredError))
    with pytest.raises(AuthRequired):
        await conn.push_press(item())


async def test_bad_item_and_unknown_peer_never_reach_the_gateway():
    gw, conn = setup()
    for bad in (
        {**item(), "message_ext_id": "x"},
        {**item(), "message_ext_id": "0"},
        {**item(), "data_b64": "@@@"},
        {**item(), "data_b64": ""},
        {"id": 3, "lease_gen": 1},
    ):
        assert await conn.push_press(bad) == PressResult("failed", reason="bad_ref")
    assert await conn.push_press(item("424242")) == PressResult("retry", reason="peer_unresolved")
    assert gw.press_calls == []
