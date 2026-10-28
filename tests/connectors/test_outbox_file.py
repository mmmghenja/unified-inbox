"""Ф10-4 B1 (`docs/architecture.md` + П3): каркас отправки с файлом —
`CoreClient.fetch_outbox_file`, `ConnectorBase.lease_files` (временный файл удаляется при любом исходе) и
таймаут `send()` для задания с файлом. Ядро — подменное (`FakeFileCore`).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from hub.connectors import base
from hub.connectors.base import ConnectorBase, OutboxFileError, SendResult

from .outbox_file_fakes import PNG_BYTES, FakeFileCore, file_meta


class _Connector(ConnectorBase):
    def __init__(self, client, tmp_dir, *, send_impl=None) -> None:
        super().__init__(client)
        self.outbox_tmp_dir = tmp_dir
        self._send_impl = send_impl

    async def sync(self, reason: str = "scheduled") -> None:
        pass

    async def send(self, lease: dict) -> SendResult:
        return await self._send_impl(lease)

    async def reconcile(self, lease: dict) -> SendResult:
        return SendResult("unknown")

    async def health(self) -> None:
        pass


def _lease(file: dict | None = None, *, mode: str = "send") -> dict:
    return {
        "outbox_id": 7,
        "lease_gen": 3,
        "channel_ext_id": "c1",
        "text": "",
        "reply_to_ext_id": None,
        "thread_ext_id": None,
        "send_token": "tok",
        "mode": mode,
        "send_started_ts": 0,
        "attempts": 1,
        "files": [file] if file is not None else None,
    }


def _setup(tmp_path, data: bytes = PNG_BYTES):
    core = FakeFileCore()
    core.files[7] = data
    tmp_dir = tmp_path / "tmp-conn"
    return core, tmp_dir, _Connector(core.client(), tmp_dir)


def _left(tmp_dir) -> list:
    return list(tmp_dir.iterdir()) if tmp_dir.exists() else []


async def test_lease_file_streams_bytes_and_removes_file(tmp_path) -> None:
    core, tmp_dir, connector = _setup(tmp_path)
    async with connector.lease_files(_lease(file_meta(PNG_BYTES))) as (path,):
        assert path.parent == tmp_dir
        assert path.read_bytes() == PNG_BYTES
        assert path.stat().st_mode & 0o777 == 0o600
    assert _left(tmp_dir) == []
    assert core.fetches == [{"outbox_id": 7, "lease_gen": "3", "idx": "0", "authorization": "Bearer s"}]


async def test_lease_file_removed_when_body_raises(tmp_path) -> None:
    _core, tmp_dir, connector = _setup(tmp_path)
    with pytest.raises(RuntimeError):
        async with connector.lease_files(_lease(file_meta(PNG_BYTES))):
            raise RuntimeError("сбой источника")
    assert _left(tmp_dir) == []


@pytest.mark.parametrize("status", [404, 409])
async def test_core_404_409_is_outbox_file_error(tmp_path, status) -> None:
    core, tmp_dir, connector = _setup(tmp_path)
    core.status = status
    with pytest.raises(OutboxFileError) as info:
        async with connector.lease_files(_lease(file_meta(PNG_BYTES))):
            pytest.fail("тело не должно выполняться")
    assert info.value.reason == f"core_{status}"
    assert _left(tmp_dir) == []


async def test_content_length_differs_from_lease_size(tmp_path) -> None:
    _core, tmp_dir, connector = _setup(tmp_path)
    meta = file_meta(PNG_BYTES)
    meta["size"] = len(PNG_BYTES) - 1
    with pytest.raises(OutboxFileError) as info:
        async with connector.lease_files(_lease(meta)):
            pass
    assert info.value.reason == "size_mismatch"
    assert _left(tmp_dir) == []


async def test_body_shorter_than_content_length(tmp_path) -> None:
    core, tmp_dir, _connector = _setup(tmp_path)
    core.content_length = str(len(PNG_BYTES) + 10)
    client = core.client()
    dest = tmp_path / "x.part"
    with pytest.raises(OutboxFileError) as info:
        await client.fetch_outbox_file(7, 3, 0, dest)
    assert info.value.reason == "short_body"


async def test_body_longer_than_content_length(tmp_path) -> None:
    core, _tmp_dir, _connector = _setup(tmp_path)
    core.content_length = str(len(PNG_BYTES) - 10)
    with pytest.raises(OutboxFileError) as info:
        await core.client().fetch_outbox_file(7, 3, 0, tmp_path / "x.part")
    assert info.value.reason == "size_mismatch"


async def test_missing_content_length(tmp_path) -> None:
    core, tmp_dir, connector = _setup(tmp_path)
    core.content_length = "omit"
    with pytest.raises(OutboxFileError) as info:
        async with connector.lease_files(_lease(file_meta(PNG_BYTES))):
            pass
    assert info.value.reason == "no_content_length"
    assert _left(tmp_dir) == []


async def test_core_unreachable_is_outbox_file_error(tmp_path) -> None:
    core, tmp_dir, connector = _setup(tmp_path)
    core.transport_error = httpx.ConnectError("нет сокета")
    with pytest.raises(OutboxFileError) as info:
        async with connector.lease_files(_lease(file_meta(PNG_BYTES))):
            pass
    assert info.value.reason == "ConnectError"
    assert _left(tmp_dir) == []


async def test_lease_without_file_is_error(tmp_path) -> None:
    _core, _tmp_dir, connector = _setup(tmp_path)
    with pytest.raises(OutboxFileError):
        async with connector.lease_files(_lease(None)):
            pass


def test_file_send_timeout_constant() -> None:
    assert base.FILE_SEND_TIMEOUT_S == 300.0  # noqa: PLR2004 — П3


async def test_process_lease_gives_file_send_longer_timeout(tmp_path, monkeypatch) -> None:
    """П3: задание с файлом — таймаут `FILE_SEND_TIMEOUT_S`, без файла — прежний `_SEND_TIMEOUT_S`."""
    monkeypatch.setattr(base, "_SEND_TIMEOUT_S", 0.05)
    monkeypatch.setattr(base, "FILE_SEND_TIMEOUT_S", 5.0)

    async def slow(_lease: dict) -> SendResult:
        await asyncio.sleep(0.2)
        return SendResult("sent", ext_id="e1")

    core, tmp_dir, _ = _setup(tmp_path)
    connector = _Connector(core.client(), tmp_dir, send_impl=slow)

    with_file = await connector._process_lease(_lease(file_meta(PNG_BYTES)))  # noqa: SLF001
    without_file = await connector._process_lease(_lease(None))  # noqa: SLF001

    assert with_file.outcome == "sent"
    assert without_file.outcome == "unknown"  # не уложился в обычный таймаут
    assert [r["outcome"] for r in core.reports] == ["sent", "unknown"]
