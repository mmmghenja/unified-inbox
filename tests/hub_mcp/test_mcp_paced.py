"""hub-mcp: черновик по графику (Ф12-2 B, тест 10) — `hub_draft(paced=true)`, `hub_drafts` с пометками «по
графику» / «уйдёт ~…» и остатком суточного лимита в шапке."""

import datetime as dt
import json
import random
import re
import zoneinfo

from mcp import Client

from hub.core import outbox
from hub.mcp_server import format as fmt

from .conftest import insert_channel, insert_project, text_of
from .test_mcp_server import _blocks, _call, _profile

MSK = zoneinfo.ZoneInfo("Europe/Moscow")
NOON_S = dt.datetime(2026, 9, 28, 12, 0, tzinfo=MSK).timestamp()


def _account(db, source="telegram") -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, config_json) VALUES (?, 'main', ?)",
                (source, json.dumps({"idempotent_send": True, "pace_daily": 5})),
            ).lastrowid
        )
    )


def _draft_id(text: str) -> int:
    return int(re.search(r"Черновик #(\d+)", text).group(1))


async def test_10_draft_paced_and_drafts_show_slot_and_remaining(db, clock, config_dir, make_server):
    clock.value = NOON_S
    project = insert_project(db, name="Umbrella")
    tg = _account(db)
    ch_a = insert_channel(db, tg, project_id=project, ext_id="u1", title="Пётр")
    ch_b = insert_channel(db, tg, project_id=project, ext_id="u2", title="Анна")
    _profile(db, config_dir, project_ids=[project], profile="iip")
    server, _hub = make_server("iip")
    async with Client(server) as client:
        out = text_of(
            await _call(client, "hub_draft", channel_id=ch_a, text="Напоминаю о подписи", paced=True)
        )
        assert "по графику" in out and "Ничего не отправлено" in out
        paced_id = _draft_id(out)
        plain = text_of(await _call(client, "hub_draft", channel_id=ch_b, text="Обычный ответ"))
        assert "по графику" not in plain
        plain_id = _draft_id(plain)

        drafts = text_of(await _call(client, "hub_drafts"))
        assert f"По графику на сегодня: аккаунт #{tg} (telegram) — 0 из 5, осталось 5." in drafts
        lines = _blocks(drafts)[0][1]
        assert any(line.startswith(f"#{paced_id} ") and line.endswith("мой · по графику") for line in lines)
        assert any(line.startswith(f"#{plain_id} ") and line.endswith("мой") for line in lines)

        now_ms = int(clock() * 1000)
        slot = db.write_sync(lambda conn: outbox.approve(conn, paced_id, now_ms=now_ms, rng=random.Random(1)))
        queued = text_of(await _call(client, "hub_drafts", status="queued"))
        assert f"уйдёт ~{fmt.fmt_ts(slot)} (по графику)" in queued
        assert "— 1 из 5, осталось 4." in queued

    with db.reader() as conn:
        paced = dict(conn.execute("SELECT id, paced FROM main.outbox").fetchall())
    assert paced == {paced_id: 1, plain_id: 0}
