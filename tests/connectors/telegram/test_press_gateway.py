"""Ф44-1c П1 (docs/architecture.md): `TgGateway.press_button` — нажатие callback-кнопки
уходит в сеть не больше одного раза.

Сети нет: вместо соединения — настоящие `MessagePacker` и `MTProtoState` Telethon 1.45.0 и «цикл отправки»,
повторяющий шаги `MTProtoSender._send_loop` / `_reconnect` / `_handle_bad_server_salt` дословно (взять пачку
у упаковщика -> записать в ожидающие -> «сеть»; переподключение: новый сеанс, ожидающие — обратно в
очередь). Контрольный тест показывает, что обычный запрос Telethon после переподключения уходит второй раз."""

import asyncio
import logging
from collections import defaultdict
from types import SimpleNamespace

import pytest
from telethon import errors
from telethon.extensions.messagepacker import MessagePacker
from telethon.network.mtprotostate import MTProtoState
from telethon.network.requeststate import RequestState
from telethon.tl import types
from telethon.tl.functions.messages import GetBotCallbackAnswerRequest

from hub.connectors.telegram.gateway import PressUnknown, TgGateway

PEER = types.InputPeerUser(user_id=777, access_hash=1)
DATA = b"callback-1"


class _Sender:
    """Те части `MTProtoSender`, которых касается `press_button`, и его шаги — без сети."""

    def __init__(self) -> None:
        loggers = defaultdict(lambda: logging.getLogger("telethon-test"))
        self._user_connected = True
        self._state = MTProtoState(None, loggers)
        self._send_queue = MessagePacker(self._state, loggers)
        self._pending_state: dict = {}
        self.wire: list[list[object]] = []  # что «ушло в сеть»: запросы каждой пачки

    async def pump(self) -> None:
        """Один оборот `_send_loop`."""
        batch, data = await self._send_queue.get()
        if not data:
            return
        for state in batch:
            self._pending_state[state.msg_id] = state
        self.wire.append([state.request for state in batch])

    def reconnect(self) -> None:
        """`_reconnect`: чистый сеанс, все ожидающие — снова в очередь."""
        self._state.reset()
        self._send_queue.extend(self._pending_state.values())
        self._pending_state.clear()

    def bad_salt(self) -> None:
        """`_handle_bad_server_salt`: сервер запрос отверг — он переотправляется в том же сеансе."""
        states = list(self._pending_state.values())
        self._pending_state.clear()
        self._send_queue.extend(states)

    def disconnect(self, error: Exception | None = None) -> None:
        """`_disconnect`: ожидающим — ошибка или отмена."""
        self._user_connected = False
        for state in self._pending_state.values():
            if error and not state.future.done():
                state.future.set_exception(error)
            else:
                state.future.cancel()
        self._pending_state.clear()

    def presses(self) -> int:
        return sum(isinstance(r, GetBotCallbackAnswerRequest) for batch in self.wire for r in batch)

    def answer(self, value=None, *, error: Exception | None = None) -> None:
        (state,) = self._pending_state.values()
        self._pending_state.clear()
        if error is not None:
            state.future.set_exception(error)
        else:
            state.future.set_result(value)


class _Client:
    def __init__(self, sender: _Sender) -> None:
        self._sender = sender
        self.calls = 0

    async def get_input_entity(self, peer):
        return peer

    def __call__(self, request):
        self.calls += 1  # путь с повторами `_call` — сюда нажатие попадать не должно
        raise AssertionError("нажатие пошло через client(request)")


@pytest.fixture
def world(tmp_path):
    sender = _Sender()
    client = _Client(sender)
    gateway = TgGateway(
        session_path=str(tmp_path / "s"),
        api_id=1,
        api_hash="x",
        proxy=None,
        device_model="d",
        system_version="s",
        app_version="a",
        client_factory=lambda *a, **kw: client,
    )
    return SimpleNamespace(sender=sender, client=client, gateway=gateway)


def _go(world, timeout_s=5.0) -> asyncio.Task:
    return asyncio.ensure_future(world.gateway.press_button(PEER, 500, DATA, timeout_s=timeout_s))


async def _sent(world) -> asyncio.Task:
    task = _go(world)
    await asyncio.sleep(0)
    await world.sender.pump()
    assert world.sender.presses() == 1
    return task


async def test_answer_is_returned_and_request_is_the_callback(world):
    task = await _sent(world)
    ((request,),) = world.sender.wire
    assert (request.msg_id, request.data, request.peer) == (500, DATA, PEER)
    answer = types.messages.BotCallbackAnswer(cache_time=0, message="готово")
    world.sender.answer(answer)
    assert await task is answer and world.client.calls == 0


async def test_timed_out_error_means_exactly_one_request(world):
    """П1: `_call` Telethon повторил бы `TimedOutError` до пяти раз — каждое было бы новым нажатием."""
    task = await _sent(world)
    world.sender.answer(error=errors.TimedOutError(request=None, message="TIMEOUT"))
    with pytest.raises(errors.TimedOutError):
        await task
    await asyncio.sleep(0)
    assert world.sender.presses() == 1 and world.client.calls == 0
    assert not world.sender._send_queue._deque and not world.sender._pending_state


async def test_rpc_errors_pass_through(world):
    for error in (
        errors.BotResponseTimeoutError(request=None),
        errors.MessageIdInvalidError(request=None),
        errors.FloodWaitError(request=None, capture=30),
    ):
        task = _go(world)
        await asyncio.sleep(0)
        await world.sender.pump()
        world.sender.answer(error=error)
        with pytest.raises(type(error)):
            await task
    assert world.sender.presses() == 3


async def test_plain_telethon_request_is_resent_after_reconnect():
    """Контроль: обычное состояние запроса после переподключения уходит в сеть второй раз — даже если его
    ожидание отменили. Поэтому у нажатия своё состояние."""
    sender = _Sender()
    state = RequestState(GetBotCallbackAnswerRequest(peer=PEER, msg_id=500, data=DATA))
    sender._send_queue.append(state)
    await sender.pump()
    state.future.cancel()
    sender.reconnect()
    await sender.pump()
    assert sender.presses() == 2


async def test_reconnect_does_not_resend_and_outcome_is_unknown(world):
    task = await _sent(world)
    world.sender.reconnect()
    await world.sender.pump()  # упаковщик снимает запрос сам, в сеть ничего не идёт
    with pytest.raises(PressUnknown) as info:
        await task
    assert info.value.reason == "reconnected"
    assert world.sender.presses() == 1
    assert not world.sender._send_queue._deque and not world.sender._pending_state


async def test_timeout_after_send_is_unknown_and_never_resent(world):
    task = _go(world, timeout_s=0.05)
    await asyncio.sleep(0)
    await world.sender.pump()
    with pytest.raises(PressUnknown) as info:
        await task
    assert info.value.reason == "no_answer"
    assert not world.sender._pending_state  # снят из ожидающих: переподключение его не найдёт
    world.sender.reconnect()
    world.sender._send_queue.append(RequestState(types.MsgsAck([1])))
    await world.sender.pump()
    assert world.sender.presses() == 1


async def test_cancel_after_send_removes_the_request(world):
    task = await _sent(world)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not world.sender._pending_state
    world.sender.reconnect()
    world.sender._send_queue.append(RequestState(types.MsgsAck([1])))
    await world.sender.pump()
    assert world.sender.presses() == 1


async def test_cancel_or_timeout_before_send_never_goes_out(world):
    # Не ушло за отведённое время — это не «неизвестно»: запроса в сети не было.
    with pytest.raises(ConnectionError):
        await world.gateway.press_button(PEER, 500, DATA, timeout_s=0.02)
    assert not world.sender._send_queue._deque
    # Отмена, пока запрос у упаковщика в очереди.
    task = _go(world)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    world.sender._send_queue.append(RequestState(types.MsgsAck([1])))
    await world.sender.pump()
    assert world.sender.presses() == 0


async def test_bad_salt_resend_in_the_same_session_is_allowed(world):
    """Сервер отверг запрос (соль) — он не выполнен; переотправка в том же сеансе не второе нажатие."""
    task = await _sent(world)
    world.sender.bad_salt()
    await world.sender.pump()
    assert world.sender.presses() == 2
    answer = types.messages.BotCallbackAnswer(cache_time=0)
    world.sender.answer(answer)
    assert await task is answer


async def test_disconnect_after_send_is_unknown_before_send_is_connection_error(world):
    task = await _sent(world)
    world.sender.disconnect()  # Telethon отменяет ожидающие future сам
    with pytest.raises(PressUnknown):
        await task
    with pytest.raises(ConnectionError):  # соединения нет — запрос даже не ставится
        await world.gateway.press_button(PEER, 500, DATA, timeout_s=1)
    assert world.sender.presses() == 1

    world.sender._user_connected = True
    task = await _sent_again(world)
    world.sender.disconnect(OSError("обрыв"))
    with pytest.raises(PressUnknown):
        await task


async def _sent_again(world) -> asyncio.Task:
    task = _go(world)
    await asyncio.sleep(0)
    await world.sender.pump()
    return task
