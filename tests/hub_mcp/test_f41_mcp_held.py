"""Ф41-2а (docs/architecture.md + П1; раздел 5 п.9): MCP-сервер внутри сессии
хаба (`HUB_SESSION`) — личность без реестра и предков, ответ `hub_subscribe` без команды ожидания, состояние
сессии хаба в строках линий. Ядро — подменный транспорт, как в test_mcp_subs.py."""

import json

import httpx
import pytest
from mcp import Client

from hub import session_registry as reg
from hub.mcp_server import server as mcp_server

from .conftest import text_of, write_profile

HUB_ID = 7
ME = {"pid": -HUB_ID, "proc_start": 0, "session_id": f"hub:{HUB_ID}"}
SUB = {
    "id": 17,
    "project_id": 3,
    "session_name": None,
    "session_pid": -HUB_ID,
    "created_ts": 1,
    "last_wait_ts": None,
    "last_delivery_ts": None,
    "live": True,
    "listening": False,
    "waiting": False,
    "ended_ts": None,
    "end_reason": None,
    "pending": 0,
    "mode": "cards",
    "msg_filter": "all",
    "role": "маркетолог",
    "role_no": 1,
    "engine": "opencode",
    "held": True,
    "hub_session_id": HUB_ID,
    "state": "asleep",
    "mine": True,
}


class Core:
    def __init__(self, routes):
        self.routes = routes
        self.seen: list[tuple[str, str, dict | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path.removeprefix("/api/v1")
        self.seen.append((request.method, path, body or dict(request.url.params) or None))
        status, payload = self.routes[(request.method, path)]
        return httpx.Response(status, json=payload)


def _server(make_server, config_dir, core, *, environ):
    write_profile(config_dir, "all", "hubt_x")
    server, hub = make_server("all", transport=httpx.MockTransport(core.handler))
    hub.hub_path = "/opt/hub-prod/.venv/bin/hub"
    hub.environ = environ

    def no_ancestors(*_args, **_kwargs):
        raise AssertionError("у сессии хаба личность — без обхода предков и реестра")

    hub.getppid = no_ancestors
    hub.find_session = no_ancestors
    hub.find_opencode_session = no_ancestors
    return server, hub


async def _call(server, name, **args):
    async with Client(server) as client:
        return await client.call_tool(name, args)


@pytest.mark.parametrize("extra", [{}, {"OPENCODE_PID": "4096"}])
async def test_subscribe_from_hub_session_no_wait_command(
    config_dir, make_server, monkeypatch, tmp_path, extra
):
    """`HUB_SESSION` главнее `OPENCODE_PID`; `oc_session`/`oc_dir` плагина в личность не идут (П1)."""
    monkeypatch.chdir(tmp_path)
    core = Core({("POST", "/subs"): (200, {"sub": SUB, "created": True, "backfilled": 0})})
    server, _hub = _server(make_server, config_dir, core, environ={"HUB_SESSION": str(HUB_ID), **extra})
    result = await _call(server, "hub_subscribe", oc_session="ses_AAAAAAAAAA1", oc_dir="/somewhere/else")
    text = text_of(result)
    assert not result.is_error, text
    session = core.seen[0][2]["session"]
    assert session == {**ME, "cwd": str(tmp_path), "name": None, "name_source": None, "kind": "hub"}
    assert "Подписка #17 на проект 3 (новая)" in text
    assert "сигналы придут сами" in text
    assert "sub wait" not in text and "run_in_background" not in text
    assert mcp_server.OC_WAIT_MARK not in text and "плагин" not in text
    assert mcp_server.SUB_RULES_TEXT in text


async def test_subscribe_channel_from_hub_session_no_wait_command(config_dir, make_server):
    chan = {**SUB, "id": 5, "channel_id": 283, "keywords": None}
    core = Core({("POST", "/chan-subs"): (200, {"sub": chan, "created": True, "count": 1, "others": []})})
    server, _hub = _server(make_server, config_dir, core, environ={"HUB_SESSION": str(HUB_ID)})
    text = text_of(await _call(server, "hub_subscribe", channel_id=283))
    assert core.seen[0][2]["session"]["kind"] == "hub"
    assert "Подписка c5 на беседу #283" in text and "сигналы придут сами" in text
    assert "sub wait" not in text


async def test_hub_session_with_hub_work_refused_without_request(config_dir, make_server):
    core = Core({})
    env = {"HUB_SESSION": str(HUB_ID), "HUB_WORK": "1"}
    server, _hub = _server(make_server, config_dir, core, environ=env)
    for tool in ("hub_subscribe", "hub_unsubscribe"):
        result = await _call(server, tool)
        assert result.is_error and "HUB_SESSION и HUB_WORK" in text_of(result)
    for bad in ("0", "-3", "семь", "7.0"):
        server, _hub = _server(make_server, config_dir, core, environ={"HUB_SESSION": bad})
        result = await _call(server, "hub_subscribe")
        assert result.is_error and "HUB_SESSION" in text_of(result), bad
    assert core.seen == []


async def test_lines_show_hub_session_state_instead_of_not_listening(config_dir, make_server):
    awake = {**SUB, "id": 18, "role_no": 2, "state": "running", "mine": False, "engine": "claude"}
    idle = {**SUB, "id": 19, "role": "тестер", "state": "idle", "mine": False}
    external = {**SUB, "id": 20, "role": "сисадмин", "session_pid": 4242, "mine": False}
    for key in ("held", "hub_session_id", "state"):
        external.pop(key)
    rows = [SUB, awake, idle, external]
    core = Core(
        {
            ("GET", "/subs"): (200, {"items": rows, "channels": [{**SUB, "id": 5, "channel_id": 283}]}),
            ("GET", "/projects"): (200, [{"id": 3, "name": "Umbrella"}]),
            ("GET", "/agents/states"): (409, {"detail": "у этой сессии нет подписки"}),
        }
    )
    server, _hub = _server(make_server, config_dir, core, environ={"HUB_SESSION": str(HUB_ID)})
    text = text_of(await _call(server, "hub_subscriptions"))
    assert core.seen[0][2] == {"live": "1", **{k: str(v) for k, v in ME.items()}}  # «это ты» — по тройке
    lines = {line.split(" · ")[0]: line for line in text.splitlines() if "подписка" in line}
    assert "сессия хаба, спит" in lines["подписка #17"] and "это ты" in lines["подписка #17"]
    assert "сессия хаба, в работе" in lines["подписка #18"]
    assert "сессия хаба, ждёт входа" in lines["подписка #19"]
    assert "НЕ слушает" in lines["подписка #20"] and "сессия хаба" not in lines["подписка #20"]
    assert "сессия хаба, спит" in lines["беседа #283"]

    agents = text_of(await _call(server, "hub_agents"))
    by_sub = {part: line for line in agents.splitlines() for part in line.split(" · ") if "#" in part}
    assert "сессия хаба, спит" in by_sub["подписка #17"] and "OpenCode" in by_sub["подписка #17"]
    assert "сессия хаба, в работе" in by_sub["подписка #18"] and "Claude Code" in by_sub["подписка #18"]
    assert "НЕ слушает" in by_sub["подписка #20"]
    # Адрес и вызов `hub_agent_send` у спящей сессии хаба — как у любого агента.
    assert 'hub_agent_send(project_id=3, role="тестер"' in agents


def test_listening_ru():
    assert mcp_server.listening_ru({"listening": True}) == "слушает"
    assert mcp_server.listening_ru({"listening": False}) == "НЕ слушает"
    assert mcp_server.listening_ru({"held": True, "listening": False}) == "сессия хаба, спит"
    assert mcp_server.listening_ru({"held": True, "state": "running"}) == "сессия хаба, в работе"
    assert mcp_server.listening_ru({"held": True, "state": "idle"}) == "сессия хаба, ждёт входа"
    assert reg.HUB_KIND == "hub"
