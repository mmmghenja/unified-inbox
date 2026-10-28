"""Ф12-1 A3/A4/A7 на внутреннем сокете: задание несёт `new_to`, отчёт принимает `peer_ext_id`/`peer_title`,
`channels/heads` не видит беседу-заготовку (`docs/architecture.md`)."""

from hub.core import new_recipient, outbox

from .conftest import (
    connector_headers,
    insert_api_token_row,
    insert_channel,
    insert_project,
    make_connector_account,
)


def _draft(db, account_id, project_id, token_id):
    def _fn(conn):
        # Ф12-2: окно графика — круглые сутки, первый слот — «сейчас» (график здесь не проверяется).
        conn.execute(
            "UPDATE main.account SET config_json = json_set(COALESCE(config_json, '{}'), '$.pace_window', "
            "'00:00-24:00') WHERE id = ?",
            (account_id,),
        )
        created = new_recipient.create_draft(
            conn,
            account_id=account_id,
            project_id=project_id,
            to="@Petr_Ivanov",
            text="Здравствуйте!",
            subject=None,
            from_address=None,
            api_token_id=token_id,
            idempotency_key=None,
            now_ms=1_000,
        )
        outbox.approve(conn, created.outbox_id, now_ms=1_000)
        return created

    return db.write_sync(_fn)


async def test_lease_new_to_report_peer_heads(internal_client, db) -> None:
    account_id, secret = make_connector_account(db, source="telegram", idempotent=True)
    project_id = insert_project(db, name="P")
    token_id = insert_api_token_row(db, kind="interactive")
    insert_channel(db, account_id, ext_id="777", kind="dm")
    created = _draft(db, account_id, project_id, token_id)

    heads = await internal_client.get("/internal/v1/channels/heads", headers=connector_headers(secret))
    assert [h["ext_id"] for h in heads.json()] == ["777"]

    r = await internal_client.post("/internal/v1/outbox/lease", headers=connector_headers(secret), json={})
    (item,) = r.json()
    assert item["new_to"] == {"kind": "tg_username", "username": "Petr_Ivanov"}
    assert item["channel_ext_id"].startswith("new:")

    r = await internal_client.post(
        "/internal/v1/outbox/report",
        headers=connector_headers(secret),
        json={
            "outbox_id": item["outbox_id"],
            "lease_gen": item["lease_gen"],
            "outcome": "sent",
            "ext_id": "9001",
            "peer_ext_id": "555",
            "peer_title": "П" * 500,  # длиннее 200 — ядро укорачивает, отчёт не отклоняется
        },
    )
    assert r.status_code == 200, r.text
    with db.reader() as conn:
        ext_id, title = conn.execute(
            "SELECT ext_id, title FROM main.channel WHERE id = ?", (created.channel_id,)
        ).fetchone()
    assert ext_id == "555" and title == "П" * 200

    heads = await internal_client.get("/internal/v1/channels/heads", headers=connector_headers(secret))
    assert sorted(h["ext_id"] for h in heads.json()) == ["555", "777"]
