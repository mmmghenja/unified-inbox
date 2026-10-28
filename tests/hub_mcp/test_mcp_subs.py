"""Ф13-1 D + C: `hub_subscribe`/`hub_unsubscribe`/`hub_subscriptions`. Ядро — подменный транспорт по контракту
B5 (реального `/subs` в этой ветке нет); сессия — подменная `find_session` (поддельный реестр)."""

import json

import httpx
from mcp import Client

from hub import session_registry as reg
from hub.mcp_server import server as mcp_server

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
    "pending": 2,
}


class Core:
    def __init__(self, routes):
        self.routes = routes  # (метод, путь) -> (статус, тело)
        self.seen: list[tuple[str, str, dict | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path.removeprefix("/api/v1")
        self.seen.append((request.method, path, body))
        status, payload = self.routes[(request.method, path)]
        return httpx.Response(status, json=payload)


def _server(make_server, config_dir, core, *, profile="all", environ=None, session=ME, pid=4242):
    write_profile(config_dir, profile, "hubt_x")
    server, hub = make_server(profile, transport=httpx.MockTransport(core.handler))
    hub.hub_path = "/opt/hub-prod/.venv/bin/hub"
    hub.environ = environ or {}
    hub.getppid = lambda: pid

    def find(start):
        assert start == pid  # от родителя процесса сервера
        if isinstance(session, BaseException):
            raise session
        return session

    hub.find_session = find
    return server, hub


async def _call(server, name, **args):
    async with Client(server) as client:
        return await client.call_tool(name, args)


async def test_subscribe_sends_session_from_registry_and_returns_wait_command(config_dir, make_server):
    core = Core({("POST", "/subs"): (200, {"sub": SUB, "created": True, "backfilled": 2})})
    server, _hub = _server(make_server, config_dir, core)
    result = await _call(server, "hub_subscribe")
    text = text_of(result)
    assert not result.is_error, text
    assert core.seen == [("POST", "/subs", {"project_id": None, "session": ME.session_in()})]
    assert "Подписка #17 на проект 3 (новая)" in text
    assert "Карточек уже ждёт: 2" in text
    assert "не закрытой за" not in text
    assert "run_in_background" in text
    assert "\n/opt/hub-prod/.venv/bin/hub sub wait 17 --profile all\n" in text
    assert "СНАЧАЛА снова запусти" in text and 'hub_triage_mark(item_ids=[…], status="done")' in text
    assert "hub_subscribe() заново" in text
    assert mcp_server.SUB_RULES_TEXT in text  # подписанная сессия узнаёт, где правила


async def test_subscribe_passes_project_and_non_default_url(config_dir, make_server):
    core = Core({("POST", "/subs"): (200, {"sub": SUB, "created": False, "backfilled": 0})})
    server, hub = _server(make_server, config_dir, core)
    hub.base_url = "http://127.0.0.1:9999/api/v1"
    text = text_of(await _call(server, "hub_subscribe", project_id=3))
    assert core.seen[0][2]["project_id"] == 3
    assert "уже была у этой сессии" in text
    assert "sub wait 17 --profile all --url http://127.0.0.1:9999/api/v1" in text


async def test_subscribe_refusals_show_core_reason(config_dir, make_server):
    """Тест 7 (сторона клиента): 403 ядра (cwd ≠ workdir, чужой проект, kind, подписки выключены, токен
    hub-wake) и 409 — ошибкой инструмента с текстом ядра."""
    for status, detail in (
        (403, "сессия не в рабочей папке проекта"),
        (403, "подписки в проекте выключены"),
        (409, "проект уже слушает сессия #5"),
    ):
        core = Core({("POST", "/subs"): (status, {"detail": detail})})
        server, _hub = _server(make_server, config_dir, core)
        result = await _call(server, "hub_subscribe")
        assert result.is_error and detail in text_of(result)


async def test_hub_work_session_refused_without_request(config_dir, make_server):
    core = Core({})
    server, _hub = _server(make_server, config_dir, core, environ={"HUB_WORK": "1", "HUB_WORK_TASK": "5"})
    for tool in ("hub_subscribe", "hub_unsubscribe"):
        result = await _call(server, tool)
        assert result.is_error and "рабочая сессия хаба" in text_of(result)
    assert core.seen == []


async def test_registry_error_refused_without_request(config_dir, make_server):
    core = Core({})
    server, _hub = _server(
        make_server, config_dir, core, session=reg.RegistryError(reg.GONE, "процесса сессии нет")
    )
    result = await _call(server, "hub_subscribe")
    assert result.is_error and "не удалось определить сессию" in text_of(result)
    assert core.seen == []


async def test_unsubscribe_ends_only_own_live_subscription(config_dir, make_server):
    other = {**SUB, "id": 18, "project_id": 1, "session_pid": 555, "session_name": None}
    core = Core(
        {
            ("GET", "/subs"): (200, {"items": [SUB, other]}),
            ("POST", "/subs/17/end"): (200, {"sub": {**SUB, "ended_ts": 5}}),
        }
    )
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_unsubscribe"))
    assert "подписка #17 (проект 3): снята" in text and "#18" not in text
    assert core.seen[1] == ("POST", "/subs/17/end", {"session": ME.session(), "reason": "unsubscribed"})
    assert len(core.seen) == 2


async def test_unsubscribe_nothing_and_forbidden(config_dir, make_server):
    core = Core({("GET", "/subs"): (200, {"items": []})})
    server, _hub = _server(make_server, config_dir, core)
    assert "живых подписок нет" in text_of(await _call(server, "hub_unsubscribe"))
    core = Core(
        {
            ("GET", "/subs"): (200, {"items": [SUB]}),
            ("POST", "/subs/17/end"): (403, {"detail": "чужая сессия"}),
        }
    )
    server, _hub = _server(make_server, config_dir, core)
    assert "не снята" in text_of(await _call(server, "hub_unsubscribe"))


async def test_subscriptions_lists_live_with_this_session_mark(config_dir, make_server):
    other = {**SUB, "id": 18, "project_id": 1, "session_pid": 555, "session_name": None, "listening": True}
    dead = {**SUB, "id": 19, "project_id": 2, "session_pid": 556, "live": False}
    core = Core({("GET", "/subs"): (200, {"items": [SUB, other, dead]})})
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_subscriptions"))
    assert core.seen == [("GET", "/subs", None)]
    lines = text.splitlines()
    mine = next(line for line in lines if "подписка #17" in line)
    theirs = next(line for line in lines if "подписка #18" in line)
    assert "это ты" in mine and "НЕ слушает" in mine and "сигналов ждёт 2" in mine and "МАРКЕТОЛОГ" in mine
    assert "другая сессия" in theirs and "без имени" in theirs and " слушает" in theirs
    assert "подписка снимается" in next(line for line in lines if "подписка #19" in line)
    assert "ДАННЫЕ" in text  # имя сессии — внутри блока данных


async def test_sub_tools_not_for_wake_and_orchestrator(config_dir, make_server):
    """Тест 7: профилю `wake-3` и `orchestrator` инструментов подписки нет; прочим — есть."""
    for profile, expected in (("all", True), ("marketer", True)):
        names = set(mcp_server.tool_names(profile))
        for tool in ("hub_subscribe", "hub_unsubscribe", "hub_subscriptions"):
            assert (tool in names) is expected, (profile, tool)
