"""Ф11-3b B2 (`docs/architecture.md` + П1, П4; тесты 14–15): альбом Telegram —
2–10 файлов одним `SendMultiMediaRequest`. Ядро — подменное (`FakeFileCore`), Telegram — `FakeGateway` в
памяти; `TgGateway.send_album` — с подменным `TelegramClient` (какие TL-запросы он строит). Сети нет.
"""

from __future__ import annotations

import pytest
from telethon import errors
from telethon.tl import types
from telethon.tl.functions.messages import SendMultiMediaRequest, UploadMediaRequest

from hub.connectors.telegram import convert
from hub.connectors.telegram.connector import TelegramConnector
from hub.connectors.telegram.gateway import AlbumItem, MediaUploadError, TgGateway

from ..outbox_file_fakes import PDF_BYTES, PNG_BYTES, FakeFileCore, file_meta
from .helpers import FakeGateway, make_dialog, make_message, make_user, ts

NOW_MS = 1_700_000_000_000
USER_ID = 42
TOKEN = "tok-7"
PNG2 = PNG_BYTES + b"-second"
PNG3 = PNG_BYTES + b"-third!"


def _photos() -> list[dict]:
    return [
        file_meta(PNG_BYTES, name="a.png"),
        file_meta(PNG2, name="b.png"),
        file_meta(PNG3, name="c.png"),
    ]


def _setup(tmp_path, *, data: list[bytes] | None = None):
    core = FakeFileCore()
    core.files[7] = data if data is not None else [PNG_BYTES, PNG2, PNG3]
    gw = FakeGateway()
    gw.now_ms = NOW_MS
    gw.add_dialog(make_dialog(make_user(USER_ID), top_message=1))
    tmp_dir = tmp_path / "tmp-conn"
    connector = TelegramConnector(
        core.client(), gw, account_id=1, media_tmp_dir=tmp_dir, wall=lambda: NOW_MS / 1000
    )
    return connector, gw, core, tmp_dir


def _lease(files: list[dict], *, text: str = "подпись", maybe_sent: bool = False, **extra) -> dict:
    lease = {
        "outbox_id": 7,
        "lease_gen": 3,
        "channel_ext_id": str(USER_ID),
        "text": text,
        "reply_to_ext_id": None,
        "thread_ext_id": None,
        "send_token": TOKEN,
        "mode": "send",
        "send_started_ts": NOW_MS,
        "attempts": 1,
        "maybe_sent": maybe_sent,
        "known_own_ext_ids": [],
        "files": files,
    }
    lease.update(extra)
    return lease


def _tmp_files(tmp_dir) -> list:
    return list(tmp_dir.iterdir()) if tmp_dir.exists() else []


def _random_ids() -> list[int]:
    return [
        convert.random_id_from_token(TOKEN),
        convert.random_id_from_token(f"{TOKEN}:1"),
        convert.random_id_from_token(f"{TOKEN}:2"),
    ]


def _album_msgs(
    peer, first_id: int, n: int, *, grouped_id: int, caption: str = "подпись", date_ms: int = NOW_MS
):
    media = types.MessageMediaPhoto(photo=types.PhotoEmpty(id=1))
    return [
        make_message(
            first_id + k, peer, message=caption if k == 0 else "", out=True, date_ms=date_ms, media=media,
            grouped_id=grouped_id,
        )
        for k in range(n)
    ]


# --- тест 14: отправка альбома ---


async def test_album_one_request_random_ids_caption_and_report_ids(tmp_path) -> None:
    connector, gw, core, tmp_dir = _setup(tmp_path)
    result = await connector.send(_lease(_photos()))

    assert gw.media_calls == []  # не по одному SendMedia
    (call,) = gw.album_calls
    assert call["random_ids"] == _random_ids()
    assert call["text"] == "подпись"
    assert [i["data"] for i in call["items"]] == [PNG_BYTES, PNG2, PNG3]
    assert [(i["kind"], i["name"], i["force_file"]) for i in call["items"]] == [
        ("photo", "a.png", False),
        ("photo", "b.png", False),
        ("photo", "c.png", False),
    ]
    sent_ids = [s["ext_id"] for s in gw.sent]
    assert result.outcome == "sent"
    assert result.ext_id == sent_ids[0]
    assert result.ext_ids == tuple(sent_ids)
    assert result.grouped_id == str(gw.sent[0]["grouped_id"])
    assert [f["idx"] for f in core.fetches] == ["0", "1", "2"]
    assert _tmp_files(tmp_dir) == []


async def test_album_report_goes_to_core_with_ext_ids(tmp_path) -> None:
    connector, gw, core, _tmp = _setup(tmp_path)
    await connector._process_lease(_lease(_photos()))  # noqa: SLF001
    (report,) = core.reports
    assert report["outcome"] == "sent"
    assert report["ext_ids"] == [s["ext_id"] for s in gw.sent]
    assert report["ext_ids"][0] == report["ext_id"]
    assert report["grouped_id"] == str(gw.sent[0]["grouped_id"])


async def test_documents_album_image_documents_are_forced_files(tmp_path) -> None:
    files = [
        file_meta(PDF_BYTES, kind="document", mime="application/pdf", name="акт.pdf"),
        file_meta(PNG_BYTES, kind="document", mime="image/png", name="big.png"),
    ]
    connector, gw, _core, _tmp = _setup(tmp_path, data=[PDF_BYTES, PNG_BYTES])
    result = await connector.send(_lease(files, text=""))
    assert result.outcome == "sent"
    (call,) = gw.album_calls
    assert [(i["kind"], i["force_file"]) for i in call["items"]] == [("document", False), ("document", True)]
    assert call["text"] == ""


async def test_single_file_still_uses_send_media(tmp_path) -> None:
    connector, gw, _core, _tmp = _setup(tmp_path, data=[PNG_BYTES])
    result = await connector.send(_lease([file_meta(PNG_BYTES)]))
    assert result.outcome == "sent"
    assert len(gw.media_calls) == 1
    assert gw.album_calls == []
    assert result.ext_ids is None


async def test_mixed_album_is_permanent_without_requests(tmp_path) -> None:
    connector, gw, core, _tmp = _setup(tmp_path, data=[PNG_BYTES, PDF_BYTES])
    pdf = file_meta(PDF_BYTES, kind="document", mime="application/pdf", name="a.pdf")
    files = [file_meta(PNG_BYTES), pdf]
    result = await connector.send(_lease(files))
    assert result.outcome == "permanent"
    assert "смешанный альбом" in result.error
    assert gw.album_calls == [] and gw.media_calls == []
    assert core.fetches == []


async def test_album_caption_over_1024_is_permanent(tmp_path) -> None:
    connector, gw, core, _tmp = _setup(tmp_path)
    result = await connector.send(_lease(_photos(), text="😀" * 513))
    assert (result.outcome, result.error) == ("permanent", "подпись длиннее 1024")
    assert gw.album_calls == []
    assert core.fetches == []


async def test_upload_media_failure_is_temporary(tmp_path) -> None:
    connector, gw, _core, tmp_dir = _setup(tmp_path)
    gw.album_faults.append(("upload_media", ConnectionError("сеть")))
    result = await connector.send(_lease(_photos()))
    assert (result.outcome, result.error) == ("temporary", "ConnectionError")
    assert gw.sent == []
    assert len(gw.album_calls) == 1
    assert _tmp_files(tmp_dir) == []


async def test_upload_file_flood_is_flood(tmp_path) -> None:
    connector, gw, _core, _tmp = _setup(tmp_path)
    gw.album_faults.append(("upload_file", errors.FloodWaitError(request=None, capture=9)))
    result = await connector.send(_lease(_photos()))
    assert (result.outcome, result.retry_after_ms) == ("flood", 9_000)


async def test_photo_rejected_by_upload_media_retries_album_as_documents_once(tmp_path) -> None:
    """П1: фото проверяет `UploadMediaRequest` — его отказ «фото не принято» -> повтор всего альбома
    документами с теми же `random_id`."""
    connector, gw, _core, tmp_dir = _setup(tmp_path)
    gw.album_faults.append(("upload_media", errors.PhotoInvalidDimensionsError(request=None)))
    result = await connector.send(_lease(_photos()))

    assert result.outcome == "sent"
    first, second = gw.album_calls
    assert [i["kind"] for i in first["items"]] == ["photo"] * 3
    assert [(i["kind"], i["force_file"]) for i in second["items"]] == [("document", True)] * 3
    assert second["random_ids"] == first["random_ids"] == _random_ids()
    assert [i["data"] for i in second["items"]] == [PNG_BYTES, PNG2, PNG3]
    assert second["text"] == "подпись"
    assert _tmp_files(tmp_dir) == []


async def test_photo_error_on_upload_file_stage_is_not_retried(tmp_path) -> None:
    connector, gw, _core, _tmp = _setup(tmp_path)
    gw.album_faults.append(("upload_file", errors.PhotoInvalidDimensionsError(request=None)))
    result = await connector.send(_lease(_photos()))
    assert result.outcome == "temporary"
    assert len(gw.album_calls) == 1


async def test_retry_as_documents_only_once(tmp_path) -> None:
    connector, gw, _core, _tmp = _setup(tmp_path)
    gw.album_faults.extend(
        [
            ("upload_media", errors.PhotoInvalidDimensionsError(request=None)),
            errors.MediaInvalidError(request=None),
        ]
    )
    result = await connector.send(_lease(_photos()))
    assert (result.outcome, result.error) == ("permanent", "MediaInvalidError")
    assert len(gw.album_calls) == 2  # noqa: PLR2004


async def test_documents_album_media_invalid_is_permanent_without_retry(tmp_path) -> None:
    files = [
        file_meta(PDF_BYTES, kind="document", mime="application/pdf", name="a.pdf"),
        file_meta(PDF_BYTES, kind="document", mime="application/pdf", name="b.pdf"),
    ]
    connector, gw, _core, _tmp = _setup(tmp_path, data=[PDF_BYTES, PDF_BYTES])
    gw.album_faults.append(("upload_media", errors.MediaInvalidError(request=None)))
    result = await connector.send(_lease(files))
    assert result.outcome == "permanent"
    assert len(gw.album_calls) == 1


async def test_random_id_duplicate_finds_album_in_history(tmp_path) -> None:
    connector, gw, _core, _tmp = _setup(tmp_path)
    first = await connector.send(_lease(_photos()))
    second = await connector.send(_lease(_photos()))  # тот же токен -> те же random_id
    assert second.outcome == "sent"
    assert second.ext_ids == first.ext_ids
    assert second.grouped_id == first.grouped_id
    assert len(gw.sent) == 3  # noqa: PLR2004 — одна доставка из трёх сообщений


async def test_network_after_send_is_abandon_then_maybe_sent_finds_album(tmp_path) -> None:
    connector, gw, core, tmp_dir = _setup(tmp_path)
    gw.album_faults.append("network_after")
    result = await connector.send(_lease(_photos()))
    assert result.outcome == "abandon"
    assert _tmp_files(tmp_dir) == []

    fetched = len(core.fetches)
    again = await connector.send(_lease(_photos(), maybe_sent=True))
    assert again.outcome == "sent"
    assert again.ext_ids == tuple(s["ext_id"] for s in gw.sent)
    assert len(gw.album_calls) == 1  # без второй отправки
    assert len(core.fetches) == fetched  # и без байтов из ядра


async def test_answer_without_ids_searches_history(tmp_path) -> None:
    connector, gw, _core, _tmp = _setup(tmp_path)
    gw.album_result_without_ids = True
    result = await connector.send(_lease(_photos()))
    assert result.outcome == "sent"
    assert result.ext_ids == tuple(s["ext_id"] for s in gw.sent)


async def test_account_window_album_of_ten_with_fifteen_taken_is_flood(tmp_path) -> None:
    ten = [file_meta(PNG_BYTES, name=f"{k}.png") for k in range(10)]
    connector, gw, _core, _tmp = _setup(tmp_path, data=[PNG_BYTES] * 10)
    # 15 мест окна 20/мин заняты: самые старые освобождаются по одному каждую секунду
    connector._send_times.extend(NOW_MS - 50_000 + k * 1_000 for k in range(15))  # noqa: SLF001
    result = await connector.send(_lease(ten))
    assert result.outcome == "flood"
    # Нужно 5 свободных мест -> ждём пятое по старшинству: (NOW-46 000) + 60 000 - NOW = 14 000 мс.
    assert result.retry_after_ms == 14_000  # noqa: PLR2004
    assert gw.album_calls == []


async def test_account_window_album_fits_exactly(tmp_path) -> None:
    five = [file_meta(PNG_BYTES, name=f"{k}.png") for k in range(5)]
    connector, gw, _core, _tmp = _setup(tmp_path, data=[PNG_BYTES] * 5)
    connector._send_times.extend(NOW_MS - 50_000 + k * 1_000 for k in range(15))  # noqa: SLF001
    result = await connector.send(_lease(five))
    assert result.outcome == "sent"
    assert len(connector._send_times) == 20  # noqa: SLF001, PLR2004 — альбом занял 5 мест


async def test_core_refuses_second_file_is_temporary_without_send(tmp_path) -> None:
    connector, gw, core, tmp_dir = _setup(tmp_path)
    core.idx_status[1] = 409
    result = await connector.send(_lease(_photos()))
    assert (result.outcome, result.error) == ("temporary", "core_409")
    assert gw.album_calls == []
    assert _tmp_files(tmp_dir) == []


async def test_album_to_new_chat_unknown_username_is_permanent(tmp_path) -> None:
    """Ф14-2 B3: альбом в новую беседу идёт после разрешения адресата; адресата нет — без загрузки байтов
    (успешная отправка — `tests/connectors/telegram/test_new_chat_files.py`)."""
    connector, gw, core, _tmp = _setup(tmp_path)
    lease = _lease(_photos(), channel_ext_id="new:1", new_to={"kind": "tg_username", "username": "petr"})
    result = await connector.send(lease)
    assert result.outcome == "permanent" and "нет такого @username" in result.error
    assert gw.album_calls == [] and core.fetches == []


# --- тест 15: поиск альбома в истории ---


async def test_maybe_sent_finds_own_album_without_sending(tmp_path) -> None:
    connector, gw, core, _tmp = _setup(tmp_path)
    peer = types.PeerUser(USER_ID)
    for msg in _album_msgs(peer, 700, 3, grouped_id=5):
        gw.add_message(USER_ID, msg)
    result = await connector.send(_lease(_photos(), maybe_sent=True))
    assert (result.outcome, result.ext_ids, result.grouped_id) == ("sent", ("700", "701", "702"), "5")
    assert result.ext_id == "700"
    assert gw.album_calls == []
    assert core.fetches == []


@pytest.mark.parametrize(
    "variant",
    ["known_own", "other_size", "other_caption", "caption_on_second", "not_out", "too_old", "no_group"],
)
async def test_album_search_rejects_non_candidates(tmp_path, variant) -> None:
    connector, gw, _core, _tmp = _setup(tmp_path)
    peer = types.PeerUser(USER_ID)
    n = 2 if variant == "other_size" else 3
    msgs = _album_msgs(
        peer, 800, n, grouped_id=6,
        caption="чужая" if variant == "other_caption" else "подпись",
        date_ms=NOW_MS - 60_000 if variant == "too_old" else NOW_MS,
    )
    if variant == "caption_on_second":
        msgs[1].message = "ещё"
    if variant == "not_out":
        for m in msgs:
            m.out = False
    if variant == "no_group":
        for m in msgs:
            m.grouped_id = None
    for msg in msgs:
        gw.add_message(USER_ID, msg)
    known = ["801"] if variant == "known_own" else []
    found = await connector._find_album_in_history(  # noqa: SLF001
        types.InputPeerUser(USER_ID, 0), None, "подпись", 3, since_ms=NOW_MS - 5_000,
        exclude_ext_ids=frozenset(known),
    )
    assert found is None


async def test_album_search_takes_freshest_candidate(tmp_path) -> None:
    connector, gw, _core, _tmp = _setup(tmp_path)
    peer = types.PeerUser(USER_ID)
    for msg in _album_msgs(peer, 900, 3, grouped_id=1) + _album_msgs(peer, 950, 3, grouped_id=2):
        gw.add_message(USER_ID, msg)
    found = await connector._find_album_in_history(  # noqa: SLF001
        types.InputPeerUser(USER_ID, 0), None, " подпись ", 3, since_ms=NOW_MS - 5_000
    )
    assert found == (["950", "951", "952"], "2")


async def test_p4_reconciled_elements_do_not_hide_album_but_bound_one_does(tmp_path) -> None:
    """П4: у строки-альбома `known_own_ext_ids` — только привязанные к outbox. Прежний альбом исключается
    целиком по своему элементу 0 (он с `outbox_id`), новый — находится, хотя его элементы сверка уже
    принесла в ядро (без `outbox_id` — в список не попадают)."""
    connector, gw, _core, _tmp = _setup(tmp_path)
    peer = types.PeerUser(USER_ID)
    for msg in _album_msgs(peer, 600, 3, grouped_id=3) + _album_msgs(peer, 610, 3, grouped_id=4):
        gw.add_message(USER_ID, msg)
    result = await connector.send(_lease(_photos(), maybe_sent=True, known_own_ext_ids=["600"]))
    assert (result.outcome, result.ext_ids) == ("sent", ("610", "611", "612"))
    assert gw.album_calls == []


async def test_reconcile_album_row_uses_album_search(tmp_path) -> None:
    connector, gw, _core, _tmp = _setup(tmp_path)
    peer = types.PeerUser(USER_ID)
    for msg in _album_msgs(peer, 500, 3, grouped_id=8):
        gw.add_message(USER_ID, msg)
    found = await connector.reconcile(_lease(_photos(), mode="reconcile"))
    assert (found.outcome, found.ext_ids, found.grouped_id) == ("sent", ("500", "501", "502"), "8")
    gw.history.clear()
    missing = await connector.reconcile(_lease(_photos(), mode="reconcile"))
    assert missing.outcome == "unknown"


def test_extract_album_ids_order_and_grouped_id() -> None:
    peer = types.PeerUser(USER_ID)
    msgs = _album_msgs(peer, 10, 2, grouped_id=99)
    result = types.Updates(
        updates=[
            types.UpdateNewMessage(message=msgs[1], pts=0, pts_count=0),
            types.UpdateMessageID(id=11, random_id=222),
            types.UpdateMessageID(id=10, random_id=111),
            types.UpdateNewMessage(message=msgs[0], pts=0, pts_count=0),
        ],
        users=[], chats=[], date=ts(NOW_MS), seq=0,
    )
    assert convert.extract_album_ids(result, [111, 222]) == (["10", "11"], "99")
    assert convert.extract_album_ids(result, [111, 333]) == (["10", None], "99")


# --- TgGateway.send_album: какие TL-запросы уходят ---


class _FakeTelegramClient:
    def __init__(self, *_a, **_kw) -> None:
        self.uploads: list[tuple] = []
        self.requests: list = []
        self.upload_media_error: Exception | None = None

    async def upload_file(self, file, *, file_name=None):
        self.uploads.append((file, file_name))
        return types.InputFile(id=len(self.uploads), parts=1, name=file_name or "x", md5_checksum="")

    async def __call__(self, request):
        self.requests.append(request)
        if isinstance(request, UploadMediaRequest):
            if self.upload_media_error is not None:
                raise self.upload_media_error
            n = len(self.requests)
            if isinstance(request.media, types.InputMediaUploadedPhoto):
                return types.MessageMediaPhoto(
                    photo=types.Photo(
                        id=n, access_hash=n, file_reference=b"r", date=ts(NOW_MS), sizes=[], dc_id=2
                    )
                )
            return types.MessageMediaDocument(
                document=types.Document(
                    id=n, access_hash=n, file_reference=b"r", date=ts(NOW_MS),
                    mime_type=request.media.mime_type, size=1, dc_id=2, attributes=[],
                )
            )
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


async def test_tg_gateway_album_uploads_each_then_one_multi_media(tmp_path) -> None:
    gw, client = _tg_gateway(tmp_path)
    peer = types.InputPeerUser(USER_ID, 0)
    reply_to = types.InputReplyToMessage(reply_to_msg_id=9)
    items = [AlbumItem(f"/p{k}", "photo", f"{k}.png", "image/png") for k in range(3)]
    await gw.send_album(peer, items=items, text="cap", random_ids=[1, 2, 3], reply_to=reply_to)

    assert [u[1] for u in client.uploads] == ["0.png", "1.png", "2.png"]
    *uploads, send = client.requests
    assert len(uploads) == 3  # noqa: PLR2004
    assert all(isinstance(r, UploadMediaRequest) for r in uploads)
    assert all(isinstance(r.media, types.InputMediaUploadedPhoto) for r in uploads)
    assert isinstance(send, SendMultiMediaRequest)
    assert send.reply_to == reply_to
    assert [m.random_id for m in send.multi_media] == [1, 2, 3]
    assert [m.message for m in send.multi_media] == ["cap", "", ""]
    assert all(isinstance(m.media, types.InputMediaPhoto) for m in send.multi_media)


async def test_tg_gateway_documents_album_keeps_name_mime_force_file(tmp_path) -> None:
    gw, client = _tg_gateway(tmp_path)
    items = [
        AlbumItem("/a", "document", "акт.pdf", "application/pdf"),
        AlbumItem("/b", "document", "b.png", "image/png", force_file=True),
    ]
    await gw.send_album(types.InputPeerUser(USER_ID, 0), items=items, text="", random_ids=[5, 6])
    up_a, up_b, send = client.requests
    assert up_a.media.mime_type == "application/pdf"
    assert [a.file_name for a in up_a.media.attributes] == ["акт.pdf"]
    assert not up_a.media.force_file
    assert up_b.media.force_file is True
    assert all(isinstance(m.media, types.InputMediaDocument) for m in send.multi_media)


async def test_tg_gateway_upload_media_error_is_media_upload_error_of_its_stage(tmp_path) -> None:
    gw, client = _tg_gateway(tmp_path)
    client.upload_media_error = errors.PhotoInvalidDimensionsError(request=None)
    items = [AlbumItem("/a", "photo", "a.png", "image/png"), AlbumItem("/b", "photo", "b.png", "image/png")]
    with pytest.raises(MediaUploadError) as info:
        await gw.send_album(types.InputPeerUser(USER_ID, 0), items=items, text="", random_ids=[1, 2])
    assert info.value.stage == "upload_media"
    assert isinstance(info.value.cause, errors.PhotoInvalidDimensionsError)
    assert not any(isinstance(r, SendMultiMediaRequest) for r in client.requests)
