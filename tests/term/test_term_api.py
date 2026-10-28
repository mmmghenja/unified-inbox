"""Ф56-5, кусок 5а: ручки `/term*` (раздел 2.2 + К2, К4, К7, К8, К12, К13), журнал запусков (2.3), миграция
0090, чистка 30 дней."""

import asyncio
import base64

import pytest
from api.conftest import BASE_URL

from hub.core import db as db_module
from hub.core.db import Db
from hub.core.runner import _hourly_cleanup
from hub.term import journal

from .conftest import API, auth, bearer, wait_for


async def _create(client, headers=None, expect=200, **body):
    r = await client.post(f"{API}/term", json=body or None, headers=headers or {})
    assert r.status_code == expect, r.text
    return r.json()


async def _screen_has(client, term_id, needle, headers=None):
    async def _check():
        r = await client.get(f"{API}/term/{term_id}/screen", params={"lines": 50}, headers=headers or {})
        return r.status_code == 200 and needle in r.json()["text"]

    await wait_for(_check)


async def _prompt(client, term_id, headers=None):
    async def _check():
        r = await client.get(f"{API}/term/{term_id}/screen", params={"lines": 5}, headers=headers or {})
        return r.status_code == 200 and r.json()["text"].rstrip().endswith("$")

    await wait_for(_check)


def _runs(db):
    with db.reader() as conn:
        return conn.execute(
            "SELECT term_id, who, source_kind, source_ref, text FROM main.term_run"
        ).fetchall()


async def test_ui_full_cycle_and_journal(ui, db):
    assert (await ui.get(f"{API}/term")).json() == []
    term = await _create(ui, cols=90, rows=20)
    assert term["id"] == 1 and (term["cols"], term["rows"]) == (90, 20) and term["owner"] == "ui"
    assert term["title"].startswith("bash в ") and term["alive"] is True
    await _prompt(ui, 1)

    # Свободный ввод — байты; в журнал НЕ пишется.
    data = base64.b64encode("echo секрет-$((3*3))\r".encode()).decode()
    r = await ui.post(f"{API}/term/1/input", json={"data_b64": data})
    assert r.status_code == 200
    await _screen_has(ui, 1, "секрет-9")
    assert _runs(db) == []

    r = await ui.post(
        f"{API}/term/1/run", json={"text": "echo ran-$((5+5))", "source": {"kind": "ui_block", "ref": "ev:7"}}
    )
    assert r.status_code == 200 and r.json()["term_id"] == 1
    await _screen_has(ui, 1, "ran-10")
    assert _runs(db) == [(1, "ui", "ui_block", "ev:7", "echo ran-$((5+5))")]
    runs = (await ui.get(f"{API}/term/runs")).json()
    assert [(x["term_id"], x["who"], x["text"]) for x in runs] == [(1, "ui", "echo ran-$((5+5))")]

    r = await ui.post(f"{API}/term/1/resize", json={"cols": 120, "rows": 40})
    assert r.json() == {"cols": 120, "rows": 40}
    await wait_for(lambda: True)
    screen = (await ui.get(f"{API}/term/1/screen")).json()
    assert (screen["cols"], screen["rows"]) == (120, 40) and screen["command"] == "bash"
    assert set(screen) >= {"text", "cursor", "alt_screen", "command"}

    # К7: номер растёт и не переиспользуется.
    assert (await ui.delete(f"{API}/term/1")).json() == {"id": 1, "closed": True}
    assert (await ui.get(f"{API}/term")).json() == []
    assert (await _create(ui))["id"] == 2
    with db.reader() as conn:
        assert conn.execute("SELECT value FROM main.meta WHERE key = 'term_seq'").fetchone()[0] == "2"
    assert (await ui.post(f"{API}/term/1/input", json={"data_b64": data})).status_code == 404
    assert (await ui.get(f"{API}/term/1/screen")).status_code == 404
    assert (await ui.delete(f"{API}/term/1")).status_code == 404


async def test_validation_and_busy(ui):
    await _create(ui)
    await _prompt(ui, 1)
    for bad in ("echo \x1b[31m", "a\rb", "x\x7f", "x‮", "x\x9b", "  \n"):
        r = await ui.post(f"{API}/term/1/run", json={"text": bad, "source": {"kind": "ui_block"}})
        assert r.status_code == 422, repr(bad)
    r = await ui.post(f"{API}/term/1/run", json={"text": "x" * 16_001, "source": {"kind": "ui_block"}})
    assert r.status_code == 422
    r = await ui.post(f"{API}/term/1/run", json={"text": "ls", "source": {"kind": "nope"}})
    assert r.status_code == 422
    for body in (
        {"data_b64": "%%%"},
        {"data_b64": ""},
        {"data_b64": base64.b64encode(b"x" * 65537).decode()},
    ):
        assert (await ui.post(f"{API}/term/1/input", json=body)).status_code == 422
    for body in ({"cols": 19, "rows": 10}, {"cols": 80, "rows": 201}, {"cols": "80", "rows": 24}):
        assert (await ui.post(f"{API}/term/1/resize", json=body)).status_code == 422

    # К8: в панели не оболочка — 409 `term_busy`, повтор с `force` (только кука) — вставляет.
    data = base64.b64encode(b"sleep 30\r").decode()
    await ui.post(f"{API}/term/1/input", json={"data_b64": data})

    async def _sleeping():
        return (await ui.get(f"{API}/term/1/screen")).json()["command"] == "sleep"

    await wait_for(_sleeping)
    r = await ui.post(f"{API}/term/1/run", json={"text": "echo later", "source": {"kind": "ui_block"}})
    assert (r.status_code, r.json()) == (409, {"detail": "term_busy"})
    r = await ui.post(
        f"{API}/term/1/run", json={"text": "echo later", "source": {"kind": "ui_block"}, "force": True}
    )
    assert r.status_code == 200


async def test_limit_409(ui):
    for _ in range(4):
        await _create(ui)
    r = await ui.post(f"{API}/term")
    assert (r.status_code, r.json()) == (409, {"detail": "term_limit"})


async def test_scopes(client, ui, db):
    """К4 + К12: кука — всё; ключ `term` — список, создание, запуск, экран и только своих терминалов;
    `human`/`human_bot`/`read` — 403 на `/term*`; ключ `term` — 403 на заявки, канал владельца и `/sync`."""
    await _create(ui)
    await _prompt(ui, 1)
    key, key_id = bearer(db, ["term"])
    key_h = auth(key)
    ui_headers = dict(ui.headers)
    for name in ("Origin", "X-Hub-CSRF"):
        ui.headers.pop(name, None)
    cookies = dict(ui.cookies)
    ui.cookies.clear()
    try:
        assert (await client.get(f"{API}/term", headers=key_h)).json() == []  # терминал владельца не виден
        mine = await _create(client, headers=key_h)
        assert mine["id"] == 2 and "owner" not in mine
        assert [t["id"] for t in (await client.get(f"{API}/term", headers=key_h)).json()] == [2]
        await _prompt(client, 2, headers=key_h)
        r = await client.post(
            f"{API}/term/2/run",
            json={"text": "echo agent-$((1+6))", "source": {"kind": "agent"}},
            headers=key_h,
        )
        assert r.status_code == 200
        await _screen_has(client, 2, "agent-7", headers=key_h)
        assert _runs(db)[-1][:3] == (2, f"token:{key_id}", "agent")
        # Чужой терминал ключу не виден — 404.
        r = await client.post(
            f"{API}/term/1/run", json={"text": "id", "source": {"kind": "agent"}}, headers=key_h
        )
        assert r.status_code == 404
        assert (await client.get(f"{API}/term/1/screen", headers=key_h)).status_code == 404
        # `force` — только кука.
        r = await client.post(
            f"{API}/term/2/run",
            json={"text": "id", "source": {"kind": "agent"}, "force": True},
            headers=key_h,
        )
        assert (r.status_code, r.json()) == (403, {"detail": "force_forbidden"})
        # Ф84: ввод байтов — в свой терминал можно (в журнал не пишется), в чужой — 404.
        b64 = base64.b64encode(b"x").decode()
        runs_before = len(_runs(db))
        r = await client.post(f"{API}/term/2/input", json={"data_b64": b64}, headers=key_h)
        assert r.status_code == 200
        assert len(_runs(db)) == runs_before
        r = await client.post(f"{API}/term/1/input", json={"data_b64": b64}, headers=key_h)
        assert r.status_code == 404
        # Остальное ключу закрыто.
        for method, path, body in (
            ("GET", "/term/stream", None),
            ("POST", "/term/2/resize", {"cols": 80, "rows": 24}),
            ("GET", "/term/runs", None),
        ):
            r = await client.request(method, f"{API}{path}", json=body, headers=key_h)
            assert r.status_code == 403, (method, path)
        # Ключ `term` чужим ручкам — 403 (структурный запрет, К4).
        for path in ("/human-requests", "/owner_channel/queue", "/sync", "/projects", "/sessions/live", "/load"):
            r = await client.get(f"{API}{path}", headers=key_h)
            assert r.status_code == 403, path
        # Лимит ключа — 2, отдельно от владельца 4.
        await _create(client, headers=key_h)
        r = await client.post(f"{API}/term", headers=key_h)
        assert (r.status_code, r.json()) == (409, {"detail": "term_limit"})
        # Ф84: свой терминал ключ закрывает сам (место освобождается), владельца — 404.
        assert (await client.delete(f"{API}/term/1", headers=key_h)).status_code == 404
        assert (await client.delete(f"{API}/term/2", headers=key_h)).status_code == 200
        assert (await client.post(f"{API}/term", headers=key_h)).status_code == 200

        # Токены заявок и прочие bearer — 403 на всех ручках терминала.
        others = [
            bearer(db, ["human"], name="h")[0],
            bearer(db, ["human_bot"], name="hb")[0],
            bearer(db, ["read", "draft"], kind="interactive", name="r")[0],
        ]
        for token in others:
            for method, path, body in (
                ("GET", "/term", None),
                ("POST", "/term", None),
                ("POST", "/term/2/run", {"text": "id", "source": {"kind": "agent"}}),
                ("GET", "/term/2/screen", None),
                ("GET", "/term/runs", None),
                ("GET", "/term/stream", None),
            ):
                r = await client.request(method, f"{API}{path}", json=body, headers=auth(token))
                assert r.status_code == 403, (token[:8], method, path)
        assert (await client.get(f"{API}/term")).status_code == 401
    finally:
        ui.headers.update(ui_headers)
        ui.cookies.update(cookies)

    # Кука видит всё, с владельцем; может закрыть терминал агента.
    terms = (await ui.get(f"{API}/term")).json()
    assert [(t["id"], t["owner"]) for t in terms] == [(1, "ui"), (3, "agent"), (4, "agent")]
    assert (await ui.delete(f"{API}/term/3")).status_code == 200


def test_term_token_shape(db):
    from hub.core import security

    for kwargs in (
        {"kind": "interactive", "scopes": ["term"], "project_ids": None},
        {"kind": "background", "scopes": ["term", "read"], "project_ids": None},
        {"kind": "background", "scopes": ["term"], "project_ids": [1]},
    ):
        with pytest.raises(ValueError):
            db.write_sync(
                lambda conn, kw=kwargs: security.create_api_token(conn, name="x", expires_ts=None, **kw)
            )
    _, token_id = bearer(db, ["term"])
    with pytest.raises(security.AttachError):
        db.write_sync(lambda conn: security.set_attach(conn, token_id=token_id, on=True, now_ms=0))


async def test_stream_http_closes_and_csrf(ui, app, client):
    await _create(ui)
    await _prompt(ui, 1)
    # Без CSRF пишущие ручки куки не проходят.
    csrf = ui.headers.pop("X-Hub-CSRF")
    assert (await ui.post(f"{API}/term/1/input", json={"data_b64": "eA=="})).status_code == 403
    ui.headers["X-Hub-CSRF"] = csrf
    assert (await ui.get(f"{API}/term/stream", params={"pos": "bad"})).status_code == 422
    r = await ui.get(f"{API}/term/stream", headers={"Last-Event-ID": "1@zz:1"})
    assert r.status_code == 422

    # К13: остановка ядра закрывает поток (ASGITransport отдаёт тело целиком после закрытия).
    asyncio.get_running_loop().call_later(0.5, app.state.notifier.close)
    r = await asyncio.wait_for(ui.get(f"{API}/term/stream"), timeout=10)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-store"
    assert "event: snapshot" in r.text and '"term_id": 1' in r.text and "\nid: 1@" in "\n" + r.text
    assert app.state.term.subscribers == 0


async def test_stream_closes_on_revoked_session(ui, app, db, monkeypatch):
    """К13: отзыв куки — поток закрывается на ближайшей проверке."""
    from hub.term import stream as stream_mod

    monkeypatch.setattr(stream_mod, "HEARTBEAT_S", 0.2)
    real = stream_mod.stream

    def _fast(hub, **kw):
        kw["heartbeat_s"] = 0.2
        return real(hub, **kw)

    monkeypatch.setattr(stream_mod, "stream", _fast)

    async def _revoke():
        await asyncio.sleep(0.4)
        await db.write(lambda conn: conn.execute("UPDATE main.api_token SET revoked = 1 WHERE kind = 'ui'"))

    task = asyncio.ensure_future(_revoke())
    r = await asyncio.wait_for(ui.get(f"{API}/term/stream"), timeout=10)
    await task
    assert r.status_code == 200 and app.state.term.subscribers == 0


async def test_no_term_hub_503(db, clock):
    from httpx import ASGITransport, AsyncClient

    from hub.core.app_public import create_public_app

    app = create_public_app(db, clock=clock, port=8787)
    key, _ = bearer(db, ["term"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as c:
        r = await c.get(f"{API}/term", headers=auth(key))
    assert (r.status_code, r.json()) == (503, {"detail": "term_unavailable"})


def test_journal_cleanup_30_days(db):
    day = 24 * 60 * 60 * 1000
    now = 100 * day
    for age in (31 * day, 29 * day):
        db.write_sync(
            lambda conn, age=age: journal.record_run(
                conn,
                now_ms=now - age,
                term_id=1,
                who="ui",
                source_kind="ui_block",
                source_ref=None,
                text="ls",
            )
        )
    db.write_sync(lambda conn: _hourly_cleanup(conn, now))
    with db.reader() as conn:
        assert [r[0] for r in conn.execute("SELECT ts FROM main.term_run")] == [now - 29 * day]


def test_migration_0090_from_0089(tmp_path, monkeypatch):
    original = db_module._migrations

    def upto(version):
        monkeypatch.setattr(
            db_module,
            "_migrations",
            lambda *, main: {v: f for v, f in original(main=main).items() if v <= version},
        )

    upto(89)
    old = Db(tmp_path)
    old.start()
    with old.reader() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM main.sqlite_master WHERE type = 'table'")}
    old.close()
    assert "term_run" not in tables
    upto(90)
    for _ in range(2):
        new = Db(tmp_path)
        new.start()
        try:
            with new.reader() as conn:
                assert conn.execute("PRAGMA main.user_version").fetchone()[0] == 90
                cols = [r[1] for r in conn.execute("PRAGMA main.table_info(term_run)")]
                indexes = {r[1] for r in conn.execute("PRAGMA main.index_list(term_run)")}
            assert cols == ["id", "ts", "term_id", "who", "source_kind", "source_ref", "text"]
            assert "idx_term_run_ts" in indexes
        finally:
            new.close()
