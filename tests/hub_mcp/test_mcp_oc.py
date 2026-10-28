"""Ф16-4 (docs/architecture.md): MCP хаба в сессии OpenCode. Сессию называет
плагин хаба (`oc_session`, `oc_dir` в аргументах инструмента); `OPENCODE_PID` — в окружении сервера. Процесс
OpenCode в тестах — сам процесс pytest (`getppid` сервера подменён на `os.getpid()`, `find_opencode_session`
настоящий, по настоящему `/proc`).

Часть тестов — с подменным ядром (тело запроса и текст ответа), часть — с настоящим ядром в процессе: две
сессии `ses_…` одного процесса, `hub_unsubscribe` в A не трогает B, «это ты» верно.
"""

import json
import os

import httpx
from mcp import Client

from hub import hostid
from hub.core import engine as engine_mod
from hub.core import subs as subs_core
from hub.mcp_server import server as mcp_server

from .conftest import insert_account, insert_channel, insert_project, make_token, text_of, write_profile

# Ф33-1: тело `session` от найденной сессии несёт машину (`hub.hostid`).
HOST = {"host": hostid.host_id({})} if hostid.host_id({}) else {}
HOST |= {"boot": hostid.boot_id({})} if hostid.boot_id({}) else {}  # Ф33-1: загрузка машины


OC_A, OC_B = "ses_AAAAAAAAAA1", "ses_BBBBBBBBBB2"
HUB = "/opt/hub-prod/.venv/bin/hub"
SUB = {
    "id": 17,
    "project_id": 3,
    "session_name": None,
    "session_pid": os.getpid(),
    "created_ts": 1,
    "last_wait_ts": None,
    "last_delivery_ts": None,
    "live": True,
    "listening": False,
    "waiting": False,
    "ended_ts": None,
    "end_reason": None,
    "pending": 0,
}


class Core:
    """Подменное ядро: `(метод, путь) -> (статус, тело)`; помнит запросы с query."""

    def __init__(self, routes):
        self.routes = routes
        self.seen: list[tuple[str, str, dict, dict | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path.removeprefix("/api/v1")
        self.seen.append((request.method, path, dict(request.url.params), body))
        status, payload = self.routes[(request.method, path)]
        return httpx.Response(status, json=payload)


def _oc_env() -> dict:
    return {"OPENCODE_PID": str(os.getpid()), "OPENCODE": "1"}


def _server(make_server, config_dir, core, *, environ=None):
    write_profile(config_dir, "all", "hubt_x")
    server, hub = make_server("all", transport=httpx.MockTransport(core.handler))
    hub.hub_path = HUB
    hub.environ = _oc_env() if environ is None else environ
    hub.getppid = os.getpid

    def no_claude(_start):
        raise AssertionError("реестр Claude Code в сессии OpenCode не читается")

    hub.find_session = no_claude
    return server, hub


async def _call(server, name, **args):
    async with Client(server) as client:
        return await client.call_tool(name, args)


def _me(sid, oc_dir):
    start = subs_core.read_proc_start(os.getpid())
    return {"pid": os.getpid(), "proc_start": start, "session_id": sid, **HOST}


def _wait_mark(text: str) -> dict:
    last = text.strip().splitlines()[-1]
    assert last.startswith(mcp_server.OC_WAIT_MARK), last
    return json.loads(last[len(mcp_server.OC_WAIT_MARK) :])


# --- подменное ядро -----------------------------------------------------------------------------------------


async def test_subscribe_without_plugin_explains_and_sends_nothing(config_dir, make_server):
    core = Core({})
    server, _hub = _server(make_server, config_dir, core)
    result = await _call(server, "hub_subscribe")
    assert result.is_error
    text = text_of(result)
    assert "сессию OpenCode" in text and "плагин хаба" in text and "opencode-hub-install.sh" in text
    assert core.seen == []


async def test_subscribe_sends_kind_opencode_and_returns_wait_mark(config_dir, make_server, tmp_path):
    core = Core({("POST", "/subs"): (200, {"sub": SUB, "created": True, "backfilled": 1})})
    server, _hub = _server(make_server, config_dir, core)
    result = await _call(server, "hub_subscribe", oc_session=OC_A, oc_dir=str(tmp_path))
    text = text_of(result)
    assert not result.is_error, text
    [(method, path, _params, body)] = core.seen
    assert (method, path) == ("POST", "/subs")
    assert set(body.pop("machine")) == {"hostname", "route", "version"}  # Ф77-1 (B3): плагин — тот же путь
    assert body == {
        "project_id": None,
        "session": {
            **_me(OC_A, tmp_path),
            "cwd": str(tmp_path),
            "name": None,
            "name_source": None,
            "kind": "opencode",
        },
    }
    assert "Подписка #17 на проект 3 (новая)" in text
    assert mcp_server.OC_WAIT_TEXT in text
    assert mcp_server.SUB_RULES_TEXT in text
    assert "run_in_background" not in text and "Bash" not in text
    assert _wait_mark(text) == {"argv": [HUB, "sub", "wait", "17", "--profile", "all"]}


async def test_channel_subscribe_wait_mark_channels_and_url(config_dir, make_server, tmp_path):
    sub = {**SUB, "id": 5, "channel_id": 152, "waiting": True}
    core = Core({("POST", "/chan-subs"): (200, {"sub": sub, "created": True, "count": 1})})
    server, hub = _server(make_server, config_dir, core)
    hub.base_url = "http://127.0.0.1:9999/api/v1"
    text = text_of(
        await _call(server, "hub_subscribe", channel_id=152, oc_session=OC_A, oc_dir=str(tmp_path))
    )
    assert core.seen[0][3]["session"]["kind"] == "opencode"
    assert "новую команду НЕ запускай" not in text  # ожидание держит плагин, а не сессия
    assert _wait_mark(text) == {
        "argv": [
            HUB,
            "sub",
            "wait",
            "--channels",
            "--profile",
            "all",
            "--url",
            "http://127.0.0.1:9999/api/v1",
        ]
    }


async def test_unsubscribe_and_subscriptions_by_mine(config_dir, make_server, tmp_path):
    mine = {**SUB, "id": 17, "mine": True}
    other = {**SUB, "id": 18, "project_id": 4, "mine": False}
    chan_other = {**SUB, "id": 9, "channel_id": 152, "project_id": 4, "mine": False}
    core = Core(
        {
            ("GET", "/subs"): (200, {"items": [mine, other], "channels": [chan_other]}),
            ("POST", "/subs/17/end"): (200, {"sub": {**mine, "ended_ts": 2}}),
        }
    )
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_subscriptions", oc_session=OC_A, oc_dir=str(tmp_path)))
    # Ф33-1: в GET машины и загрузки нет
    triple = {k: str(v) for k, v in _me(OC_A, tmp_path).items() if k not in ("host", "boot")}
    assert core.seen[0][2] == {"live": "1", **triple}
    lines = text.splitlines()
    assert any("подписка #17" in ln and "это ты" in ln for ln in lines)
    assert any("подписка #18" in ln and "другая сессия" in ln for ln in lines)
    assert any("подписка c9" in ln and "другая сессия" in ln for ln in lines)

    core.seen.clear()
    text = text_of(await _call(server, "hub_unsubscribe", oc_session=OC_A, oc_dir=str(tmp_path)))
    posts = [(m, p, b) for m, p, _q, b in core.seen if m == "POST"]
    assert posts == [("POST", "/subs/17/end", {"session": _me(OC_A, tmp_path), "reason": "unsubscribed"})]
    assert "подписка #17 (проект 3): снята" in text and "плагина" in text


async def test_react_sends_oc_session(config_dir, make_server, tmp_path):
    core = Core({("POST", "/messages/345/reactions"): (200, {"status": "queued", "not_before_ts": 0})})
    server, _hub = _server(make_server, config_dir, core)
    result = await _call(
        server, "hub_react", message_id=345, emoji="👀", oc_session=OC_A, oc_dir=str(tmp_path)
    )
    assert not result.is_error, text_of(result)
    assert core.seen[0][3] == {"emoji": "👀", "on": True, "session": _me(OC_A, tmp_path)}


async def test_propose_rule_sends_oc_session(config_dir, make_server, tmp_path):
    # Ф38-3 A1: без сессии в теле проситель в карточке «Завести правило?» пуст.
    core = Core(
        {
            ("GET", "/rule-requests"): (200, []),
            ("POST", "/rule-requests"): (200, {"id": 7, "status": "pending", "project_id": 3}),
        }
    )
    server, _hub = _server(make_server, config_dir, core)
    result = await _call(
        server,
        "hub_propose_rule",
        project_id=3,
        account_id=6,
        kind="domain",
        value="example.com",
        oc_session=OC_A,
        oc_dir=str(tmp_path),
    )
    assert not result.is_error, text_of(result)
    posted = next(seen for seen in core.seen if seen[0] == "POST")
    assert posted[3]["session"] == _me(OC_A, tmp_path)


async def test_oc_params_described_as_service(config_dir, make_server):
    server, _hub = _server(make_server, config_dir, Core({}))
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    # Ф34-2: напоминания ставит и ведёт подписанная сессия — три их инструмента тоже опознают сессию.
    session_tools = (
        "hub_subscribe",
        "hub_unsubscribe",
        "hub_subscriptions",
        "hub_react",
        "hub_join",
        "hub_remind",
        "hub_reminders",
        "hub_remind_done",
        "hub_agents",  # Ф35-1b
        "hub_agent_send",  # Ф35-2b
        "hub_agent_inbox",
        "hub_ask_owner",  # Ф56-1б: вопрос владельцу — сессия с подпиской-ролью
        "hub_draft",  # Ф37-1 B1: сессия черновика — для отправки без подтверждения
        "hub_draft_new",  # Ф39 B2: по сессии ядро записывает автора черновика
        "hub_handled",  # Ф37-3 D1: отметку «разобрано» ставит подписанная сессия с ролью
        "hub_mute",  # Ф99-2: заглушку ставит и снимает подписанная сессия с ролью
        "hub_propose_rule",  # Ф38-3 A1: заявку на правило раскладки ядро подписывает ролью сессии
        "hub_role",  # Ф41-8: правила роли подписки этой сессии
        "hub_request_session",  # Ф41-5б: напарника просит сессия с подпиской на проект с ролью
        "hub_press",  # Ф44-1c: нажатие кнопки бота — ядро сверяет роль сессии
        "hub_presses",  # Ф44-1c: свои нажатия — ответ бота только автору
        "hub_agent_state",  # Ф50-4: состояние роли — только сессии с подпиской на её проект
        "hub_human",  # Ф54-1: заявка «нужен человек» — сессии с подпиской на проект с ролью
        "hub_humans",
        "hub_human_cancel",
        "hub_triage_mark",  # 12.10: закрытие карточки сессией с ролью снимает беседу с «Нужна реакция»
        "hub_inbound_rules",  # Ф63-1: правила входящих своего проекта — сессия с подпиской-ролью
        "hub_group_create",  # Ф62-6: исход заявки по группе WhatsApp — сигналом сессии-автору
        "hub_group_members",
        "hub_group_link",
        "hub_group_ops",  # Ф62-6: свои заявки по группам
        "hub_digest",  # Ф86-1: сводка дня — проект подписки этой сессии
    )
    for name in session_tools:
        props = tools[name].input_schema["properties"]
        for key in ("oc_session", "oc_dir"):
            assert "заполняет плагин хаба" in props[key]["description"], (name, key)
    # Только эти инструменты опознают сессию — у прочих служебных полей нет.
    for name, tool in tools.items():
        if name not in session_tools:
            assert "oc_session" not in tool.input_schema.get("properties", {}), name


# --- настоящее ядро: две сессии одного процесса ------------------------------------------------------------


async def test_two_oc_sessions_real_core(db, config_dir, make_server, tmp_path):
    first_dir, second_dir = tmp_path / "p1", tmp_path / "p2"
    first_dir.mkdir()
    second_dir.mkdir()
    first = insert_project(db, name="Umbrella", workdir=str(first_dir))
    second = insert_project(db, name="Initech", workdir=str(second_dir))
    for project_id in (first, second):
        db.write_sync(lambda conn, pid=project_id: subs_core.set_project_subs(conn, pid, True, now_ms=0))
    acc = insert_account(db, source="mail")
    chan = insert_channel(db, acc, project_id=second, ext_id="t1", kind="mail_thread")
    db.write_sync(lambda conn: engine_mod._meta_set(conn, engine_mod.META_ENGINE, "opencode"))  # noqa: SLF001
    token, _token_id = make_token(db, scopes=("read", "draft"), name="mcp:all")
    write_profile(config_dir, "all", token)
    server, hub = make_server("all")
    hub.hub_path = HUB
    hub.environ = _oc_env()
    hub.getppid = os.getpid

    async with Client(server) as client:
        a = await client.call_tool("hub_subscribe", {"oc_session": OC_A, "oc_dir": str(first_dir)})
        assert not a.is_error, text_of(a)
        b = await client.call_tool("hub_subscribe", {"oc_session": OC_B, "oc_dir": str(second_dir)})
        assert not b.is_error, text_of(b)
        bc = await client.call_tool(
            "hub_subscribe", {"channel_id": chan, "oc_session": OC_B, "oc_dir": str(second_dir)}
        )
        assert not bc.is_error, text_of(bc)
        # Вторая сессия на проект первой — рядом (Ф26-1), не вытеснение и не 409.
        dup = await client.call_tool("hub_subscribe", {"oc_session": OC_B, "oc_dir": str(first_dir)})
        assert not dup.is_error, text_of(dup)
        assert "Рядом слушают: #1 без роли 1 — карточки." in text_of(dup)

        listing = text_of(
            await client.call_tool("hub_subscriptions", {"oc_session": OC_A, "oc_dir": str(first_dir)})
        )
        mine_lines = [ln for ln in listing.splitlines() if "это ты" in ln]
        assert len(mine_lines) == 1 and f"проект {first}" in mine_lines[0]

        un = text_of(
            await client.call_tool("hub_unsubscribe", {"oc_session": OC_A, "oc_dir": str(first_dir)})
        )
        assert "снята" in un

    with db.reader() as conn:
        rows = conn.execute(
            "SELECT session_id, ended_ts, end_reason FROM main.session_sub ORDER BY id"
        ).fetchall()
        chans = conn.execute("SELECT session_id, ended_ts FROM main.session_chan_sub").fetchall()
    assert rows == [(OC_A, rows[0][1], "unsubscribed"), (OC_B, None, None), (OC_B, None, None)]
    assert rows[0][1] is not None
    assert chans == [(OC_B, None)]
