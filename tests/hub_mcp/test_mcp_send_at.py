"""hub-mcp: отправка к точному времени (Ф30-1 B, тест D7 и поправки П3, П5) — `hub_draft(send_at=…)` передаёт
строку ядру (и с файлами), `hub_drafts` показывает «к ЧЧ:ММ ДД.ММ», «уйдёт в …» и «опоздало, не ушло»."""

import datetime as dt
import json
import re
import zoneinfo

from mcp import Client

from hub.core import outbox
from hub.mcp_server import server as mcp_server

from .conftest import insert_channel, insert_project, text_of
from .test_mcp_files import _pdf, _write, env, recording  # noqa: F401 — фикстуры файлов
from .test_mcp_server import _blocks, _call, _profile

MSK = zoneinfo.ZoneInfo("Europe/Moscow")
NOW_S = dt.datetime(2026, 10, 2, 9, 0, tzinfo=MSK).timestamp()
AT = int(dt.datetime(2026, 10, 5, 10, 0, tzinfo=MSK).timestamp() * 1000)
AT_LABEL = dt.datetime.fromtimestamp(AT / 1000).strftime("%H:%M %d.%m")  # местное время, как у MCP


def _account(db) -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, config_json) VALUES ('telegram', 'main', ?)",
                (json.dumps({"idempotent_send": True}),),
            ).lastrowid
        )
    )


def _draft_id(text: str) -> int:
    return int(re.search(r"Черновик #(\d+)", text).group(1))


def _line(drafts: str, outbox_id: int) -> str:
    (line,) = [x for x in _blocks(drafts)[0][1] if x.startswith(f"#{outbox_id} ")]
    return line


async def test_d7_draft_send_at_and_drafts_marks(db, clock, config_dir, make_server):
    clock.value = NOW_S
    project = insert_project(db, name="Umbrella")
    tg = _account(db)
    channel = insert_channel(db, tg, project_id=project, ext_id="g1", title="Вебинар")
    _profile(db, config_dir, project_ids=[project], profile="iip")
    server, _hub = make_server("iip")
    async with Client(server) as client:
        out = text_of(
            await _call(
                client, "hub_draft", channel_id=channel, text="Через час начинаем", send_at="2026-10-05 10:00"
            )
        )
        assert f"к {AT_LABEL}" in out and "Ничего не отправлено" in out
        oid = _draft_id(out)
        refused = await client.call_tool(
            "hub_draft", {"channel_id": channel, "text": "x", "send_at": "2026-10-05 10:00", "paced": True}
        )
        assert refused.is_error and "paced" in text_of(refused)

        drafts = text_of(await _call(client, "hub_drafts"))
        assert _line(drafts, oid).endswith(f"мой · к {AT_LABEL}")

        now_ms = int(clock() * 1000)
        db.write_sync(lambda conn: outbox.approve(conn, oid, now_ms=now_ms))
        queued = text_of(await _call(client, "hub_drafts", status="queued"))
        assert _line(queued, oid).endswith(f"уйдёт в {AT_LABEL} (к сроку)")

        late_ms = AT + outbox.SEND_AT_GRACE_MS + 1
        db.write_sync(lambda conn: outbox.lease(conn, account_id=tg, owner="c", now_ms=late_ms))
        failed = text_of(await _call(client, "hub_drafts", status="failed"))
        assert _line(failed, oid).endswith(mcp_server.SEND_AT_LATE_RU)
        assert "send_at_late" not in failed


async def test_p5_draft_with_files_passes_send_at(db, clock, env, make_server, recording):  # noqa: F811
    clock.value = NOW_S
    pdf = _write(env["workdir"] / "out" / "afisha.pdf", _pdf("1"))
    server, _hub = make_server("all", transport=recording)
    async with Client(server) as client:
        out = await client.call_tool(
            "hub_draft", {"channel_id": env["channel"], "files": [pdf], "send_at": "2026-10-05 10:00"}
        )
        assert not out.is_error, text_of(out)
        assert f"уйдёт в {AT_LABEL}" in text_of(out)
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert "send_at" in tools["hub_draft"].input_schema["properties"]
    assert 'send_at="2026-10-05 10:00"' in tools["hub_draft"].description
    with db.reader() as conn:
        (send_at_ts, files) = conn.execute(
            "SELECT send_at_ts, file_json FROM main.outbox WHERE id = ?", (_draft_id(text_of(out)),)
        ).fetchone()
    assert send_at_ts == AT and len(json.loads(files)) == 1


async def test_d7_plain_profile_tool_has_send_at(db, clock, config_dir, make_server):
    project = insert_project(db, name="Umbrella")
    _profile(db, config_dir, project_ids=[project], profile="iip")
    server, _hub = make_server("iip")
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert "send_at" in tools["hub_draft"].input_schema["properties"]
    assert "files" not in tools["hub_draft"].input_schema["properties"]


def test_late_unknown_has_mark() -> None:
    """Ревью Ф30-1: опоздавшая строка, которая могла уйти (`unknown`), — с пометкой, а не голым статусом."""
    row = {"status": "unknown", "error": mcp_server.SEND_AT_LATE, "send_at_ts": AT}
    assert "опоздало" in mcp_server._pace_mark(row)
