"""Ф29-1 (docs/architecture.md): ручки поиска и чтения Telegram без
вступления — запреты `check_attach`, выбор аккаунта, проверки тела, коннектор не запущен, подстановка
`channel_id`, аудит без строки поиска; внутренние `/lookup/next|result`.

Сессия — bearer `interactive` со скоупом `draft`. Коннектор — внутреннее приложение с секретом аккаунта на той
же БД, тех же часах и том же посреднике (`TgLookup`), что публичное."""

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import connector_auth, security
from hub.core.app_internal import create_internal_app
from hub.core.app_public import create_public_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.feed import Notifier
from hub.core.tg_lookup import TgLookup

from .conftest import ORIGIN, PORT, csrf_headers, insert_channel, login_via_code

API = "/api/v1"
INTERNAL = "/internal/v1"
ADMIN_SECRET = ADMIN_PREFIX + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHI"


@pytest.fixture
def lookup():
    return TgLookup(answer_timeout_s=5.0)


@pytest.fixture
def app(db, clock, lookup):
    return create_public_app(db, clock=clock, port=PORT, tg_lookup=lookup)


@pytest.fixture
def internal_app(db, clock, lookup):
    notifier = Notifier()
    db.on_commit(notifier.notify)
    return create_internal_app(
        db, clock=clock, notifier=notifier, admin_key_hash=hash_secret(ADMIN_SECRET), tg_lookup=lookup
    )


@pytest.fixture
async def ic(internal_app):
    async with AsyncClient(transport=ASGITransport(app=internal_app), base_url="http://hub-internal") as c:
        yield c


def _account(db, clock, *, source="telegram", alive=True, status="ok") -> tuple[int, dict]:
    last_ok = int(clock() * 1000) if alive else None
    account_id = db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, status, last_ok_ts) VALUES (?, 'main', ?, ?)",
                (source, status, last_ok),
            ).lastrowid
        )
    )
    secret = connector_auth.generate_connector_secret()
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.connector_secret (account_id, hash, created_ts) VALUES (?, ?, 0)",
            (account_id, connector_auth.hash_secret(secret)),
        )
    )
    return account_id, {"Authorization": f"Bearer {secret}"}


def _token(db, *, name="mcp:all", kind="interactive", scopes=("read", "draft")):
    return db.write_sync(
        lambda conn: security.create_api_token(
            conn, name=name, kind=kind, scopes=list(scopes), project_ids=None, expires_ts=None
        )
    ).token


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN}


def _audit(db, action):
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT result, actor, detail_json FROM main.audit WHERE action = ? ORDER BY id", (action,)
        ).fetchall()
    return [(r[0], r[1], json.loads(r[2])) for r in rows]


async def _answer(ic, h, *, outcome="ok", payload=None, reason=None, retry_after_s=None) -> dict:
    """Коннектор: взять одну заявку (long-poll) и ответить."""
    r = await ic.post(f"{INTERNAL}/lookup/next", headers=h, json={"timeout_s": 5})
    assert r.status_code == 200, r.text
    item = r.json()
    body: dict = {"id": item["id"], "outcome": outcome}
    if payload is not None:
        body["payload"] = payload
    if reason is not None:
        body["reason"] = reason
    if retry_after_s is not None:
        body["retry_after_s"] = retry_after_s
    r = await ic.post(f"{INTERNAL}/lookup/result", headers=h, json=body)
    assert r.status_code == 200, r.text
    return item


async def _with_connector(client, ic, h, path, body, token, **answer):
    task = asyncio.ensure_future(client.post(f"{API}{path}", json=body, headers=_bearer(token)))
    item = await _answer(ic, h, **answer)
    return await task, item


# --- запреты и проверки ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "kind", "scopes"),
    [
        ("mcp:wake-3", "interactive", ("read", "draft")),
        ("mcp:orchestrator", "interactive", ("read", "draft")),
        ("mcp:all", "background", ("read", "draft")),
    ],
)
async def test_check_attach_denied_403(client, db, clock, name, kind, scopes):
    _account(db, clock)
    token = _token(db, name=name, kind=kind, scopes=scopes)
    for path, body in (("/tg/search", {"query": "продавцы"}), ("/tg/peek", {"target": "@chan_x"})):
        r = await client.post(f"{API}{path}", json=body, headers=_bearer(token))
        assert r.status_code == 403, r.text
        assert "только из интерактивной сессии" in r.json()["detail"]
    assert [row[0] for row in _audit(db, "tg_search")] == ["denied"]


async def test_ui_session_denied_403(client, db, clock):
    _account(db, clock)
    cookie, _ = await login_via_code(client, db, clock)
    r = await client.post(f"{API}/tg/search", json={"query": "продавцы"}, headers=csrf_headers(cookie))
    assert r.status_code == 403, r.text


async def test_without_draft_scope_403(client, db, clock):
    _account(db, clock)
    token = _token(db, scopes=("read",))
    r = await client.post(f"{API}/tg/search", json={"query": "продавцы"}, headers=_bearer(token))
    assert r.status_code == 403


async def test_several_telegram_accounts_need_account_id(client, db, clock):
    a1, _ = _account(db, clock)
    a2, _ = _account(db, clock)
    token = _token(db)
    r = await client.post(f"{API}/tg/search", json={"query": "продавцы"}, headers=_bearer(token))
    assert r.status_code == 422
    assert f"аккаунтов Telegram несколько ({a1}, {a2})" in r.json()["detail"]


@pytest.mark.parametrize("query", ["ab", "  ab  ", "x" * 65])
async def test_query_length_422(client, db, clock, query):
    _account(db, clock)
    r = await client.post(f"{API}/tg/search", json={"query": query}, headers=_bearer(_token(db)))
    assert r.status_code == 422 and "query: от 3 до 64" in r.json()["detail"]


async def test_limits_422(client, db, clock):
    _account(db, clock)
    token = _token(db)
    r = await client.post(f"{API}/tg/search", json={"query": "abc", "limit": 21}, headers=_bearer(token))
    assert r.status_code == 422 and "limit: от 1 до 20" in r.json()["detail"]
    r = await client.post(f"{API}/tg/peek", json={"target": "@chan_x", "limit": 51}, headers=_bearer(token))
    assert r.status_code == 422 and "limit: от 1 до 50" in r.json()["detail"]


@pytest.mark.parametrize("target", ["t.me/+AbCdEfGh12", "https://t.me/joinchat/AbCdEfGh12"])
async def test_peek_invite_422(client, db, clock, target):
    _account(db, clock)
    r = await client.post(f"{API}/tg/peek", json={"target": target}, headers=_bearer(_token(db)))
    assert r.status_code == 422
    assert r.json()["detail"] == "по ссылке-приглашению читать нельзя — только вступить (hub_join)"
    [(result, _actor, detail)] = _audit(db, "tg_peek")
    assert result == "invite" and detail["target"] is None


async def test_connector_not_running_503_without_queue(client, db, clock, lookup):
    account_id, _ = _account(db, clock, alive=False)
    r = await client.post(f"{API}/tg/search", json={"query": "abc"}, headers=_bearer(_token(db)))
    assert r.status_code == 503 and r.json()["detail"] == f"коннектор Telegram #{account_id} не запущен"
    assert not lookup._queues.get(account_id)  # noqa: SLF001


async def test_connector_silent_or_auth_required_503(client, db, clock):
    account_id, _ = _account(db, clock)
    token = _token(db)
    clock.advance_ms(5 * 60 * 1000 + 1)
    r = await client.post(f"{API}/tg/search", json={"query": "abc"}, headers=_bearer(token))
    assert r.status_code == 503
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.account SET status = 'auth_required', last_ok_ts = ? WHERE id = ?",
            (int(clock() * 1000), account_id),
        )
    )
    r = await client.post(f"{API}/tg/search", json={"query": "abc"}, headers=_bearer(token))
    assert r.status_code == 503 and "нужен вход" in r.json()["detail"]


async def test_not_telegram_account_422(client, db, clock):
    account_id, _ = _account(db, clock, source="slack")
    r = await client.post(
        f"{API}/tg/search", json={"query": "abc", "account_id": account_id}, headers=_bearer(_token(db))
    )
    assert r.status_code == 422


# --- сквозь посредника ------------------------------------------------------------------------------------


async def test_search_flow_channel_id_and_audit_without_query(client, ic, db, clock):
    account_id, h = _account(db, clock)
    known = insert_channel(db, account_id, ext_id="-1001500", kind="channel")
    token = _token(db)
    payload = {
        "results": [
            {
                "ext_id": "-1001500",
                "username": "sellers",
                "title": "Продавцы",
                "kind": "megagroup",
                "participants": 900,
                "member": False,
                "verified": False,
                "scam": False,
                "fake": False,
                "join_request": False,
                "extra": "лишнее",
            },
            {
                "ext_id": "-1001600",
                "username": "pvz_owners",
                "title": "ПВЗ",
                "kind": "broadcast",
                "participants": None,
                "member": True,
                "verified": True,
                "scam": False,
                "fake": False,
                "join_request": True,
            },
        ]
    }
    r, item = await _with_connector(
        client, ic, h, "/tg/search", {"query": "  секретная строка  ", "limit": 5}, token, payload=payload
    )
    assert item["kind"] == "search" and item["params"] == {"query": "секретная строка", "limit": 5}
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["account_id"] == account_id
    assert [x["channel_id"] for x in body["results"]] == [known, None]
    assert "extra" not in body["results"][0]
    [(result, actor, detail)] = _audit(db, "tg_search")
    assert (result, actor) == ("ok", "interactive")
    assert detail == {"account_id": account_id, "count": 2}
    with db.reader() as conn:
        dump = json.dumps(conn.execute("SELECT * FROM main.audit").fetchall(), ensure_ascii=False)
    assert "секретная" not in dump


async def test_peek_flow_channel_id_fields_and_next_before(client, ic, db, clock):
    account_id, h = _account(db, clock)
    known = insert_channel(db, account_id, ext_id="-1001700", kind="channel")
    token = _token(db)
    payload = {
        "chat": {
            "ext_id": "-1001700",
            "username": "Some_Chan",
            "title": "Канал",
            "kind": "broadcast",
            "participants": 12_400,
            "about": "о" * 500,
            "member": False,
        },
        "messages": [
            {
                "id": 77,
                "ts": 1_700_000_000_000,
                "author": "Канал",
                "fwd": None,
                "text": "пост",
                "media": ["photo"],
                "views": 1200,
                "forwards": 3,
                "replies": 5,
                "reactions": 9,
                "album": None,
            },
        ],
        "next_before": 77,
    }
    r, item = await _with_connector(
        client,
        ic,
        h,
        "/tg/peek",
        {"target": "https://t.me/Some_Chan/55", "limit": 1, "before": 100},
        token,
        payload=payload,
    )
    assert item["kind"] == "peek" and item["params"] == {"username": "some_chan", "limit": 1, "before": 100}
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["chat"]["channel_id"] == known and len(body["chat"]["about"]) == 300
    assert body["messages"][0]["views"] == 1200 and body["next_before"] == 77
    [(result, _actor, detail)] = _audit(db, "tg_peek")
    assert result == "ok" and detail == {"account_id": account_id, "target": "some_chan", "count": 1}


async def test_connector_error_is_text_for_session(client, ic, db, clock):
    _account_id, h = _account(db, clock)
    token = _token(db)
    r, _ = await _with_connector(
        client, ic, h, "/tg/peek", {"target": "@closed_chan"}, token, outcome="error", reason="private"
    )
    assert r.status_code == 422
    assert r.json()["detail"] == "закрытый — читать можно только после вступления (hub_join)"
    clock.advance_ms(3_000)  # интервал между выдачами аккаунта
    r, _ = await _with_connector(
        client,
        ic,
        h,
        "/tg/search",
        {"query": "abc"},
        token,
        outcome="error",
        reason="flood",
        retry_after_s=42,
    )
    assert r.status_code == 429 and r.json()["detail"] == "Telegram просит подождать 42 с"
    # Пауза аккаунта: следующий запрос — сразу 429, без очереди.
    r = await client.post(f"{API}/tg/search", json={"query": "abd"}, headers=_bearer(token))
    assert r.status_code == 429 and "подождать" in r.json()["detail"]
    assert [row[0] for row in _audit(db, "tg_search")] == ["flood", "flood"]


async def test_internal_next_empty_204_and_foreign_result_404(ic, client, db, clock, lookup):
    a1, h1 = _account(db, clock)
    _a2, h2 = _account(db, clock)
    r = await ic.post(f"{INTERNAL}/lookup/next", headers=h1, json={"timeout_s": 0})
    assert r.status_code == 204
    token = _token(db)
    task = asyncio.ensure_future(
        client.post(f"{API}/tg/search", json={"query": "abc", "account_id": a1}, headers=_bearer(token))
    )
    r = await ic.post(f"{INTERNAL}/lookup/next", headers=h1, json={"timeout_s": 5})
    item = r.json()
    # Чужой коннектор на чужую заявку — 404; неизвестная — 404.
    r = await ic.post(f"{INTERNAL}/lookup/result", headers=h2, json={"id": item["id"], "outcome": "ok"})
    assert r.status_code == 404
    r = await ic.post(f"{INTERNAL}/lookup/result", headers=h1, json={"id": 99_999, "outcome": "ok"})
    assert r.status_code == 404
    r = await ic.post(
        f"{INTERNAL}/lookup/result", headers=h1, json={"id": item["id"], "outcome": "ok", "payload": {}}
    )
    assert r.status_code == 200
    r = await task
    assert r.status_code == 200 and r.json() == {"account_id": a1, "results": []}


async def test_timeout_504_and_late_result_404(ic, client, db, clock, lookup):
    lookup.answer_timeout_s = 0.05
    _a1, h = _account(db, clock)
    token = _token(db)
    task = asyncio.ensure_future(
        client.post(f"{API}/tg/search", json={"query": "abc"}, headers=_bearer(token))
    )
    r = await ic.post(f"{INTERNAL}/lookup/next", headers=h, json={"timeout_s": 5})
    item = r.json()
    r = await task
    assert r.status_code == 504 and "не ответил" in r.json()["detail"]
    r = await ic.post(f"{INTERNAL}/lookup/result", headers=h, json={"id": item["id"], "outcome": "ok"})
    assert r.status_code == 404
