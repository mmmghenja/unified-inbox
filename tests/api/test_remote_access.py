"""Ф10-7: доступ с телефона — второй слушатель, вход паролем, сессии (docs/architecture.md,
решения 1–7, раздел «API», поправки П1, П4–П6).

Слушатель определяется ПРИНЯВШИМ сокетом: у `ASGITransport` это `scope["server"] = (host, port)` из URL
запроса, поэтому «телефон» — клиент с `base_url=http://127.0.0.1:8790` и заголовком `Host` удалённого адреса
(как у настоящего туннеля: браузер шлёт `Host: hub.<домен>:8787`, а принимает сокет 127.0.0.1:8790).
"""

import asyncio
import json
import logging

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import remote as remote_mod
from hub.core import security
from hub.core.app_internal import create_internal_app
from hub.core.app_public import create_public_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.feed import Notifier
from hub.core.remote import RemoteState
from hub.core.security import compute_csrf

from .conftest import BASE_URL, ORIGIN, PORT, make_bearer_token

REMOTE_PORT = 8790
REMOTE_HOST = "hub.test.example:8787"
REMOTE_ORIGIN = f"http://{REMOTE_HOST}"
OTHER_REMOTE_HOST = "hub2.test.example:8787"
PASSWORD = "correct horse battery"
PHONE_UA = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/140.0 Mobile Safari/537.36"
ADMIN_SECRET = ADMIN_PREFIX + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHI"


@pytest.fixture(autouse=True)
def fast_scrypt(monkeypatch):
    """Параметры scrypt хранятся в строке хеша — для скорости тестов считаем с n=2**10. Настоящие параметры
    (n=2**15) проверяет `tests/core/test_remote.py`."""
    monkeypatch.setattr(remote_mod, "SCRYPT_LOG_N", 10)


@pytest.fixture
def remote_state(db) -> RemoteState:
    state = RemoteState(port=REMOTE_PORT)
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
async def local(rapp):
    async with AsyncClient(transport=ASGITransport(app=rapp), base_url=BASE_URL) as c:
        yield c


def _phone_client(app) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"http://127.0.0.1:{REMOTE_PORT}",
        headers={"Host": REMOTE_HOST, "User-Agent": PHONE_UA},
    )


@pytest.fixture
async def phone(rapp):
    async with _phone_client(rapp) as c:
        yield c


async def local_login(local, db, clock) -> tuple[str, str]:
    now_ms = int(clock() * 1000)
    code = db.write_sync(lambda conn: security.create_login_code(conn, now_ms=now_ms))
    r = await local.post("/api/v1/auth/session", json={"code": code}, headers={"Origin": ORIGIN})
    assert r.status_code == 200, r.text
    return local.cookies.get("hub_session"), r.json()["csrf"]


def local_headers(csrf: str) -> dict:
    return {"Origin": ORIGIN, "X-Hub-CSRF": csrf}


def phone_headers(csrf: str) -> dict:
    return {"Origin": REMOTE_ORIGIN, "X-Hub-CSRF": csrf}


async def enable_remote(local, csrf, *, hosts=(REMOTE_HOST,), password=PASSWORD) -> None:
    r = await local.put("/api/v1/remote/password", json={"password": password}, headers=local_headers(csrf))
    assert r.status_code == 200, r.text
    r = await local.put(
        "/api/v1/remote", json={"enabled": True, "hosts": list(hosts)}, headers=local_headers(csrf)
    )
    assert r.status_code == 200, r.text


async def phone_login(phone, password=PASSWORD):
    return await phone.post(
        "/api/v1/auth/password", json={"password": password}, headers={"Origin": REMOTE_ORIGIN}
    )


@pytest.fixture
async def enabled(local, db, clock):
    """Локальная UI-сессия + удалённый доступ включён с паролем и адресом. Возвращает (cookie, csrf)."""
    cookie, csrf = await local_login(local, db, clock)
    await enable_remote(local, csrf)
    return cookie, csrf


def _audit_rows(db, action: str | None = None) -> list[tuple]:
    with db.reader() as conn:
        if action is None:
            return conn.execute("SELECT * FROM main.audit ORDER BY id").fetchall()
        return conn.execute(
            "SELECT result, api_token_id, detail_json FROM main.audit WHERE action = ? ORDER BY id", (action,)
        ).fetchall()


def _token_row(db, token_id: int) -> dict:
    with db.reader() as conn:
        row = conn.execute(
            "SELECT revoked, expires_ts, created_ts, last_used_ts, via, user_agent FROM main.api_token "
            "WHERE id = ?",
            (token_id,),
        ).fetchone()
    keys = ("revoked", "expires_ts", "created_ts", "last_used_ts", "via", "user_agent")
    return dict(zip(keys, row, strict=True))


# === Решение 1–2: слушатель по принявшему сокету, политика удалённого слушателя ===


async def test_remote_disabled_is_421_on_everything(phone, local, db, clock):
    # Пароль и адрес заданы, но доступ не включён — удалённый слушатель закрыт целиком.
    _cookie, csrf = await local_login(local, db, clock)
    r = await local.put("/api/v1/remote/password", json={"password": PASSWORD}, headers=local_headers(csrf))
    assert r.status_code == 200
    r = await local.put("/api/v1/remote", json={"hosts": [REMOTE_HOST]}, headers=local_headers(csrf))
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    for path in ("/api/v1/healthz", "/", "/api/v1/auth/me"):
        r = await phone.get(path)
        assert r.status_code == 421, path
    r = await phone_login(phone)
    assert r.status_code == 421
    # Локальный слушатель — как был.
    assert (await local.get("/api/v1/healthz")).status_code == 200


async def test_unknown_listener_port_is_421(rapp, enabled):
    async with AsyncClient(
        transport=ASGITransport(app=rapp), base_url="http://127.0.0.1:9999", headers={"Host": REMOTE_HOST}
    ) as other:
        assert (await other.get("/api/v1/healthz")).status_code == 421
    async with AsyncClient(
        transport=ASGITransport(app=rapp),
        base_url="http://127.0.0.1:9999",
        headers={"Host": "127.0.0.1:8787"},
    ) as other:
        assert (await other.get("/api/v1/healthz")).status_code == 421


async def test_local_host_on_remote_port_and_remote_host_on_local_port_are_421(rapp, enabled, local, phone):
    assert (await phone.get("/api/v1/healthz")).status_code == 200
    # Host локальный, пришёл на 8790.
    assert (await phone.get("/api/v1/healthz", headers={"Host": "127.0.0.1:8787"})).status_code == 421
    assert (await phone.get("/api/v1/healthz", headers={"Host": "localhost:8787"})).status_code == 421
    # Host удалённый, пришёл на 8787.
    assert (await local.get("/api/v1/healthz", headers={"Host": REMOTE_HOST})).status_code == 421


async def test_remote_host_must_be_listed_and_single(phone, enabled):
    assert (await phone.get("/api/v1/healthz", headers={"Host": "evil.example:8787"})).status_code == 421
    assert (await phone.get("/api/v1/healthz", headers={"Host": "HUB.TEST.EXAMPLE:8787"})).status_code == 421
    r = await phone.get("/api/v1/healthz", headers=[("Host", REMOTE_HOST), ("Host", "evil.example:8787")])
    assert r.status_code == 421


async def test_authorization_on_remote_is_403_get_and_post(rapp, phone, enabled, db):
    token = make_bearer_token(db, scopes=("read",))
    for headers in (
        {"Authorization": f"Bearer {token}"},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": ""},
    ):
        assert (await phone.get("/api/v1/projects", headers=headers)).status_code == 403
        r = await phone.post(
            "/api/v1/auth/password", json={"password": PASSWORD}, headers={**headers, "Origin": REMOTE_ORIGIN}
        )
        assert r.status_code == 403
    # На локальном bearer — как был.
    async with AsyncClient(transport=ASGITransport(app=rapp), base_url=BASE_URL) as local2:
        assert (
            await local2.get("/api/v1/projects", headers={"Authorization": f"Bearer {token}"})
        ).status_code == 200


async def test_remote_options_is_405(phone, enabled):
    assert (await phone.options("/api/v1/healthz")).status_code == 405


async def test_remote_origin_rules(phone, enabled, db):
    r = await phone_login(phone)
    assert r.status_code == 200
    csrf = r.json()["csrf"]

    async def logout(headers):
        return await phone.post("/api/v1/auth/logout", headers=headers)

    assert (await logout({"X-Hub-CSRF": csrf})).status_code == 403  # без Origin
    assert (await logout({"Origin": "http://evil.example:8787", "X-Hub-CSRF": csrf})).status_code == 403
    assert (await logout({"Origin": ORIGIN, "X-Hub-CSRF": csrf})).status_code == 403  # локальный Origin
    assert (await logout({"Origin": f"https://{REMOTE_HOST}", "X-Hub-CSRF": csrf})).status_code == 403
    r = await phone.post(
        "/api/v1/auth/logout",
        headers=[("Origin", REMOTE_ORIGIN), ("Origin", REMOTE_ORIGIN), ("X-Hub-CSRF", csrf)],
    )
    assert r.status_code == 403
    assert (await logout({**phone_headers(csrf), "Sec-Fetch-Site": "cross-site"})).status_code == 403
    assert (await logout({"Origin": REMOTE_ORIGIN})).status_code == 403  # без CSRF при куке
    assert (await logout({"Origin": REMOTE_ORIGIN, "X-Hub-CSRF": "0" * 64})).status_code == 403
    assert (await logout({**phone_headers(csrf), "Sec-Fetch-Site": "same-origin"})).status_code == 200


async def test_origin_of_other_listed_remote_host_is_403(rapp, local, db, clock):
    _cookie, csrf = await local_login(local, db, clock)
    await enable_remote(local, csrf, hosts=(REMOTE_HOST, OTHER_REMOTE_HOST))
    async with _phone_client(rapp) as phone:
        r = await phone_login(phone)
        assert r.status_code == 200
        phone_csrf = r.json()["csrf"]
        r = await phone.post(
            "/api/v1/auth/renew", headers={"Origin": f"http://{OTHER_REMOTE_HOST}", "X-Hub-CSRF": phone_csrf}
        )
        assert r.status_code == 403
        r = await phone.post("/api/v1/auth/renew", headers=phone_headers(phone_csrf))
        assert r.status_code == 200


async def test_password_login_is_csrf_exempt_even_with_stale_cookie(phone, enabled):
    phone.cookies.set("hub_session", "stale-cookie-value")
    r = await phone_login(phone)
    assert r.status_code == 200, r.text
    # Без Origin — нельзя и здесь.
    r = await phone.post("/api/v1/auth/password", json={"password": PASSWORD})
    assert r.status_code == 403


async def test_code_login_is_local_only_and_password_login_remote_only(phone, local, enabled, db, clock):
    now_ms = int(clock() * 1000)
    code = db.write_sync(lambda conn: security.create_login_code(conn, now_ms=now_ms))
    r = await phone.post("/api/v1/auth/session", json={"code": code}, headers={"Origin": REMOTE_ORIGIN})
    assert r.status_code == 403
    r = await local.post("/api/v1/auth/password", json={"password": PASSWORD}, headers={"Origin": ORIGIN})
    assert r.status_code == 403
    # Код не израсходован удалённой попыткой — локально он всё ещё проходит.
    local.cookies.clear()
    r = await local.post("/api/v1/auth/session", json={"code": code}, headers={"Origin": ORIGIN})
    assert r.status_code == 200


# === Вход паролем, сессии ===


async def test_password_login_creates_remote_session(phone, enabled, db, clock):
    r = await phone_login(phone)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"csrf", "expires_ts"}
    set_cookie = r.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "samesite=strict" in set_cookie and "path=/" in set_cookie
    assert "domain" not in set_cookie and "secure" not in set_cookie
    assert "max-age=2592000" in set_cookie

    r = await phone.get("/api/v1/auth/me")
    assert r.status_code == 200
    me = r.json()
    assert me["remote"] is True
    assert me["kind"] == "ui"
    row = _token_row(db, me["token_id"])
    now_ms = int(clock() * 1000)
    assert row == {
        "revoked": 0,
        "expires_ts": now_ms + security.UI_SESSION_TTL_MS,
        "created_ts": now_ms,
        "last_used_ts": now_ms,
        "via": "remote",
        "user_agent": PHONE_UA,
    }
    assert [row[0] for row in _audit_rows(db, "remote_login")] == ["ok"]


async def test_local_me_says_not_remote_and_code_login_fills_fields(local, db, clock):
    long_ua = "X" * 500
    now_ms = int(clock() * 1000)
    code = db.write_sync(lambda conn: security.create_login_code(conn, now_ms=now_ms))
    r = await local.post(
        "/api/v1/auth/session", json={"code": code}, headers={"Origin": ORIGIN, "User-Agent": long_ua}
    )
    assert r.status_code == 200
    me = (await local.get("/api/v1/auth/me")).json()
    assert me["remote"] is False
    row = _token_row(db, me["token_id"])
    assert row["via"] == "local"
    assert row["created_ts"] == now_ms and row["last_used_ts"] == now_ms
    assert row["user_agent"] == "X" * 300


async def test_wrong_password_is_401_and_audited(phone, enabled, db):
    r = await phone_login(phone, "wrong password!")
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid password"}
    assert [row[0] for row in _audit_rows(db, "remote_login")] == ["invalid"]


async def test_password_not_set_is_403(rapp, db, remote_state):
    db.write_sync(
        lambda conn: (
            remote_mod._meta_set(conn, remote_mod.META_ENABLED, "1"),  # noqa: SLF001
            remote_mod._meta_set(conn, remote_mod.META_HOSTS, json.dumps([REMOTE_HOST])),  # noqa: SLF001
        )
    )
    with db.reader() as conn:
        remote_state.load(conn)
    async with _phone_client(rapp) as phone:
        r = await phone_login(phone)
    assert r.status_code == 403


async def test_block_after_five_failures_doubles_and_skips_verification(
    phone, enabled, db, clock, monkeypatch
):
    calls = []
    real_verify = remote_mod.verify_password

    def counting_verify(password, stored):
        calls.append(1)
        return real_verify(password, stored)

    monkeypatch.setattr(remote_mod, "verify_password", counting_verify)

    for _ in range(4):
        assert (await phone_login(phone, "wrong password!")).status_code == 401
    assert (await phone_login(phone, "wrong password!")).status_code == 401  # пятая — начало блока
    assert len(calls) == 5

    r = await phone_login(phone, "wrong password!")
    assert r.status_code == 429
    assert r.json() == {"detail": "too many attempts", "retry_after_s": 60}
    # Верный под блоком тоже 429 и без проверки пароля.
    r = await phone_login(phone)
    assert r.status_code == 429
    assert len(calls) == 5

    clock.advance_ms(61_000)
    assert (await phone_login(phone, "wrong password!")).status_code == 401  # шестая подряд -> блок 2 мин
    r = await phone_login(phone)
    assert r.status_code == 429
    assert r.json()["retry_after_s"] == 120

    clock.advance_ms(121_000)
    assert (await phone_login(phone)).status_code == 200  # верный сбрасывает счётчик
    assert (await phone_login(phone, "wrong password!")).status_code == 401
    assert (await phone_login(phone)).status_code == 200


async def test_block_is_capped_at_one_hour(phone, enabled, clock):
    for _ in range(5):
        await phone_login(phone, "wrong password!")
    for _ in range(12):
        clock.advance_ms(61 * 60_000)
        assert (await phone_login(phone, "wrong password!")).status_code == 401
    r = await phone_login(phone)
    assert r.status_code == 429
    assert r.json()["retry_after_s"] == 3600


async def test_block_survives_app_restart(rapp, enabled, db, clock, notifier):
    async with _phone_client(rapp) as phone:
        for _ in range(5):
            await phone_login(phone, "wrong password!")
    new_state = RemoteState(port=REMOTE_PORT)
    with db.reader() as conn:
        new_state.load(conn)
    new_app = create_public_app(
        db, clock=clock, port=PORT, remote_port=REMOTE_PORT, remote=new_state, notifier=notifier
    )
    async with _phone_client(new_app) as phone:
        r = await phone_login(phone)
    assert r.status_code == 429


async def test_parallel_burst_gives_at_most_five_checks(phone, enabled, db, monkeypatch):
    calls = []
    real_verify = remote_mod.verify_password

    def slow_counting_verify(password, stored):
        calls.append(1)
        return real_verify(password, stored)

    monkeypatch.setattr(remote_mod, "verify_password", slow_counting_verify)
    total_401 = 0
    for burst in range(5):
        responses = await asyncio.gather(
            *(phone_login(phone, f"wrong password {burst}-{i}") for i in range(20))
        )
        statuses = [r.status_code for r in responses]
        assert set(statuses) <= {401, 429}
        total_401 += statuses.count(401)
        assert len(calls) <= 5
    # Ровно пять проверок до блока (без замка было бы по 20 на пачку), дальше — только 429.
    assert len(calls) == 5
    assert total_401 == 5


async def test_queue_overflow_is_429_without_db_write(phone, enabled, db, remote_state):
    before = len(_audit_rows(db))
    remote_state.pw_waiting = remote_mod.QUEUE_MAX_WAITING
    try:
        r = await phone_login(phone, "wrong password!")
    finally:
        remote_state.pw_waiting = 0
    assert r.status_code == 429
    assert r.json()["retry_after_s"] >= 1
    assert len(_audit_rows(db)) == before


async def test_audit_under_block_is_not_written_and_blocked_once(phone, enabled, db):
    for _ in range(5):
        await phone_login(phone, "wrong password!")
    rows = [row[0] for row in _audit_rows(db, "remote_login")]
    assert rows == ["invalid"] * 5 + ["blocked"]
    before = len(_audit_rows(db))
    for _ in range(10):
        assert (await phone_login(phone, "wrong password!")).status_code == 429
    assert len(_audit_rows(db)) == before


async def test_password_never_in_audit_or_logs(local, db, clock, rapp, caplog):
    secret_pw = "UniqueSecretPw-7f3a9"
    wrong_pw = "UniqueWrongPw-c2d1e"
    caplog.set_level(logging.DEBUG)
    _cookie, csrf = await local_login(local, db, clock)
    await enable_remote(local, csrf, password=secret_pw)
    async with _phone_client(rapp) as phone:
        assert (await phone_login(phone, wrong_pw)).status_code == 401
        assert (await phone_login(phone, secret_pw)).status_code == 200
    r = await local.put("/api/v1/remote/password", json={"password": "Shrt-9x"}, headers=local_headers(csrf))
    assert r.status_code == 422
    dump = json.dumps(_audit_rows(db), ensure_ascii=False, default=str)
    with db.reader() as conn:
        meta = json.dumps(conn.execute("SELECT key, value FROM main.meta").fetchall())
    for needle in (secret_pw, wrong_pw, "Shrt-9x"):
        assert needle not in dump
        assert needle not in caplog.text
        for record in caplog.records:
            assert needle not in record.getMessage()
    assert secret_pw not in meta and wrong_pw not in meta
    assert "scrypt$" in meta


# === П5: гонка «вход против выключения/смены пароля» ===


async def test_race_password_changed_during_check_is_401(phone, enabled, db, clock, monkeypatch):
    real_verify = remote_mod.verify_password

    def verify_then_change(password, stored):
        result = real_verify(password, stored)
        new_stored = remote_mod.hash_password("another password 123")
        db.write_sync(
            lambda conn: remote_mod.set_password(conn, stored=new_stored, now_ms=int(clock() * 1000))
        )
        return result

    monkeypatch.setattr(remote_mod, "verify_password", verify_then_change)
    r = await phone_login(phone)
    assert r.status_code == 401
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.api_token WHERE via = 'remote'").fetchone()[0] == 0


async def test_race_disabled_during_check_is_421(phone, enabled, db, clock, monkeypatch):
    real_verify = remote_mod.verify_password

    def verify_then_disable(password, stored):
        result = real_verify(password, stored)
        db.write_sync(
            lambda conn: remote_mod.update_config(
                conn, enabled=False, hosts=None, now_ms=int(clock() * 1000), actor="admin"
            )
        )
        return result

    monkeypatch.setattr(remote_mod, "verify_password", verify_then_disable)
    r = await phone_login(phone)
    assert r.status_code == 421
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.api_token WHERE via = 'remote'").fetchone()[0] == 0


# === Настройки: пароль, включение, адреса ===


async def test_password_change_revokes_remote_sessions_only(local, rapp, enabled, db):
    _cookie, csrf = enabled
    async with _phone_client(rapp) as phone:
        assert (await phone_login(phone)).status_code == 200
        assert (await phone.get("/api/v1/auth/me")).status_code == 200
        r = await local.put(
            "/api/v1/remote/password", json={"password": "new password 456"}, headers=local_headers(csrf)
        )
        assert r.status_code == 200
        assert r.json() == {"ok": True, "revoked_remote": 1}
        assert (await phone.get("/api/v1/auth/me")).status_code == 401
        assert (await phone_login(phone)).status_code == 401
        assert (await phone_login(phone, "new password 456")).status_code == 200
    assert (await local.get("/api/v1/auth/me")).status_code == 200


async def test_disable_revokes_remote_sessions_only_and_closes_listener(local, rapp, enabled, db):
    _cookie, csrf = enabled
    async with _phone_client(rapp) as phone:
        assert (await phone_login(phone)).status_code == 200
        r = await local.put("/api/v1/remote", json={"enabled": False}, headers=local_headers(csrf))
        assert r.status_code == 200
        assert r.json()["enabled"] is False
        assert (await phone.get("/api/v1/auth/me")).status_code == 421
        assert (await phone_login(phone)).status_code == 421
        with db.reader() as conn:
            live_remote = conn.execute(
                "SELECT COUNT(*) FROM main.api_token WHERE via = 'remote' AND revoked = 0"
            ).fetchone()[0]
        assert live_remote == 0
        # Включение обратно — старые телефонные сессии не оживают.
        r = await local.put("/api/v1/remote", json={"enabled": True}, headers=local_headers(csrf))
        assert r.status_code == 200
        assert (await phone.get("/api/v1/auth/me")).status_code == 401
    assert (await local.get("/api/v1/auth/me")).status_code == 200


async def test_enable_requires_password_and_host(local, db, clock):
    _cookie, csrf = await local_login(local, db, clock)
    r = await local.put("/api/v1/remote", json={"enabled": True}, headers=local_headers(csrf))
    assert r.status_code == 422
    r = await local.put("/api/v1/remote", json={"hosts": [REMOTE_HOST]}, headers=local_headers(csrf))
    assert r.status_code == 200
    r = await local.put("/api/v1/remote", json={"enabled": True}, headers=local_headers(csrf))
    assert r.status_code == 422  # пароля нет
    r = await local.put("/api/v1/remote/password", json={"password": PASSWORD}, headers=local_headers(csrf))
    assert r.status_code == 200
    r = await local.put("/api/v1/remote", json={"enabled": True}, headers=local_headers(csrf))
    assert r.status_code == 200
    assert r.json()["enabled"] is True


@pytest.mark.parametrize(
    "hosts",
    [
        [],
        ["localhost:8787"],
        ["127.0.0.1:8787"],
        ["127.1.2.3:80"],
        ["hub.localhost:8787"],
        ["hub.example.com"],
        ["hub.example.com:0"],
        ["hub.example.com:70000"],
        ["-hub.example.com:8787"],
        ["hub..example.com:8787"],
        ["hub_example.com:8787"],
        ["http://hub.example.com:8787"],
        [f"h{i}.example.com:8787" for i in range(6)],
    ],
)
async def test_invalid_hosts_are_422(local, db, clock, hosts):
    _cookie, csrf = await local_login(local, db, clock)
    r = await local.put("/api/v1/remote", json={"hosts": hosts}, headers=local_headers(csrf))
    assert r.status_code == 422


async def test_hosts_are_normalized_to_lowercase(local, db, clock):
    _cookie, csrf = await local_login(local, db, clock)
    r = await local.put(
        "/api/v1/remote",
        json={"hosts": [" Hub.Example.COM:8787 ", "hub.example.com:8787"]},
        headers=local_headers(csrf),
    )
    assert r.status_code == 200
    assert r.json()["hosts"] == ["hub.example.com:8787"]


async def test_password_length_bounds(local, db, clock):
    _cookie, csrf = await local_login(local, db, clock)
    for pw, status in (("x" * 9, 422), ("x" * 10, 200), ("x" * 200, 200), ("x" * 201, 422)):
        r = await local.put("/api/v1/remote/password", json={"password": pw}, headers=local_headers(csrf))
        assert r.status_code == status, len(pw)


async def test_remote_settings_are_local_only(phone, enabled):
    r = await phone_login(phone)
    csrf = r.json()["csrf"]
    r = await phone.put("/api/v1/remote", json={"enabled": False}, headers=phone_headers(csrf))
    assert r.status_code == 403
    r = await phone.put("/api/v1/remote/password", json={"password": "x" * 20}, headers=phone_headers(csrf))
    assert r.status_code == 403
    r = await phone.get("/api/v1/remote")
    assert r.status_code == 200
    assert r.json()["enabled"] is True


async def test_get_remote_status_fields(local, phone, enabled, db, clock, remote_state):
    remote_state.listening = True
    await phone_login(phone, "wrong password!")
    r = await local.get("/api/v1/remote")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "enabled": True,
        "hosts": [REMOTE_HOST],
        "password_set": True,
        "password_set_ts": int(clock() * 1000),
        "listening": True,
        "failures_24h": 1,
        "blocked_until": None,
        "tls": None,  # Ф14-9: пары https нет — http
    }
    for _ in range(4):
        await phone_login(phone, "wrong password!")
    body = (await local.get("/api/v1/remote")).json()
    assert body["failures_24h"] == 5
    assert body["blocked_until"] == int(clock() * 1000) + 60_000
    clock.advance_ms(24 * 60 * 60 * 1000 + 1)
    body = (await local.get("/api/v1/remote")).json()
    assert body["failures_24h"] == 0
    assert body["blocked_until"] is None


async def test_remote_endpoints_need_ui_session(rapp, db, enabled):
    token = make_bearer_token(db, scopes=("read",))
    async with AsyncClient(transport=ASGITransport(app=rapp), base_url=BASE_URL) as bare:
        r = await bare.get("/api/v1/remote", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


# === Сессии: слушатель, отзыв прежней, продление, список, выход ===


async def test_remote_cookie_on_local_is_401_and_local_cookie_on_remote_is_401(local, rapp, enabled):
    local_cookie, _csrf = enabled
    async with _phone_client(rapp) as phone:
        r = await phone_login(phone)
        phone_cookie = phone.cookies.get("hub_session")
        assert r.status_code == 200
        async with AsyncClient(transport=ASGITransport(app=rapp), base_url=BASE_URL) as bare_local:
            r = await bare_local.get("/api/v1/auth/me", headers={"Cookie": f"hub_session={phone_cookie}"})
            assert r.status_code == 401
        phone.cookies.clear()
        r = await phone.get("/api/v1/auth/me", headers={"Cookie": f"hub_session={local_cookie}"})
        assert r.status_code == 401


async def test_old_null_via_session_is_local_only(local, rapp, enabled, db, clock):
    """Сессии до 25.09 (via NULL) — локальные: на удалённом слушателе не действуют."""
    local_cookie, _csrf = enabled
    db.write_sync(lambda conn: conn.execute("UPDATE main.api_token SET via = NULL WHERE kind = 'ui'"))
    assert (await local.get("/api/v1/auth/me")).status_code == 200
    async with _phone_client(rapp) as phone:
        r = await phone.get("/api/v1/auth/me", headers={"Cookie": f"hub_session={local_cookie}"})
        assert r.status_code == 401


async def test_login_over_live_cookie_revokes_previous(local, phone, enabled, db, clock):
    first_id = (await local.get("/api/v1/auth/me")).json()["token_id"]
    await local_login(local, db, clock)  # тот же браузер, кука первой сессии уходит с запросом
    second_id = (await local.get("/api/v1/auth/me")).json()["token_id"]
    assert second_id != first_id
    assert _token_row(db, first_id)["revoked"] == 1
    assert _token_row(db, second_id)["revoked"] == 0

    assert (await phone_login(phone)).status_code == 200
    p1 = (await phone.get("/api/v1/auth/me")).json()["token_id"]
    assert (await phone_login(phone)).status_code == 200
    p2 = (await phone.get("/api/v1/auth/me")).json()["token_id"]
    assert p1 != p2
    assert _token_row(db, p1)["revoked"] == 1
    assert _token_row(db, p2)["revoked"] == 0


async def test_renew_writes_at_most_every_ten_minutes(local, db, clock):
    _cookie, csrf = await local_login(local, db, clock)
    token_id = (await local.get("/api/v1/auth/me")).json()["token_id"]
    start = _token_row(db, token_id)

    clock.advance_ms(9 * 60_000)
    r = await local.post("/api/v1/auth/renew", headers=local_headers(csrf))
    assert r.status_code == 200
    assert r.json() == {"expires_ts": start["expires_ts"]}
    assert "max-age=2592000" in r.headers["set-cookie"].lower()
    assert _token_row(db, token_id) == start  # без записи

    clock.advance_ms(60_000)  # ровно 10 минут с входа
    now_ms = int(clock() * 1000)
    r = await local.post("/api/v1/auth/renew", headers=local_headers(csrf))
    assert r.status_code == 200
    assert r.json() == {"expires_ts": now_ms + security.UI_SESSION_TTL_MS}
    row = _token_row(db, token_id)
    assert row["expires_ts"] == now_ms + security.UI_SESSION_TTL_MS
    assert row["last_used_ts"] == now_ms

    clock.advance_ms(5 * 60_000)
    r = await local.post("/api/v1/auth/renew", headers=local_headers(csrf))
    assert r.json() == {"expires_ts": now_ms + security.UI_SESSION_TTL_MS}
    assert _token_row(db, token_id)["last_used_ts"] == now_ms


async def test_renew_keeps_session_alive_past_30_days(local, db, clock):
    _cookie, csrf = await local_login(local, db, clock)
    for _ in range(3):
        clock.advance_ms(20 * 24 * 60 * 60 * 1000)
        assert (await local.post("/api/v1/auth/renew", headers=local_headers(csrf))).status_code == 200
    assert (await local.get("/api/v1/auth/me")).status_code == 200


async def test_renew_is_ui_only(local, db):
    token = make_bearer_token(db, scopes=("read",))
    r = await local.post("/api/v1/auth/renew", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


async def test_sessions_list_order_and_current(local, rapp, enabled, db, clock):
    local_id = (await local.get("/api/v1/auth/me")).json()["token_id"]
    # Старая сессия «до 25.09»: все новые поля NULL.
    old_id = db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.api_token (name, kind, hash, scopes, expires_ts, revoked) "
                "VALUES ('ui', 'ui', 'h-old', 'read send admin', ?, 0)",
                (int(clock() * 1000) + 1000,),
            ).lastrowid
        )
    )
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.api_token (name, kind, hash, scopes, expires_ts, revoked, via) "
            "VALUES ('ui', 'ui', 'h-dead', 'read send admin', ?, 1, 'local')",
            (int(clock() * 1000) + 1000,),
        )
    )
    make_bearer_token(db, scopes=("read",))
    clock.advance_ms(1000)
    async with _phone_client(rapp) as phone:
        assert (await phone_login(phone)).status_code == 200
        phone_id = (await phone.get("/api/v1/auth/me")).json()["token_id"]
        r = await phone.get("/api/v1/auth/sessions")
    assert r.status_code == 200
    sessions = r.json()["sessions"]
    assert [s["id"] for s in sessions] == [phone_id, local_id]  # old_id истекла при сдвиге часов
    assert sessions[0]["current"] is True and sessions[1]["current"] is False
    assert sessions[0]["via"] == "remote" and sessions[0]["user_agent"] == PHONE_UA
    assert set(sessions[0]) == {
        "id",
        "via",
        "created_ts",
        "last_used_ts",
        "expires_ts",
        "user_agent",
        "current",
    }

    db.write_sync(
        lambda conn: conn.execute("UPDATE main.api_token SET expires_ts = ? WHERE id = ?", (10**15, old_id))
    )
    sessions = (await local.get("/api/v1/auth/sessions")).json()["sessions"]
    assert [s["id"] for s in sessions] == [phone_id, local_id, old_id]  # NULL last_used_ts — в конце
    assert sessions[2]["via"] is None and sessions[2]["created_ts"] is None
    assert [s["current"] for s in sessions] == [False, True, False]


async def test_revoke_session_from_laptop_logs_phone_out(local, rapp, enabled, db):
    _cookie, csrf = enabled
    async with _phone_client(rapp) as phone:
        await phone_login(phone)
        phone_id = (await phone.get("/api/v1/auth/me")).json()["token_id"]
        r = await local.post(f"/api/v1/auth/sessions/{phone_id}/revoke", headers=local_headers(csrf))
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        assert "set-cookie" not in r.headers  # не своя — кука ноутбука не трогается
        assert (await phone.get("/api/v1/auth/me")).status_code == 401
    r = await local.post(f"/api/v1/auth/sessions/{phone_id}/revoke", headers=local_headers(csrf))
    assert r.status_code == 404
    bearer_id = db.write_sync(
        lambda conn: security.create_api_token(
            conn, name="b", kind="interactive", scopes=["read"], project_ids=None, expires_ts=None
        )
    ).token_id
    r = await local.post(f"/api/v1/auth/sessions/{bearer_id}/revoke", headers=local_headers(csrf))
    assert r.status_code == 404
    r = await local.post("/api/v1/auth/sessions/999999/revoke", headers=local_headers(csrf))
    assert r.status_code == 404


async def test_revoke_own_session_deletes_cookie(local, db, clock):
    _cookie, csrf = await local_login(local, db, clock)
    my_id = (await local.get("/api/v1/auth/me")).json()["token_id"]
    r = await local.post(f"/api/v1/auth/sessions/{my_id}/revoke", headers=local_headers(csrf))
    assert r.status_code == 200
    assert 'hub_session=""' in r.headers["set-cookie"] or "max-age=0" in r.headers["set-cookie"].lower()


async def test_revoke_all_keep_current_and_all(local, rapp, enabled, db, clock):
    _cookie, csrf = enabled
    async with _phone_client(rapp) as phone:
        await phone_login(phone)
        phone_csrf = (await phone.get("/api/v1/auth/me")).json()["csrf"]
        r = await phone.post(
            "/api/v1/auth/sessions/revoke-all", json={"keep_current": True}, headers=phone_headers(phone_csrf)
        )
        assert r.status_code == 200
        assert r.json() == {"revoked": 1}
        assert "set-cookie" not in r.headers
        assert (await local.get("/api/v1/auth/me")).status_code == 401
        assert (await phone.get("/api/v1/auth/me")).status_code == 200
        r = await phone.post("/api/v1/auth/sessions/revoke-all", json={}, headers=phone_headers(phone_csrf))
        assert r.status_code == 422  # keep_current обязателен
        r = await phone.post(
            "/api/v1/auth/sessions/revoke-all",
            json={"keep_current": False},
            headers=phone_headers(phone_csrf),
        )
        assert r.json() == {"revoked": 1}
        assert "set-cookie" in r.headers
        assert (await phone.get("/api/v1/auth/me")).status_code == 401


async def test_sessions_endpoints_are_ui_only(local, db):
    token = make_bearer_token(db, scopes=("read",))
    headers = {"Authorization": f"Bearer {token}"}
    assert (await local.get("/api/v1/auth/sessions", headers=headers)).status_code == 403
    assert (await local.post("/api/v1/auth/sessions/1/revoke", headers=headers)).status_code == 403
    r = await local.post("/api/v1/auth/sessions/revoke-all", json={"keep_current": True}, headers=headers)
    assert r.status_code == 403


# === П1: админ-сокет и публичное приложение — одно состояние ===


@pytest.fixture
def shared_internal(db, clock, notifier, remote_state):
    return create_internal_app(
        db, clock=clock, notifier=notifier, admin_key_hash=hash_secret(ADMIN_SECRET), remote=remote_state
    )


async def test_admin_off_closes_password_login_immediately(rapp, shared_internal, enabled, db):
    admin_headers = {"Authorization": f"Bearer {ADMIN_SECRET}"}
    async with (
        _phone_client(rapp) as phone,
        AsyncClient(transport=ASGITransport(app=shared_internal), base_url="http://hub-internal") as admin,
    ):
        assert (await phone_login(phone)).status_code == 200
        r = await admin.get("/internal/v1/admin/remote", headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["enabled"] is True and r.json()["hosts"] == [REMOTE_HOST]
        r = await admin.post("/internal/v1/admin/remote/off", headers=admin_headers)
        assert r.status_code == 200
        assert r.json() == {"revoked_remote": 1}
        # Браузер телефона помнит пароль — следующий вход обязан упереться в выключенный доступ.
        assert (await phone_login(phone)).status_code == 421
        assert (await phone.get("/api/v1/auth/me")).status_code == 421
        r = await admin.get("/internal/v1/admin/remote", headers=admin_headers)
        assert r.json()["enabled"] is False


async def test_admin_off_stays_closed_even_if_write_fails(rapp, shared_internal, enabled, monkeypatch):
    """Стоп-кран закрывается в памяти ДО записи и не откатывается при её сбое."""
    admin_headers = {"Authorization": f"Bearer {ADMIN_SECRET}"}

    def broken_turn_off(conn, **_kwargs):
        raise RuntimeError("диск")

    monkeypatch.setattr(remote_mod, "turn_off", broken_turn_off)
    async with (
        _phone_client(rapp) as phone,
        AsyncClient(
            transport=ASGITransport(app=shared_internal, raise_app_exceptions=False),
            base_url="http://hub-internal",
        ) as admin,
    ):
        r = await admin.post("/internal/v1/admin/remote/off", headers=admin_headers)
        assert r.status_code == 500
        assert (await phone_login(phone)).status_code == 421


async def test_admin_watch_has_remote_login_failures(rapp, shared_internal, enabled):
    admin_headers = {"Authorization": f"Bearer {ADMIN_SECRET}"}
    async with (
        _phone_client(rapp) as phone,
        AsyncClient(transport=ASGITransport(app=shared_internal), base_url="http://hub-internal") as admin,
    ):
        for _ in range(3):
            await phone_login(phone, "wrong password!")
        r = await admin.get("/internal/v1/admin/watch", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["remote_login_failures_24h"] == 3


# === П4: открытый SSE-поток перепроверяет сессию ===


async def _open_stream(client, **kwargs) -> asyncio.Task:
    task = asyncio.create_task(client.get("/api/v1/stream", **kwargs))
    await asyncio.sleep(0.3)  # поток открылся и ждёт уведомления
    assert not task.done(), task.result() if task.done() else None
    return task


async def test_stream_closes_when_session_revoked(local, rapp, enabled, db):
    _cookie, csrf = enabled
    async with _phone_client(rapp) as phone:
        await phone_login(phone)
        phone_id = (await phone.get("/api/v1/auth/me")).json()["token_id"]
        task = await _open_stream(phone)
        r = await local.post(f"/api/v1/auth/sessions/{phone_id}/revoke", headers=local_headers(csrf))
        assert r.status_code == 200
        response = await asyncio.wait_for(task, timeout=5)
        assert response.status_code == 200


async def test_stream_closes_when_remote_disabled(local, rapp, enabled, db):
    _cookie, csrf = enabled
    async with _phone_client(rapp) as phone:
        await phone_login(phone)
        task = await _open_stream(phone)
        r = await local.put("/api/v1/remote", json={"enabled": False}, headers=local_headers(csrf))
        assert r.status_code == 200
        await asyncio.wait_for(task, timeout=5)


async def test_stream_rechecks_remote_enabled_flag(rapp, enabled, db, remote_state):
    """Поток телефона смотрит и на флаг доступа в памяти, не только на строку сессии."""
    async with _phone_client(rapp) as phone:
        await phone_login(phone)
        task = await _open_stream(phone)
        remote_state.enabled = False  # как в первый миг выключения, до записи
        db.write_sync(
            lambda conn: conn.execute("INSERT INTO main.project (name) VALUES ('будильник потока')")
        )
        await asyncio.wait_for(task, timeout=5)


async def test_local_stream_closes_on_logout(rapp, db, clock):
    async with AsyncClient(transport=ASGITransport(app=rapp), base_url=BASE_URL) as viewer:
        cookie, _csrf = await local_login(viewer, db, clock)
        task = await _open_stream(viewer)
        async with AsyncClient(transport=ASGITransport(app=rapp), base_url=BASE_URL) as other:
            r = await other.post(
                "/api/v1/auth/logout",
                headers={
                    "Cookie": f"hub_session={cookie}",
                    "Origin": ORIGIN,
                    "X-Hub-CSRF": compute_csrf(cookie),
                },
            )
            assert r.status_code == 200
        await asyncio.wait_for(task, timeout=5)


# --- находка ревью: размер тела на удалённом слушателе -----------------------------------------------------


async def test_remote_post_body_over_limit_is_413_without_reading(phone, enabled):
    """Тело больше 1 МиБ отбивается по заголовку, до разбора JSON, очереди входа и проверки сессии."""
    big = b'{"password": "' + b"x" * (1024 * 1024) + b'"}'
    r = await phone.post(
        "/api/v1/auth/password",
        content=big,
        headers={"Origin": REMOTE_ORIGIN, "Content-Type": "application/json"},
    )
    assert r.status_code == 413
    r = await phone.post(
        "/api/v1/messages/send",
        content=big,
        headers={"Origin": REMOTE_ORIGIN, "Content-Type": "application/json"},
    )
    assert r.status_code == 413


async def test_remote_post_without_content_length_is_411(phone, enabled):
    async def chunks():
        yield b'{"password": '
        yield b'"correct horse battery"}'

    r = await phone.post(
        "/api/v1/auth/password",
        content=chunks(),
        headers={"Origin": REMOTE_ORIGIN, "Content-Type": "application/json"},
    )
    assert r.status_code == 411


async def test_remote_body_limit_keeps_honest_requests_and_uploads(phone, enabled):
    """Вход паролем проходит; длинное честное тело (~80 КБ кириллицы) не упирается в потолок; загрузка файлов
    — свой путь (без куки она получает 401 от ручки, а не 413/411 от middleware)."""
    r = await phone_login(phone)
    assert r.status_code == 200, r.text
    csrf = r.json()["csrf"]
    long_text = "ж" * 40_000
    r = await phone.post(
        "/api/v1/messages/send",
        json={"channel_id": 10**9, "text": long_text, "idempotency_key": "k-long"},
        headers=phone_headers(csrf),
    )
    assert r.status_code not in (411, 413), r.status_code
    r = await phone.post(
        "/api/v1/uploads",
        content=b"x" * (2 * 1024 * 1024),
        headers={"Origin": REMOTE_ORIGIN, "X-Hub-CSRF": csrf, "Content-Type": "application/octet-stream"},
    )
    assert r.status_code not in (411, 413), r.status_code
