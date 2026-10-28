"""Ф74-8 (план фазы 74, раздел 7, п. 2): `hub-all` из `~/.cursor/mcp.json` владельца внутри сессии хаба на Cursor.
Cursor поднимает пользовательский сервер и в сессии хаба; имя совпадает с сервером плагина сессии — вызов
уходит пользовательскому (проба `.runs/f748-probe/`, `dup`: `WHO=user`). Окружение сессии ему не передаётся
(`HOME`, `LC_CTYPE`, `PATH`), а у предка `cursor-agent` стоит `HUB_SESSION`. Такой сервер инструментов не
даёт — вызов берёт сервер плагина (проба `hide`: `WHO=plugin`)."""

import json
import os
import subprocess
import sys

from test_session_registry_ext import CU_PID, CX_PID, Tree

from hub import session_registry as reg
from hub.mcp_server import server as server_mod


def _tree(tmp_path, *, cursor_env: bytes | None, codex_env: bytes | None = None) -> Tree:
    t = Tree(tmp_path)
    t.add(1, 0, 1, "systemd")
    t.add(CU_PID, 1, 5100, "MainThread", "/home/u/.local/bin/cursor-agent")
    t.add(210, CU_PID, 6100, "hub")  # MCP-сервер, поднятый Cursor
    t.add(CX_PID, 1, 5000, "codex", "/home/u/.local/share/codex/0.163.0/bin/codex")
    t.add(220, CX_PID, 6200, "hub")
    t.add(300, 1, 7000, "bash")
    if cursor_env is not None:
        (t.proc / str(CU_PID) / "environ").write_bytes(cursor_env)
    if codex_env is not None:
        (t.proc / str(CX_PID) / "environ").write_bytes(codex_env)
    return t


def test_held_ancestor_needs_hub_session_in_the_engine_environment(tmp_path):
    t = _tree(tmp_path, cursor_env=b"PATH=/bin\0HUB_SESSION=42\0HOME=/h\0", codex_env=b"PATH=/bin\0")
    assert reg.held_ancestor(210, t.proc) == ("cursor", CU_PID, 42)
    assert reg.held_ancestor(220, t.proc) is None  # внешнее окно Codex: сессии хаба нет
    assert reg.held_ancestor(300, t.proc) is None  # предка-движка нет


def test_held_ancestor_ignores_bad_or_unreadable_environment(tmp_path):
    bad = (b"HUB_SESSION=\0", b"HUB_SESSION=abc\0", b"HUB_SESSION=0\0", b"XHUB_SESSION=5\0", None)
    for n, env in enumerate(bad):
        (tmp_path / str(n)).mkdir()
        t = _tree(tmp_path / str(n), cursor_env=env)
        assert reg.held_ancestor(210, t.proc) is None, env


def test_stray_only_without_own_hub_session(tmp_path):
    t = _tree(tmp_path, cursor_env=b"HUB_SESSION=42\0")
    assert server_mod.stray_in_held({}, pid=210, proc_root=t.proc) == ("cursor", CU_PID, 42)
    # Сервер плагина сессии: свой `HUB_SESSION` есть — он и есть сервер сессии.
    assert server_mod.stray_in_held({"HUB_SESSION": "42"}, pid=210, proc_root=t.proc) is None


FAKE_CURSOR = """
import os, subprocess, sys
env = {k: v for k, v in os.environ.items() if k != "HUB_SESSION"}
sys.exit(subprocess.call(sys.argv[1:], env=env))
"""


def _list_tools(tmp_path, *, hub_session: str | None) -> tuple[list, str]:
    """Настоящий `hub mcp --profile all` под поддельным `cursor-agent` (имя процесса — `cursor-agent`);
    у предка `HUB_SESSION` — как у процесса сессии хаба, серверу он не передаётся."""
    fake = tmp_path / "cursor-agent"
    fake.write_text(f"#!{sys.executable}\n{FAKE_CURSOR}", encoding="utf-8")
    fake.chmod(0o755)
    (tmp_path / "cfg").mkdir(exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "HUB_CONFIG_DIR": str(tmp_path / "cfg"),
        "HUB_DATA_DIR": str(tmp_path / "data"),
        "LANG": "C.UTF-8",
    }
    if hub_session is not None:
        env["HUB_SESSION"] = hub_session
    hub = os.path.join(os.path.dirname(sys.executable), "hub")
    lines = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    proc = subprocess.Popen(
        [str(fake), hub, "mcp", "--profile", "all", "--url", "http://127.0.0.1:9/api/v1"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    for line in lines:
        proc.stdin.write(json.dumps(line) + "\n")
        proc.stdin.flush()
    tools = None
    while tools is None:
        raw = proc.stdout.readline()
        assert raw, proc.stderr.read()
        msg = json.loads(raw)
        if msg.get("id") == 2:
            tools = msg["result"]["tools"]
    proc.stdin.close()
    err = proc.stderr.read()
    proc.wait(timeout=30)
    return tools, err


def test_user_config_hub_server_inside_hub_session_has_no_tools(tmp_path):
    tools, err = _list_tools(tmp_path, hub_session="42")
    assert tools == [] and "внутри сессии хаба 42" in err


def test_user_config_hub_server_in_external_window_keeps_tools(tmp_path):
    tools, _err = _list_tools(tmp_path, hub_session=None)
    assert any(t["name"] == "hub_read" for t in tools)
