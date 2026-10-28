"""Ф14-5 C1–C3, П15, П20: `hub_subscribe(channel_id=…)`, `hub_subscribe(every_message=…)`,
`hub_unsubscribe()` / `hub_unsubscribe(channel_id=…)`, беседы в `hub_subscriptions()`. Ядро — подменный
транспорт; сессия — подменная `find_session`."""

import json

import httpx
from mcp import Client

from hub import session_registry as reg

from .conftest import text_of, write_profile

ME = reg.SessionInfo(100, 5000, "sess-1", "/work/iip", "МАРКЕТОЛОГ", "user", "interactive")
SUB = {
    "id": 17,
    "project_id": 3,
    "session_name": "МАРКЕТОЛОГ",
    "session_pid": 100,
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
}
CHAN = {**{k: v for k, v in SUB.items() if k != "mode"}, "id": 4, "channel_id": 152}


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


def _server(make_server, config_dir, core):
    write_profile(config_dir, "all", "hubt_x")
    server, hub = make_server("all", transport=httpx.MockTransport(core.handler))
    hub.hub_path = "/opt/hub-prod/.venv/bin/hub"
    hub.environ = {}
    hub.getppid = lambda: 4242
    hub.find_session = lambda start: ME
    return server, hub


async def _call(server, name, **args):
    async with Client(server) as client:
        return await client.call_tool(name, args)


async def test_subscribe_channel_body_and_text(config_dir, make_server):
    core = Core({("POST", "/chan-subs"): (200, {"sub": CHAN, "created": True, "count": 2})})
    server, _hub = _server(make_server, config_dir, core)
    result = await _call(server, "hub_subscribe", channel_id=152)
    text = text_of(result)
    assert not result.is_error, text
    assert core.seen == [("POST", "/chan-subs", {"channel_id": 152, "session": ME.session_in()})]
    assert "Подписка c4 на беседу #152 (новая). Слушают беседы: 2 из 500." in text
    assert "run_in_background" in text
    assert "\n/opt/hub-prod/.venv/bin/hub sub wait --channels --profile all\n" in text
    assert "СНАЧАЛА снова запусти" in text and "hub_read(channel_id=…)" in text
    assert "hub_subscribe(channel_id=…) заново" in text
    assert "уже идёт" not in text


async def test_subscribe_channel_waiting_says_do_not_start_p15(config_dir, make_server):
    core = Core(
        {("POST", "/chan-subs"): (200, {"sub": {**CHAN, "waiting": True}, "created": True, "count": 3})}
    )
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_subscribe", channel_id=152))
    assert "уже идёт — новую команду НЕ запускай" in text
    assert "Запусти ожидание" not in text


async def test_subscribe_both_or_bad_combo_refused_without_request(config_dir, make_server):
    core = Core({})
    server, _hub = _server(make_server, config_dir, core)
    result = await _call(server, "hub_subscribe", project_id=3, channel_id=152)
    assert result.is_error and "ИЛИ" in text_of(result)
    result = await _call(server, "hub_subscribe", channel_id=152, every_message=True)
    assert result.is_error and "every_message" in text_of(result)
    assert core.seen == []


async def test_subscribe_channel_refusal_shows_core_reason(config_dir, make_server):
    # Ф39 A2: отказа «беседу уже слушает сессия» больше нет — 409 остался у предела бесед на сессию.
    limit = "сессия уже слушает 20 бесед — сначала hub_unsubscribe(channel_id=…)"
    core = Core({("POST", "/chan-subs"): (409, {"detail": limit})})
    server, _hub = _server(make_server, config_dir, core)
    result = await _call(server, "hub_subscribe", channel_id=152)
    assert result.is_error


async def test_subscribe_every_message_sends_mode_p20(config_dir, make_server):
    core = Core(
        {("POST", "/subs"): (200, {"sub": {**SUB, "mode": "messages"}, "created": True, "backfilled": 0})}
    )
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_subscribe", every_message=True))
    assert core.seen[0][2] == {"project_id": None, "session": ME.session_in(), "mode": "messages"}
    assert "каждое новое чужое сообщение" in text
    assert "sub wait 17 --profile all" in text
    await _call(server, "hub_subscribe", every_message=False)
    assert core.seen[1][2]["mode"] == "cards"
    await _call(server, "hub_subscribe")
    assert "mode" not in core.seen[2][2]


async def test_unsubscribe_all_ends_projects_and_channels(config_dir, make_server):
    other = {**CHAN, "id": 5, "channel_id": 153, "session_pid": 555}
    core = Core(
        {
            ("GET", "/subs"): (200, {"items": [SUB], "channels": [CHAN, other]}),
            ("POST", "/subs/17/end"): (200, {"sub": {**SUB, "ended_ts": 5}}),
            ("POST", "/chan-subs/end"): (200, {"items": [{**CHAN, "ended_ts": 5}]}),
        }
    )
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_unsubscribe"))
    assert "подписка #17 (проект 3): снята" in text
    assert "подписка c4 на беседу #152: снята" in text and "#153" not in text
    assert core.seen[2] == ("POST", "/chan-subs/end", {"session": ME.session(), "reason": "unsubscribed"})


async def test_unsubscribe_only_channels(config_dir, make_server):
    core = Core(
        {
            ("GET", "/subs"): (200, {"items": [], "channels": [CHAN]}),
            ("POST", "/chan-subs/end"): (200, {"items": [{**CHAN, "ended_ts": 5}]}),
        }
    )
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_unsubscribe"))
    assert "подписка c4 на беседу #152: снята" in text


async def test_unsubscribe_one_channel(config_dir, make_server):
    core = Core({("POST", "/chan-subs/end"): (200, {"items": [{**CHAN, "ended_ts": 5}]})})
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_unsubscribe", channel_id=152))
    assert core.seen == [
        ("POST", "/chan-subs/end", {"session": ME.session(), "reason": "unsubscribed", "channel_id": 152})
    ]
    assert "подписка c4 на беседу #152: снята" in text


async def test_subscriptions_lists_channels(config_dir, make_server):
    unsorted = {**CHAN, "id": 6, "channel_id": 160, "project_id": None, "session_pid": 555, "listening": True}
    core = Core(
        {("GET", "/subs"): (200, {"items": [{**SUB, "mode": "messages"}], "channels": [CHAN, unsorted]})}
    )
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_subscriptions"))
    lines = text.splitlines()
    assert "Живые подписки сессий на беседы: 2." in text
    assert "каждое сообщение" in next(line for line in lines if "подписка #17" in line)
    mine = next(line for line in lines if "беседа #152" in line)
    theirs = next(line for line in lines if "беседа #160" in line)
    assert "подписка c4 · проект 3" in mine and "это ты" in mine and "НЕ слушает" in mine
    assert "Неразобранное" in theirs and "другая сессия" in theirs


def test_f35_chan_sub_limit_matches_core():
    """Ф35-0: предел в текстах MCP — дубль константы ядра (ядро в процесс hub-mcp не импортируется)."""
    from hub.core import chan_subs
    from hub.mcp_server import server

    assert server.MAX_CHAN_SUBS == chan_subs.MAX_CHAN_SUBS == 500


# --- Ф39 A6: беседу слушают и другие сессии ---------------------------------------------------------------


async def test_f39_subscribe_channel_names_other_listeners(config_dir, make_server):
    others = [
        {"id": 33, "role": "маркетолог", "role_no": 1, "session_name": "М", "keys": 3, "exclude": 0},
        {"id": 35, "role": "маркетолог", "role_no": 2, "session_name": None, "keys": 0, "exclude": 0},
        {"id": 41, "role": None, "role_no": None, "session_name": None, "keys": 0, "exclude": 0},
        {"id": True, "role": "мусор"},
        "мусор",
    ]
    payload = {"sub": CHAN, "created": True, "count": 1, "others": others}
    core = Core({("POST", "/chan-subs"): (200, payload)})
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_subscribe", channel_id=152))
    assert (
        "Беседу слушают ещё: «маркетолог» (подписка c33), «маркетолог 2» (подписка c35), сессия без роли "
        "(подписка c41). У каждой подписки свои ключи, сигнал получит каждая — перед ответом сверь hub_read "
        "и hub_drafts." in text
    )
    assert "мусор" not in text
    # соседей нет (и старое ядро без поля) — строки нет
    for extra in ({"others": []}, {}):
        core = Core({("POST", "/chan-subs"): (200, {"sub": CHAN, "created": True, "count": 1, **extra})})
        server, _hub = _server(make_server, config_dir, core)
        assert "слушают ещё" not in text_of(await _call(server, "hub_subscribe", channel_id=152))


async def test_f39_unsubscribe_channel_without_own_sub_404(config_dir, make_server):
    core = Core({("POST", "/chan-subs/end"): (404, {"detail": "not found"})})
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_unsubscribe", channel_id=152))
    assert "у этой сессии нет подписки на беседу #152" in text
    assert "не снята" not in text and "отказал" not in text


async def test_f39_description_says_subs_are_independent(config_dir, make_server):
    server, _hub = _server(make_server, config_dir, Core({}))
    async with Client(server) as client:
        tools = {tool.name: tool.description for tool in (await client.list_tools()).tools}
    assert "беседу могут слушать несколько сессий сразу, подписки независимы" in tools["hub_subscribe"]
