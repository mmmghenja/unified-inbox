"""Ф74-2 (docs/architecture.md; план 6.2 Б1): MCP хаба во внешнем окне Codex
или Cursor. Движок — по предку процесса сервера (поддельный `/proc`: процесс «codex» — сам pytest с его
настоящим `starttime`, чтобы ядро в процессе сочло его живым), разговор и папку называет хук хаба
(`ext_session`, `ext_dir` в аргументах инструмента). Ответ `hub_subscribe` — метка `HUB-WAIT` с движком."""

import functools
import json
import os

import httpx
import pytest
from mcp import Client

from hub import hostid
from hub import session_registry as reg
from hub.core import subs as subs_core
from hub.mcp_server import server as mcp_server

from .conftest import insert_account, insert_channel, insert_project, make_token, text_of, write_profile

HUB = "/opt/hub-prod/.venv/bin/hub"
CONV_A, CONV_B = "019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a51", "019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a52"
MCP_PID = 2_000_001  # поддельный процесс MCP-сервера — потомок «codex»
HOST = {"host": hostid.host_id({})} if hostid.host_id({}) else {}
HOST |= {"boot": hostid.boot_id({})} if hostid.boot_id({}) else {}


@pytest.fixture
def proc(tmp_path):
    """Поддельный `/proc`: pytest — процесс `codex` (его настоящий starttime), MCP-сервер — его потомок."""
    root = tmp_path / "proc"
    start = subs_core.read_proc_start(os.getpid())

    def add(pid, ppid, starttime, comm):
        d = root / str(pid)
        d.mkdir(parents=True)
        fields = ["S", str(ppid), *["0"] * 17, str(starttime), *["0"] * 10]
        (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields) + "\n")
        (d / "comm").write_text(comm + "\n")
        (d / "cmdline").write_bytes(comm.encode() + b"\0")

    add(os.getpid(), 1, start, "codex")
    add(MCP_PID, os.getpid(), start + 1, "hub")
    return root


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


def _setup(hub, proc):
    hub.hub_path = HUB
    hub.environ = {}
    hub.getppid = lambda: MCP_PID

    def no_claude(_start):
        raise reg.RegistryError(reg.GONE, "процесса сессии Claude Code среди предков нет")

    hub.find_session = no_claude
    hub.find_ext_session = functools.partial(reg.find_ext_session, proc_root=proc)


def _mark(text: str) -> dict:
    last = text.strip().splitlines()[-1]
    assert last.startswith(mcp_server.EXT_WAIT_MARK), last
    return json.loads(last[len(mcp_server.EXT_WAIT_MARK) :])


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


async def test_subscribe_sends_codex_identity_and_hub_wait_mark(config_dir, make_server, proc, tmp_path):
    write_profile(config_dir, "all", "hubt_x")
    core = Core({("POST", "/subs"): (200, {"sub": SUB, "created": True, "backfilled": 0})})
    server, hub = make_server("all", transport=httpx.MockTransport(core.handler))
    _setup(hub, proc)
    async with Client(server) as client:
        result = await client.call_tool("hub_subscribe", {"ext_session": CONV_A, "ext_dir": str(tmp_path)})
    text = text_of(result)
    assert not result.is_error, text
    [(_m, _p, body)] = core.seen
    assert body["session"] == {
        "pid": os.getpid(),
        "proc_start": subs_core.read_proc_start(os.getpid()),
        "session_id": f"codex:{CONV_A}",
        **HOST,
        "cwd": str(tmp_path),
        "name": None,
        "name_source": None,
        "kind": "codex",
    }
    assert mcp_server.OC_WAIT_TEXT in text and "run_in_background" not in text
    argv = [HUB, "sub", "wait", "17", "--profile", "all"]
    assert _mark(text) == {"engine": "codex", "pid": os.getpid(), "argv": argv}


async def test_without_hook_args_names_codex_installer(config_dir, make_server, proc):
    write_profile(config_dir, "all", "hubt_x")
    core = Core({})
    server, hub = make_server("all", transport=httpx.MockTransport(core.handler))
    _setup(hub, proc)
    async with Client(server) as client:
        result = await client.call_tool("hub_subscribe", {})
    assert result.is_error and "codex-hub-install.sh" in text_of(result)
    assert core.seen == []


async def test_claude_registry_wins_over_hook_args(config_dir, make_server, proc, tmp_path):
    """Б1: аргументы хука принимаются только после реестра Claude — модель Claude Code ими не назовётся."""
    write_profile(config_dir, "all", "hubt_x")
    core = Core({("POST", "/subs"): (200, {"sub": SUB, "created": True, "backfilled": 0})})
    server, hub = make_server("all", transport=httpx.MockTransport(core.handler))
    _setup(hub, proc)
    me = reg.SessionInfo(4242, 777, "0f6f6d6a-9c3d-4d3f", str(tmp_path), None, None, "interactive")
    hub.find_session = lambda _start: me
    async with Client(server) as client:
        result = await client.call_tool("hub_subscribe", {"ext_session": CONV_A, "ext_dir": str(tmp_path)})
    assert not result.is_error, text_of(result)
    assert core.seen[0][2]["session"]["session_id"] == me.session_id
    assert mcp_server.EXT_WAIT_MARK not in text_of(result)


async def test_ext_params_on_the_same_tools_as_oc(config_dir, make_server, proc):
    write_profile(config_dir, "all", "hubt_x")
    server, _hub = make_server("all", transport=httpx.MockTransport(Core({}).handler))
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
    with_oc = {t.name for t in tools if "oc_session" in t.input_schema.get("properties", {})}
    with_ext = {t.name for t in tools if "ext_session" in t.input_schema.get("properties", {})}
    assert with_oc and with_oc == with_ext
    props = next(t for t in tools if t.name == "hub_draft").input_schema["properties"]
    assert "хук хаба" in props["ext_session"]["description"]


async def test_two_codex_conversations_real_core(db, config_dir, make_server, proc, tmp_path):
    first_dir, second_dir = tmp_path / "p1", tmp_path / "p2"
    first_dir.mkdir()
    second_dir.mkdir()
    first = insert_project(db, name="Umbrella", workdir=str(first_dir))
    second = insert_project(db, name="Initech", workdir=str(second_dir))
    for project_id in (first, second):
        db.write_sync(lambda conn, pid=project_id: subs_core.set_project_subs(conn, pid, True, now_ms=0))
    acc = insert_account(db, source="mail")
    chan = insert_channel(db, acc, project_id=second, ext_id="t1", kind="mail_thread")
    token, _ = make_token(db, scopes=("read", "draft"), name="mcp:all")
    write_profile(config_dir, "all", token)
    server, hub = make_server("all")
    _setup(hub, proc)
    a = {"ext_session": CONV_A, "ext_dir": str(first_dir)}
    b = {"ext_session": CONV_B, "ext_dir": str(second_dir)}
    async with Client(server) as client:
        ra = await client.call_tool("hub_subscribe", a)
        assert not ra.is_error, text_of(ra)
        rb = await client.call_tool("hub_subscribe", b)
        assert not rb.is_error, text_of(rb)
        rc = await client.call_tool("hub_subscribe", {"channel_id": chan, **b})
        assert not rc.is_error, text_of(rc)
        assert _mark(text_of(rc))["argv"][3] == "--channels"
        listing = text_of(await client.call_tool("hub_subscriptions", a))
        assert len([ln for ln in listing.splitlines() if "это ты" in ln]) == 1
        un = text_of(await client.call_tool("hub_unsubscribe", a))
        assert "снята" in un and "плагина" in un
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT session_id, end_reason, session_engine FROM main.session_sub ORDER BY id"
        ).fetchall()
        chans = conn.execute(
            "SELECT session_id, ended_ts, session_engine FROM main.session_chan_sub"
        ).fetchall()
    assert rows == [(f"codex:{CONV_A}", "unsubscribed", "codex"), (f"codex:{CONV_B}", None, "codex")]
    assert chans == [(f"codex:{CONV_B}", None, "codex")]
