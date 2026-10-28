"""Тест 8 (кусок 7): `/admin/watch` и `/admin/backup` без admin-ключа и с ключом коннектора -> 401; в
ответе `/admin/watch` нет `text`/`last_error` ни для одного аккаунта. Спецификация:
docs/architecture.md.
"""

from hub.core.connector_auth import generate_connector_secret, hash_secret

from .conftest import admin_headers, connector_headers, insert_account, set_account_status


def _make_connector_secret(db, account_id: int, *, secret: str) -> None:
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.connector_secret (account_id, hash, created_ts) VALUES (?, ?, ?)",
            (account_id, hash_secret(secret), 0),
        )
    )


async def test_admin_watch_without_key_is_401(raw_client) -> None:
    r = await raw_client.get("/internal/v1/admin/watch")
    assert r.status_code == 401


async def test_admin_backup_without_key_is_401(raw_client) -> None:
    r = await raw_client.post("/internal/v1/admin/backup")
    assert r.status_code == 401


async def test_admin_watch_with_connector_secret_is_401(raw_client, db) -> None:
    account_id = insert_account(db, source="fake")
    secret = generate_connector_secret()
    _make_connector_secret(db, account_id, secret=secret)

    r = await raw_client.get("/internal/v1/admin/watch", headers=connector_headers(secret))
    assert r.status_code == 401


async def test_admin_backup_with_connector_secret_is_401(raw_client, db) -> None:
    account_id = insert_account(db, source="fake")
    secret = generate_connector_secret()
    _make_connector_secret(db, account_id, secret=secret)

    r = await raw_client.post("/internal/v1/admin/backup", headers=connector_headers(secret))
    assert r.status_code == 401


async def test_admin_watch_ok_and_no_text_or_last_error(raw_client, db) -> None:
    # Ф51-1 A4: причину отказа входа (`auth_required`) сторож теперь получает — см. тест ниже; у прочих
    # статусов `last_error` по-прежнему не отдаётся.
    account_id = insert_account(db, source="fake")
    set_account_status(db, account_id, status="no_network", last_ok_ts=None)
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.account SET last_error = ? WHERE id = ?", ("секретная деталь", account_id)
        )
    )

    r = await raw_client.get("/internal/v1/admin/watch", headers=admin_headers())
    assert r.status_code == 200, r.text
    body = r.json()

    # Поправки после opus-критика кода куска 2b (R4): + source_time_hwm — hub-watch поднимает
    # `retention_stalled`, когда источники молчат дольше недели.
    assert set(body.keys()) == {
        "now",
        "accounts",
        "dead_letter_total",
        "outbox_unknown",
        "last_backup_ts",
        "source_time_hwm",
        "media",  # Ф8-5 П12: объём медиа и свободное место — только числа
        "remote_login_failures_24h",  # Ф10-7, решение 4: неверные пароли входа с телефона — число
        "transcribe",  # Ф14-6 E: очередь расшифровки — только числа и флаг «служба заведена»
        "remote_tls",  # Ф14-9 П1: пара https удалённого слушателя — срок и ошибка (null — http)
    }
    assert set(body["media"].keys()) == {"total_bytes", "free_bytes"}
    assert set(body["transcribe"].keys()) == {
        "configured",
        "queued",
        "working",
        "failed_24h",
        "oldest_queued_ts",
    }
    assert len(body["accounts"]) == 1
    account = body["accounts"][0]
    assert set(account.keys()) == {
        "id",
        "source",
        "title",
        "status",
        "last_ok_ts",
        "flood_until",
        "has_connector_secret",  # раздел E куска 1a: опора проблемы account:<id>:silent
        "last_alert_ts",  # кусок 1c: опора проблемы account:<id>:alert
        "last_error",  # Ф51-1 A4: только причина отказа входа у `auth_required`, иначе null
        "peer_flood_ts",  # Ф69-1 E: опора проблемы account:<id>:peer_flood
    }
    assert account["last_alert_ts"] is None
    assert account["last_error"] is None
    assert "секретная деталь" not in str(body)


async def test_admin_watch_gives_auth_required_reason(raw_client, db) -> None:
    """Ф51-1 A4: у аккаунта в `auth_required` снимок несёт причину отказа входа."""
    account_id = insert_account(db, source="fake")
    set_account_status(db, account_id, status="auth_required", last_ok_ts=None)
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.account SET last_error = ? WHERE id = ?",
            ("imap: вход не принят (other)", account_id),
        )
    )

    r = await raw_client.get("/internal/v1/admin/watch", headers=admin_headers())
    assert r.status_code == 200, r.text
    (account,) = r.json()["accounts"]
    assert account["last_error"] == "imap: вход не принят (other)"
