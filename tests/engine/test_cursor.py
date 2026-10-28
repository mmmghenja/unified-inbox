"""Ф71-1: перевод вызовов Cursor (`cursor_tools`), тонкий хук (`hub.work.cursor_hook`), модель и путь
(`choice`), плагин сессии (`cursor_process.write_plugin`). Настоящий Cursor не запускается."""

import asyncio
import io
import json
import os
import socket
import stat
import subprocess
import sys

import pytest

from hub.engine import choice, cursor_tools
from hub.engine.cursor_process import write_plugin, write_rules
from hub.work import cursor_hook

# --- перевод хуков (П2, П3) ---------------------------------------------------------------------------------


def test_from_hook_whitelist():
    pre = cursor_tools.PRE
    shell = cursor_tools.from_hook(
        pre, {"tool_name": "Shell", "tool_input": {"command": "ls", "cwd": "", "timeout": 30000}}, cwd="/w"
    )
    assert shell == {"tool_name": "Bash", "tool_input": {"command": "ls"}}
    sub = cursor_tools.from_hook(
        pre, {"tool_name": "Shell", "tool_input": {"command": "ls", "cwd": "sub"}}, cwd="/w"
    )
    assert sub["cwd"] == "sub"
    read = cursor_tools.from_hook(pre, {"tool_name": "Read", "tool_input": {"file_path": "/w/a"}}, cwd="/w")
    assert read == {"tool_name": "Read", "tool_input": {"file_path": "/w/a"}}
    write = cursor_tools.from_hook(
        pre, {"tool_name": "Write", "tool_input": {"file_path": "a", "content": "x"}}, cwd="/w"
    )
    assert write == {"tool_name": "Write", "tool_input": {"file_path": "a", "content": "x"}}
    delete = cursor_tools.from_hook(
        pre, {"tool_name": "Delete", "tool_input": {"file_path": "d e"}}, cwd="/w"
    )
    assert delete == {"tool_name": "Bash", "tool_input": {"command": "rm -- '/w/d e'"}}
    grep = cursor_tools.from_hook(
        pre,
        {
            "tool_name": "Grep",
            "tool_input": {"pattern": "x", "file_path": "/w", "glob": "*.py", "output_mode": "c"},
        },
        cwd="/w",
    )
    assert grep == {"tool_name": "Grep", "tool_input": {"pattern": "x", "path": "/w", "glob": "*.py"}}
    # MCP в `preToolUse` — решает `beforeMCPExecution`
    assert cursor_tools.from_hook(pre, {"tool_name": "MCP:hub_read", "tool_input": {}}, cwd="/w")["skip"]


def test_from_hook_unknown_is_never_allowed_silently():
    pre = cursor_tools.PRE
    assert cursor_tools.from_hook(pre, {"tool_name": "Teleport", "tool_input": {}}, cwd="/w")["unknown_tool"]
    keys = cursor_tools.from_hook(
        pre, {"tool_name": "Read", "tool_input": {"file_path": "a", "x": 1}}, cwd="/w"
    )
    assert keys["unknown_keys"] == ["x"]
    assert cursor_tools.from_hook(pre, {"tool_name": "Write", "tool_input": {"content": "x"}}, cwd="/w")[
        "unparsed"
    ]
    assert cursor_tools.from_hook(pre, {"tool_name": "Shell", "tool_input": {}}, cwd="/w")["unparsed"]
    assert cursor_tools.from_hook("afterFileEdit", {}, cwd="/w")["unknown_tool"]


def test_from_hook_mcp_and_subagent():
    mcp = cursor_tools.from_hook(
        cursor_tools.MCP,
        {"mcp_server_name": "plugin-s3-hub-all", "tool_name": "hub_read", "tool_input": '{"channel_id": 5}'},
        cwd="/w",
    )
    assert mcp == {"tool_name": "mcp__plugin-s3-hub-all__hub_read", "tool_input": {"channel_id": 5}}
    bad = cursor_tools.from_hook(
        cursor_tools.MCP, {"mcp_server_name": "x", "tool_name": "t", "tool_input": "{не json"}, cwd="/w"
    )
    assert bad["unparsed"]
    assert cursor_tools.from_hook(cursor_tools.MCP, {"tool_name": "t"}, cwd="/w")["unparsed"]
    task = cursor_tools.from_hook(cursor_tools.SUBAGENT, {"subagent_type": "explore", "x": 1}, cwd="/w")
    assert task == {"tool_name": "Task", "tool_input": {"subagent_type": "explore"}}


def test_stream_call_and_result_text():
    name, args, result = cursor_tools.stream_call(
        {"shellToolCall": {"args": {"command": "ls", "workingDirectory": "/w"}, "result": {"success": {}}}}
    )
    assert (name, args) == ("Bash", {"command": "ls", "cwd": "/w"})
    text, err, use = cursor_tools.result_text(
        "Bash", {"success": {"stdout": "a\n", "stderr": "", "exitCode": 1, "interleavedOutput": "a\n"}}
    )
    assert (text, err, use) == ("a\n", True, None)
    name, args, _ = cursor_tools.stream_call({"editToolCall": {"args": {"path": "/w/a"}}})
    assert (name, args) == ("Write", {"file_path": "/w/a"})
    text, err, use = cursor_tools.result_text("Write", {"success": {"path": "/w/a", "diffString": "@@ x"}})
    assert use == {"patch": "@@ x", "filePath": "/w/a"} and not err
    name, args, _ = cursor_tools.stream_call(
        {"mcpToolCall": {"args": {"providerIdentifier": "hub-all", "toolName": "hub_read", "args": {"a": 1}}}}
    )
    assert (name, args) == ("mcp__hub-all__hub_read", {"a": 1})
    name, args, _ = cursor_tools.stream_call(
        {"globToolCall": {"args": {"globPattern": "*.py", "targetDirectory": "/w"}}}
    )
    assert (name, args) == ("Glob", {"pattern": "*.py", "path": "/w"})
    assert cursor_tools.result_text("Read", {"rejected": {"reason": "нельзя"}}) == ("нельзя", True, None)
    assert cursor_tools.result_text("Read", {"error": {"errorMessage": "сбой"}}) == ("сбой", True, None)


# --- тонкий хук (П1, П4) ------------------------------------------------------------------------------------


def _run_hook(environ, data=b'{"tool_name": "Shell"}', argv=("preToolUse",)):
    return cursor_hook.run(list(argv), io.BytesIO(data), environ)


def test_hook_denies_on_any_failure():
    code, out = _run_hook({})
    assert code == cursor_hook.EXIT_DENY and json.loads(out)["permission"] == "deny"
    code, out = _run_hook({"HUB_CURSOR_SOCK": "hub-test-nope", "HUB_CURSOR_PID": str(os.getpid())})
    assert code == cursor_hook.EXIT_DENY and json.loads(out)["permission"] == "deny"
    code, _out = _run_hook({}, argv=())
    assert code == cursor_hook.EXIT_DENY
    code, _out = _run_hook({"HUB_CURSOR_SOCK": "x", "HUB_CURSOR_PID": "1"}, data=b"[1]")
    assert code == cursor_hook.EXIT_DENY


async def _serve(answer: bytes | None):
    name = f"hub-test-hook-{os.getpid()}-{id(answer)}"
    seen = []

    async def handle(reader, writer):
        seen.append(json.loads(await reader.readline()))
        if answer is not None:
            writer.write(answer)
            await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(handle, path="\0" + name)
    return server, name, seen


async def test_hook_passes_host_answer_and_checks_peer():
    server, name, seen = await _serve(b'{"permission": "deny", "agent_message": "\\u043d\\u0435\\u0442"}\n')
    env = {"HUB_CURSOR_SOCK": name, "HUB_CURSOR_PID": str(os.getpid())}
    code, out = await asyncio.to_thread(_run_hook, env)
    assert code == 0 and json.loads(out) == {
        "permission": "deny",
        "agent_message": "нет",
        "user_message": "нет",
    }
    assert seen == [{"event": "preToolUse", "input": {"tool_name": "Shell"}}]
    # Не тот собеседник (pid хоста другой) — запрет, вход хоста не отправлен.
    code, out = await asyncio.to_thread(_run_hook, {**env, "HUB_CURSOR_PID": str(os.getpid() + 1)})
    assert code == cursor_hook.EXIT_DENY and len(seen) == 1
    server.close()
    await server.wait_closed()
    # Хост закрыл соединение без ответа — запрет.
    server, name, seen = await _serve(None)
    code, out = await asyncio.to_thread(
        _run_hook, {"HUB_CURSOR_SOCK": name, "HUB_CURSOR_PID": str(os.getpid())}
    )
    assert code == cursor_hook.EXIT_DENY and json.loads(out)["permission"] == "deny"
    server.close()
    await server.wait_closed()


def test_hook_main_prints_deny_and_exits_2():
    proc = subprocess.run(
        [sys.executable, "-m", "hub.work.cursor_hook", "preToolUse"],
        input=b"{}",
        capture_output=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("HUB_CURSOR")},
        timeout=60,
    )
    assert proc.returncode == 2 and json.loads(proc.stdout)["permission"] == "deny"


def test_hook_uses_only_stdlib():
    """Ф74-5: общий клиент сокета (`hook_client`) — только stdlib; хуки Cursor и Codex берут из хаба
    только его (`hub` и `hub.work` при импорте ничего не тянут)."""
    from hub.work import codex_hook, hook_client

    for module, allowed in (
        (hook_client, set()),
        (cursor_hook, {"from hub.work import hook_client"}),
        (codex_hook, {"from hub.work import hook_client"}),
    ):
        with open(module.__file__, encoding="utf-8") as f:
            imports = [line.split("#")[0].strip() for line in f if line.startswith(("import ", "from "))]
        own = [line for line in imports if line.startswith("from hub") or "import hub" in line]
        assert set(own) <= allowed, module.__name__
    for package in ("hub", "hub.work"):
        init = __import__(package, fromlist=["_"]).__file__
        with open(init, encoding="utf-8") as f:
            assert not [line for line in f if line.startswith(("import ", "from "))]
    assert socket  # сокет — stdlib


# --- модель, усилие, путь (Р1, П11) -------------------------------------------------------------------------


def test_cursor_model_effort_floor_and_fast():
    assert choice.cursor_model(None, None) == ("grok-4.7-high", None)
    assert choice.cursor_model("grok-4.7", "xhigh") == ("grok-4.7-xhigh", None)
    model, warn = choice.cursor_model("grok-4.7", "low")
    assert model == "grok-4.7-high" and "поднято до high" in warn
    model, warn = choice.cursor_model("grok-4.7-medium", None)
    assert model == "grok-4.7-high" and warn
    assert choice.cursor_model("grok-4.7-xhigh", None) == ("grok-4.7-xhigh", None)
    assert choice.cursor_model("composer-2.5", "low") == ("composer-2.5", None)  # усилия нет — как есть
    assert choice.cursor_model("claude-opus-5-5", None) == ("claude-opus-5-5-high", None)
    assert choice.cursor_model("claude-opus-5-5", "xhigh") == ("claude-opus-5-5-xhigh", None)
    assert choice.cursor_model("claude-opus-5-5-high", None) == ("claude-opus-5-5-high", None)
    assert choice.cursor_model("claude-sonnet-5-5", None) == ("claude-sonnet-5-5-high", None)
    # Слово владельца: у Claude в Cursor — усилия Claude Code, medium и low не поднимаются.
    assert choice.cursor_model("claude-sonnet-5-5", "medium") == ("claude-sonnet-5-5-medium", None)
    assert choice.cursor_model("claude-opus-5-5", "low") == ("claude-opus-5-5-low", None)
    assert choice.cursor_model("claude-opus-5-5-max", None) == ("claude-opus-5-5-max", None)
    assert choice.cursor_efforts("claude-opus-5-5") == ("low", "medium", "high", "xhigh", "max")
    assert choice.cursor_efforts("grok-4.7") == ("high", "xhigh")
    for model, effort in (("grok-4.7-fast", None), ("grok-4.7", "fast"), ("composer-2.5-fast", None)):
        with pytest.raises(choice.EngineUnavailable, match="fast выключен решением владельца"):
            choice.cursor_model(model, effort)


def test_cursor_path_and_section(tmp_path):
    home = tmp_path / "home"
    binary = home / ".local" / "bin" / "cursor-agent"
    with pytest.raises(choice.EngineUnavailable, match="cursor-agent не найден"):
        choice.cursor_path({}, home)
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o700)
    assert choice.cursor_path({}, home) == str(binary)
    with pytest.raises(choice.EngineUnavailable):
        choice.cursor_path({"path": "relative/agent"}, home)
    config = tmp_path / "config"
    config.mkdir()
    assert choice.load_cursor(config) == {}
    (config / "engine.json").write_text(json.dumps({"cursor": {"path": str(binary)}, "opencode": {}}))
    assert choice.load_cursor(config) == {"path": str(binary)}
    (config / "engine.json").write_text(json.dumps({"cursor": []}))
    with pytest.raises(choice.EngineUnavailable, match="«cursor» должен быть объектом"):
        choice.load_cursor(config)


# --- плагин сессии (Р5, П4, П10) ----------------------------------------------------------------------------


def test_write_plugin_and_rules(tmp_path):
    directory = tmp_path / "s7"
    write_plugin(
        directory,
        name="s7",
        hook_command=["/venv/bin/hub-cursor-hook"],
        mcp_name="hub-all",
        mcp_command=["/venv/bin/hub", "mcp", "--profile", "all"],
        mcp_env={"HUB_SESSION": "${env:HUB_SESSION}"},
    )
    write_rules(directory, "ПРАВИЛА")
    hooks = json.loads((directory / "hooks" / "hooks.json").read_text())["hooks"]
    assert hooks["preToolUse"] == [
        {"command": "/venv/bin/hub-cursor-hook preToolUse", "timeout": 86400, "failClosed": True}
    ]
    mcp = json.loads((directory / "mcp.json").read_text())["mcpServers"]
    assert mcp == {
        "hub-all": {
            "command": "/venv/bin/hub",
            "args": ["mcp", "--profile", "all"],
            "env": {"HUB_SESSION": "${env:HUB_SESSION}"},
        }
    }
    assert json.loads((directory / ".cursor-plugin" / "plugin.json").read_text())["name"] == "s7"
    rules = (directory / "rules" / "hub-session.mdc").read_text()
    assert "alwaysApply: true" in rules and rules.rstrip().endswith("ПРАВИЛА")
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    # Без профиля MCP хаба в плагине нет.
    write_plugin(directory, name="s7", hook_command=["h"], mcp_name="hub-", mcp_command=[], mcp_env={})
    assert json.loads((directory / "mcp.json").read_text()) == {"mcpServers": {}}
