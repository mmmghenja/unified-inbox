"""Ф14-4 B (`docs/architecture.md`, B1–B3, тесты E4–E7): голосовое в Telegram. Ядро — подменное
(`FakeFileCore`), Telegram — `FakeGateway`; ffmpeg/ffprobe — подделка `voice.run_process` (реальный запуск —
один тест, пропускается без ffmpeg). Сети нет."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

import pytest
from telethon import errors
from telethon.tl import types
from telethon.tl.functions.messages import SendMediaRequest

from hub.connectors.telegram import convert
from hub.connectors.telegram import voice as voice_mod

from ..outbox_file_fakes import FakeFileCore, file_meta
from .helpers import FakeGateway, make_dialog, make_message, make_user
from .test_send_file import NOW_MS, USER_ID, _lease, _tg_gateway

WEBM = b"\x1a\x45\xdf\xa3" + b"\x42\x82\x84webm" + b"\x00" * 100
OGG_IN = b"OggS" + b"\x01" * 100
OGG_OUT = b"OggS" + b"\x02" * 80  # «результат ремукса»


def _setup(tmp_path, data: bytes):
    core = FakeFileCore()
    core.files[7] = data
    gw = FakeGateway()
    gw.now_ms = NOW_MS
    gw.add_dialog(make_dialog(make_user(USER_ID), top_message=1))
    tmp_dir = tmp_path / "tmp-conn"
    from hub.connectors.telegram.connector import TelegramConnector

    connector = TelegramConnector(
        core.client(), gw, account_id=1, media_tmp_dir=tmp_dir, wall=lambda: NOW_MS / 1000
    )
    return connector, gw, core, tmp_dir


def _voice_meta(data: bytes, *, mime: str = "audio/webm", name: str = "voice.webm") -> dict:
    return file_meta(data, kind="voice", mime=mime, name=name)


def _left(tmp_dir: Path) -> list:
    return list(tmp_dir.rglob("*")) if tmp_dir.exists() else []


def _write(path: Path, data: bytes) -> None:
    path.write_bytes(data)


class _FakeProcs:
    """Подделка `voice.run_process`: журнал вызовов; ffmpeg пишет `OGG_OUT` в последний аргумент (или падает),
    ffprobe отвечает `probe_out` (или падает). Пути входа запоминаются, пока файлы ещё есть."""

    def __init__(self, *, ffmpeg_ok: bool = True, probe_out: bytes = b"3.2\n", probe_ok: bool = True):
        self.calls: list[list[str]] = []
        self.ffmpeg_ok = ffmpeg_ok
        self.probe_out = probe_out
        self.probe_ok = probe_ok
        self.seen_paths: list[Path] = []

    async def __call__(self, args: list[str], limit_s: float) -> bytes:
        self.calls.append(list(args))
        if args[0] == voice_mod.FFMPEG:
            assert limit_s == 30.0
            self.seen_paths.append(Path(args[args.index("-i") + 1]))
            if not self.ffmpeg_ok:
                _write(Path(args[-1]), b"half")  # недописанный выход тоже должен удалиться
                raise voice_mod.ProcessFailed("exit 1")
            _write(Path(args[-1]), OGG_OUT)
            self.seen_paths.append(Path(args[-1]))
            return b""
        assert args[0] == voice_mod.FFPROBE
        assert limit_s == 10.0
        if not self.probe_ok:
            raise voice_mod.ProcessFailed("timeout")
        return self.probe_out


# --- E4: webm -> ремукс, ogg — без ------------------------------------------------------------------------


async def test_webm_is_remuxed_and_sent_as_voice_with_duration(tmp_path, monkeypatch) -> None:
    procs = _FakeProcs()
    monkeypatch.setattr(voice_mod, "run_process", procs)
    connector, gw, _core, tmp_dir = _setup(tmp_path, WEBM)

    result = await connector.send(_lease(_voice_meta(WEBM), text=""))

    assert result.outcome == "sent"
    ffmpeg, ffprobe = procs.calls
    assert ffmpeg[:4] == ["ffmpeg", "-nostdin", "-v", "error"]
    assert ffmpeg[ffmpeg.index("-map") + 1] == "0:a:0"
    assert ffmpeg[ffmpeg.index("-c:a") + 1] == "copy"
    assert ffmpeg[ffmpeg.index("-f") + 1] == "ogg"
    assert ffprobe[:6] == ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of"]
    assert ffprobe[-1] == ffmpeg[-1]  # длительность — у готового ogg
    (call,) = gw.media_calls
    assert call["kind"] == "voice"
    assert call["duration"] == 4  # 3.2 с -> вверх
    assert call["data"] == OGG_OUT
    assert call["name"] == "voice.ogg"
    assert call["text"] == ""
    assert _left(tmp_dir) == []
    assert not any(p.exists() for p in procs.seen_paths)


async def test_ogg_is_sent_without_remux(tmp_path, monkeypatch) -> None:
    procs = _FakeProcs(probe_out=b"7.000000\n")
    monkeypatch.setattr(voice_mod, "run_process", procs)
    connector, gw, _core, tmp_dir = _setup(tmp_path, OGG_IN)

    result = await connector.send(_lease(_voice_meta(OGG_IN, mime="audio/ogg", name="voice.ogg"), text=""))

    assert result.outcome == "sent"
    assert [c[0] for c in procs.calls] == ["ffprobe"]
    (call,) = gw.media_calls
    assert (call["kind"], call["duration"], call["data"]) == ("voice", 7, OGG_IN)
    assert _left(tmp_dir) == []


async def test_probe_failure_gives_zero_duration(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(voice_mod, "run_process", _FakeProcs(probe_ok=False))
    connector, gw, _core, _tmp = _setup(tmp_path, WEBM)
    assert (await connector.send(_lease(_voice_meta(WEBM), text=""))).outcome == "sent"
    assert (gw.media_calls[0]["kind"], gw.media_calls[0]["duration"]) == ("voice", 0)


@pytest.mark.parametrize("out", [b"", b"N/A\n", b"nan\n", b"-1\n"])
async def test_probe_garbage_is_zero(tmp_path, monkeypatch, out) -> None:
    monkeypatch.setattr(voice_mod, "run_process", _FakeProcs(probe_out=out))
    assert await voice_mod.probe_duration(tmp_path / "x.ogg") == 0


# --- E5: ремукс не удался / нет ffmpeg -> документ --------------------------------------------------------


async def test_remux_failure_sends_document_and_warns(tmp_path, monkeypatch, caplog) -> None:
    procs = _FakeProcs(ffmpeg_ok=False)
    monkeypatch.setattr(voice_mod, "run_process", procs)
    connector, gw, _core, tmp_dir = _setup(tmp_path, WEBM)

    with caplog.at_level(logging.WARNING):
        result = await connector.send(_lease(_voice_meta(WEBM), text=""))

    assert result.outcome == "sent"
    (call,) = gw.media_calls
    assert (call["kind"], call["name"], call["mime"]) == ("document", "voice.webm", "audio/webm")
    assert call["data"] == WEBM
    assert [c[0] for c in procs.calls] == ["ffmpeg"]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("голосовое ушло файлом" in m for m in warnings)
    assert not any("voice.webm" in m or str(tmp_dir) in m for m in warnings)
    assert _left(tmp_dir) == []
    assert not any(p.exists() for p in procs.seen_paths)


async def test_no_ffmpeg_binary_sends_document(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(voice_mod, "FFMPEG", str(tmp_path / "нет-такого-ffmpeg"))
    connector, gw, _core, tmp_dir = _setup(tmp_path, WEBM)

    result = await connector.send(_lease(_voice_meta(WEBM), text=""))

    assert result.outcome == "sent"
    assert gw.media_calls[0]["kind"] == "document"
    assert _left(tmp_dir) == []


async def test_run_process_timeout_kills(tmp_path) -> None:
    sleep = shutil.which("sleep")
    if sleep is None:
        pytest.skip("нет sleep")
    with pytest.raises(voice_mod.ProcessFailed, match="timeout"):
        await voice_mod.run_process([sleep, "5"], 0.1)


# --- E6: maybe_sent ---------------------------------------------------------------------------------------


async def test_maybe_sent_found_own_voice_no_resend(tmp_path, monkeypatch) -> None:
    procs = _FakeProcs()
    monkeypatch.setattr(voice_mod, "run_process", procs)
    connector, gw, core, _tmp = _setup(tmp_path, WEBM)
    peer = types.PeerUser(USER_ID)
    media = types.MessageMediaDocument(document=types.DocumentEmpty(id=1))
    gw.add_message(USER_ID, make_message(700, peer, message="", out=True, date_ms=NOW_MS, media=media))

    result = await connector.send(_lease(_voice_meta(WEBM), text="", maybe_sent=True))

    assert (result.outcome, result.ext_id) == ("sent", "700")
    assert gw.media_calls == []
    assert procs.calls == []
    assert core.fetches == []


# --- E7: шлюз -----------------------------------------------------------------------------------------------


async def test_tg_gateway_voice_request(tmp_path) -> None:
    gw, client = _tg_gateway(tmp_path)
    await gw.send_media(
        types.InputPeerUser(USER_ID, 0), path="/v", kind="voice", name="voice.ogg", mime="audio/webm",
        text="", random_id=11, duration=5,
    )
    (req,) = client.requests
    assert isinstance(req, SendMediaRequest)
    assert req.random_id == 11
    media = req.media
    assert isinstance(media, types.InputMediaUploadedDocument)
    assert media.mime_type == "audio/ogg"
    (attr,) = media.attributes
    assert isinstance(attr, types.DocumentAttributeAudio)
    assert (attr.voice, attr.duration) == (True, 5)


# --- реальный ffmpeg (пропуск, если его нет) ----------------------------------------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="нет ffmpeg")
async def test_real_ffmpeg_remux_webm_to_ogg(tmp_path) -> None:
    src = tmp_path / "in.webm"
    made = subprocess.run(  # noqa: ASYNC221 — подготовка входа теста
        ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1.5",
         "-c:a", "libopus", "-f", "webm", str(src)],
        check=False,
    )  # fmt: skip
    if made.returncode != 0:
        pytest.skip("ffmpeg без libopus")
    work = tmp_path / "work"
    async with voice_mod.prepared(src, "audio/webm", work) as ready:
        assert ready is not None
        ogg, duration = ready
        assert ogg.read_bytes()[:4] == b"OggS"
        assert duration == 2
    assert list(work.iterdir()) == []


# --- собеседник/чат запретил голосовые -> один повтор документом ------------------------------------------


@pytest.mark.parametrize(
    "error_cls", [errors.VoiceMessagesForbiddenError, errors.ChatSendVoicesForbiddenError]
)
async def test_voice_forbidden_retries_once_as_document(tmp_path, monkeypatch, caplog, error_cls) -> None:
    monkeypatch.setattr(voice_mod, "run_process", _FakeProcs())
    connector, gw, _core, tmp_dir = _setup(tmp_path, WEBM)
    gw.media_faults.append(error_cls(request=None))

    with caplog.at_level(logging.WARNING):
        result = await connector.send(_lease(_voice_meta(WEBM), text=""))

    assert result.outcome == "sent"
    first, second = gw.media_calls
    assert first["kind"] == "voice"
    assert (second["kind"], second["name"], second["data"]) == ("document", "voice.webm", WEBM)
    assert first["random_id"] == convert.random_id_from_token("tok-7")
    assert second["random_id"] == convert.random_id_from_token("tok-7:doc")
    assert result.ext_id == gw.sent[0]["ext_id"]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("собеседник запретил голосовые" in m for m in warnings)
    assert not any("voice.webm" in m for m in warnings)
    assert _left(tmp_dir) == []


async def test_voice_forbidden_and_document_fails_is_classified(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(voice_mod, "run_process", _FakeProcs())
    connector, gw, _core, _tmp = _setup(tmp_path, WEBM)
    gw.media_faults.extend(
        [errors.VoiceMessagesForbiddenError(request=None), errors.ChatWriteForbiddenError(request=None)]
    )
    result = await connector.send(_lease(_voice_meta(WEBM), text=""))
    assert result.outcome == "permanent"
    assert len(gw.media_calls) == 2


async def test_voice_forbidden_error_on_plain_document_is_not_retried(tmp_path) -> None:
    connector, gw, _core, _tmp = _setup(tmp_path, WEBM)
    gw.media_faults.append(errors.VoiceMessagesForbiddenError(request=None))
    meta = file_meta(WEBM, kind="document", mime="video/webm", name="clip.webm")
    result = await connector.send(_lease(meta, text=""))
    assert result.outcome == "permanent"
    assert len(gw.media_calls) == 1
