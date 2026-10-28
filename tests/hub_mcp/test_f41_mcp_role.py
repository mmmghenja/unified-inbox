"""Ф41-8: MCP `hub_role` и строка о нём в ответе `hub_subscribe` (с ролью — есть, без роли — нет)."""

import pytest
from mcp import Client

from hub import roles
from hub import session_registry as reg
from hub.core import subs
from hub.mcp_server import server as mcp_server

from .conftest import insert_project, make_token, text_of, write_profile

LINE = "Правила роли и взаимодействия агентов: hub_role()"


@pytest.fixture
def stand(db, config_dir, make_server, tmp_path, monkeypatch):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    me = reg.SessionInfo(4242, 777_000, "sess-aaaaaaaa-1111", str(workdir), None, None, "interactive")
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: {me.pid: me.proc_start}.get(pid))
    project = insert_project(db, name="Umbrella", workdir=str(workdir))
    db.write_sync(lambda conn: subs.set_project_subs(conn, project, True, now_ms=0))
    token, token_id = make_token(db, scopes=("read", "draft"), name="mcp:all")
    write_profile(config_dir, "all", token, token_id)
    server, hub = make_server("all")
    hub.environ = {}
    hub.getppid = lambda: 1
    hub.find_session = lambda start: me
    return server


async def _call(server, name, **args):
    async with Client(server) as client:
        return await client.call_tool(name, args)


async def test_hub_role_without_subscription_hints(stand):
    text = text_of(await _call(stand, "hub_role"))
    assert "сначала hub_subscribe(role=" in text
    assert roles.common_text() not in text


async def test_hub_role_with_role_gives_common_and_role_text(stand):
    sub = text_of(await _call(stand, "hub_subscribe", role="программист"))
    assert LINE in sub
    text = text_of(await _call(stand, "hub_role"))
    # Ф48-3: встроенного текста роли нет — только общий текст (файла роли у внешней сессии нет).
    assert text == roles.prompt("программист") == roles.common_text()


async def test_hub_role_unknown_role_gives_common_only(stand):
    await _call(stand, "hub_subscribe", role="маркетолог")
    assert text_of(await _call(stand, "hub_role")) == roles.common_text()


async def test_subscribe_without_role_has_no_line_and_hub_role_hints(stand):
    sub = text_of(await _call(stand, "hub_subscribe"))
    assert LINE not in sub
    assert "сначала hub_subscribe(role=" in text_of(await _call(stand, "hub_role"))


def test_hub_role_allowed_for_all_not_wake():
    assert "hub_role" in mcp_server.tool_names("all")
