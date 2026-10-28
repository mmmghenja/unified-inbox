"""hub-mcp, Ф69-1 F: стоп первых сообщений новым людям виден сессиям — строка в ответе `hub_draft_new`,
пометка задержанной строки и причина `failed` в `hub_drafts`. Ядро — в процессе, MCP-клиент — в памяти."""

import json

from mcp import Client

from hub.core import peer_flood
from hub.core.outbox import enqueue

from .conftest import insert_project, text_of
from .test_mcp_server import _call, _profile

STOP_LINE = "сейчас не пишет новым людям"


def _tg_account(db) -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, title, db, config_json) "
                "VALUES ('telegram', 'Личный', 'main', ?)",
                (json.dumps({"idempotent_send": True}),),
            ).lastrowid
        )
    )


def _stop(db, account_id, ts) -> None:
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.account SET peer_flood_ts = ? WHERE id = ?", (ts, account_id))
    )


async def test_f69_draft_new_says_account_does_not_write_new_people(db, config_dir, make_server):
    project = insert_project(db, name="Hooli")
    tg = _tg_account(db)
    _profile(db, config_dir, project_ids=[project], profile="fl")
    server, _hub = make_server("fl")
    async with Client(server) as client:
        out = await _call(
            client, "hub_draft_new", project_id=project, account_id=tg, to="@petr_one", text="Добрый день"
        )
        assert not out.is_error, text_of(out)
        assert STOP_LINE not in text_of(out)

        _stop(db, tg, 1_760_000_000_000)
        out = await _call(
            client, "hub_draft_new", project_id=project, account_id=tg, to="@petr_two", text="Добрый день"
        )
        assert not out.is_error, text_of(out)
        text = text_of(out)
        assert f"Telegram-аккаунт {tg} {STOP_LINE}" in text
        assert "Можно положить с другого аккаунта." in text


async def test_f69_drafts_mark_held_and_failed_reason(db, config_dir, make_server):
    project = insert_project(db, name="Hooli")
    tg = _tg_account(db)
    _token, token_id = _profile(db, config_dir, project_ids=[project], profile="fl")
    channel_id = db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.channel (account_id, ext_id, kind, project_id, title, my_msg_count, "
                "first_seen_ts) "
                "VALUES (?, '501', 'dm', ?, 'Пётр', 1, 0)",
                (tg, project),
            ).lastrowid
        )
    )
    queued = db.write_sync(
        lambda conn: enqueue(
            conn, channel_id=channel_id, text="первое", origin="ui", api_token_id=token_id,
            idempotency_key=None, now_ms=1000,
        )
    ).outbox_id  # fmt: skip
    failed = db.write_sync(
        lambda conn: enqueue(
            conn, channel_id=channel_id, text="второе", origin="ui", api_token_id=token_id,
            idempotency_key=None, now_ms=1001,
        )
    ).outbox_id  # fmt: skip
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.outbox SET status = 'failed', error = ? WHERE id = ?",
            (peer_flood.FAILED_TEXT, failed),
        )
    )
    server, _hub = make_server("fl")
    async with Client(server) as client:
        out = await _call(client, "hub_drafts", status="queued")
        assert f"#{queued}" in text_of(out) and "ждёт снятия стопа новых людей" not in text_of(out)
        _stop(db, tg, 1000)
        out = await _call(client, "hub_drafts", status="queued")
        assert "ждёт снятия стопа новых людей" in text_of(out)
        out = await _call(client, "hub_drafts", status="failed")
        assert "спам-ограничение на первые сообщения новым людям (PeerFlood)" in text_of(out)
