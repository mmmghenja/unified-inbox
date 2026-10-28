"""Ф37-3 (docs/architecture.md: MCP `hub_handled` — тексты, `session`
через `_me()`, OpenCode; шапка `hub_read` с непрочитанным у владельца. Плюс правки по Ф37-1/Ф37-2: инструкции
сервера про беседы «без подтверждения» и один запрос открытых карточек на `hub_read`."""

import json
import os

import httpx
import pytest
from mcp import Client

from hub import hostid
from hub import session_registry as reg
from hub.core import handled, subs
from hub.mcp_server import server as mcp_server

from .conftest import (
    insert_account,
    insert_channel,
    insert_message,
    insert_project,
    make_token,
    text_of,
    write_profile,
)
from .test_mcp_server import _blocks

# Ф33-1: тело `session` от найденной сессии несёт машину (`hub.hostid`).
HOST = {"host": hostid.host_id({})} if hostid.host_id({}) else {}
HOST |= {"boot": hostid.boot_id({})} if hostid.boot_id({}) else {}  # Ф33-1: загрузка машины


ROLE = "маркетолог"
NOTE = "автоответ об отпуске, ответа не требует"
OC_A = "ses_AAAAAAAAAA1"


async def _call(server, name, **args):
    async with Client(server) as client:
        return await client.call_tool(name, args)


@pytest.fixture
def stand(db, clock, config_dir, make_server, tmp_path, monkeypatch):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    me = reg.SessionInfo(4242, 777_000, "sess-aaaaaaaa-1111", str(workdir), None, None, "interactive")
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: {me.pid: me.proc_start}.get(pid))
    project = insert_project(db, name="Umbrella", workdir=str(workdir))
    db.write_sync(lambda conn: subs.set_project_subs(conn, project, True, now_ms=0))
    account = insert_account(db, source="telegram")
    channel = insert_channel(db, account, project_id=project, ext_id="dm1", kind="dm", title="Партнёр")
    now = int(clock() * 1000)
    insert_message(db, channel, ext_id="100", ts=now - 120_000)
    last = insert_message(db, channel, ext_id="101", ts=now - 60_000)
    token, token_id = make_token(db, scopes=("read", "draft"), name="mcp:all")
    write_profile(config_dir, "all", token, token_id)
    server, hub = make_server("all")
    hub.environ = {}
    hub.getppid = lambda: 1
    hub.find_session = lambda start: me
    return {"server": server, "hub": hub, "db": db, "project": project, "channel": channel, "last": last}


def _rows(db) -> list[tuple]:
    with db.reader() as conn:
        return conn.execute("SELECT channel_id, message_id, role, note FROM main.channel_handled").fetchall()


async def test_handled_refusals_then_success_real_core(stand):
    server, db, ch, last = stand["server"], stand["db"], stand["channel"], stand["last"]

    # Нет подписки с ролью.
    result = await _call(server, "hub_handled", channel_id=ch, up_to=last, note=NOTE)
    assert result.is_error and "живая подписка сессии на проект беседы с ролью" in text_of(result)
    assert not (await _call(server, "hub_subscribe", role=ROLE)).is_error

    # Выключено в проекте — с командой для владельца; шапка hub_read подсказки не даёт.
    result = await _call(server, "hub_handled", channel_id=ch, up_to=last, note=NOTE)
    text = text_of(result)
    assert result.is_error and f"hub project set {stand['project']} --session-handled on" in text
    assert "hub_handled" not in text_of(await _call(server, "hub_read", channel_id=ch))
    assert _rows(db) == []

    db.write_sync(lambda conn: handled.set_project_enabled(conn, stand["project"], True))
    read = text_of(await _call(server, "hub_read", channel_id=ch))
    line = f'У владельца непрочитано 2. Разобрал — hub_handled(channel_id={ch}, up_to=<id>, note="…").'
    assert line in read
    assert line not in "\n".join("\n".join(block[1]) for block in _blocks(read))  # служебная строка

    bad = await _call(server, "hub_handled", channel_id=ch, up_to=last, note="ок")
    assert bad.is_error and "note" in text_of(bad)

    result = await _call(server, "hub_handled", channel_id=ch, up_to=last, note=NOTE)
    text = text_of(result)
    assert not result.is_error, text
    assert text == (
        f"Беседа #{ch} отмечена разобранной до сообщения #{last}. "
        f"Пометка: {NOTE}"
    )
    assert _rows(db) == [(ch, last, ROLE, NOTE)]
    # Непрочитанного больше нет — строки в шапке тоже.
    assert "У владельца непрочитано" not in text_of(await _call(server, "hub_read", channel_id=ch))

    missing = await _call(server, "hub_handled", channel_id=999_999, up_to=last, note=NOTE)
    assert missing.is_error and "не найдена или вне проектов профиля" in text_of(missing)


async def test_f66_handled_batch_real_core(stand):
    server, db, ch, last = stand["server"], stand["db"], stand["channel"], stand["last"]
    assert not (await _call(server, "hub_subscribe", role=ROLE)).is_error
    db.write_sync(lambda conn: handled.set_project_enabled(conn, stand["project"], True))

    result = await _call(
        server,
        "hub_handled",
        items=[
            {"channel_id": ch, "up_to": last, "note": NOTE},
            {"channel_id": 999_999, "up_to": last, "note": NOTE},
        ],
    )
    assert not result.is_error, text_of(result)
    lines = text_of(result).splitlines()
    assert lines[0] == f"#{ch} — отмечено до сообщения #{last}"
    assert lines[1].startswith("#999999 — отказ: ")
    assert _rows(db) == [(ch, last, ROLE, NOTE)]

    empty = await _call(server, "hub_handled", items=[])
    assert empty.is_error
    nothing = await _call(server, "hub_handled")
    assert nothing.is_error and "items" in text_of(nothing)


async def test_handled_tool_description_and_profiles(stand, db, config_dir, make_server):
    async with Client(stand["server"]) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    tool = tools["hub_handled"]
    assert "НЕ отмечай" in tool.description and "Пример: hub_handled(" in tool.description
    # Ф66-3 D: одиночные поля необязательны — вместо них можно передать `items`.
    assert not tool.input_schema.get("required")
    assert "items" in tool.input_schema["properties"] and "пачкой" in tool.description.lower()
    for key in ("oc_session", "oc_dir"):
        assert "заполняет плагин хаба" in tool.input_schema["properties"][key]["description"]
    db.write_sync(lambda conn: handled.set_project_enabled(conn, stand["project"], True))
    stand["hub"].handled_tool = False
    assert "hub_handled" not in text_of(await _call(stand["server"], "hub_read", channel_id=stand["channel"]))


class Core:
    def __init__(self, routes):
        self.routes = routes
        self.seen: list[tuple[str, str, dict, dict | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path.removeprefix("/api/v1")
        self.seen.append((request.method, path, dict(request.url.params), body))
        status, payload = self.routes[(request.method, path)]
        return httpx.Response(status, json=payload)


HANDLED_OK = {"id": 1, "channel_id": 40, "message_id": 345, "role": ROLE, "note": NOTE, "changed": True}


def _mock(make_server, config_dir, routes, *, environ=None):
    core = Core(routes)
    write_profile(config_dir, "all", "hubt_x")
    server, hub = make_server("all", transport=httpx.MockTransport(core.handler))
    hub.environ = {} if environ is None else environ
    hub.getppid = lambda: 1

    def gone(_start):
        raise reg.RegistryError(reg.GONE, "процесса сессии Claude Code среди предков нет")

    hub.find_session = gone
    return server, hub, core


async def test_handled_session_body_limit_text_and_opencode(config_dir, make_server, tmp_path):
    routes = {("POST", "/channels/40/handled"): (200, HANDLED_OK)}
    # Сессия не определилась — запрос уходит без `session` (откажет ядро словами).
    server, _hub, core = _mock(make_server, config_dir, routes)
    assert not (await _call(server, "hub_handled", channel_id=40, up_to=345, note=NOTE)).is_error
    assert core.seen[0][3] == {"up_to_message_id": 345, "note": NOTE}

    # OpenCode: сессия — из служебных полей плагина.
    env = {"OPENCODE_PID": str(os.getpid()), "OPENCODE": "1"}
    server, hub, core = _mock(make_server, config_dir, routes, environ=env)
    hub.getppid = os.getpid
    result = await _call(
        server, "hub_handled", channel_id=40, up_to=345, note=NOTE, oc_session=OC_A, oc_dir=str(tmp_path)
    )
    assert not result.is_error, text_of(result)
    assert core.seen[0][3]["session"] == {
        "pid": os.getpid(),
        "proc_start": subs.read_proc_start(os.getpid()),
        "session_id": OC_A,
        **HOST,
    }

    # Лимит — словами ядра.
    limit = {("POST", "/channels/40/handled"): (429, {"detail": "лимит отметок «разобрано» — 60 в час"})}
    server, _hub, _core = _mock(make_server, config_dir, limit)
    result = await _call(server, "hub_handled", channel_id=40, up_to=345, note=NOTE)
    assert result.is_error and "60 в час" in text_of(result)


async def test_read_makes_one_cards_request(config_dir, make_server):
    """Ф37-2 B2 (правка): открытые карточки беседы — одним запросом `status=open`, не двумя."""
    channel = {"id": 40, "project_id": 3, "source": "telegram", "kind": "dm", "unread": 0, "title": "x"}
    routes = {
        ("GET", "/channels/40"): (200, channel),
        ("GET", "/channels/40/messages"): (200, {"messages": [], "next_before": None}),
        ("GET", "/agent/items"): (200, [{"id": 7, "label": "urgent", "status": "seen"}]),
    }
    server, _hub, core = _mock(make_server, config_dir, routes)
    text = text_of(await _call(server, "hub_read", channel_id=40))
    assert "Открытые карточки дежурного: #7 (срочно)." in text
    cards = [seen for seen in core.seen if seen[1] == "/agent/items"]
    assert [seen[2] for seen in cards] == [{"status": "open", "channel": "40", "limit": "20"}]
    assert len(core.seen) == 3  # беседа, карточки, сообщения — подсказка `hub_handled` запросов не добавляет


def test_instructions_mention_autosend_exception():
    text = mcp_server.INSTRUCTIONS
    assert "ждёт подтверждения владельца в хабе, отправляет только он" in text
    assert "«без подтверждения» для роли сессии" in text and "уходит сразу" in text
