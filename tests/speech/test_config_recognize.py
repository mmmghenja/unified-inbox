"""Ф59-1: настройки диктовки (`hub.speech.config`), вызов SpeechKit (`hub.speech.recognize`) и пределы
(`hub.speech.limits`) — без ядра, подделка SpeechKit на `httpx.MockTransport`."""

import asyncio
import datetime as dt
import json
import stat

import httpx
import pytest

from hub.speech import config as speech_config
from hub.speech import recognize as recognize_mod
from hub.speech.limits import DictateLimits
from hub.speech.recognize import recognize_lpcm16k

from .conftest import FOLDER, KEY, configure


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_load_none_until_key_and_folder(config_dir):
    assert speech_config.load(config_dir) is None
    configure(config_dir, folder="")
    assert speech_config.load(config_dir) is None
    configure(config_dir)
    cfg = speech_config.load(config_dir)
    assert cfg is not None
    assert (cfg.api_key, cfg.folder_id, cfg.lang, cfg.daily_chunks, cfg.proxy) == (
        KEY,
        FOLDER,
        "ru-RU",
        2000,
        None,
    )


def test_save_permissions_and_files(config_dir):
    configure(config_dir)
    assert _mode(speech_config.service_dir(config_dir)) == 0o700
    assert _mode(speech_config.key_path(config_dir)) == 0o600
    assert speech_config.key_path(config_dir).read_text(encoding="utf-8") == KEY
    data = json.loads(speech_config.config_path(config_dir).read_text(encoding="utf-8"))
    assert data == {"folder_id": FOLDER, "lang": "ru-RU", "daily_chunks": 2000}
    assert KEY not in speech_config.config_path(config_dir).read_text(encoding="utf-8")


def test_save_replaces_key_and_keeps_other_settings(config_dir):
    configure(config_dir, lang="en-US", daily_chunks=50, proxy="socks5://127.0.0.1:10808")
    speech_config.save(config_dir, api_key="NEW-KEY", folder_id="b1g-other")
    cfg = speech_config.load(config_dir)
    assert (cfg.api_key, cfg.folder_id, cfg.lang, cfg.daily_chunks, cfg.proxy) == (
        "NEW-KEY",
        "b1g-other",
        "en-US",
        50,
        "socks5://127.0.0.1:10808",
    )
    assert _mode(speech_config.key_path(config_dir)) == 0o600
    assert [p.name for p in speech_config.service_dir(config_dir).iterdir() if p.name.startswith(".")] == []


@pytest.mark.parametrize(("raw", "want"), [(0, 2000), (-5, 2000), ("10", 2000), (True, 2000), (7, 7)])
def test_daily_chunks_garbage_falls_back(config_dir, raw, want):
    configure(config_dir, daily_chunks=raw)
    assert speech_config.load(config_dir).daily_chunks == want


def test_proxy_none_string_is_direct(config_dir):
    configure(config_dir, proxy="none")
    assert speech_config.load(config_dir).proxy is None


def test_broken_config_json_is_off(config_dir):
    configure(config_dir)
    speech_config.config_path(config_dir).write_text("{broken", encoding="utf-8")
    assert speech_config.load(config_dir) is None


def test_parse_env_file():
    text = "\n".join(
        [
            "# комментарий",
            "",
            "SPEECHKIT_API_KEY='AQVN key'",
            'export SPEECHKIT_FOLDER_ID="b1g123"',
            "OCR_YANDEX_FOLDER_ID=b1gocr # хвост-комментарий",
            "BROKEN LINE",
            "EMPTY=",
            "URL=https://x/#frag",
        ]
    )
    assert speech_config.parse_env_file(text) == {
        "SPEECHKIT_API_KEY": "AQVN key",
        "SPEECHKIT_FOLDER_ID": "b1g123",
        "OCR_YANDEX_FOLDER_ID": "b1gocr",
        "EMPTY": "",
        "URL": "https://x/#frag",
    }


# --- recognize ------------------------------------------------------------------------------------


def _cfg(**kw):
    return speech_config.SpeechConfig(api_key=KEY, folder_id=FOLDER, **kw)


async def test_recognize_ok(fake_sk):
    res = await recognize_lpcm16k(b"\x00\x00" * 100, _cfg(), transport=fake_sk.transport)
    assert (res.ok, res.text, res.status) == (True, "привет мир", 200)


@pytest.mark.parametrize(
    ("status", "code"), [(401, "unauthorized"), (403, "unauthorized"), (400, "bad_audio"), (503, "upstream")]
)
async def test_recognize_status_codes(fake_sk, status, code):
    fake_sk.status = status
    res = await recognize_lpcm16k(b"\x00\x00", _cfg(), transport=fake_sk.transport)
    assert (res.ok, res.code, res.status) == (False, code, status)
    assert len(fake_sk.requests) == 1


async def test_recognize_whole_call_timeout(config_dir):
    """Таймаут — на весь вызов (не только на фазу сети): медленный ответ -> `timeout`, один запрос."""
    calls = []

    async def slow(request):
        calls.append(request)
        await asyncio.sleep(5)
        return httpx.Response(200, json={"result": "поздно"})

    res = await recognize_lpcm16k(b"\x00\x00", _cfg(), transport=httpx.MockTransport(slow), timeout_s=0.05)
    assert (res.ok, res.code) == (False, "timeout")
    assert len(calls) == 1


async def test_recognize_error_never_carries_key(fake_sk, monkeypatch):
    monkeypatch.setattr(recognize_mod, "CONNECT_RETRY_PAUSE_S", 0)
    fake_sk.raise_exc = httpx.ConnectError(f"boom {KEY}")
    res = await recognize_lpcm16k(b"\x00\x00", _cfg(), transport=fake_sk.transport)
    assert (res.ok, res.code, res.status) == (False, "upstream", None)
    assert KEY not in repr(res)


async def test_recognize_retries_only_failed_connect(monkeypatch):
    """19.10: соединение не установилось (запрос не ушёл, оплаты нет) — до трёх попыток; обрыв после
    отправки — без повтора (повторяет интерфейс)."""
    monkeypatch.setattr(recognize_mod, "CONNECT_RETRY_PAUSE_S", 0)
    calls = []

    def flaky(request):
        calls.append(request)
        if len(calls) < 3:
            raise httpx.ConnectError("нет связи")
        return httpx.Response(200, json={"result": "дошло"})

    res = await recognize_lpcm16k(b"\x00\x00", _cfg(), transport=httpx.MockTransport(flaky))
    assert (res.ok, res.text, len(calls)) == (True, "дошло", 3)

    calls.clear()

    def read_broken(request):
        calls.append(request)
        raise httpx.ReadError("оборвалось")

    res = await recognize_lpcm16k(b"\x00\x00", _cfg(), transport=httpx.MockTransport(read_broken))
    assert (res.ok, res.code, len(calls)) == (False, "upstream", 1)


# --- limits ---------------------------------------------------------------------------------------


def test_limits_minute_window():
    lim = DictateLimits(per_minute=2)
    assert lim.take_minute(0) and lim.take_minute(10)
    assert not lim.take_minute(59_999)
    assert lim.take_minute(60_000)  # первый вышел из окна


def test_limits_day_resets_at_local_midnight():
    lim = DictateLimits()
    late = int(dt.datetime(2026, 10, 14, 23, 59, 30).timestamp() * 1000)  # местное время
    assert lim.take_day(late, 1)
    assert not lim.take_day(late + 20_000, 1)
    assert lim.used_today(late) == 1
    assert lim.take_day(late + 31_000, 1)  # 00:00:01 следующих суток
