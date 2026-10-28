"""Ф8-5 (5b, C1 + П4/П10/П13/П15): вложения Telegram (поля для строк `media`) и загрузчик — T11, T13."""

from __future__ import annotations

import asyncio
from collections import deque

from telethon import errors, utils
from telethon.tl import types

from hub.connectors.media import MediaSink
from hub.connectors.telegram import convert
from hub.connectors.telegram.connector import TelegramConnector

from ..media_fakes import FakeMediaCore, media_job
from . import helpers as h
from .helpers import FakeGateway

PAYLOAD = b"\xff\xd8\xff" + b"J" * 5000


def make_photo(photo_id: int = 77) -> types.Photo:
    return types.Photo(
        id=photo_id,
        access_hash=0,
        file_reference=b"",
        date=None,
        sizes=[
            types.PhotoStrippedSize("i", b"xx"),
            types.PhotoSize("m", 320, 240, 1000),
            types.PhotoSizeProgressive("y", 1280, 960, [100, 5000, 20000]),
        ],
        dc_id=2,
    )


def make_document(doc_id: int, mime: str, attributes: list, size: int = 12345) -> types.Document:
    return types.Document(
        id=doc_id,
        access_hash=0,
        file_reference=b"",
        date=None,
        mime_type=mime,
        size=size,
        dc_id=2,
        attributes=attributes,
    )


# --- конвертация (C1, П10, П13) -----------------------------------------------------------------------------


def test_photo_largest_size_and_src_id():
    [item] = convert.convert_attachments(types.MessageMediaPhoto(photo=make_photo(77)))
    assert item == {"type": "photo", "width": 1280, "height": 960, "size": 20000, "src_id": "77"}


def test_video_document_fields():
    doc = make_document(
        9,
        "video/mp4",
        [
            types.DocumentAttributeVideo(duration=12.7, w=640, h=360),
            types.DocumentAttributeFilename("clip.mp4"),
        ],
        size=3_000_000,
    )
    [item] = convert.convert_attachments(types.MessageMediaDocument(document=doc))
    assert item == {
        "type": "video",
        "size": 3_000_000,
        "mime": "video/mp4",
        "name": "clip.mp4",
        "width": 640,
        "height": 360,
        "duration_s": 12,
        "src_id": "9",
    }


def test_voice_duration():
    doc = make_document(10, "audio/ogg", [types.DocumentAttributeAudio(duration=7, voice=True)])
    [item] = convert.convert_attachments(types.MessageMediaDocument(document=doc))
    assert item["type"] == "voice" and item["duration_s"] == 7 and item["src_id"] == "10"


def test_stickers_three_formats():
    sattr = types.DocumentAttributeSticker(alt="😀", stickerset=types.InputStickerSetEmpty())
    tgs = make_document(
        20, "application/x-tgsticker", [sattr, types.DocumentAttributeFilename("AnimatedSticker.tgs")]
    )
    webm = make_document(21, "video/webm", [sattr, types.DocumentAttributeVideo(duration=2, w=512, h=512)])
    webp = make_document(22, "image/webp", [sattr])
    [t] = convert.convert_attachments(types.MessageMediaDocument(document=tgs))
    [m] = convert.convert_attachments(types.MessageMediaDocument(document=webm))
    [w] = convert.convert_attachments(types.MessageMediaDocument(document=webp))
    assert (t["type"], t["sticker_format"], t["emoji"]) == ("sticker", "animated", "😀")
    assert (m["type"], m["sticker_format"], m["emoji"]) == ("sticker", "video", "😀")
    assert (w["type"], w["sticker_format"]) == ("sticker", "static")


def test_webp_sticker_is_sticker_tgs_is_sticker():
    webp = make_document(
        11,
        "image/webp",
        [
            types.DocumentAttributeImageSize(512, 512),
            types.DocumentAttributeSticker(alt="x", stickerset=types.InputStickerSetEmpty()),
        ],
    )
    tgs = make_document(
        12,
        "application/x-tgsticker",
        [types.DocumentAttributeSticker(alt="x", stickerset=types.InputStickerSetEmpty())],
    )
    [w] = convert.convert_attachments(types.MessageMediaDocument(document=webp))
    [t] = convert.convert_attachments(types.MessageMediaDocument(document=tgs))
    assert w["type"] == "sticker" and w["width"] == 512 and w["height"] == 512
    assert t["type"] == "sticker" and t["sticker_format"] == "animated"


def test_ttl_media_marked_ttl():
    [p] = convert.convert_attachments(types.MessageMediaPhoto(photo=make_photo(), ttl_seconds=10))
    doc = make_document(13, "video/mp4", [types.DocumentAttributeVideo(duration=1, w=1, h=1)])
    [v] = convert.convert_attachments(types.MessageMediaDocument(document=doc, ttl_seconds=10))
    assert p["ttl"] is True and v["ttl"] is True
    [plain] = convert.convert_attachments(types.MessageMediaPhoto(photo=make_photo()))
    assert "ttl" not in plain


def test_no_file_kinds_have_no_src_id():
    [geo] = convert.convert_attachments(types.MessageMediaGeo(geo=types.GeoPointEmpty()))
    assert geo == {"type": "location"}


def test_convert_is_deterministic():
    media = types.MessageMediaPhoto(photo=make_photo())
    assert convert.convert_attachments(media) == convert.convert_attachments(media)


# --- загрузчик (T11) ----------------------------------------------------------------------------------------


class MediaGateway(FakeGateway):
    def __init__(self) -> None:
        super().__init__()
        self.by_ids_calls: list[tuple[int, list[int]]] = []
        self.downloads: list[int] = []
        self.download_faults: deque = deque()
        self.block: asyncio.Event | None = None
        self.started = asyncio.Event()

    async def get_messages_by_ids(self, peer, ids):
        marked = utils.get_peer_id(peer)
        self.by_ids_calls.append((marked, list(ids)))
        by_id = {m.id: m for m in self.history.get(marked, [])}
        return [by_id.get(i) for i in ids]

    async def download_media(self, message, file):
        self.downloads.append(message.id)
        self.started.set()
        if self.download_faults:
            fault = self.download_faults.popleft()
            file.write(b"partial")
            raise fault
        if self.block is not None:
            await self.block.wait()
        if message.media is None:
            return None
        file.write(PAYLOAD[:1000])
        file.write(PAYLOAD[1000:])
        return file


USER = h.make_user(42, first_name="Петя")


def setup(tmp_path, *, with_message: bool = True, media=None):
    core = FakeMediaCore()
    gw = MediaGateway()
    gw.add_dialog(h.make_dialog(USER, top_message=5))
    if with_message:
        media = media if media is not None else types.MessageMediaPhoto(photo=make_photo(77))
        gw.add_message(42, h.make_message(5, h.user_peer(USER), message=None, media=media))
    conn = TelegramConnector(core.client(), gw, account_id=7, media_tmp_dir=tmp_path / "tmp-conn")
    return core, gw, conn


async def test_t11_job_rereads_message_and_puts_bytes(tmp_path):
    core, gw, conn = setup(tmp_path)
    core.jobs = [media_job(3, channel_ext_id="42", ext_id="5", src_id="77")]
    await conn._media.step()
    assert gw.by_ids_calls == [(42, [5])]
    assert core.puts[0]["body"] == PAYLOAD
    assert core.puts[0]["content_length"] == str(len(PAYLOAD))
    assert core.reports == []


async def test_t11_topic_channel_uses_forum_peer(tmp_path):
    core = FakeMediaCore()
    gw = MediaGateway()
    forum = h.make_channel(500, forum=True, megagroup=True)
    marked = utils.get_peer_id(h.channel_peer(forum))
    gw.seed_peer(marked, h.input_peer_of(forum))
    gw.add_message(marked, h.make_message(9, h.channel_peer(forum), message=None,
                                          media=types.MessageMediaPhoto(photo=make_photo(1))))
    conn = TelegramConnector(core.client(), gw, account_id=7, media_tmp_dir=tmp_path / "t")
    core.jobs = [media_job(1, channel_ext_id=f"{marked}:3", ext_id="9")]
    await conn._media.step()
    assert gw.by_ids_calls == [(marked, [9])]
    assert core.puts[0]["body"] == PAYLOAD


async def test_t11_no_message_is_gone(tmp_path):
    core, gw, conn = setup(tmp_path, with_message=False)
    core.jobs = [media_job(3, channel_ext_id="42", ext_id="5")]
    await conn._media.step()
    assert core.puts == []
    assert core.reports[0]["outcome"] == "gone"
    assert gw.downloads == []


async def test_t11_message_without_media_is_gone(tmp_path):
    core = FakeMediaCore()
    gw = MediaGateway()
    gw.add_dialog(h.make_dialog(USER, top_message=5))
    gw.add_message(42, h.make_message(5, h.user_peer(USER), message="text"))
    conn = TelegramConnector(core.client(), gw, account_id=7, media_tmp_dir=tmp_path / "t")
    core.jobs = [media_job(3, channel_ext_id="42", ext_id="5")]
    await conn._media.step()
    assert core.reports[0]["outcome"] == "gone"


async def test_p10_changed_media_is_gone(tmp_path):
    core, gw, conn = setup(tmp_path)
    core.jobs = [media_job(3, channel_ext_id="42", ext_id="5", src_id="1234")]
    await conn._media.step()
    assert core.reports[0]["outcome"] == "gone"
    assert core.reports[0]["error"] == "src_changed"
    assert gw.downloads == []


async def test_p13_ttl_media_is_not_downloaded(tmp_path):
    core, gw, conn = setup(
        tmp_path, media=types.MessageMediaPhoto(photo=make_photo(77), ttl_seconds=10)
    )
    core.jobs = [media_job(3, channel_ext_id="42", ext_id="5")]
    await conn._media.step()
    assert core.reports[0]["outcome"] == "gone"
    assert gw.downloads == []


async def test_t11_flood_wait_is_temporary_with_retry_after_and_pause(tmp_path):
    core, gw, conn = setup(tmp_path)
    gw.download_faults.append(errors.FloodWaitError(request=None, capture=90))
    core.jobs = [media_job(3, channel_ext_id="42", ext_id="5")]
    await conn._media.step()
    assert core.reports[0]["outcome"] == "temporary"
    assert core.reports[0]["retry_after_s"] == 90.0
    assert conn._media.paused_until is not None
    assert conn._media.paused_until - conn._mono() > 80


async def test_t11_file_reference_expired_rereads_and_retries_once(tmp_path):
    core, gw, conn = setup(tmp_path)
    gw.download_faults.append(errors.FileReferenceExpiredError(request=None))
    core.jobs = [media_job(3, channel_ext_id="42", ext_id="5")]
    await conn._media.step()
    assert gw.by_ids_calls == [(42, [5]), (42, [5])]  # перечитано заново
    assert gw.downloads == [5, 5]
    assert core.puts[0]["body"] == PAYLOAD  # недокачанный кусок первой попытки отброшен
    assert core.reports == []


async def test_t11_file_reference_expired_twice_is_temporary(tmp_path):
    core, gw, conn = setup(tmp_path)
    gw.download_faults.extend(
        [errors.FileReferenceExpiredError(request=None), errors.FileReferenceExpiredError(request=None)]
    )
    core.jobs = [media_job(3, channel_ext_id="42", ext_id="5")]
    await conn._media.step()
    assert gw.downloads == [5, 5]
    assert core.puts == []
    assert core.reports[0]["outcome"] == "temporary"


async def test_p15_peer_cache_miss_is_temporary_without_dialogs_pass(tmp_path):
    core, gw, conn = setup(tmp_path)
    gw.forget_peer(42)
    calls_before = gw.iter_dialogs_calls
    core.jobs = [media_job(3, channel_ext_id="42", ext_id="5")]
    await conn._media.step()
    assert core.reports[0]["outcome"] == "temporary"
    assert core.reports[0]["error"] == "peer_unresolved"
    assert gw.iter_dialogs_calls == calls_before


async def test_channel_private_is_gone(tmp_path):
    core, gw, conn = setup(tmp_path)
    gw.download_faults.append(errors.ChannelPrivateError(request=None))
    core.jobs = [media_job(3, channel_ext_id="42", ext_id="5")]
    await conn._media.step()
    assert core.reports[0]["outcome"] == "gone"


async def test_sink_reset_drops_partial():
    import io

    buf = io.BytesIO()
    sink = MediaSink(buf, 100)
    sink.write(b"abc")
    sink.reset()
    sink.write(b"xy")
    assert buf.getvalue() == b"xy" and sink.written == 2


# --- T13: загрузчик не мешает отправке и сверке ---------------------------------------------------------


async def test_t13_long_download_does_not_block_send_and_sync(tmp_path):
    core, gw, conn = setup(tmp_path)
    conn._wall = lambda: 1_700_000_000.0  # фейковые настенные часы (лимитер отправки)
    gw.block = asyncio.Event()
    core.jobs = [media_job(3, channel_ext_id="42", ext_id="5")]
    core.outbox_leases = [
        {
            "outbox_id": 1,
            "lease_gen": 1,
            "mode": "send",
            "channel_ext_id": "42",
            "text": "привет",
            "reply_to_ext_id": None,
            "send_token": "tok",
            "send_started_ts": 1_700_000_000_000,
        }
    ]
    task = asyncio.ensure_future(conn._media.run())
    try:
        await asyncio.wait_for(gw.started.wait(), 5)  # загрузка началась и висит

        processed = await asyncio.wait_for(conn.step_outbox(), 5)
        assert processed == 1
        assert gw.delivered_count("привет") == 1
        assert core.outbox_reports[0]["outcome"] == "sent"

        conn._me_id = h.ME_ID
        await asyncio.wait_for(conn.sync("scheduled"), 5)  # сверка тоже проходит

        assert not task.done() and core.puts == []  # загрузка всё ещё идёт
        gw.block.set()
        for _ in range(200):
            await asyncio.sleep(0.01)
            if core.puts:
                break
        assert core.puts[0]["body"] == PAYLOAD
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
