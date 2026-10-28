"""Ф59-1, раздел 1 «Ручки»: `GET`/`POST /api/v1/dictate` — порядок проверок, коды, пределы, подделка
SpeechKit, только кука интерфейса, удалённый слушатель с телом 900 КБ."""

import logging
from urllib.parse import parse_qs

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import remote as remote_mod
from hub.core.app_public import create_public_app
from hub.core.feed import Notifier
from hub.core.remote import RemoteState
from hub.speech import recognize as recognize_mod

from .conftest import FOLDER, KEY, ORIGIN, PORT, configure, login, make_bearer

API = "/api/v1/dictate"
SEC = 32_000  # байт в секунде LPCM 16 кГц Int16 моно


def pcm(seconds: float) -> bytes:
    n = int(seconds * SEC)
    return b"\x01\x00" * (n // 2) + (b"\x01" if n % 2 else b"")


def hdrs(csrf: str) -> dict:
    return {"Origin": ORIGIN, "X-Hub-CSRF": csrf, "Content-Type": "application/octet-stream"}


@pytest.fixture
async def csrf(client, db, clock):
    return await login(client, db, clock)


# --- GET ------------------------------------------------------------------------------------------


async def test_get_disabled_without_config(client, csrf):
    r = await client.get(API)
    assert r.status_code == 200 and r.json() == {"enabled": False}


async def test_get_disabled_with_key_but_no_folder(client, csrf, config_dir):
    configure(config_dir, folder="")
    assert (await client.get(API)).json() == {"enabled": False}


async def test_get_enabled_after_auth_without_restart(client, csrf, config_dir):
    assert (await client.get(API)).json() == {"enabled": False}
    configure(config_dir)
    assert (await client.get(API)).json() == {"enabled": True}


async def test_get_without_session_is_401(client):
    assert (await client.get(API)).status_code == 401


@pytest.mark.parametrize(
    ("scopes", "kind"),
    [
        (("read",), "interactive"),
        (("read", "draft"), "background"),
        (("human",), "background"),
        (("term",), "background"),
    ],
)
async def test_bearer_is_403_on_get_and_post(client, db, config_dir, fake_sk, scopes, kind):
    configure(config_dir)
    token = make_bearer(db, scopes=scopes, kind=kind)
    auth = {"Authorization": f"Bearer {token}"}
    assert (await client.get(API, headers=auth)).status_code == 403
    r = await client.post(API, content=pcm(1), headers={**auth, "Content-Type": "application/octet-stream"})
    assert r.status_code == 403
    assert fake_sk.requests == []


async def test_post_without_csrf_is_403(client, csrf, config_dir, fake_sk):
    configure(config_dir)
    r = await client.post(API, content=pcm(1), headers={"Origin": ORIGIN})
    assert r.status_code == 403
    assert fake_sk.requests == []


# --- POST: порядок проверок -----------------------------------------------------------------------


async def test_post_off_is_404_before_length_checks(client, csrf, fake_sk):
    r = await client.post(API, content=b"", headers=hdrs(csrf))
    assert r.status_code == 404 and r.json() == {"detail": "dictate_off"}
    assert fake_sk.requests == []


async def test_post_without_content_length_is_411(client, csrf, config_dir, fake_sk):
    configure(config_dir)

    async def chunks():
        yield pcm(1)

    r = await client.post(API, content=chunks(), headers=hdrs(csrf))
    assert r.status_code == 411
    assert fake_sk.requests == []


@pytest.mark.parametrize(
    ("body", "status", "detail"),
    [
        (b"", 400, "bad_audio"),
        (b"\x00" * (1024 * 1024 + 2), 413, "dictate_too_big"),
        (b"\x00" * (SEC + 1), 400, "bad_audio"),  # нечётная длина
        (pcm(0.29), 400, "bad_audio"),  # короче 0,3 с
        (b"\x00" * (30 * SEC + 2), 400, "bad_audio"),  # длиннее 30 с, но меньше 1 МиБ
    ],
    ids=["empty", "over_1mib", "odd", "short", "long"],
)
async def test_post_body_checks(client, csrf, config_dir, fake_sk, body, status, detail):
    configure(config_dir)
    r = await client.post(API, content=body, headers=hdrs(csrf))
    assert r.status_code == status and r.json() == {"detail": detail}
    assert fake_sk.requests == []


@pytest.mark.parametrize("seconds", [0.3, 30])
async def test_post_duration_bounds_inclusive(client, csrf, config_dir, fake_sk, seconds):
    configure(config_dir)
    r = await client.post(API, content=pcm(seconds), headers=hdrs(csrf))
    assert r.status_code == 200, r.text


# --- POST: вызов SpeechKit ------------------------------------------------------------------------


async def test_post_ok_sends_params_headers_and_body(client, csrf, config_dir, fake_sk):
    configure(config_dir)
    body = pcm(2)
    r = await client.post(API, content=body, headers=hdrs(csrf))
    assert r.status_code == 200 and r.json() == {"text": "привет мир"}
    (req,) = fake_sk.requests
    assert req.method == "POST"
    assert (req.url.scheme, req.url.host, req.url.path) == (
        "https",
        "stt.api.cloud.yandex.net",
        "/speech/v1/stt:recognize",
    )
    assert parse_qs(req.url.query.decode()) == {
        "lang": ["ru-RU"],
        "format": ["lpcm"],
        "sampleRateHertz": ["16000"],
        "topic": ["general"],
        "folderId": [FOLDER],
    }
    assert req.headers["authorization"] == f"Api-Key {KEY}"
    assert req.headers["x-data-logging-enabled"] == "false"
    assert req.headers["content-type"] == "application/octet-stream"
    assert fake_sk.bodies == [body]


async def test_post_uses_lang_from_config(client, csrf, config_dir, fake_sk):
    configure(config_dir, lang="en-US")
    assert (await client.post(API, content=pcm(1), headers=hdrs(csrf))).status_code == 200
    assert parse_qs(fake_sk.requests[0].url.query.decode())["lang"] == ["en-US"]


async def test_post_url_from_env(client, csrf, config_dir, fake_sk, monkeypatch):
    monkeypatch.setenv("HUB_SPEECHKIT_URL", "http://127.0.0.1:18999/stt")
    configure(config_dir)
    assert (await client.post(API, content=pcm(1), headers=hdrs(csrf))).status_code == 200
    req = fake_sk.requests[0]
    assert (req.url.host, req.url.port, req.url.path) == ("127.0.0.1", 18999, "/stt")


@pytest.mark.parametrize(
    ("status", "body", "want_status", "want_detail"),
    [
        (401, {"error_code": "UNAUTHORIZED"}, 502, "dictate_key"),
        (403, {"error_code": "PERMISSION_DENIED"}, 502, "dictate_key"),
        (400, {"error_code": "BAD_REQUEST"}, 400, "bad_audio"),
        (500, {"error_code": "INTERNAL"}, 502, "dictate_upstream"),
        (429, {"error_code": "LIMIT"}, 502, "dictate_upstream"),
        (200, b"not json", 502, "dictate_upstream"),
        (200, {"no_result": 1}, 502, "dictate_upstream"),
    ],
)
async def test_post_speechkit_errors_once_without_retry(
    client, csrf, config_dir, fake_sk, status, body, want_status, want_detail
):
    configure(config_dir)
    fake_sk.status, fake_sk.body = status, body
    r = await client.post(API, content=pcm(1), headers=hdrs(csrf))
    assert r.status_code == want_status and r.json() == {"detail": want_detail}
    assert len(fake_sk.requests) == 1  # без повторов: повтор — вторая оплата


@pytest.mark.parametrize(
    ("exc", "want_status", "want_detail"),
    [
        (httpx.ReadTimeout("t"), 504, "dictate_timeout"),
        (httpx.ConnectTimeout("t"), 504, "dictate_timeout"),
        (httpx.ConnectError("refused"), 502, "dictate_upstream"),
        (httpx.RemoteProtocolError("eof"), 502, "dictate_upstream"),
    ],
)
async def test_post_network_errors_retry_only_failed_connect(
    client, csrf, config_dir, fake_sk, exc, want_status, want_detail, monkeypatch
):
    """Соединение не установилось — запрос не ушёл, оплаты нет: три попытки (19.10). Остальное — одна."""
    monkeypatch.setattr(recognize_mod, "CONNECT_RETRY_PAUSE_S", 0)
    configure(config_dir)
    fake_sk.raise_exc = exc
    r = await client.post(API, content=pcm(1), headers=hdrs(csrf))
    assert r.status_code == want_status and r.json() == {"detail": want_detail}
    connect_failed = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
    assert len(fake_sk.requests) == (recognize_mod.CONNECT_ATTEMPTS if connect_failed else 1)


# --- Пределы --------------------------------------------------------------------------------------


async def test_minute_limit_60_then_429_and_window_slides(client, csrf, config_dir, fake_sk, clock):
    configure(config_dir)
    body = pcm(0.5)
    for _ in range(60):
        assert (await client.post(API, content=body, headers=hdrs(csrf))).status_code == 200
    r = await client.post(API, content=body, headers=hdrs(csrf))
    assert r.status_code == 429 and r.json() == {"detail": "dictate_rate"}
    assert len(fake_sk.requests) == 60
    clock.advance_ms(60_001)
    assert (await client.post(API, content=body, headers=hdrs(csrf))).status_code == 200


async def test_daily_cap_from_config_then_429_and_resets_next_day(client, csrf, config_dir, fake_sk, clock):
    configure(config_dir, daily_chunks=3)
    body = pcm(0.5)
    for _ in range(3):
        assert (await client.post(API, content=body, headers=hdrs(csrf))).status_code == 200
    r = await client.post(API, content=body, headers=hdrs(csrf))
    assert r.status_code == 429 and r.json() == {"detail": "dictate_daily"}
    assert len(fake_sk.requests) == 3
    clock.advance_ms(24 * 3600 * 1000)
    assert (await client.post(API, content=body, headers=hdrs(csrf))).status_code == 200


async def test_failed_call_counts_toward_daily_cap(client, csrf, config_dir, fake_sk):
    configure(config_dir, daily_chunks=1)
    fake_sk.status = 500
    assert (await client.post(API, content=pcm(0.5), headers=hdrs(csrf))).status_code == 502
    fake_sk.status = 200
    r = await client.post(API, content=pcm(0.5), headers=hdrs(csrf))
    assert r.status_code == 429 and r.json() == {"detail": "dictate_daily"}


async def test_rejected_body_does_not_spend_limits(client, csrf, config_dir, fake_sk):
    configure(config_dir, daily_chunks=1)
    for _ in range(70):
        assert (await client.post(API, content=pcm(0.1), headers=hdrs(csrf))).status_code == 400
    assert (await client.post(API, content=pcm(0.5), headers=hdrs(csrf))).status_code == 200


# --- Журнал ---------------------------------------------------------------------------------------


async def test_log_one_debug_line_without_text_audio_or_key(client, csrf, config_dir, fake_sk, caplog):
    configure(config_dir)
    fake_sk.body = {"result": "секретная фраза"}
    with caplog.at_level(logging.DEBUG):
        r = await client.post(API, content=pcm(1), headers=hdrs(csrf))
    assert r.status_code == 200
    lines = [rec.getMessage() for rec in caplog.records if rec.name == "hub.core.routes.dictate"]
    assert len(lines) == 1 and lines[0].startswith(f"диктовка: {SEC} байт, ") and lines[0].endswith(", 200")
    everything = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "секретная фраза" not in everything and KEY not in everything and FOLDER not in everything


# --- Удалённый слушатель (телефон) ----------------------------------------------------------------

REMOTE_PORT = 8790
REMOTE_HOST = "hub.test.example:8787"
REMOTE_ORIGIN = f"http://{REMOTE_HOST}"
PASSWORD = "correct horse battery"


@pytest.fixture
def rapp(db, clock, config_dir, fake_sk, monkeypatch):
    monkeypatch.setattr(remote_mod, "SCRYPT_LOG_N", 10)
    state = RemoteState(port=REMOTE_PORT)
    with db.reader() as conn:
        state.load(conn)
    notifier = Notifier()
    db.on_commit(notifier.notify)
    application = create_public_app(
        db, clock=clock, port=PORT, remote_port=REMOTE_PORT, remote=state, notifier=notifier
    )
    application.state.config_dir = config_dir
    application.state.speech_transport = fake_sk.transport
    return application


async def _phone_session(rapp, db, clock):
    local = AsyncClient(transport=ASGITransport(app=rapp), base_url=f"http://127.0.0.1:{PORT}")
    csrf = await login(local, db, clock)
    h = {"Origin": ORIGIN, "X-Hub-CSRF": csrf}
    assert (
        await local.put("/api/v1/remote/password", json={"password": PASSWORD}, headers=h)
    ).status_code == 200
    r = await local.put("/api/v1/remote", json={"enabled": True, "hosts": [REMOTE_HOST]}, headers=h)
    assert r.status_code == 200, r.text
    await local.aclose()
    phone = AsyncClient(
        transport=ASGITransport(app=rapp),
        base_url=f"http://127.0.0.1:{REMOTE_PORT}",
        headers={"Host": REMOTE_HOST},
    )
    r = await phone.post(
        "/api/v1/auth/password", json={"password": PASSWORD}, headers={"Origin": REMOTE_ORIGIN}
    )
    assert r.status_code == 200, r.text
    return phone, r.json()["csrf"]


async def test_remote_listener_passes_900kb_chunk(rapp, db, clock, config_dir, fake_sk):
    configure(config_dir)
    phone, csrf = await _phone_session(rapp, db, clock)
    try:
        body = b"\x00\x01" * 450_000  # 900 000 байт = 28,1 с
        h = {"Origin": REMOTE_ORIGIN, "X-Hub-CSRF": csrf, "Content-Type": "application/octet-stream"}
        assert (await phone.get(API)).json() == {"enabled": True}
        r = await phone.post(API, content=body, headers=h)
        assert r.status_code == 200 and r.json() == {"text": "привет мир"}
        assert fake_sk.bodies == [body]
        # Больше 1 МиБ на удалённом слушателе режет общий предел middleware — до ручки.
        r = await phone.post(API, content=b"\x00" * (1024 * 1024 + 2), headers=h)
        assert r.status_code == 413
        assert len(fake_sk.requests) == 1
    finally:
        await phone.aclose()
