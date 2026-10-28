"""hub-mcp: догон к сроку не уходит, если ответили (Ф30-2 A4, тест B6) — `hub_draft(send_at=…,
cancel_if_reply=True)` передаёт флажок ядру (и с файлами), `hub_drafts` показывает «не уйдёт, если ответят» и
причину неудачи как есть."""

import json

from mcp import Client

from hub.core import outbox
from hub.mcp_server import server as mcp_server

from .conftest import insert_channel, insert_project, text_of
from .test_mcp_files import _pdf, _write, env, recording  # noqa: F401 — фикстуры файлов
from .test_mcp_send_at import AT, AT_LABEL, NOW_S, _account, _draft_id, _line
from .test_mcp_server import _call, _profile

MARK = mcp_server.CANCEL_IF_REPLY_RU


async def test_b6_draft_passes_flag_and_drafts_mark(db, clock, config_dir, make_server):
    clock.value = NOW_S
    project = insert_project(db, name="Umbrella")
    tg = _account(db)
    channel = insert_channel(db, tg, project_id=project, ext_id="p1", title="Партнёр")
    _profile(db, config_dir, project_ids=[project], profile="iip")
    server, _hub = make_server("iip")
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert "cancel_if_reply" in tools["hub_draft"].input_schema["properties"]
        assert "для догонов и напоминаний — true" in tools["hub_draft"].description

        out = text_of(
            await _call(
                client,
                "hub_draft",
                channel_id=channel,
                text="Напоминаю про договор",
                send_at="2026-10-05 10:00",
                cancel_if_reply=True,
            )
        )
        assert "ответят — не уйдёт" in out and "Ничего не отправлено" in out
        oid = _draft_id(out)
        with db.reader() as conn:
            (flag,) = conn.execute("SELECT cancel_if_reply FROM main.outbox WHERE id = ?", (oid,)).fetchone()
        assert flag == 1

        refused = await client.call_tool(
            "hub_draft", {"channel_id": channel, "text": "x", "cancel_if_reply": True}
        )
        assert refused.is_error and "только вместе с send_at" in text_of(refused)

        drafts = text_of(await _call(client, "hub_drafts"))
        assert _line(drafts, oid).endswith(f"к {AT_LABEL} · {MARK}")

        now_ms = int(clock() * 1000)
        db.write_sync(lambda conn: outbox.approve(conn, oid, now_ms=now_ms))
        queued = text_of(await _call(client, "hub_drafts", status="queued"))
        assert _line(queued, oid).endswith(f"уйдёт в {AT_LABEL} (к сроку) · {MARK}")

        db.write_sync(
            lambda conn: conn.execute(
                "UPDATE main.outbox SET status = 'failed', error = ? WHERE id = ?",
                (outbox.SEND_AT_REPLIED_ERROR, oid),
            )
        )
        failed = text_of(await _call(client, "hub_drafts", status="failed"))
        assert outbox.SEND_AT_REPLIED_ERROR in failed


async def test_b6_plain_draft_has_no_mark(db, clock, config_dir, make_server):
    clock.value = NOW_S
    project = insert_project(db, name="Umbrella")
    channel = insert_channel(db, _account(db), project_id=project, ext_id="g1", title="Вебинар")
    _profile(db, config_dir, project_ids=[project], profile="iip")
    server, _hub = make_server("iip")
    async with Client(server) as client:
        out = text_of(
            await _call(client, "hub_draft", channel_id=channel, text="Пост", send_at="2026-10-05 10:00")
        )
        assert "ответят" not in out
        drafts = text_of(await _call(client, "hub_drafts"))
        assert _line(drafts, _draft_id(out)).endswith(f"к {AT_LABEL}")


async def test_b6_draft_with_files_passes_flag(db, clock, env, make_server, recording):  # noqa: F811
    clock.value = NOW_S
    pdf = _write(env["workdir"] / "out" / "dogovor.pdf", _pdf("1"))
    server, _hub = make_server("all", transport=recording)
    async with Client(server) as client:
        out = await client.call_tool(
            "hub_draft",
            {
                "channel_id": env["channel"],
                "files": [pdf],
                "send_at": "2026-10-05 10:00",
                "cancel_if_reply": True,
            },
        )
        assert not out.is_error, text_of(out)
        assert f"уйдёт в {AT_LABEL} ({MARK})" in text_of(out)
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert "cancel_if_reply" in tools["hub_draft"].input_schema["properties"]
    with db.reader() as conn:
        (send_at_ts, flag, files) = conn.execute(
            "SELECT send_at_ts, cancel_if_reply, file_json FROM main.outbox WHERE id = ?",
            (_draft_id(text_of(out)),),
        ).fetchone()
    assert (send_at_ts, flag, len(json.loads(files))) == (AT, 1, 1)
