"""`DELETE /admin/accounts/{id}` (`hub account rm`, владелец 04.10.2026): пустой аккаунт — заведённый `hub auth
tg --new`, но так и не подключённый — удаляется вместе с секретом коннектора; аккаунт, который подключался
или имеет данные, — 409 и ничего не тронуто; чужого нет — 404; только админ; аудит. Обвязка —
`tests/internal/conftest.py`."""

import json

import pytest

from .conftest import admin_headers, connector_headers, make_connector_account


def _exists(db, account_id) -> tuple[bool, bool]:
    """`(строка аккаунта, секрет коннектора)`."""
    with db.reader() as conn:
        account = conn.execute("SELECT 1 FROM main.account WHERE id = ?", (account_id,)).fetchone()
        secret = conn.execute(
            "SELECT 1 FROM main.connector_secret WHERE account_id = ?", (account_id,)
        ).fetchone()
    return account is not None, secret is not None


async def _delete(internal_client, account_id, headers=None):
    return await internal_client.delete(
        f"/internal/v1/admin/accounts/{account_id}", headers=headers or admin_headers()
    )


async def test_empty_account_deleted_with_secret_and_audit(internal_client, db) -> None:
    keep, _ = make_connector_account(db, source="telegram")
    account_id, secret = make_connector_account(db, source="telegram")
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.account_meta (account_id, key, value_json, updated_ts) VALUES (?, 'k', '1', 0)",
            (account_id,),
        )
    )

    r = await _delete(internal_client, account_id)
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": True, "source": "telegram"}
    assert _exists(db, account_id) == (False, False)
    assert _exists(db, keep) == (True, True)
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.account_meta").fetchone()[0] == 0
        detail = conn.execute("SELECT detail_json FROM main.audit WHERE action = 'account_delete'").fetchone()
    assert json.loads(detail[0]) == {"account_id": account_id, "source": "telegram"}

    # секрет удалённого аккаунта больше не пускает
    r = await internal_client.get("/internal/v1/account", headers=connector_headers(secret))
    assert r.status_code in (401, 403)
    # номер повторно не выдаётся
    again, _ = make_connector_account(db, source="telegram")
    assert again > account_id


async def test_connected_account_is_409(internal_client, db) -> None:
    account_id, _ = make_connector_account(db, source="telegram")
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.account SET last_ok_ts = 1 WHERE id = ?", (account_id,))
    )
    r = await _delete(internal_client, account_id)
    assert r.status_code == 409
    assert "уже подключался" in r.json()["detail"]
    assert _exists(db, account_id) == (True, True)


_CHANNEL = "INSERT INTO {}.channel (account_id, ext_id, kind) VALUES (?, 'c1', 'dm')"
_MAP_RULE = "INSERT INTO main.map_rule (account_id, match_json) VALUES (?, '{\"all\": true}')"
# Ф38-2 П4: заявка на правило раскладки любого статуса держит аккаунт.
_RULE_REQUEST = (
    "INSERT INTO main.rule_request (seq, account_id, project_id, kind, match_key, status, created_ts) "
    "VALUES (1, ?, 1, 'all', 'k', '{}', 0)"
)


@pytest.mark.parametrize(
    ("db_name", "sql", "table"),
    [
        ("main", _CHANNEL.format("main"), "main.channel"),
        ("work", _CHANNEL.format("work"), "work.channel"),
        ("main", _MAP_RULE, "main.map_rule"),
        ("main", _RULE_REQUEST.format("pending"), "main.rule_request"),
        ("main", _RULE_REQUEST.format("approved"), "main.rule_request"),
        ("work", _RULE_REQUEST.format("declined"), "main.rule_request"),
    ],
)
async def test_account_with_data_is_409(internal_client, db, db_name, sql, table) -> None:
    account_id, _ = make_connector_account(db, source="slack", db_name=db_name)
    db.write_sync(lambda conn: conn.execute(sql, (account_id,)))
    r = await _delete(internal_client, account_id)
    assert r.status_code == 409
    assert table in r.json()["detail"]
    assert _exists(db, account_id) == (True, True)


async def test_unknown_account_is_404(internal_client) -> None:
    r = await _delete(internal_client, 999999)
    assert r.status_code == 404


async def test_requires_admin(internal_client, db) -> None:
    account_id, secret = make_connector_account(db)
    r = await _delete(internal_client, account_id, headers=connector_headers(secret))
    assert r.status_code in (401, 403)
    assert _exists(db, account_id) == (True, True)
