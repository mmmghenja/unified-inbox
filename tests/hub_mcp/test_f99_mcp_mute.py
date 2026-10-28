"""Ф99-2: MCP `hub_mute` — сессия через `_me()`, отказы ядра словами, ответ «было — стало»."""

import pytest
from mcp import Client

from hub import session_registry as reg
from hub.core import subs

from .conftest import insert_account, insert_channel, insert_project, make_token, text_of, write_profile

ROLE = "маркетолог"
NOTE = "рассылка курса, к проекту не относится"


async def _call(server, name, **args):
    async with Client(server) as client:
        return await client.call_tool(name, args)


@pytest.fixture
def stand(db, config_dir, make_server, tmp_path, monkeypatch):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    me = reg.SessionInfo(4242, 777_000, "sess-aaaaaaaa-1111", str(workdir), None, None, "interactive")
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: {me.pid: me.proc_start}.get(pid))
    project = insert_project(db, name="Umbrella", workdir=str(workdir))
    db.write_sync(lambda conn: subs.set_project_subs(conn, project, True, now_ms=0))
    account = insert_account(db, source="telegram")
    channel = insert_channel(db, account, project_id=project, ext_id="g1", kind="group", title="Рассылка")
    token, token_id = make_token(db, scopes=("read", "draft"), name="mcp:all")
    write_profile(config_dir, "all", token, token_id)
    server, hub = make_server("all")
    hub.environ = {}
    hub.getppid = lambda: 1
    hub.find_session = lambda start: me
    return {"server": server, "db": db, "channel": channel}


def _muted(db, channel_id):
    with db.reader() as conn:
        return conn.execute(
            "SELECT muted, muted_by IS NOT NULL FROM main.channel WHERE id = ?", (channel_id,)
        ).fetchone()


async def test_mute_refusal_then_mute_and_unmute(stand):
    server, db, ch = stand["server"], stand["db"], stand["channel"]
    result = await _call(server, "hub_mute", channel_id=ch, note=NOTE)
    assert result.is_error and "подписка сессии на проект беседы с ролью" in text_of(result)
    assert _muted(db, ch) == (0, 0)

    assert not (await _call(server, "hub_subscribe", role=ROLE)).is_error
    result = await _call(server, "hub_mute", channel_id=ch, note=NOTE)
    assert not result.is_error, text_of(result)
    assert f"было — со звуком, стало — заглушена (сессией: {ROLE})" in text_of(result)
    assert _muted(db, ch) == (1, 1)

    result = await _call(server, "hub_mute", channel_id=ch, on=False, note="снова пишут по делу")
    assert not result.is_error, text_of(result)
    assert f"было — заглушена (сессией: {ROLE}), стало — со звуком" in text_of(result)
    assert _muted(db, ch) == (0, 0)


async def test_mute_owner_mute_refused_in_words(stand):
    server, db, ch = stand["server"], stand["db"], stand["channel"]
    assert not (await _call(server, "hub_subscribe", role=ROLE)).is_error
    db.write_sync(lambda conn: conn.execute("UPDATE main.channel SET muted = 1 WHERE id = ?", (ch,)))
    result = await _call(server, "hub_mute", channel_id=ch, on=False, note=NOTE)
    assert result.is_error and "заглушил владелец" in text_of(result)
    result = await _call(server, "hub_mute", channel_id=ch, note=NOTE)
    assert not result.is_error and "снять эту заглушку ты не сможешь" in text_of(result)
