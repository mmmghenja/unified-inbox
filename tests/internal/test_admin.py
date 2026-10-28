"""Тест 7: admin-ручки внутреннего сокета. Спецификация: docs/architecture.md.

`login-code` проверяется насквозь: код, отданный внутренним сокетом, должен сработать в ПУБЛИЧНОМ
`POST /auth/session` — это и есть контракт «внутренний сокет заводит, публичный API принимает».
"""

from httpx import ASGITransport, AsyncClient

from hub.core.app_public import create_public_app

from .conftest import admin_headers


async def test_accounts_with_secret_in_config_is_422(internal_client) -> None:
    r = await internal_client.post(
        "/internal/v1/admin/accounts",
        headers=admin_headers(),
        json={"source": "tg", "db": "main", "config": {"api_hash": "deadbeef"}},
    )
    assert r.status_code == 422


async def test_accounts_secret_returned_once_and_hashed_in_db(internal_client, db) -> None:
    r = await internal_client.post(
        "/internal/v1/admin/accounts",
        headers=admin_headers(),
        json={"source": "tg", "title": "Личный", "db": "main", "config": {"idempotent_send": True}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    account_id = body["account_id"]
    secret = body["connector_secret"]
    assert secret.startswith("hubc_")

    row = db.write_sync(
        lambda conn: conn.execute(
            "SELECT hash FROM main.connector_secret WHERE account_id = ?", (account_id,)
        ).fetchone()
    )
    assert row is not None
    stored_hash = row[0]
    assert stored_hash != secret
    assert secret not in stored_hash


async def test_bearer_token_returned_once_and_hashed_in_db(internal_client, db) -> None:
    r = await internal_client.post(
        "/internal/v1/admin/tokens",
        headers=admin_headers(),
        json={"name": "cli-token", "kind": "interactive", "scopes": ["read"]},
    )
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    assert token.startswith("hub_")

    row = db.write_sync(
        lambda conn: conn.execute("SELECT hash FROM main.api_token WHERE name = 'cli-token'").fetchone()
    )
    assert row is not None
    assert token not in row[0]


async def test_login_code_works_on_public_auth_session(internal_client, db, clock) -> None:
    r = await internal_client.post("/internal/v1/admin/login-code", headers=admin_headers())
    assert r.status_code == 200
    code = r.json()["code"]

    public_app = create_public_app(db, clock=clock, port=8787)
    transport = ASGITransport(app=public_app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8787") as public_client:
        session_r = await public_client.post(
            "/api/v1/auth/session", json={"code": code}, headers={"Origin": "http://127.0.0.1:8787"}
        )
    assert session_r.status_code == 200, session_r.text
    assert "csrf" in session_r.json()
