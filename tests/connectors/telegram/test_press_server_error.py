"""Находка 2: ошибка сервера Telegram 5xx на `GetBotCallbackAnswer` (исход неясен: запрос ушёл и принят,
сервер упал при обработке) отчитывается как `failed` — сессии это печатается «не нажато: <класс>», повтор
выглядит законным. Нажатие не идемпотентно: по спеке 4.4 всё неясное после отправки — `unknown`."""
import pytest
from telethon import errors
from telethon.tl.types import RpcError

from connectors.telegram import test_press as tp


def _server_errors():
    yield errors.rpc_message_to_error(RpcError(error_code=500, error_message="RPC_CALL_FAIL"), None)
    yield errors.rpc_message_to_error(RpcError(error_code=-500, error_message="No workers running"), None)
    yield errors.rpc_message_to_error(RpcError(error_code=500, error_message="INTERNAL"), None)


@pytest.mark.parametrize("fault", list(_server_errors()), ids=lambda e: type(e).__name__)
async def test_server_5xx_is_unknown_not_failed(fault):
    assert isinstance(fault, errors.ServerError)
    gw, conn = tp.setup()
    gw.press_faults.append(fault)
    res = await conn.push_press(tp.item())
    assert len(gw.press_calls) == 1
    assert res.outcome == "unknown", res
