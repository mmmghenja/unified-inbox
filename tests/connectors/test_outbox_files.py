"""Ф11-3b B1 + B5 (`docs/architecture.md` и контракт A↔B): каркас отправки с
несколькими файлами — `ConnectorBase.lease_files` (байты всех файлов по `idx`, временные файлы удаляются при
любом исходе), отчёт `sent` с `ext_ids`/`grouped_id`, таймаут `send()` у задания с `files`; `fake`-коннектор —
одно эхо с N вложениями. Ядро — подменное (`FakeFileCore`).
"""

from __future__ import annotations

import asyncio

import pytest

from hub.connectors import base
from hub.connectors.base import ConnectorBase, OutboxFileError, SendResult, lease_files_meta
from hub.connectors.fake import FakeConnector, FakeSource

from .outbox_file_fakes import PDF_BYTES, PNG_BYTES, FakeFileCore, file_meta

THIRD = b"third-file-bytes" * 5


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


def _metas() -> list[dict]:
    return [
        file_meta(PNG_BYTES),
        file_meta(PDF_BYTES, kind="document", mime="application/pdf", name="акт.pdf"),
        file_meta(THIRD, kind="document", mime="text/plain", name="c.txt"),
    ]


def _lease(files: list[dict] | None, *, text: str = "", **extra) -> dict:
    lease = {
        "outbox_id": 7,
        "lease_gen": 3,
        "channel_ext_id": "c1",
        "text": text,
        "reply_to_ext_id": None,
        "thread_ext_id": None,
        "send_token": "tok",
        "mode": "send",
        "send_started_ts": 0,
        "attempts": 1,
        "files": files,
    }
    lease.update(extra)
    return lease


def _setup(tmp_path):
    core = FakeFileCore()
    core.files[7] = [PNG_BYTES, PDF_BYTES, THIRD]
    tmp_dir = tmp_path / "tmp-conn"
    return core, tmp_dir, _Connector(core.client(), tmp_dir)


def _left(tmp_dir) -> list:
    return list(tmp_dir.iterdir()) if tmp_dir.exists() else []


# --- тест 13: lease_files ---


async def test_lease_files_fetches_each_idx_in_order_and_removes_all(tmp_path) -> None:
    core, tmp_dir, connector = _setup(tmp_path)
    async with connector.lease_files(_lease(_metas())) as paths:
        assert [p.read_bytes() for p in paths] == [PNG_BYTES, PDF_BYTES, THIRD]
        assert all(p.parent == tmp_dir for p in paths)
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in paths)
        assert len(set(paths)) == 3  # noqa: PLR2004
    assert _left(tmp_dir) == []
    assert [(f["outbox_id"], f["lease_gen"], f["idx"]) for f in core.fetches] == [
        (7, "3", "0"),
        (7, "3", "1"),
        (7, "3", "2"),
    ]


async def test_lease_files_second_fails_is_outbox_file_error_and_nothing_left(tmp_path) -> None:
    core, tmp_dir, connector = _setup(tmp_path)
    core.idx_status[1] = 404
    with pytest.raises(OutboxFileError) as info:
        async with connector.lease_files(_lease(_metas())):
            pytest.fail("тело не должно выполняться")
    assert info.value.reason == "core_404"
    assert _left(tmp_dir) == []  # и первый (скачанный) файл удалён
    assert [f["idx"] for f in core.fetches] == ["0", "1"]  # третий уже не качали


async def test_lease_files_size_mismatch_on_last_file(tmp_path) -> None:
    _core, tmp_dir, connector = _setup(tmp_path)
    metas = _metas()
    metas[2]["size"] += 1
    with pytest.raises(OutboxFileError) as info:
        async with connector.lease_files(_lease(metas)):
            pass
    assert info.value.reason == "size_mismatch"
    assert _left(tmp_dir) == []


async def test_lease_files_removed_when_send_body_raises(tmp_path) -> None:
    _core, tmp_dir, connector = _setup(tmp_path)
    with pytest.raises(RuntimeError):
        async with connector.lease_files(_lease(_metas())) as paths:
            assert len(paths) == 3  # noqa: PLR2004
            raise RuntimeError("сбой источника посреди отправки")
    assert _left(tmp_dir) == []


async def test_lease_files_old_single_file_field_is_not_understood(tmp_path) -> None:
    """Контракт A↔B: поля `file` больше нет — каркас его не читает (ядро и коннекторы деплоятся вместе)."""
    core, _tmp_dir, connector = _setup(tmp_path)
    lease = _lease(None)
    lease["file"] = file_meta(PNG_BYTES)
    assert lease_files_meta(lease) == []
    with pytest.raises(OutboxFileError):
        async with connector.lease_files(lease):
            pass
    assert core.fetches == []


def test_lease_files_meta_keeps_only_dicts() -> None:
    assert lease_files_meta({"files": [{"a": 1}, "x", None, {"b": 2}]}) == [{"a": 1}, {"b": 2}]
    assert lease_files_meta({"files": {"a": 1}}) == []
    assert lease_files_meta({}) == []


# --- таймаут и отчёт каркаса ---


async def test_process_lease_with_files_gets_file_timeout(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(base, "_SEND_TIMEOUT_S", 0.05)
    monkeypatch.setattr(base, "FILE_SEND_TIMEOUT_S", 5.0)

    async def slow(_lease: dict) -> SendResult:
        await asyncio.sleep(0.2)
        return SendResult("sent", ext_id="e1")

    core, tmp_dir, _ = _setup(tmp_path)
    connector = _Connector(core.client(), tmp_dir, send_impl=slow)
    with_files = await connector._process_lease(_lease(_metas()))  # noqa: SLF001
    old_field = _lease(None)
    old_field["file"] = file_meta(PNG_BYTES)
    with_old_field = await connector._process_lease(old_field)  # noqa: SLF001
    assert with_files.outcome == "sent"
    assert with_old_field.outcome == "unknown"  # старое поле `file` таймаут не удлиняет


async def test_report_sent_carries_ext_ids_and_grouped_id(tmp_path) -> None:
    async def album(_lease: dict) -> SendResult:
        return SendResult("sent", ext_id="11", ext_ids=("11", "12", "13"), grouped_id="555")

    core, tmp_dir, _ = _setup(tmp_path)
    connector = _Connector(core.client(), tmp_dir, send_impl=album)
    await connector._process_lease(_lease(_metas()))  # noqa: SLF001
    (report,) = core.reports
    assert report["outcome"] == "sent"
    assert report["ext_id"] == "11"
    assert report["ext_ids"] == ["11", "12", "13"]
    assert report["grouped_id"] == "555"


async def test_report_without_album_has_no_album_fields(tmp_path) -> None:
    results = iter(
        [
            SendResult("sent", ext_id="21"),
            # не `sent` — поля альбома не уходят, даже если коннектор их заполнил
            SendResult("temporary", error="x", ext_ids=("1", "2"), grouped_id="g"),
        ]
    )

    async def impl(_lease: dict) -> SendResult:
        return next(results)

    core, tmp_dir, _ = _setup(tmp_path)
    connector = _Connector(core.client(), tmp_dir, send_impl=impl)
    await connector._process_lease(_lease(None))  # noqa: SLF001
    await connector._process_lease(_lease(None))  # noqa: SLF001
    assert [r["outcome"] for r in core.reports] == ["sent", "temporary"]
    assert all("ext_ids" not in r and "grouped_id" not in r for r in core.reports)


# --- Ф40-1 A: время источника в отчёте ---


async def test_report_sent_carries_ts_ms(tmp_path) -> None:
    async def impl(_lease: dict) -> SendResult:
        return SendResult("sent", ext_id="p" * 26, ts_ms=1_700_000_000_123)

    core, tmp_dir, _ = _setup(tmp_path)
    connector = _Connector(core.client(), tmp_dir, send_impl=impl)
    await connector._process_lease(_lease(None))  # noqa: SLF001
    (report,) = core.reports
    assert report["outcome"] == "sent"
    assert report["ext_id"] == "p" * 26
    assert report["ts_ms"] == 1_700_000_000_123


async def test_report_without_ts_ms_or_not_sent_has_no_ts_field(tmp_path) -> None:
    results = iter(
        [
            SendResult("sent", ext_id="21"),
            # не `sent` — время не уходит, даже если коннектор его заполнил
            SendResult("temporary", error="x", ts_ms=5),
        ]
    )

    async def impl(_lease: dict) -> SendResult:
        return next(results)

    core, tmp_dir, _ = _setup(tmp_path)
    connector = _Connector(core.client(), tmp_dir, send_impl=impl)
    await connector._process_lease(_lease(None))  # noqa: SLF001
    await connector._process_lease(_lease(None))  # noqa: SLF001
    assert [r["outcome"] for r in core.reports] == ["sent", "temporary"]
    assert all("ts_ms" not in r for r in core.reports)


# --- B5: fake-коннектор ---


async def test_fake_connector_sends_one_echo_with_all_attachments() -> None:
    core = FakeFileCore()
    source = FakeSource()
    connector = FakeConnector(core.client(), source)
    result = await connector.send(_lease(_metas(), text="три файла", channel_ext_id="chat"))
    assert result.outcome == "sent"
    (delivery,) = source.deliveries  # одно сообщение, а не три
    assert delivery.text == "три файла"
    assert delivery.attachments == [
        {"type": "photo", "name": "shot.png", "size": len(PNG_BYTES), "mime": "image/png"},
        {"type": "document", "name": "акт.pdf", "size": len(PDF_BYTES), "mime": "application/pdf"},
        {"type": "document", "name": "c.txt", "size": len(THIRD), "mime": "text/plain"},
    ]
    echo = source.echo_for(delivery.ext_id)
    assert echo["kind"] == "file"
    assert [a["name"] for a in echo["attachments"]] == ["shot.png", "акт.pdf", "c.txt"]
