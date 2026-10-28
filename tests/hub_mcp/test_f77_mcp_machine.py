"""Ф77-1 (docs/architecture.md, C1–C5): машина в текстах MCP — `hub_agents`,
`hub_subscribe`, `hub_agent_state`, `hub_agent_inbox`; клиент шлёт поле `machine` (и не шлёт его старому
ядру после 422). Ядро — подменный транспорт."""

import httpx

from hub import session_registry as reg
from hub.mcp_server import server as mcp_server

from .conftest import text_of, write_profile
from .test_f35_mcp_peer_msgs import MY_SUB, _inbox_core, _row
from .test_mcp_chan_subs import SUB, Core, _call, _server

LAPTOP = "1a" * 8
CORE = "c0" * 8
FAR = reg.SessionInfo(100, 5000, "sess-1", "/work/iip", None, None, "interactive", host=LAPTOP)
MACHINE = {"hostname": "laptop", "route": "s1", "version": "abcdef12"}


def _label(host, display, *, core=False, route=None, name=None) -> dict:
    return {
        "host": host,
        "name": name,
        "hostname": display,
        "display": name or display,
        "route": "core" if core else route,
        "core": core,
        "online": True,
        "last_ts": 1,
        "version": None,
    }


STATION = _label(CORE, "desktop", core=True, name="стационарник")
NOTEBOOK = _label(LAPTOP, "my-laptop", route="s1")


def test_machine_text():
    assert mcp_server.machine_text(STATION) == "стационарник (машина ядра)"
    assert mcp_server.machine_text(NOTEBOOK) == "my-laptop (через relay)"
    assert mcp_server.machine_text({**NOTEBOOK, "route": "s1-socks"}) == "my-laptop (через relay (socks))"
    assert mcp_server.machine_text({**NOTEBOOK, "route": "lan"}) == "my-laptop (локальная сеть)"
    assert mcp_server.machine_text({**NOTEBOOK, "route": None}) == "my-laptop"
    assert mcp_server.machine_text(None) is None  # ядро старше Ф77


async def test_hub_agents_shows_machine(config_dir, make_server):
    rows = [
        {**SUB, "id": 1, "role": "маркетолог", "role_no": 1, "live": True, "machine": STATION},
        {
            **SUB,
            "id": 2,
            "role": "программист",
            "role_no": 1,
            "live": True,
            "mine": True,
            "machine": NOTEBOOK,
        },
        {**SUB, "id": 3, "role": "тестер", "role_no": 1, "live": True},  # старое ядро — без метки
    ]
    core = Core(
        {
            ("GET", "/subs"): (200, {"items": rows, "channels": []}),
            ("GET", "/projects"): (200, [{"id": 3, "name": "ИИП"}]),
            ("GET", "/agents/states"): (409, {"detail": "нет подписки"}),
        }
    )
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_agents"))
    assert "«маркетолог» · подписка #1 · Claude Code · машина: стационарник (машина ядра) ·" in text
    assert "«программист» · подписка #2 · Claude Code · машина: my-laptop (через relay) ·" in text
    assert "это ты" in text
    tester = next(line for line in text.splitlines() if "тестер" in line and "подписка #3" in line)
    assert "машина" not in tester


async def test_hub_agents_description_mentions_machine(config_dir, make_server):
    from mcp import Client

    server, _hub = _server(make_server, config_dir, Core({}))
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert "договаривайтесь по метке машины" in tools["hub_agents"].description


async def test_subscribe_line_and_hint(config_dir, make_server):
    sub = {**SUB, "role": "программист", "role_no": 1}
    answer = {"sub": sub, "created": True, "backfilled": 0, "machine": NOTEBOOK, "core_machine": STATION}
    core = Core({("POST", "/subs"): (200, answer)})
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_subscribe", role="программист"))
    assert (
        "Твоя машина: my-laptop (через relay). Ядро — на стационарник. "
        "Имя машины можно задать в хабе: ⋯ → Машины." in text
    )
    # Своя машина — машина ядра с именем: без «Ядро — на» и без подсказки.
    core.routes[("POST", "/subs")] = (200, {**answer, "machine": STATION})
    text = text_of(await _call(server, "hub_subscribe", role="программист"))
    assert "Твоя машина: стационарник (машина ядра)." in text
    assert "Ядро — на" not in text and "⋯ → Машины" not in text
    # Старое ядро — строки нет.
    core.routes[("POST", "/subs")] = (200, {"sub": sub, "created": True, "backfilled": 0})
    assert "Твоя машина" not in text_of(await _call(server, "hub_subscribe", role="программист"))


async def test_agent_state_shows_machine(config_dir, make_server):
    body = {
        "agent": {
            "project_id": 3,
            "role": "программист",
            "role_no": 1,
            "engine": "claude",
            "held": False,
            "hub_session_id": None,
            "sub_id": 9,
            "machine": NOTEBOOK,
        },
        "state": {"held": False, "listening": True, "silent_min": None, "signals": {"waiting": 0}},
        "events": [],
    }
    core = Core({("GET", "/agents/state"): (200, body)})
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_agent_state", project_id=3, role="программист"))
    assert "машина: my-laptop (через relay)" in text


async def test_inbox_names_sender_machine_only_when_other(config_dir, make_server):
    here = {**MY_SUB, "machine": STATION}
    src_far = {"project_id": 2, "role": "маркетолог", "role_no": 1, "sub_id": 31, "engine": "claude"}
    items = [
        _row(1, **{"from": {**src_far, "machine": NOTEBOOK}}),
        _row(2, **{"from": {**src_far, "sub_id": 32, "machine": STATION}}),
    ]
    core = _inbox_core(items, sub=here)
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_agent_inbox"))
    line1 = next(line for line in text.splitlines() if line.startswith("#1 ·"))
    line2 = next(line for line in text.splitlines() if line.startswith("#2 ·"))
    assert "подписка #31, Claude Code, машина my-laptop)" in line1
    assert "машина" not in line2


# --- клиент: поле `machine` ---------------------------------------------------------------------------------


class _Core:
    """Ядро старше Ф77: незнакомое поле `machine` — 422."""

    def __init__(self, old: bool):
        self.old = old
        self.seen: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        self.seen.append(body)
        if self.old and "machine" in body:
            return httpx.Response(422, json={"detail": "invalid request"})
        sub = {**SUB, "role": None, "role_no": 1}
        return httpx.Response(200, json={"sub": sub, "created": True, "backfilled": 0})


def _far_server(make_server, config_dir, core):
    write_profile(config_dir, "all", "hubt_x")
    server, hub = make_server("all", transport=httpx.MockTransport(core.handler))
    hub.environ = {}
    hub.getppid = lambda: 4242
    hub.find_session = lambda start: FAR
    hub.local_machine = lambda: MACHINE
    return server, hub


async def test_subscribe_sends_machine(config_dir, make_server):
    core = _Core(old=False)
    server, _hub = _far_server(make_server, config_dir, core)
    result = await _call(server, "hub_subscribe")
    assert not result.is_error, text_of(result)
    assert core.seen[0]["machine"] == MACHINE and core.seen[0]["session"]["host"] == LAPTOP


async def test_old_core_422_falls_back_without_machine(config_dir, make_server):
    core = _Core(old=True)
    server, hub = _far_server(make_server, config_dir, core)
    result = await _call(server, "hub_subscribe")
    assert not result.is_error, text_of(result)
    assert ["machine" in b for b in core.seen] == [True, False]
    assert hub.machine_off is True
    await _call(server, "hub_subscribe")
    assert "machine" not in core.seen[-1]  # больше не шлёт


class _DomainCore(_Core):
    """Новое ядро: доменный 422 (беседа ещё заготовка) на первый запрос, дальше — успех."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        import json

        if not self.seen:
            self.seen.append(json.loads(request.content))
            return httpx.Response(422, json={"detail": "беседа в архиве или ещё заготовка"})
        return super().handler(request)


async def test_domain_422_keeps_machine(config_dir, make_server):
    """Ревью Ф77-1 п.1: доменный 422 нового ядра — не повод слать без машины и выключать её насовсем."""
    core = _DomainCore(old=False)
    server, hub = _far_server(make_server, config_dir, core)
    result = await _call(server, "hub_subscribe")
    assert result.is_error and len(core.seen) == 1
    assert hub.machine_off is False
    await _call(server, "hub_subscribe")
    assert core.seen[-1]["machine"] == MACHINE


async def test_no_machine_without_host(config_dir, make_server):
    core = _Core(old=False)
    server, hub = _far_server(make_server, config_dir, core)
    hub.find_session = lambda start: reg.SessionInfo(100, 5000, "s", "/w", None, None, "interactive")
    await _call(server, "hub_subscribe")
    assert "machine" not in core.seen[0]
