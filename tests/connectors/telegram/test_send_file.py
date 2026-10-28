"""Ф10-4 B2 (`docs/architecture.md` + П3, П11): отправка файла в Telegram.
Ядро — подменное (`FakeFileCore`, фиксированные ответы ручки байтов), Telegram — `FakeGateway` в памяти;
`TgGateway.send_media` — с подменным `TelegramClient` (какой TL-запрос он строит). Сети нет.
"""

from __future__ import annotations

import asyncio

import pytest
from telethon import errors
from telethon.tl import types
from telethon.tl.functions.messages import SendMediaRequest

from hub.connectors.telegram import convert
from hub.connectors.telegram.connector import TelegramConnector
from hub.connectors.telegram.gateway import MediaUploadError, TgGateway

from ..outbox_file_fakes import PDF_BYTES, PNG_BYTES, FakeFileCore, file_meta
from .helpers import FakeGateway, make_dialog, make_message, make_user, ts

NOW_MS = 1_700_000_000_000
USER_ID = 42


def _setup(tmp_path, *, data: bytes = PNG_BYTES):
    core = FakeFileCore()
    core.files[7] = data
    gw = FakeGateway()
    gw.now_ms = NOW_MS
    gw.add_dialog(make_dialog(make_user(USER_ID), top_message=1))
    tmp_dir = tmp_path / "tmp-conn"
    connector = TelegramConnector(
        core.client(), gw, account_id=1, media_tmp_dir=tmp_dir, wall=lambda: NOW_MS / 1000
    )
    return connector, gw, core, tmp_dir


def _lease(file: dict | None, *, text: str = "подпись", maybe_sent: bool = False, **extra) -> dict:
    lease = {
        "outbox_id": 7,
        "lease_gen": 3,
        "channel_ext_id": str(USER_ID),
        "text": text,
        "reply_to_ext_id": None,
        "thread_ext_id": None,
        "send_token": "tok-7",
        "mode": "send",
        "send_started_ts": NOW_MS,
        "attempts": 1,
        "maybe_sent": maybe_sent,
        "known_own_ext_ids": [],
        "files": [file] if file is not None else None,
    }
    lease.update(extra)
    return lease


def _tmp_files(tmp_dir) -> list:
    return list(tmp_dir.iterdir()) if tmp_dir.exists() else []


async def test_photo_sent_with_token_random_id_and_caption(tmp_path) -> None:
    connector, gw, core, tmp_dir = _setup(tmp_path)
    result = await connector.send(_lease(file_meta(PNG_BYTES)))

    assert result.outcome == "sent"
    assert len(gw.media_calls) == 1
    call = gw.media_calls[0]
    assert call["kind"] == "photo"
    assert call["random_id"] == convert.random_id_from_token("tok-7")
    assert call["text"] == "подпись"
    assert call["data"] == PNG_BYTES  # байты из ядра, целиком
    assert call["force_file"] is False
    assert result.ext_id == gw.sent[0]["ext_id"]
    assert core.fetches == [{"outbox_id": 7, "lease_gen": "3", "idx": "0", "authorization": "Bearer s"}]
    assert _tmp_files(tmp_dir) == []  # временный файл удалён


async def test_document_sent_with_name_and_mime(tmp_path) -> None:
    connector, gw, _core, tmp_dir = _setup(tmp_path, data=PDF_BYTES)
    meta = file_meta(PDF_BYTES, kind="document", mime="application/pdf", name="акт.pdf")
    result = await connector.send(_lease(meta, text=""))

    assert result.outcome == "sent"
    call = gw.media_calls[0]
    assert (call["kind"], call["name"], call["mime"]) == ("document", "акт.pdf", "application/pdf")
    assert call["text"] == ""
    assert call["force_file"] is False
    assert _tmp_files(tmp_dir) == []


async def test_big_picture_as_document_is_forced_file(tmp_path) -> None:
    connector, gw, _core, _tmp = _setup(tmp_path)
    meta = file_meta(PNG_BYTES, kind="document", mime="image/png")
    assert (await connector.send(_lease(meta))).outcome == "sent"
    assert gw.media_calls[0]["force_file"] is True


async def test_caption_over_1024_utf16_is_permanent_without_requests(tmp_path) -> None:
    connector, gw, core, _tmp = _setup(tmp_path)
    caption = "😀" * 513  # 513 символов Python, 1026 единиц UTF-16
    result = await connector.send(_lease(file_meta(PNG_BYTES), text=caption))

    assert result.outcome == "permanent"
    assert result.error == "подпись длиннее 1024"
    assert gw.media_calls == []
    assert core.fetches == []


async def test_caption_exactly_1024_utf16_is_sent(tmp_path) -> None:
    connector, gw, _core, _tmp = _setup(tmp_path)
    result = await connector.send(_lease(file_meta(PNG_BYTES), text="😀" * 512))
    assert result.outcome == "sent"
    assert len(gw.media_calls) == 1


async def test_maybe_sent_finds_own_media_message_without_resend(tmp_path) -> None:
    connector, gw, core, _tmp = _setup(tmp_path)
    peer = types.PeerUser(USER_ID)
    # Своё ТЕКСТОВОЕ сообщение с той же подписью — не наше (нужно сообщение с медиа).
    gw.add_message(USER_ID, make_message(500, peer, message="подпись", out=True, date_ms=NOW_MS))
    media = types.MessageMediaPhoto(photo=types.PhotoEmpty(id=1))
    gw.add_message(
        USER_ID, make_message(501, peer, message="подпись", out=True, date_ms=NOW_MS, media=media)
    )

    result = await connector.send(_lease(file_meta(PNG_BYTES), maybe_sent=True))

    assert (result.outcome, result.ext_id) == ("sent", "501")
    assert gw.media_calls == []
    assert core.fetches == []


async def test_maybe_sent_empty_caption_matches_only_media(tmp_path) -> None:
    connector, gw, _core, _tmp = _setup(tmp_path)
    peer = types.PeerUser(USER_ID)
    gw.add_message(USER_ID, make_message(600, peer, message="", out=True, date_ms=NOW_MS))
    result = await connector.send(_lease(file_meta(PNG_BYTES), text="", maybe_sent=True))
    # Текстового «пустого» совпадения нет -> файл отправляется.
    assert result.outcome == "sent"
    assert len(gw.media_calls) == 1


@pytest.mark.parametrize(
    "exc",
    [
        errors.PhotoInvalidDimensionsError(request=None),
        errors.ImageProcessFailedError(request=None),
        errors.PhotoExtInvalidError(request=None),
        errors.PhotoSaveFileInvalidError(request=None),
        errors.MediaInvalidError(request=None),
        errors.FilePartsInvalidError(request=None),
    ],
)
async def test_photo_rejected_retries_once_as_forced_document(tmp_path, exc) -> None:
    connector, gw, _core, tmp_dir = _setup(tmp_path)
    gw.media_faults.append(exc)
    result = await connector.send(_lease(file_meta(PNG_BYTES)))

    assert result.outcome == "sent"
    assert [(c["kind"], c["force_file"]) for c in gw.media_calls] == [("photo", False), ("document", True)]
    assert gw.media_calls[1]["name"] == "shot.png"
    assert gw.media_calls[1]["data"] == PNG_BYTES
    assert _tmp_files(tmp_dir) == []


async def test_photo_retry_as_document_only_once(tmp_path) -> None:
    connector, gw, _core, _tmp = _setup(tmp_path)
    gw.media_faults.extend(
        [errors.PhotoInvalidDimensionsError(request=None), errors.MediaInvalidError(request=None)]
    )
    result = await connector.send(_lease(file_meta(PNG_BYTES)))
    assert result.outcome == "permanent"
    assert result.error == "MediaInvalidError"
    assert len(gw.media_calls) == 2


async def test_document_media_invalid_is_permanent_without_retry(tmp_path) -> None:
    connector, gw, _core, _tmp = _setup(tmp_path, data=PDF_BYTES)
    gw.media_faults.append(errors.MediaInvalidError(request=None))
    meta = file_meta(PDF_BYTES, kind="document", mime="application/pdf", name="a.pdf")
    result = await connector.send(_lease(meta))
    assert result.outcome == "permanent"
    assert len(gw.media_calls) == 1


async def test_upload_failure_before_send_media_is_temporary(tmp_path) -> None:
    connector, gw, _core, tmp_dir = _setup(tmp_path)
    gw.media_faults.append(("upload", ConnectionError("сеть")))
    result = await connector.send(_lease(file_meta(PNG_BYTES)))
    assert result.outcome == "temporary"
    assert result.error == "ConnectionError"
    assert gw.sent == []
    assert _tmp_files(tmp_dir) == []


async def test_upload_flood_wait_is_flood(tmp_path) -> None:
    connector, gw, _core, _tmp = _setup(tmp_path)
    gw.media_faults.append(("upload", errors.FloodWaitError(request=None, capture=12)))
    result = await connector.send(_lease(file_meta(PNG_BYTES)))
    assert (result.outcome, result.retry_after_ms) == ("flood", 12_000)


async def test_send_media_flood_wait_is_flood(tmp_path) -> None:
    connector, gw, _core, _tmp = _setup(tmp_path)
    gw.media_faults.append(errors.FloodWaitError(request=None, capture=5))
    result = await connector.send(_lease(file_meta(PNG_BYTES)))
    assert (result.outcome, result.retry_after_ms) == ("flood", 5_000)


async def test_network_after_send_media_is_abandon_and_file_removed(tmp_path) -> None:
    connector, gw, _core, tmp_dir = _setup(tmp_path)
    gw.media_faults.append("network_after")
    result = await connector.send(_lease(file_meta(PNG_BYTES)))
    assert result.outcome == "abandon"  # идемпотентный источник: лиз истечёт, повтор найдёт в истории
    assert _tmp_files(tmp_dir) == []

    # Повтор после истёкшего лиза: `maybe_sent` -> своё сообщение с медиа найдено, второй отправки нет.
    again = await connector.send(_lease(file_meta(PNG_BYTES), maybe_sent=True, send_started_ts=NOW_MS))
    assert (again.outcome, again.ext_id) == ("sent", gw.sent[0]["ext_id"])
    assert len(gw.sent) == 1


async def test_random_id_duplicate_finds_media_message(tmp_path) -> None:
    connector, gw, _core, _tmp = _setup(tmp_path)
    first = await connector.send(_lease(file_meta(PNG_BYTES)))
    second = await connector.send(_lease(file_meta(PNG_BYTES)))  # тот же токен -> тот же random_id
    assert (second.outcome, second.ext_id) == ("sent", first.ext_id)
    assert len(gw.sent) == 1


async def test_answer_without_id_searches_history(tmp_path) -> None:
    connector, gw, _core, _tmp = _setup(tmp_path)
    gw.media_result_without_id = True
    result = await connector.send(_lease(file_meta(PNG_BYTES)))
    assert (result.outcome, result.ext_id) == ("sent", gw.sent[0]["ext_id"])


@pytest.mark.parametrize("status", [404, 409])
async def test_core_refuses_bytes_is_temporary(tmp_path, status) -> None:
    connector, gw, core, tmp_dir = _setup(tmp_path)
    core.status = status
    result = await connector.send(_lease(file_meta(PNG_BYTES)))
    assert result.outcome == "temporary"
    assert result.error == f"core_{status}"
    assert gw.media_calls == []
    assert _tmp_files(tmp_dir) == []


async def test_size_mismatch_with_lease_is_temporary(tmp_path) -> None:
    connector, gw, _core, tmp_dir = _setup(tmp_path)
    meta = file_meta(PNG_BYTES)
    meta["size"] += 1
    result = await connector.send(_lease(meta))
    assert (result.outcome, result.error) == ("temporary", "size_mismatch")
    assert gw.media_calls == []
    assert _tmp_files(tmp_dir) == []


async def test_peer_resolve_network_error_is_temporary_not_abandon(tmp_path) -> None:
    connector, gw, _core, _tmp = _setup(tmp_path)

    async def boom(_marked_id):
        raise ConnectionError("сеть")

    gw.get_input_entity = boom
    result = await connector.send(_lease(file_meta(PNG_BYTES)))
    assert result.outcome == "temporary"


async def test_unexpected_error_still_removes_temp_file(tmp_path) -> None:
    connector, gw, _core, tmp_dir = _setup(tmp_path)
    gw.media_faults.append(RuntimeError("дефект"))
    with pytest.raises(RuntimeError):
        await connector.send(_lease(file_meta(PNG_BYTES)))
    assert _tmp_files(tmp_dir) == []


async def test_framework_timeout_cancels_and_removes_temp_file(tmp_path, monkeypatch) -> None:
    connector, gw, core, tmp_dir = _setup(tmp_path)
    monkeypatch.setattr("hub.connectors.base.FILE_SEND_TIMEOUT_S", 0.05)

    async def hang(*_a, **_kw):
        await asyncio.sleep(10)

    gw.send_media = hang
    result = await connector._process_lease(_lease(file_meta(PNG_BYTES)))  # noqa: SLF001
    assert result is not None
    assert result.outcome == "abandon"
    assert _tmp_files(tmp_dir) == []
    assert core.reports == []  # abandon — отчёта нет, лиз истечёт сам


# --- TgGateway.send_media: какой TL-запрос уходит -----------------------------------------------------------


class _FakeTelegramClient:
    def __init__(self, *_a, **_kw) -> None:
        self.uploads: list[tuple] = []
        self.requests: list = []
        self.upload_error: Exception | None = None

    async def upload_file(self, file, *, file_name=None):
        if self.upload_error is not None:
            raise self.upload_error
        self.uploads.append((file, file_name))
        return types.InputFile(id=1, parts=1, name=file_name or "x", md5_checksum="")

    async def __call__(self, request):
        self.requests.append(request)
        return types.Updates(updates=[], users=[], chats=[], date=ts(NOW_MS), seq=0)


def _tg_gateway(tmp_path) -> tuple[TgGateway, _FakeTelegramClient]:
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


async def test_tg_gateway_photo_request(tmp_path) -> None:
    gw, client = _tg_gateway(tmp_path)
    peer = types.InputPeerUser(USER_ID, 0)
    reply_to = types.InputReplyToMessage(reply_to_msg_id=9)
    await gw.send_media(
        peer, path="/p", kind="photo", name="shot.png", mime="image/png", text="cap", random_id=77,
        reply_to=reply_to,
    )
    assert client.uploads == [("/p", "shot.png")]
    (req,) = client.requests
    assert isinstance(req, SendMediaRequest)
    assert isinstance(req.media, types.InputMediaUploadedPhoto)
    assert (req.random_id, req.message, req.reply_to) == (77, "cap", reply_to)


async def test_tg_gateway_document_request(tmp_path) -> None:
    gw, client = _tg_gateway(tmp_path)
    peer = types.InputPeerUser(USER_ID, 0)
    await gw.send_media(
        peer, path="/p", kind="document", name="акт.pdf", mime="application/pdf", text="", random_id=5
    )
    await gw.send_media(
        peer, path="/p", kind="document", name="a.png", mime="image/png", text="", random_id=6,
        force_file=True,
    )
    doc, forced = client.requests
    assert isinstance(doc.media, types.InputMediaUploadedDocument)
    assert doc.media.mime_type == "application/pdf"
    assert [a.file_name for a in doc.media.attributes] == ["акт.pdf"]
    assert not doc.media.force_file
    assert forced.media.force_file is True


async def test_tg_gateway_upload_failure_is_media_upload_error(tmp_path) -> None:
    gw, client = _tg_gateway(tmp_path)
    client.upload_error = errors.FloodWaitError(request=None, capture=3)
    with pytest.raises(MediaUploadError) as info:
        await gw.send_media(
            types.InputPeerUser(USER_ID, 0), path="/p", kind="photo", name="a", mime=None, text="",
            random_id=1,
        )
    assert isinstance(info.value.cause, errors.FloodWaitError)
    assert client.requests == []
