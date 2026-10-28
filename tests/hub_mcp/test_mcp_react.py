"""Ф15-1 B (docs/architecture.md): `hub_react` — сессия из
реестра в теле (П7, как `hub_subscribe`), тексты ответа и отказов; профилям дежурной и оркестратора
инструмента нет. Ядро — подменный транспорт, сессия — подменная `find_session`."""

import json
from datetime import datetime

import httpx
import pytest
from mcp import Client

from hub import session_registry as reg
from hub.mcp_server import server as mcp_server

from .conftest import text_of, write_profile

ME = reg.SessionInfo(100, 5000, "sess-1", "/work/iip", "МАРКЕТОЛОГ", "user", "interactive")
WHEN = 1_700_000_300_000


class Core:
    def __init__(self, routes):
        self.routes = routes
        self.seen: list[tuple[str, str, dict | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path.removeprefix("/api/v1")
        self.seen.append((request.method, path, body))
        status, payload = self.routes[(request.method, path)]
        return httpx.Response(status, json=payload)


def _server(make_server, config_dir, core, *, environ=None):
    write_profile(config_dir, "all", "hubt_x")
    server, hub = make_server("all", transport=httpx.MockTransport(core.handler))
    hub.environ = environ or {}
    hub.getppid = lambda: 4242
    hub.find_session = lambda start: ME
    return server, hub


async def _call(server, name, **args):
    async with Client(server) as client:
        return await client.call_tool(name, args)


async def test_react_sends_session_and_says_when(config_dir, make_server):
    core = Core({("POST", "/messages/345/reactions"): (200, {"status": "queued", "not_before_ts": WHEN})})
    server, _hub = _server(make_server, config_dir, core)
    result = await _call(server, "hub_react", message_id=345, emoji="👀")
    text = text_of(result)
    assert not result.is_error, text
    assert core.seen == [
        ("POST", "/messages/345/reactions", {"emoji": "👀", "on": True, "session": ME.session()})
    ]
    hhmm = datetime.fromtimestamp(WHEN / 1000).strftime("%H:%M")
    assert f"Реакция 👀 уйдёт не раньше {hhmm}" in text


async def test_react_off_and_already(config_dir, make_server):
    core = Core({("POST", "/messages/345/reactions"): (200, {"status": "already"})})
    server, _hub = _server(make_server, config_dir, core)
    assert "уже стоит" in text_of(await _call(server, "hub_react", message_id=345, emoji="👍"))
    core.routes[("POST", "/messages/345/reactions")] = (200, {"status": "queued", "not_before_ts": WHEN})
    text = text_of(await _call(server, "hub_react", message_id=345, emoji="👀", on=False))
    assert "Снятие реакции 👀" in text
    assert core.seen[-1][2]["on"] is False


async def test_react_project_off_gives_command_for_owner(config_dir, make_server):
    core = Core(
        {
            ("POST", "/messages/345/reactions"): (403, {"detail": "session reactions off"}),
            ("GET", "/messages/345"): (200, {"id": 345, "project_id": 3}),
        }
    )
    server, _hub = _server(make_server, config_dir, core)
    result = await _call(server, "hub_react", message_id=345, emoji="👀")
    assert result.is_error
    assert "hub project set 3 --session-reactions on" in text_of(result)


@pytest.mark.parametrize(
    "status, detail, expected",
    [
        (409, "owner reaction", "у сообщения уже есть реакция владельца"),
        (409, "not own", "только свои реакции"),
        (409, "not google", "адресат не на Gmail"),
        (409, "sent, cannot undo", "после отправки не снять"),
        (409, "too many", "20 реакций"),
        (409, "owner pending", "владелец сейчас сам ставит"),
        (429, "too many reactions", "60 реакций в час"),
        (403, "реакции — только из сессии, подписанной на этот проект", "подписанной на этот проект"),
        (404, "not found", "не найдено"),
    ],
)
async def test_react_refusals_in_russian(config_dir, make_server, status, detail, expected):
    core = Core({("POST", "/messages/345/reactions"): (status, {"detail": detail})})
    server, _hub = _server(make_server, config_dir, core)
    result = await _call(server, "hub_react", message_id=345, emoji="👀")
    assert result.is_error and expected in text_of(result)


async def test_react_work_session_refused_without_request(config_dir, make_server):
    core = Core({})
    server, _hub = _server(make_server, config_dir, core, environ={"HUB_WORK": "1", "HUB_WORK_TASK": "5"})
    result = await _call(server, "hub_react", message_id=345, emoji="👀")
    assert result.is_error and "рабочая сессия хаба" in text_of(result)
    assert core.seen == []


async def test_react_not_for_orchestrator(config_dir, make_server):
    # Ф57-2: профиля дежурной `wake-*` больше нет; оркестратору `hub_react` не положен.
    for profile, expected in (("all", True), ("marketer", True)):
        assert ("hub_react" in set(mcp_server.tool_names(profile))) is expected, profile


async def test_react_session_passes_optional_channel(config_dir, make_server):
    core = Core({("POST", "/messages/345/reactions"): (200, {"status": "queued", "not_before_ts": WHEN})})
    server, _hub = _server(make_server, config_dir, core)
    await _call(server, "hub_react", message_id=345, emoji="👀", channel_id=7)
    assert core.seen[-1][2] == {"emoji": "👀", "on": True, "session": ME.session(), "channel_id": 7}
