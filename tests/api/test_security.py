import hashlib
import sqlite3
from pathlib import Path

import pytest

import hub.core.db as db_module
from hub.core import security
from hub.core.outbox import enqueue

from .conftest import (
    ORIGIN,
    csrf_headers,
    insert_account,
    insert_channel,
    insert_message,
    insert_project,
    login_via_code,
    make_bearer_token,
)


# 1. Чужой Host -> 421 на API, /healthz и несуществующем пути.
# host="" — эмуляция «без заголовка»: httpx/ASGITransport всегда сами подставляют Host из base_url, если
# заголовок не задан вовсе, поэтому «отсутствие» проверяем явным пустым значением, а не пропуском заголовка.
@pytest.mark.parametrize("path", ["/api/v1/projects", "/api/v1/healthz", "/api/v1/does-not-exist-at-all"])
@pytest.mark.parametrize("host", ["evil.com", "127.0.0.1:9999", ""])
async def test_wrong_host_is_421(client, path, host):
    r = await client.get(path, headers={"Host": host})
    assert r.status_code == 421
    assert r.content == b""


# 2. POST без Origin / с чужим Origin / Sec-Fetch-Site: cross-site -> 403; верный Origin без X-Hub-CSRF ->
#    403; с неверным CSRF -> 403; с верным -> проходит.
async def test_post_origin_and_csrf(client, db, clock):
    cookie, _csrf = await login_via_code(client, db, clock)

    r = await client.post("/api/v1/auth/logout")
    assert r.status_code == 403

    r = await client.post("/api/v1/auth/logout", headers={"Origin": "http://evil.com"})
    assert r.status_code == 403

    r = await client.post("/api/v1/auth/logout", headers={"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403

    r = await client.post("/api/v1/auth/logout", headers={"Origin": ORIGIN})
    assert r.status_code == 403

    r = await client.post("/api/v1/auth/logout", headers={"Origin": ORIGIN, "X-Hub-CSRF": "0" * 64})
    assert r.status_code == 403

    r = await client.post("/api/v1/auth/logout", headers=csrf_headers(cookie))
    assert r.status_code == 200


# 3. Ни в одном ответе (200/401/403/404/421) нет Access-Control-*; OPTIONS -> 405.
async def test_no_cors_headers_ever(client, db, clock):
    cookie, _csrf = await login_via_code(client, db, clock)
    client.cookies.clear()  # дальше сами решаем, у какого запроса есть cookie, а у какого нет
    cookie_header = {"Cookie": f"hub_session={cookie}"}

    responses = [
        await client.get("/api/v1/healthz"),  # 200
        await client.get("/api/v1/auth/me"),  # 401 (без токена вовсе)
        await client.post("/api/v1/auth/logout"),  # 403 (нет Origin)
        await client.get("/api/v1/projects/999/channels", headers=cookie_header),  # 404 (валидная сессия)
        await client.get("/api/v1/projects", headers={"Host": "evil.com"}),  # 421
        await client.options("/api/v1/healthz"),  # 405
    ]
    statuses = {r.status_code for r in responses}
    assert statuses == {200, 401, 403, 404, 421, 405}
    for r in responses:
        assert not any(name.lower().startswith("access-control") for name in r.headers)

    r = await client.options("/api/v1/projects")
    assert r.status_code == 405


# 4. CSP и остальные заголовки безопасности — на 200, 404 и 401.
async def test_security_headers_present(client):
    expected = {
        "content-security-policy",
        "x-content-type-options",
        "referrer-policy",
        "cross-origin-opener-policy",
        "cross-origin-resource-policy",
    }
    for r in (
        await client.get("/api/v1/healthz"),
        await client.get("/api/v1/does-not-exist"),
        await client.get("/api/v1/auth/me"),
    ):
        present = {name.lower() for name in r.headers}
        assert expected <= present, (r.status_code, r.headers)
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["cache-control"] == "no-store"


# 5. Логин-код: верный -> cookie HttpOnly+SameSite=Strict; повтор -> 401; протухший -> 401;
#    в БД — хеш, не значение.
async def test_login_code_flow(client, db, clock):
    now_ms = int(clock() * 1000)
    code = db.write_sync(lambda conn: security.create_login_code(conn, now_ms=now_ms))

    r = await client.post("/api/v1/auth/session", json={"code": code}, headers={"Origin": ORIGIN})
    assert r.status_code == 200
    set_cookie = r.headers.get("set-cookie", "")
    assert "hub_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "samesite=strict" in set_cookie.lower()
    # Срок куки = срок сессии (30 дней): закладка на UI переживает закрытие браузера (23.09).
    assert f"Max-Age={security.UI_SESSION_TTL_MS // 1000}" in set_cookie
    cookie_value = client.cookies.get("hub_session")
    assert cookie_value is not None
    assert code not in cookie_value

    with db.reader() as conn:
        code_row = conn.execute("SELECT hash FROM main.login_code").fetchone()
        assert code_row[0] == hashlib.sha256(code.encode()).hexdigest()
        token_row = conn.execute("SELECT hash FROM main.api_token WHERE kind = 'ui'").fetchone()
        assert token_row[0] == hashlib.sha256(cookie_value.encode()).hexdigest()

    # Дальше проверяем именно код, а не CSRF: без этого второй POST споткнулся бы на «cookie есть — нужен
    # X-Hub-CSRF» ещё в middleware, до того как маршрут вообще посмотрел бы на повторный/протухший код.
    client.cookies.clear()

    # повторное использование того же кода
    r = await client.post("/api/v1/auth/session", json={"code": code}, headers={"Origin": ORIGIN})
    assert r.status_code == 401

    # протухший код: выписан ДО сдвига часов на +61 с
    code2 = db.write_sync(lambda conn: security.create_login_code(conn, now_ms=now_ms))
    clock.advance_ms(61_000)
    r = await client.post("/api/v1/auth/session", json={"code": code2}, headers={"Origin": ORIGIN})
    assert r.status_code == 401


# 6. Bearer: валидный проходит; revoked/протухший/мусорный/kind='ui' -> 401; Bearer+cookie -> 400.
async def test_bearer_validation(client, db, clock):
    good_token = make_bearer_token(db, kind="interactive", scopes=("read",), project_ids=None)
    r = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {good_token}"})
    assert r.status_code == 200
    assert r.json()["kind"] == "interactive"

    revoked_token = make_bearer_token(db, name="revoked")
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.api_token SET revoked = 1 WHERE hash = ?",
            (hashlib.sha256(revoked_token.encode()).hexdigest(),),
        )
    )
    r = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {revoked_token}"})
    assert r.status_code == 401

    now_ms = int(clock() * 1000)
    expired_token = make_bearer_token(db, name="expired", expires_ts=now_ms - 1)
    r = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {expired_token}"})
    assert r.status_code == 401

    r = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer garbage-value"})
    assert r.status_code == 401

    cookie, _csrf = await login_via_code(client, db, clock)
    client.cookies.clear()  # иначе httpx сам добавит Cookie к запросу ниже и получится Bearer+cookie
    r = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {cookie}"})
    assert r.status_code == 401  # kind='ui' как bearer

    # Bearer + cookie одновременно (GET — Origin/CSRF не задействованы) -> 400.
    r = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {good_token}", "Cookie": f"hub_session={cookie}"},
    )
    assert r.status_code == 400


# 6. Троттлинг: 11-я неудача за минуту -> 429; после +60 с снова обычная проверка.
async def test_bearer_throttle_after_ten_failures(client, clock):
    for _ in range(10):
        r = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401

    r = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 429

    r = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 429  # всё ещё в окне блокировки

    clock.advance_ms(60_001)
    r = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401  # блокировка снята, обычная проверка


# 7. Изоляция проектов — параметризовано по ВСЕМ GET-ручкам 4a.
@pytest.fixture
def isolation_setup(db):
    """Проект A и проект B, каждый с каналом/сообщением/outbox в main И в work; плюс «Неразобранное»."""
    project_a = insert_project(db, name="A")
    project_b = insert_project(db, name="B")

    def _insert_token(conn):
        return conn.execute(
            "INSERT INTO main.api_token (name, kind, hash) VALUES ('t', 'interactive', 'x')"
        ).lastrowid

    token_id = db.write_sync(_insert_token)

    setup: dict = {"project_a": project_a, "project_b": project_b, "channels": {}, "outbox_ids": {}}
    for db_name in ("main", "work"):
        acc = insert_account(db, source="tg", db_name=db_name)
        ch_a = insert_channel(
            db, acc, project_id=project_a, ext_id=f"a-{db_name}", db_name=db_name, my_msg_count=1
        )
        ch_b = insert_channel(
            db, acc, project_id=project_b, ext_id=f"b-{db_name}", db_name=db_name, my_msg_count=1
        )
        ch_u = insert_channel(
            db, acc, project_id=None, ext_id=f"u-{db_name}", db_name=db_name, my_msg_count=1
        )
        insert_message(db, ch_b, ext_id="mb", ts=1000, text=f"секретслово{db_name}")
        outbox_id = db.write_sync(
            lambda conn, ch_b=ch_b: (
                enqueue(
                    conn,
                    channel_id=ch_b,
                    text="черновик B",
                    origin="ui",
                    api_token_id=token_id,
                    idempotency_key=f"k-{ch_b}",
                    now_ms=1000,
                ).outbox_id
            )
        )
        setup["channels"][db_name] = {"a": ch_a, "b": ch_b, "u": ch_u}
        setup["outbox_ids"][db_name] = outbox_id
    return setup


async def test_project_isolation_across_all_get_routes(client, db, isolation_setup):
    token = make_bearer_token(db, project_ids=[isolation_setup["project_a"]])
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.get("/api/v1/projects", headers=headers)
    assert r.status_code == 200
    assert {p["id"] for p in r.json()} == {isolation_setup["project_a"]}

    r = await client.get(f"/api/v1/projects/{isolation_setup['project_b']}/channels", headers=headers)
    assert r.status_code == 404

    r = await client.get("/api/v1/projects/-1/channels", headers=headers)  # «Неразобранное» — не для scoped
    assert r.status_code == 404

    for db_name, chans in isolation_setup["channels"].items():
        r = await client.get(f"/api/v1/channels/{chans['b']}/messages", headers=headers)
        assert r.status_code == 404, db_name
        r = await client.get(f"/api/v1/channels/{chans['u']}/messages", headers=headers)
        assert r.status_code == 404, db_name

    r = await client.get("/api/v1/inbox", headers=headers)
    assert r.status_code == 200
    seen_channel_ids = {c["id"] for c in r.json()}
    leaked = {chans["b"] for chans in isolation_setup["channels"].values()} | {
        chans["u"] for chans in isolation_setup["channels"].values()
    }
    assert not (seen_channel_ids & leaked)

    for db_name in ("main", "work"):
        r = await client.get(f"/api/v1/search?q=секретслово{db_name}", headers=headers)
        assert r.status_code == 200
        assert r.json() == [], db_name

    for db_name, outbox_id in isolation_setup["outbox_ids"].items():
        r = await client.get(f"/api/v1/outbox/{outbox_id}", headers=headers)
        assert r.status_code == 404, db_name


# 11. Миграция 0002_main.sql поверх БД версии 1 С ДАННЫМИ; user_version=2 у main, у work остаётся 1.
def test_migration_0002_upgrades_v1_main_with_data(tmp_path):
    migrations_dir = Path(db_module.__file__).parent / "migrations"
    main_path = tmp_path / "hub.db"

    # Ручная сборка «настоящей» БД версии 1 (main + shared, как это сделал бы _init_file до появления 0002).
    conn = sqlite3.connect(main_path, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript((migrations_dir / "0001_main.sql").read_text(encoding="utf-8"))
        conn.executescript((migrations_dir / "0001_shared.sql").read_text(encoding="utf-8"))
        conn.execute("INSERT INTO project (name) VALUES ('до миграции')")
        conn.execute("PRAGMA user_version=1")
    finally:
        conn.close()

    db = db_module.Db(tmp_path)
    db.start()
    try:
        with db.reader() as conn2:
            # версия main — какая бы ни была последней; важно, что 0002 легла поверх данных v1
            assert conn2.execute("PRAGMA main.user_version").fetchone()[0] >= 2
            assert conn2.execute("PRAGMA work.user_version").fetchone()[0] >= 1
            assert conn2.execute("SELECT name FROM main.project").fetchone()[0] == "до миграции"
            assert conn2.execute("SELECT COUNT(*) FROM main.login_code").fetchone()[0] == 0
    finally:
        db.close()


# Протухшая cookie в браузере не должна запирать повторный вход: CSRF страница логина вычислить не может.
async def test_relogin_with_stale_cookie_needs_no_csrf(client, db, clock):
    await login_via_code(client, db, clock)
    clock.advance_ms(security.UI_SESSION_TTL_MS + 1000)
    assert (await client.get("/api/v1/auth/me")).status_code == 401  # сессия протухла, cookie осталась

    code = db.write_sync(lambda conn: security.create_login_code(conn, now_ms=int(clock() * 1000)))
    r = await client.post("/api/v1/auth/session", json={"code": code}, headers={"Origin": ORIGIN})
    assert r.status_code == 200
    assert (await client.get("/api/v1/auth/me")).status_code == 200

    # а Origin для входа обязателен по-прежнему
    code2 = db.write_sync(lambda conn: security.create_login_code(conn, now_ms=int(clock() * 1000)))
    r = await client.post("/api/v1/auth/session", json={"code": code2}, headers={"Origin": "http://evil.com"})
    assert r.status_code == 403


# Троттлинг логин-кодов бьёт ТОЛЬКО по неудачам: после 10 промахов неверный код получает 429 (без записи),
# а ВЕРНЫЙ проходит — иначе любой, кто достучался до ручки, запирал бы хозяину вход (критик, F2).
async def test_login_code_throttle_never_locks_out_valid_code(client, db, clock):
    for _ in range(security.THROTTLE_FAILURES):
        r = await client.post("/api/v1/auth/session", json={"code": "мимо"}, headers={"Origin": ORIGIN})
        assert r.status_code == 401

    with db.reader() as conn:
        audit_before = conn.execute("SELECT COUNT(*) FROM main.audit").fetchone()[0]
    r = await client.post("/api/v1/auth/session", json={"code": "мимо"}, headers={"Origin": ORIGIN})
    assert r.status_code == 429
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.audit").fetchone()[0] == audit_before  # без записи

    code = db.write_sync(lambda conn: security.create_login_code(conn, now_ms=int(clock() * 1000)))
    r = await client.post("/api/v1/auth/session", json={"code": code}, headers={"Origin": ORIGIN})
    assert r.status_code == 200


# Положительный контроль к изоляции: токен проекта A своё видит (иначе «не отдаём ничего» прошло бы тест),
# и ни в одном теле ответа нет ни байта данных B — включая /status.
async def test_scoped_token_sees_own_data_and_no_foreign_bytes(client, db, isolation_setup):
    chans = isolation_setup["channels"]
    for db_name in ("main", "work"):
        insert_message(db, chans[db_name]["a"], ext_id="ma", ts=1000, text=f"своёслово{db_name}")

    token = make_bearer_token(db, project_ids=[isolation_setup["project_a"]])
    headers = {"Authorization": f"Bearer {token}"}
    project_a = isolation_setup["project_a"]

    bodies = []
    for url in (
        "/api/v1/projects",
        f"/api/v1/projects/{project_a}/channels",
        "/api/v1/inbox",
        "/api/v1/status",
        "/api/v1/search?q=своёсловоmain",
        "/api/v1/search?q=своёсловоwork",
        f"/api/v1/channels/{chans['main']['a']}/messages",
        f"/api/v1/channels/{chans['work']['a']}/messages",
    ):
        r = await client.get(url, headers=headers)
        assert r.status_code == 200, url
        bodies.append(r.text)

    assert {c["id"] for c in (await client.get("/api/v1/inbox", headers=headers)).json()} == {
        chans["main"]["a"],
        chans["work"]["a"],
    }
    assert "своёсловоmain" in bodies[4] and "своёсловоwork" in bodies[5]
    assert "своёсловоmain" in bodies[6] and "своёсловоwork" in bodies[7]

    everything = "\n".join(bodies)
    for foreign in ("секретслово", "черновик B", "b-main", "b-work", "u-main", "u-work"):
        assert foreign not in everything, foreign
