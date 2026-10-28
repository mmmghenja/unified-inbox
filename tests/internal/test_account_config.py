"""`PATCH /admin/accounts/{id}/config` (`hub account set`, почта 24.09): слияние верхнего уровня
конфига, `null` удаляет ключ, ключ-секрет -> 422 без записи, чужого аккаунта нет -> 404, аудит без значений,
коннектор видит новый конфиг через `GET /account`. Обвязка — `tests/internal/conftest.py`."""

import json

from .conftest import admin_headers, connector_headers, make_connector_account


def _config(db, account_id):
    with db.reader() as conn:
        raw = conn.execute("SELECT config_json FROM main.account WHERE id = ?", (account_id,)).fetchone()[0]
    return json.loads(raw) if raw else None


def _set_config(db, account_id, config):
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.account SET config_json = ? WHERE id = ?", (json.dumps(config), account_id)
        )
    )


async def test_set_merges_top_level_and_null_removes(internal_client, db) -> None:
    account_id, secret = make_connector_account(db, source="mail")
    _set_config(db, account_id, {"address": "a@b.c", "send_enabled": False, "aliases": ["x@b.c"]})

    r = await internal_client.patch(
        f"/internal/v1/admin/accounts/{account_id}/config",
        headers=admin_headers(),
        json={"set": {"send_enabled": True, "aliases": None, "exclude_categories": ["promotions"]}},
    )
    assert r.status_code == 200, r.text
    expected = {"address": "a@b.c", "send_enabled": True, "exclude_categories": ["promotions"]}
    assert r.json()["config"] == expected
    assert _config(db, account_id) == expected

    # коннектор после перезапуска читает именно это
    r = await internal_client.get("/internal/v1/account", headers=connector_headers(secret))
    assert r.status_code == 200, r.text
    assert r.json()["config"] == expected


async def test_set_on_empty_config_creates_it(internal_client, db) -> None:
    account_id, _secret = make_connector_account(db)
    assert _config(db, account_id) is None

    r = await internal_client.patch(
        f"/internal/v1/admin/accounts/{account_id}/config", headers=admin_headers(), json={"set": {"k": 1}}
    )
    assert r.status_code == 200, r.text
    assert _config(db, account_id) == {"k": 1}


async def test_secret_like_key_is_422_and_nothing_written(internal_client, db) -> None:
    account_id, _secret = make_connector_account(db, source="mail")
    _set_config(db, account_id, {"address": "a@b.c"})

    r = await internal_client.patch(
        f"/internal/v1/admin/accounts/{account_id}/config",
        headers=admin_headers(),
        json={"set": {"send_enabled": True, "app_password": "x"}},
    )
    assert r.status_code == 422
    assert _config(db, account_id) == {"address": "a@b.c"}


async def test_unknown_account_is_404(internal_client) -> None:
    r = await internal_client.patch(
        "/internal/v1/admin/accounts/999999/config", headers=admin_headers(), json={"set": {"k": 1}}
    )
    assert r.status_code == 404


async def test_requires_admin(internal_client, db) -> None:
    account_id, secret = make_connector_account(db)
    r = await internal_client.patch(
        f"/internal/v1/admin/accounts/{account_id}/config",
        headers=connector_headers(secret),
        json={"set": {"k": 1}},
    )
    assert r.status_code in (401, 403)
    assert _config(db, account_id) is None


async def test_audit_has_keys_not_values(internal_client, db) -> None:
    account_id, _secret = make_connector_account(db, source="mail")
    r = await internal_client.patch(
        f"/internal/v1/admin/accounts/{account_id}/config",
        headers=admin_headers(),
        json={"set": {"send_enabled": True, "display_name": "Секретное имя"}},
    )
    assert r.status_code == 200, r.text
    with db.reader() as conn:
        detail = conn.execute(
            "SELECT detail_json FROM main.audit WHERE action = 'account_config'"
        ).fetchone()[0]
    assert json.loads(detail) == {"account_id": account_id, "keys": ["display_name", "send_enabled"]}
    assert "Секретное" not in detail
