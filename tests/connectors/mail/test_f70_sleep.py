"""Ф70-1 (docs/architecture.md.2–C.3): почта спит вместе с днём проекта — слушатель IDLE
остановлен (новое письмо не будит сверку), отправочное соединение не тронуто; сон и пробуждение повторяются на
живом процессе, первое подключение нового слушателя лишней сверки не заказывает (её заказывает каркас)."""

from __future__ import annotations

import asyncio
import time

import pytest

from .conftest import make_connector

SLEEP = {"project_id": 1, "since_ts": 1}


async def _until(pred, limit: float = 5.0) -> None:
    deadline = time.monotonic() + limit
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("условие не наступило")
        await asyncio.sleep(0.01)


def _queued(connector) -> set[str]:
    out = set()
    q = connector._sync_queue  # noqa: SLF001
    while not q.empty():
        out.add(q.get_nowait())
    return out


def _idles(gm) -> int:
    return sum(1 for _name, cmd in gm.commands if cmd == "IDLE")


@pytest.fixture
async def made():
    items = []
    yield items
    for gw in items:
        await gw.close()


async def test_mail_sleep_stops_listener_keeps_sender_and_wakes_twice(
    admin_client, internal_transport, gm, made
):
    connector, gw, _client, _aid = await make_connector(admin_client, internal_transport, gm)
    made.append(gw)
    await connector.start_source()
    await _until(gm.idle_started.is_set)
    for _round in (1, 2):
        await _until(lambda: gw._listener is not None and gw._listener.connected)  # noqa: SLF001
        _queued(connector)
        await connector._apply_sleep(SLEEP)  # noqa: SLF001
        assert connector.sleeping is True
        assert gw._listener is None  # noqa: SLF001 — слушателя нет: новое письмо сверку не будит
        assert gw.listener_down_since() is None  # пульсу нечего считать упавшим
        assert gw.sender._closed is False  # noqa: SLF001 — отправка при сне идёт
        idles = _idles(gm)
        gm.idle_started.clear()
        await connector._apply_sleep(None)  # noqa: SLF001
        await _until(gm.idle_started.is_set)
        await _until(lambda idles=idles: _idles(gm) > idles)
        await asyncio.sleep(0.05)
        assert _queued(connector) == {"resume"}  # «connected» нового слушателя лишней сверки не заказал
