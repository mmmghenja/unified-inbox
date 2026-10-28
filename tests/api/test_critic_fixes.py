"""Находки opus-критика публичного API (20.09). Каждая была воспроизведена скриптом до исправления."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import security
from hub.core.feed import HEARTBEAT_FRAME, Notifier, stream
from hub.core.security import Principal

from .conftest import (
    BASE_URL,
    ORIGIN,
    csrf_headers,
    insert_account,
    insert_channel,
    insert_message,
    insert_project,
    login_via_code,
    make_bearer_token,
)


# F1: послабление «не браузер» (Origin не нужен при Authorization: Bearer) на ручке входа не действует.
async def test_login_requires_origin_even_with_bearer_header(client, db, clock):
    code = db.write_sync(lambda conn: security.create_login_code(conn, now_ms=int(clock() * 1000)))
    for headers in (
        {"Authorization": "Bearer hub_garbage"},
        {"Authorization": "Bearer hub_garbage", "Sec-Fetch-Site": "cross-site"},
        {"Authorization": "Bearer hub_garbage", "Origin": "http://evil.com"},
    ):
        r = await client.post("/api/v1/auth/session", json={"code": code}, headers=headers)
        assert r.status_code == 403, headers
    r = await client.post("/api/v1/auth/session", json={"code": code}, headers={"Origin": ORIGIN})
    assert r.status_code == 200  # код не сгорел на отбитых попытках


# F2: мусорные bearer-запросы не отключают клиентов с валидным токеном.
async def test_bearer_throttle_never_blocks_valid_token(client, db):
    token = make_bearer_token(db)
    for _ in range(security.THROTTLE_FAILURES + 1):
        await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer hub_garbage"})
    r = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer hub_garbage"})
    assert r.status_code == 429
    r = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


# F3: bearer «на все проекты» — не UI: псевдо-проекты ему 404, в списке их нет.
async def test_unscoped_bearer_gets_no_pseudo_projects(client, db):
    acc = insert_account(db)
    insert_project(db, name="P")
    ch = insert_channel(db, acc, ext_id="u1", title="Без проекта")
    insert_message(db, ch, ext_id="1", ts=1000, text="x")
    headers = {"Authorization": f"Bearer {make_bearer_token(db, project_ids=None)}"}

    ids = {p["id"] for p in (await client.get("/api/v1/projects", headers=headers)).json()}
    assert not ids & {-1, -2}
    for url in ("/api/v1/projects/-1/channels", "/api/v1/projects/-2/channels", "/api/v1/inbox?project=-1"):
        assert (await client.get(url, headers=headers)).status_code == 404, url
    r = await client.post("/api/v1/channels/resolve", json={"query": "Без", "project": -1}, headers=headers)
    assert r.status_code == 404


# F4: целое больше 2^63-1 — ошибка клиента, а не 500; Last-Event-ID — только цифры в диапазоне (F7).
async def test_huge_integers_are_client_errors(client, db, clock):
    await login_via_code(client, db, clock)
    huge = 10**20
    for url in (
        f"/api/v1/channels/{huge}/messages",
        f"/api/v1/outbox/{huge}",
        f"/api/v1/projects/{huge}/channels",
        f"/api/v1/sync?since={huge}",
        f"/api/v1/inbox?project={huge}",
        f"/api/v1/search?q=hello&project={huge}",
    ):
        r = await client.get(url)
        assert r.status_code in (404, 422), (url, r.status_code)

    for bad in ("-1", "+3", "1_0", " 7 ", str(huge), "abc", ""):
        r = await client.get("/api/v1/stream", headers={"Last-Event-ID": bad})
        assert r.status_code == 422, repr(bad)


# F5: ответ 500 тоже несёт заголовки безопасности (обработчик живёт снаружи SecurityMiddleware).
async def test_500_has_security_headers(app, db, clock, monkeypatch):
    from hub.core import queries

    def _boom(*_args, **_kwargs):
        raise RuntimeError("секрет из трейса")

    monkeypatch.setattr(queries, "list_projects", _boom)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url=BASE_URL) as c:
        await login_via_code(c, db, clock)
        r = await c.get("/api/v1/projects")
    assert r.status_code == 500
    assert r.json() == {"detail": "internal error"}
    assert "секрет" not in r.text
    for name in ("content-security-policy", "x-content-type-options", "referrer-policy", "cache-control"):
        assert name in r.headers, name


# F6: записи в ЧУЖОМ проекте будят поток, но не должны глушить heartbeat.
async def test_heartbeat_survives_foreign_project_writes(db):
    acc = insert_account(db)
    project_a = insert_project(db, name="A")
    project_b = insert_project(db, name="B")
    ch_b = insert_channel(db, acc, ext_id="b1", project_id=project_b)
    notifier = Notifier()
    notifier.attach(asyncio.get_running_loop())
    db.on_commit(notifier.notify)
    principal = Principal(
        token_id=1, kind="interactive", scopes=frozenset({"read"}), project_ids=frozenset({project_a})
    )

    gen = stream(db, principal, last_seq=0, notifier=notifier, heartbeat_s=0.1)
    frames: list[str] = []

    async def _consume():
        async for frame in gen:
            frames.append(frame)

    task = asyncio.create_task(_consume())
    for i in range(8):  # шум в проекте B чаще, чем heartbeat
        insert_message(db, ch_b, ext_id=f"n{i}", ts=1000 + i, text="шум")
        await asyncio.sleep(0.04)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await gen.aclose()

    assert frames, "во время чужого шума не пришло ни одного heartbeat"
    assert all(frame == HEARTBEAT_FRAME for frame in frames)  # и ни одного кадра проекта B


# F8 + F9: отказ bearer в UI-only действии оставляет след в аудите; logout — тоже только UI.
async def test_ui_only_denial_is_audited_and_bearer_cannot_logout(client, db):
    acc = insert_account(db)
    ch = insert_channel(db, acc, ext_id="c1")
    msg = insert_message(db, ch, ext_id="1", ts=1000, text="x")
    token = make_bearer_token(db, scopes=("read", "send"))
    headers = {"Authorization": f"Bearer {token}", "X-Hub-Client": "agent-x"}

    r = await client.post(f"/api/v1/channels/{ch}/read", json={"up_to_message_id": msg}, headers=headers)
    assert r.status_code == 403
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT action, result, client_name, detail_json FROM main.audit WHERE action = 'ui_only_denied'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][:3] == ("ui_only_denied", "denied", "agent-x")
    assert f"/channels/{ch}/read" in rows[0][3]

    r = await client.post("/api/v1/auth/logout", headers=headers)
    assert r.status_code == 403
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 200  # токен жив


# F10: дубликат Origin / Host — «первый верный, второй чужой» не проходит.
async def test_duplicate_origin_and_host_are_rejected(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    good = csrf_headers(cookie)
    dup = [("Origin", ORIGIN), ("Origin", "http://evil.com"), ("X-Hub-CSRF", good["X-Hub-CSRF"])]
    r = await client.post("/api/v1/auth/logout", headers=dup)
    assert r.status_code == 403

    r = await client.get("/api/v1/healthz", headers=[("Host", "127.0.0.1:8787"), ("Host", "evil.com")])
    assert r.status_code == 421
