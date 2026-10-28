"""hub-mcp: `hub_draft_new` (Ф12-1 C1, тест 16). Ядро — в процессе, MCP-клиент — в памяти."""

import json

from mcp import Client

from .conftest import insert_project, text_of
from .test_mcp_server import _call, _profile


def _account(db, source, config=None) -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, config_json) VALUES (?, 'main', ?)",
                (source, json.dumps(config) if config is not None else None),
            ).lastrowid
        )
    )


async def test_16_draft_new_both_forms_and_errors(db, config_dir, make_server):
    project = insert_project(db, name="Umbrella")
    other = insert_project(db, name="Initech")
    tg = _account(db, "telegram")
    mail = _account(db, "mail", {"address": "me@gmail.com"})
    _profile(db, config_dir, project_ids=[project], profile="iip")
    server, _hub = make_server("iip")
    async with Client(server) as client:
        out = await _call(
            client,
            "hub_draft_new",
            project_id=project,
            account_id=tg,
            to="@petr_ivanov",
            text="Здравствуйте!",
        )
        assert not out.is_error, text_of(out)
        assert "новая беседа #" in text_of(out) and "Ничего не" in text_of(out)

        out = await _call(
            client,
            "hub_draft_new",
            project_id=project,
            account_id=mail,
            to="client@example.com",
            subject="Подпись",
            text="Добрый день!",
        )
        assert not out.is_error, text_of(out)
        assert "новая беседа #" in text_of(out)

        # ошибки ядра — текстом
        out = await _call(
            client, "hub_draft_new", project_id=project, account_id=mail, to="client@example.com", text="x"
        )
        assert out.is_error and "subject" in text_of(out)
        out = await _call(client, "hub_draft_new", project_id=project, account_id=tg, to="@12", text="x")
        assert out.is_error and "@username" in text_of(out)
        out = await _call(client, "hub_draft_new", project_id=other, account_id=tg, to="@petr_x", text="x")
        assert out.is_error and "проект" in text_of(out)
        out = await _call(
            client, "hub_draft_new", project_id=project, account_id=tg, to="@petr_ivanov", text="ещё"
        )
        assert out.is_error and "уже есть черновик #" in text_of(out)

        # в выдаче каналов беседа-заготовка помечена
        listing = await _call(client, "hub_channels", project_id=project)
        assert "новая беседа (ждёт первого сообщения)" in text_of(listing)
        assert "трекер" not in text_of(listing)
        overview = await _call(client, "hub_overview")
        assert "hub_draft_new" in text_of(overview)
    with db.reader() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM main.outbox WHERE status = 'pending_approval'").fetchone()[0]
            == 2
        )


