"""Ф40-3 C (`docs/architecture.md`): выключатель статуса «в сети» у аккаунта Mattermost — свой
ключ настроек `mm_status`; ключ Slack (`slack_status`) на Mattermost не действует и наоборот."""

import json

import pytest

from hub.core import connector_auth

from .conftest import insert_account
from .test_f20_activity import activity, app, internal  # noqa: F401 — фикстуры ядра с памятью активности


def _connector(db, source: str, config: dict | None) -> dict:
    account_id = insert_account(db, source=source)
    secret = connector_auth.generate_connector_secret()

    def _fn(conn):
        conn.execute(
            "UPDATE main.account SET config_json = ? WHERE id = ?",
            (json.dumps(config) if config is not None else None, account_id),
        )
        conn.execute(
            "INSERT INTO main.connector_secret (account_id, hash, created_ts) VALUES (?, ?, 0)",
            (account_id, connector_auth.hash_secret(secret)),
        )

    db.write_sync(_fn)
    return {"Authorization": f"Bearer {secret}"}


@pytest.mark.parametrize(
    ("source", "config", "enabled"),
    [
        ("mattermost", None, True),
        ("mattermost", {"mm_status": True}, True),
        ("mattermost", {"mm_status": False}, False),
        ("mattermost", {"slack_status": False}, True),  # чужой ключ не действует
        ("mattermost", {"idempotent_send": True}, True),
        ("slack", {"mm_status": False}, True),  # у Slack — по-прежнему только свой ключ
        ("slack", {"slack_status": False}, False),
    ],
)
async def test_status_switch_key_by_source(internal, db, source, config, enabled):  # noqa: F811
    headers = _connector(db, source, config)
    assert (await internal.get("/internal/v1/activity", headers=headers)).json()["enabled"] is enabled
