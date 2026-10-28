"""Ф46-1 (`docs/architecture.md`): `hub_draft_new(from_message_id=…)` —
личка автору сообщения группы Telegram: параметр в обоих объявлениях инструмента, взаимоисключение с `to`,
текстовая и файловая ветки, текст ответа (имя и группа — в блоке данных), отказ «уже есть беседа».
Ядро — в процессе, MCP-клиент — в памяти."""

import json

import pytest
from mcp import Client

from .conftest import insert_channel, insert_message, insert_project, text_of
from .test_mcp_files import _attach_profile, _pdf, _Recording, _write
from .test_mcp_server import _call, _profile

AUTHOR = 5550001
GROUP_EXT = "-1000000004242"
NAME = "Пенопласт"


@pytest.fixture
def env(db, tmp_path):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    project = insert_project(db, name="Umbrella", workdir=str(workdir))
    tg = db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, config_json) VALUES ('telegram', 'main', ?)",
                (json.dumps({"idempotent_send": True, "pace_window": "00:00-24:00"}),),
            ).lastrowid
        )
    )
    group = insert_channel(db, tg, project_id=project, ext_id=GROUP_EXT, kind="group", title="Продавцы")
    contact = db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.contact (account_id, ext_id, name) VALUES (?, ?, ?)",
                (tg, str(AUTHOR), NAME),
            ).lastrowid
        )
    )
    message = insert_message(db, group, ext_id="812", ts=1_700_000_000_000, sender_contact_id=contact)
    return {"workdir": workdir, "project": project, "tg": tg, "group": group, "message": message}


def _data_block(text: str) -> str:
    start = text.index("----- ДАННЫЕ")
    return text[start : text.index("----- КОНЕЦ ДАННЫХ")]


def _stub_meta(db) -> dict:
    with db.reader() as conn:
        (meta_json,) = conn.execute("SELECT meta_json FROM main.channel WHERE ext_id LIKE 'new:%'").fetchone()
    return json.loads(meta_json)


async def test_text_branch_param_exclusion_and_answer(db, config_dir, make_server, env):
    _profile(db, config_dir, project_ids=[env["project"]], profile="iip")
    server, _hub = make_server("iip")
    base = {"project_id": env["project"], "account_id": env["tg"]}
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        schema = tools["hub_draft_new"].input_schema
        assert "from_message_id" in schema["properties"] and "to" not in schema.get("required", [])
        assert "files" not in schema["properties"]  # объявление без файлов
        description = tools["hub_draft_new"].description
        assert "from_message_id=55886" in description and "10 в сутки" in description

        both = await _call(client, "hub_draft_new", **base, to="@x_user", from_message_id=1, text="x")
        assert both.is_error and "что-то одно" in text_of(both)
        none = await _call(client, "hub_draft_new", **base, text="x")
        assert none.is_error and "to или from_message_id" in text_of(none)
        mail = await _call(client, "hub_draft_new", **base, from_message_id=1, subject="Тема", text="x")
        assert mail.is_error and "только для почты" in text_of(mail)
        zero = await _call(client, "hub_draft_new", **base, from_message_id=0, text="x")
        assert zero.is_error and "целое больше 0" in text_of(zero)
        with db.reader() as conn:
            assert conn.execute("SELECT COUNT(*) FROM main.outbox").fetchone()[0] == 0

        out = await _call(client, "hub_draft_new", **base, from_message_id=env["message"], text="Привет!")
        text = text_of(out)
        assert not out.is_error, text
        assert "новая беседа #" in text and "ждёт подтверждения владельца" in text
        assert "только по кнопке владельца" in text
        block = _data_block(text)
        assert NAME in block and "«Продавцы»" in block
        assert NAME not in text.replace(block, "") and str(AUTHOR) not in text
        assert _stub_meta(db)["new_draft"]["to"]["kind"] == "tg_msg_author"

        again = await _call(client, "hub_draft_new", **base, from_message_id=env["message"], text="ещё")
        assert again.is_error and "уже есть черновик #" in text_of(again)
        missing = await _call(client, "hub_draft_new", **base, from_message_id=999_999, text="x")
        assert missing.is_error and "сообщение не найдено" in text_of(missing)


async def test_existing_dm_refusal_names_the_channel(db, config_dir, make_server, env):
    dm = insert_channel(db, env["tg"], project_id=env["project"], ext_id=str(AUTHOR), kind="dm", title=NAME)
    _profile(db, config_dir, project_ids=[env["project"]], profile="iip")
    server, _hub = make_server("iip")
    async with Client(server) as client:
        out = await _call(
            client,
            "hub_draft_new",
            project_id=env["project"],
            account_id=env["tg"],
            from_message_id=env["message"],
            text="Привет!",
        )
    assert out.is_error and f"hub_draft(channel_id={dm}" in text_of(out)


async def test_files_branch_sends_object_and_keeps_draft(db, config_dir, make_server, env, public_app):
    _attach_profile(db, config_dir)
    recording = _Recording(public_app)
    doc = _write(env["workdir"] / "kp.pdf", _pdf("kp"))
    server, _hub = make_server("all", transport=recording)
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        schema = tools["hub_draft_new"].input_schema
        assert {"files", "from_message_id"} <= set(schema["properties"])
        assert "to" not in schema.get("required", [])
        out = await client.call_tool(
            "hub_draft_new",
            {
                "project_id": env["project"],
                "account_id": env["tg"],
                "from_message_id": env["message"],
                "text": "Коммерческое во вложении",
                "files": [doc],
            },
        )
    text = text_of(out)
    assert not out.is_error, text
    assert "kp.pdf" in text and NAME in _data_block(text[text.index("Личка автору") :])
    assert recording.writes() == ["/api/v1/drafts/new", "/api/v1/uploads", "/api/v1/messages/send"]
    meta = _stub_meta(db)
    assert meta["new_draft"]["to"]["kind"] == "tg_msg_author" and meta["new_draft"]["hold"] is True
    assert meta["new_draft"]["from_message_id"] == env["message"]
    with db.reader() as conn:
        assert conn.execute("SELECT status FROM main.outbox").fetchall() == [("pending_approval",)]
