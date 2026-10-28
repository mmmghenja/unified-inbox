"""Ф69-1 B + П3, П5: PeerFlood на тексте, файле и альбоме — `permanent` с `error="PeerFlood"`, остаток пачки
останавливается режимом `STOP_PEER_FLOOD`, `limited` коннектор сам не ставит (решает ядро)."""

from __future__ import annotations

from telethon import errors

from hub.connectors.base import STOP_PEER_FLOOD

from ..outbox_file_fakes import PNG_BYTES, file_meta
from . import test_send_album as album
from . import test_send_file as single


def _watch_status(connector) -> list:
    calls: list = []

    async def _set_status(status, last_error=None):
        calls.append((status, last_error))

    connector._client.set_status = _set_status  # noqa: SLF001
    return calls


def _assert_peer_flood(connector, result, calls) -> None:
    assert (result.outcome, result.error) == ("permanent", "PeerFlood")
    assert connector._stop_batch is True  # noqa: SLF001
    assert connector._stop_mode == STOP_PEER_FLOOD  # noqa: SLF001
    assert calls == []  # ни `limited`, ни иного статуса


async def test_text_peer_flood(tmp_path) -> None:
    connector, gw, _core, _tmp = single._setup(tmp_path)  # noqa: SLF001
    calls = _watch_status(connector)
    gw.queue_send_faults(errors.PeerFloodError(request=None))
    result = await connector.send(single._lease(None, text="первое новому"))  # noqa: SLF001
    _assert_peer_flood(connector, result, calls)


async def test_file_peer_flood(tmp_path) -> None:
    connector, gw, _core, _tmp = single._setup(tmp_path)  # noqa: SLF001
    calls = _watch_status(connector)
    gw.media_faults.append(errors.PeerFloodError(request=None))
    result = await connector.send(single._lease(file_meta(PNG_BYTES)))  # noqa: SLF001
    _assert_peer_flood(connector, result, calls)


async def test_album_peer_flood(tmp_path) -> None:
    connector, gw, _core, _tmp = album._setup(tmp_path)  # noqa: SLF001
    calls = _watch_status(connector)
    gw.album_faults.append(errors.PeerFloodError(request=None))
    result = await connector.send(album._lease(album._photos()))  # noqa: SLF001
    _assert_peer_flood(connector, result, calls)
