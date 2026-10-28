"""Ф99-0 (docs/architecture.md): MCP-сервер окна Codex живёт под общим демоном
`codex app-server`; демон перезапустился — новый сервер и новый процесс у той же нити. Перед первым вызовом
инструмента с разговором (`ext_session` хука) сервер шлёт ядру `POST /subs/rebind` — подписки разговора
переезжают на процесс сервера; один раз на разговор, у `hub_subscribe` — нет (подписка переносит сама).

Обвязка (поддельный `/proc`: процесс «codex» — сам pytest) — из `test_f74_mcp_ext.py`."""

import os

import httpx
from mcp import Client

from hub.core import subs as subs_core

from . import test_f74_mcp_ext as f74
from .conftest import insert_project, make_token, text_of, write_profile
from .test_f74_mcp_ext import CONV_A, CONV_B, SUB, Core, _setup

proc = f74.proc  # фикстура: поддельный `/proc` с процессом «codex»

OLD = (999_991, 5)  # прежний демон (его уже нет)
INBOX = {"items": [], "sub": {"id": 17, "project_id": 3, "role": "р", "role_no": 1}}


async def test_rebind_once_per_conversation_not_on_subscribe(config_dir, make_server, proc, tmp_path):
    write_profile(config_dir, "all", "hubt_x")
    core = Core(
        {
            ("POST", "/subs/rebind"): (200, {"rebound": 1}),
            ("GET", "/peer-msgs"): (200, INBOX),
            ("POST", "/subs"): (200, {"sub": SUB, "created": False, "backfilled": 0}),
        }
    )
    server, hub = make_server("all", transport=httpx.MockTransport(core.handler))
    _setup(hub, proc)
    a = {"ext_session": CONV_A, "ext_dir": str(tmp_path)}
    b = {"ext_session": CONV_B, "ext_dir": str(tmp_path)}
    async with Client(server) as client:
        for args in (a, a, b):
            result = await client.call_tool("hub_agent_inbox", args)
            assert not result.is_error, text_of(result)
        assert not (await client.call_tool("hub_subscribe", a)).is_error
    paths = [(m, p) for m, p, _b in core.seen]
    assert paths == [
        ("POST", "/subs/rebind"),
        ("GET", "/peer-msgs"),
        ("GET", "/peer-msgs"),
        ("POST", "/subs/rebind"),
        ("GET", "/peer-msgs"),
        ("POST", "/subs"),
    ]
    rebinds = [body["session"]["session_id"] for m, p, body in core.seen if p == "/subs/rebind"]
    assert rebinds == [f"codex:{CONV_A}", f"codex:{CONV_B}"]


async def test_old_core_404_does_not_break_tool(config_dir, make_server, proc, tmp_path):
    write_profile(config_dir, "all", "hubt_x")
    core = Core(
        {("POST", "/subs/rebind"): (404, {"detail": "Not Found"}), ("GET", "/peer-msgs"): (200, INBOX)}
    )
    server, hub = make_server("all", transport=httpx.MockTransport(core.handler))
    _setup(hub, proc)
    args = {"ext_session": CONV_A, "ext_dir": str(tmp_path)}
    async with Client(server) as client:
        for _ in range(2):
            result = await client.call_tool("hub_agent_inbox", args)
            assert not result.is_error, text_of(result)
    assert [p for _m, p, _b in core.seen] == ["/subs/rebind", "/peer-msgs", "/peer-msgs"]


async def test_restarted_daemon_real_core_keeps_subscription(db, config_dir, make_server, proc, tmp_path):
    """Подписка сделана прежним демоном (в строке его pid) — первый вызов нового сервера переводит её на себя:
    входящие читаются без «сначала hub_subscribe()», повторная подписка — та же строка и номер."""
    folder = tmp_path / "p1"
    folder.mkdir()
    project = insert_project(db, name="Umbrella", workdir=str(folder))
    db.write_sync(lambda conn: subs_core.set_project_subs(conn, project, True, now_ms=0))
    token, _ = make_token(db, scopes=("read", "draft"), name="mcp:all")
    write_profile(config_dir, "all", token)
    server, hub = make_server("all")
    _setup(hub, proc)
    args = {"ext_session": CONV_A, "ext_dir": str(folder)}
    async with Client(server) as client:
        first = await client.call_tool("hub_subscribe", {**args, "role": "разработчик"})
        assert not first.is_error, text_of(first)
        db.write_sync(
            lambda conn: conn.execute(
                "UPDATE main.session_sub SET session_pid = ?, session_proc_start = ?", OLD
            )
        )
        hub.ext_bound.clear()  # новый сервер под новым демоном ничего не знает
        inbox = await client.call_tool("hub_agent_inbox", args)
        assert not inbox.is_error, text_of(inbox)
        again = await client.call_tool("hub_subscribe", {**args, "role": "разработчик"})
        assert not again.is_error, text_of(again)
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT session_pid, role_no, ended_ts FROM main.session_sub ORDER BY id"
        ).fetchall()
    assert rows == [(os.getpid(), 1, None)]
