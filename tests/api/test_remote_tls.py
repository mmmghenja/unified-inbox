"""Ф14-9 (docs/architecture.md–7, П1, П5): удалённый слушатель по https.

Схему даёт принявший сокет: у `ASGITransport` это `scope["scheme"]` из URL запроса, поэтому «телефон по
https» — клиент с `base_url=https://127.0.0.1:8790` и заголовком `Host` удалённого адреса (как uvicorn на
TLS-сокете).

- T3: на https-слушателе Origin `https://host` проходит, `http://host` — 403; на http-слушателе наоборот;
  локальный — без изменений.
- T4 + П5: кука входа паролем и `/renew` по https — `Secure`; локальный вход кодом и `/renew` — без него.
- T5: `tls` в `GET /api/v1/remote` и `GET /internal/v1/admin/remote`; `remote_tls` в `/admin/watch` (П1).
"""

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import remote as remote_mod
from hub.core.app_internal import create_internal_app
from hub.core.app_public import create_public_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.feed import Notifier
from hub.core.remote import RemoteState
from hub.core.tls import TlsInfo

from .conftest import ORIGIN, PORT
from .test_remote_access import PASSWORD, REMOTE_HOST, REMOTE_PORT, enable_remote, local_headers, local_login

HTTPS_ORIGIN = f"https://{REMOTE_HOST}"
HTTP_ORIGIN = f"http://{REMOTE_HOST}"
ADMIN_SECRET = ADMIN_PREFIX + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHI"
ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_SECRET}"}


@pytest.fixture(autouse=True)
def fast_scrypt(monkeypatch):
    monkeypatch.setattr(remote_mod, "SCRYPT_LOG_N", 10)


@pytest.fixture
def remote_state(db) -> RemoteState:
    state = RemoteState(port=REMOTE_PORT, listening=True)
    with db.reader() as conn:
        state.load(conn)
    return state


@pytest.fixture
def notifier(db) -> Notifier:
    n = Notifier()
    db.on_commit(n.notify)
    return n


@pytest.fixture
def rapp(db, clock, remote_state, notifier):
    return create_public_app(
        db, clock=clock, port=PORT, remote_port=REMOTE_PORT, remote=remote_state, notifier=notifier
    )


@pytest.fixture
def internal(db, clock, notifier, remote_state):
    return create_internal_app(
        db, clock=clock, notifier=notifier, admin_key_hash=hash_secret(ADMIN_SECRET), remote=remote_state
    )


def _client(app, base_url: str, host: str | None = None) -> AsyncClient:
    headers = {"Host": host} if host else {}
    return AsyncClient(transport=ASGITransport(app=app), base_url=base_url, headers=headers)


@pytest.fixture
async def local(rapp):
    async with _client(rapp, f"http://127.0.0.1:{PORT}") as c:
        yield c


@pytest.fixture
async def phone_https(rapp):
    async with _client(rapp, f"https://127.0.0.1:{REMOTE_PORT}", REMOTE_HOST) as c:
        yield c


@pytest.fixture
async def phone_http(rapp):
    async with _client(rapp, f"http://127.0.0.1:{REMOTE_PORT}", REMOTE_HOST) as c:
        yield c


@pytest.fixture
async def enabled(local, db, clock):
    _cookie, csrf = await local_login(local, db, clock)
    await enable_remote(local, csrf)
    return csrf


async def _login(client: AsyncClient, origin: str):
    return await client.post("/api/v1/auth/password", json={"password": PASSWORD}, headers={"Origin": origin})


def _set_cookie(response) -> str:
    values = response.headers.get_list("set-cookie")
    assert len(values) == 1, values
    return values[0]


def _has_secure(set_cookie: str) -> bool:
    return any(part.strip().lower() == "secure" for part in set_cookie.split(";"))


# === T3: Origin по схеме слушателя ===


async def test_https_listener_accepts_https_origin_only(phone_https, enabled):
    assert (await _login(phone_https, HTTP_ORIGIN)).status_code == 403
    r = await _login(phone_https, HTTPS_ORIGIN)
    assert r.status_code == 200, r.text
    csrf = r.json()["csrf"]
    r = await phone_https.post("/api/v1/auth/logout", headers={"Origin": HTTP_ORIGIN, "X-Hub-CSRF": csrf})
    assert r.status_code == 403
    r = await phone_https.post("/api/v1/auth/logout", headers={"Origin": HTTPS_ORIGIN, "X-Hub-CSRF": csrf})
    assert r.status_code == 200


async def test_http_listener_accepts_http_origin_only(phone_http, enabled):
    assert (await _login(phone_http, HTTPS_ORIGIN)).status_code == 403
    assert (await _login(phone_http, HTTP_ORIGIN)).status_code == 200


async def test_local_listener_origin_unchanged(local, db, clock):
    _cookie, csrf = await local_login(local, db, clock)
    r = await local.post("/api/v1/auth/renew", headers=local_headers(csrf))
    assert r.status_code == 200
    r = await local.post(
        "/api/v1/auth/renew", headers={"Origin": f"https://127.0.0.1:{PORT}", "X-Hub-CSRF": csrf}
    )
    assert r.status_code == 403


async def test_unknown_scheme_on_remote_is_403(rapp, enabled):
    """Не http и не https (не бывает у uvicorn) — отказ, а не `b"None://…"`."""
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "ws",
        "path": "/api/v1/auth/password",
        "raw_path": b"/api/v1/auth/password",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", REMOTE_HOST.encode()),
            (b"origin", b"ws://" + REMOTE_HOST.encode()),
            (b"content-type", b"application/json"),
            (b"content-length", b"2"),
        ],
        "client": ("127.0.0.1", 5555),
        "server": ("127.0.0.1", REMOTE_PORT),
    }
    await rapp(scope, receive, send)
    assert sent[0]["status"] == 403


# === T4 + П5: Secure у куки по https ===


async def test_password_login_over_https_sets_secure_cookie(phone_https, enabled):
    r = await _login(phone_https, HTTPS_ORIGIN)
    assert r.status_code == 200
    cookie = _set_cookie(r)
    assert cookie.startswith("hub_session=")
    assert _has_secure(cookie)
    assert "httponly" in cookie.lower()
    assert "samesite=strict" in cookie.lower()


async def test_password_login_over_http_listener_has_no_secure(phone_http, enabled):
    r = await _login(phone_http, HTTP_ORIGIN)
    assert r.status_code == 200
    assert not _has_secure(_set_cookie(r))


async def test_renew_over_https_sets_secure_cookie(phone_https, enabled):
    r = await _login(phone_https, HTTPS_ORIGIN)
    csrf = r.json()["csrf"]
    r = await phone_https.post("/api/v1/auth/renew", headers={"Origin": HTTPS_ORIGIN, "X-Hub-CSRF": csrf})
    assert r.status_code == 200, r.text
    assert _has_secure(_set_cookie(r))
    r = await phone_https.post("/api/v1/auth/logout", headers={"Origin": HTTPS_ORIGIN, "X-Hub-CSRF": csrf})
    assert r.status_code == 200
    assert _has_secure(_set_cookie(r))


async def test_local_code_login_and_renew_have_no_secure(local, db, clock):
    from hub.core import security

    now_ms = int(clock() * 1000)
    code = db.write_sync(lambda conn: security.create_login_code(conn, now_ms=now_ms))
    r = await local.post("/api/v1/auth/session", json={"code": code}, headers={"Origin": ORIGIN})
    assert r.status_code == 200
    assert not _has_secure(_set_cookie(r))
    r = await local.post("/api/v1/auth/renew", headers=local_headers(r.json()["csrf"]))
    assert r.status_code == 200
    assert not _has_secure(_set_cookie(r))


# === T5: tls в статусе и в снимке hub-watch ===


async def test_status_tls_null_info_and_error(local, internal, enabled, remote_state):
    async with AsyncClient(transport=ASGITransport(app=internal), base_url="http://hub-internal") as admin:

        async def both() -> tuple[object, object, object]:
            public = (await local.get("/api/v1/remote")).json()["tls"]
            admin_body = (await admin.get("/internal/v1/admin/remote", headers=ADMIN_HEADERS)).json()
            watch = (await admin.get("/internal/v1/admin/watch", headers=ADMIN_HEADERS)).json()
            return public, admin_body["tls"], watch["remote_tls"]

        assert await both() == (None, None, None)

        remote_state.tls = TlsInfo(not_after_ms=1_800_000_000_000, names=("hub.test.example",), error=None)
        info = {"not_after_ms": 1_800_000_000_000, "names": ["hub.test.example"], "error": None}
        assert await both() == (info, info, {"not_after_ms": 1_800_000_000_000, "error": None})

        remote_state.tls = TlsInfo(not_after_ms=None, names=(), error="ключ не от этого сертификата")
        err = {"not_after_ms": None, "names": [], "error": "ключ не от этого сертификата"}
        assert await both() == (err, err, {"not_after_ms": None, "error": "ключ не от этого сертификата"})
