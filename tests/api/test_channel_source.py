# Спека 2e, раздел «Ядро»: поле `source` (источник аккаунта — telegram/slack/fake/…) в словаре канала везде,
# где канал отдаётся UI: `GET /projects/{id}/channels`, `GET /inbox`, изменения канала в `GET /sync`.
# `POST /channels/resolve` уже отдавал `source` (queries.resolve_channels) — до этого куска не трогаем.
from hub.core.db import next_seq

from .conftest import insert_account, insert_channel, insert_message, insert_project, login_via_code


def _bump_channel_seq(db, channel_id):
    """Тот же приём, что в test_threads_api.py: каналу нужен `seq > 0`, чтобы попасть в `/sync`."""
    schema = "work" if channel_id >= 2**40 else "main"

    def _fn(conn):
        seq = next_seq(conn)
        conn.execute(f"UPDATE {schema}.channel SET seq = ? WHERE id = ?", (seq, channel_id))

    db.write_sync(_fn)


async def test_list_channels_carries_account_source(client, db, clock):
    proj = insert_project(db)
    tg = insert_account(db, source="telegram")
    slack = insert_account(db, source="slack", db_name="work")
    ch_tg = insert_channel(db, tg, project_id=proj, ext_id="c-tg", title="Telegram-чат")
    ch_slack = insert_channel(
        db, slack, project_id=proj, ext_id="c-slack", db_name="work", title="Slack-канал"
    )

    await login_via_code(client, db, clock)
    r = await client.get(f"/api/v1/projects/{proj}/channels")
    assert r.status_code == 200
    by_id = {c["id"]: c for c in r.json()}
    assert by_id[ch_tg]["source"] == "telegram"
    assert by_id[ch_slack]["source"] == "slack"


async def test_inbox_carries_account_source(client, db, clock):
    proj = insert_project(db)
    slack = insert_account(db, source="slack", db_name="work")
    ch = insert_channel(db, slack, project_id=proj, ext_id="c-slack", db_name="work", title="Slack-канал")
    insert_message(db, ch, ext_id="m1", ts=1000)  # непрочитанное — иначе inbox его не покажет

    await login_via_code(client, db, clock)
    r = await client.get("/api/v1/inbox")
    assert r.status_code == 200
    by_id = {c["id"]: c for c in r.json()}
    assert by_id[ch]["source"] == "slack"


async def test_sync_channel_change_carries_account_source(client, db, clock):
    proj = insert_project(db)
    slack = insert_account(db, source="slack", db_name="work")
    ch = insert_channel(db, slack, project_id=proj, ext_id="c-slack", db_name="work", title="Slack-канал")
    _bump_channel_seq(db, ch)

    await login_via_code(client, db, clock)
    r = await client.get("/api/v1/sync", params={"since": 0})
    assert r.status_code == 200
    by_id = {c["id"]: c for c in r.json()["channels"]}
    assert by_id[ch]["source"] == "slack"
