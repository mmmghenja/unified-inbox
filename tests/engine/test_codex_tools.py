"""Ф74-5: перевод вызовов Codex (`codex_tools`), тонкий хук (`hub.work.codex_hook`), флаги запуска и выбор
модели (`codex_process`). Настоящий Codex здесь не запускается."""

import asyncio
import io
import json
import os
import shlex
import subprocess
import sys
import tomllib

import pytest

from hub.engine import codex_process, codex_tools
from hub.work import codex_hook

CWD = "/w"
PATCH = "*** Begin Patch\n*** Update File: /w/a.py\n@@\n-x\n+y\n*** Add File: /w/b.py\n+z\n*** End Patch\n"


def test_from_hook_bash_patch_mcp_skip_unknown():
    bash = codex_tools.from_hook({"tool_name": "Bash", "tool_input": {"command": "ls"}}, cwd=CWD)
    assert bash == {"tool_name": "Bash", "tool_input": {"command": "ls"}}
    tty = codex_tools.from_hook({"tool_name": "Bash", "tool_input": {"command": "read x </dev/tty"}}, cwd=CWD)
    assert tty["tty"] is True
    extra = codex_tools.from_hook({"tool_name": "Bash", "tool_input": {"command": "ls", "x": 1}}, cwd=CWD)
    assert extra["unknown_keys"] == ["x"]
    assert codex_tools.from_hook({"tool_name": "Bash", "tool_input": {}}, cwd=CWD)["unparsed"] is True
    patch = codex_tools.from_hook({"tool_name": "apply_patch", "tool_input": {"command": PATCH}}, cwd=CWD)
    assert patch["tool_name"] == "Edit" and patch["files"] == ["/w/a.py", "/w/b.py"] and patch["patch"]
    relative = PATCH.replace("/w/a.py", "src/a.py")
    rel = codex_tools.from_hook({"tool_name": "apply_patch", "tool_input": {"command": relative}}, cwd=CWD)
    assert rel["files"] == ["/w/src/a.py", "/w/b.py"] and rel["tool_input"]["file_path"] == "/w/src/a.py"
    bad = codex_tools.from_hook({"tool_name": "apply_patch", "tool_input": {"command": "мусор"}}, cwd=CWD)
    assert bad["unparsed"] is True
    mcp = codex_tools.from_hook({"tool_name": "mcp__hub-all__hub_read", "tool_input": {"a": 1}}, cwd=CWD)
    assert mcp == {"tool_name": "mcp__hub-all__hub_read", "tool_input": {"a": 1}}
    assert codex_tools.from_hook({"tool_name": "update_plan", "tool_input": {}}, cwd=CWD)["skip"] is True
    task = codex_tools.from_hook(
        {"tool_name": "spawn_agent", "tool_input": {"model": "o9", "message": "x"}}, cwd=CWD
    )
    assert task == {"tool_name": "Task", "tool_input": {"model": "o9"}}
    unknown = codex_tools.from_hook({"tool_name": "js_repl", "tool_input": {"code": "1"}}, cwd=CWD)
    assert unknown["unknown_tool"] is True
    assert codex_tools.from_hook(
        {"tool_name": "view_image", "tool_input": {"path": "/w/a.png"}}, cwd=CWD
    ) == {
        "tool_name": "Read",
        "tool_input": {"file_path": "/w/a.png"},
    }


def test_bound_command_runs_only_in_session_dir(tmp_path):
    """Проба 5: `workdir` вызова хук не видит — команда исполняется только в папке сессии, без ввода."""
    session = tmp_path / "s"
    session.mkdir()
    other = tmp_path / "o"
    other.mkdir()
    bound = codex_tools.bound_command("pwd; cat", str(session))
    assert codex_tools.unbound(bound) == "pwd; cat"
    ok = subprocess.run(
        ["/bin/bash", "-lc", bound], cwd=session, input="ввод", capture_output=True, text=True
    )
    assert ok.returncode == 0 and ok.stdout.strip() == str(session)  # `cat` читает пустой ввод
    refused = subprocess.run(["/bin/bash", "-lc", bound], cwd=other, capture_output=True, text=True)
    assert refused.returncode == 126 and "только в папке сессии" in refused.stderr and refused.stdout == ""


def test_stdin_bypass_and_shell_command():
    assert codex_tools.stdin_bypass("hello\n") and not codex_tools.stdin_bypass("\x03")
    assert not codex_tools.stdin_bypass("") and not codex_tools.stdin_bypass(None)
    bound = codex_tools.bound_command("git status", "/w")
    item = {"type": "commandExecution", "command": "/bin/bash -lc " + shlex.quote(bound)}
    assert codex_tools.shell_command(item) == "git status"
    assert codex_tools.shell_command({"commandActions": [{"command": bound}]}) == "git status"


def test_stream_item_and_result_text():
    cmd = {"type": "commandExecution", "id": "c", "command": "/bin/bash -lc ls", "cwd": "/w"}
    assert codex_tools.stream_item(cmd) == ("Bash", {"command": "ls", "cwd": "/w"})
    assert codex_tools.result_text({**cmd, "aggregatedOutput": "a\n", "exitCode": 0}) == ("a\n", False, None)
    assert codex_tools.result_text({**cmd, "exitCode": 1}, output="err")[1] is True
    change = {"type": "fileChange", "changes": [{"path": "/w/a.py", "kind": {"type": "update"}, "diff": ""}]}
    assert codex_tools.stream_item(change) == ("Edit", {"file_path": "/w/a.py"})
    text, failed, use = codex_tools.result_text(change, patch=PATCH)
    assert not failed and use == {"patch": PATCH, "filePath": "/w/a.py"} and "a.py" in text
    added = {"type": "fileChange", "changes": [{"path": "/w/n.py", "kind": {"type": "add"}}]}
    assert codex_tools.stream_item(added)[0] == "Write"
    mcp = {
        "type": "mcpToolCall",
        "server": "hub-all",
        "tool": "hub_read",
        "arguments": {"a": 1},
        "result": {"content": [{"type": "text", "text": "ok"}]},
    }
    assert codex_tools.stream_item(mcp) == ("mcp__hub-all__hub_read", {"a": 1})
    assert codex_tools.result_text(mcp) == ("ok", False, None)
    assert codex_tools.result_text({**mcp, "error": {"message": "нет"}}) == ("нет", True, None)
    assert codex_tools.stream_item({"type": "agentMessage"}) is None


# --- флаги запуска, модель ----------------------------------------------------------------------------------


def test_hook_flags_are_valid_toml_and_fail_closed():
    flags = codex_process.hook_flags(["/opt/hub hook", "--x"])
    assert flags[0::2] == ["-c", "-c"]
    for flag, event in zip(flags[1::2], codex_tools.HOOK_EVENTS, strict=True):
        parsed = tomllib.loads(flag)
        [entry] = parsed["hooks"][event]
        [handler] = entry["hooks"]
        assert handler["type"] == "command" and handler["timeout"] == codex_process.HOOK_TIMEOUT_S
        assert handler["command"].startswith("'/opt/hub hook' --x " + event + " ||")
        assert "exit 2" in handler["command"]
    # Падение интерпретатора хука (не 0 и не 2) — тоже запрет: код 2 и причина в stderr.
    line = codex_process.hook_command_line(["/bin/false"], "PreToolUse")
    done = subprocess.run(["/bin/sh", "-c", line], capture_output=True, text=True)
    assert done.returncode == 2 and codex_process.HOOK_FAILED in done.stderr


def test_trust_and_own_hooks_and_mcp_flags():
    listing = {
        "data": [
            {
                "hooks": [
                    {
                        "source": "sessionFlags",
                        "key": "/f/config.toml:pre_tool_use:0:0",
                        "currentHash": "h1",
                        "eventName": "preToolUse",
                    },
                    {"source": "user", "key": "/u", "currentHash": "h2", "eventName": "preToolUse"},
                ]
            }
        ]
    }
    own = codex_process.own_hooks(listing)
    assert [h["currentHash"] for h in own] == ["h1"]
    [_, flag] = codex_process.trust_flag(own)
    assert tomllib.loads(flag)["hooks"]["state"] == {
        "/f/config.toml:pre_tool_use:0:0": {"trusted_hash": "h1"}
    }
    assert codex_process.trust_flag([]) == []
    # Ф74-5б: ключ `-c` Codex делит по точкам буквально — имя без кавычек; имя с точкой флагом не выразить.
    assert codex_process.mcp_disable_flags(["hub-ext", "a.b", 'x"y', "ok_1"]) == [
        "-c",
        "mcp_servers.hub-ext.enabled=false",
        "-c",
        "mcp_servers.ok_1.enabled=false",
    ]


MODELS = [
    {
        "id": "gpt-6.1-sol",
        "model": "gpt-6.1-sol",
        "isDefault": True,
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ("low", "medium", "high", "max")],
    },
    {"id": "gpt-6.1-mini", "model": "gpt-6.1-mini", "supportedReasoningEfforts": []},
]


def test_check_model_refuses_instead_of_replacing():
    assert codex_process.check_model(MODELS, None, None) == ("gpt-6.1-sol", "medium")
    assert codex_process.check_model(MODELS, "gpt-6.1-sol", "low") == ("gpt-6.1-sol", "low")
    with pytest.raises(codex_process.CodexStartError, match="нет усилия «ultra»"):
        codex_process.check_model(MODELS, "gpt-6.1-sol", "ultra")
    with pytest.raises(codex_process.CodexStartError, match="модели «sonnet» нет"):
        codex_process.check_model(MODELS, "sonnet", None)
    with pytest.raises(codex_process.CodexStartError):
        codex_process.check_model([], None, None)


# --- тонкий хук ---------------------------------------------------------------------------------------------


def _run_hook(environ, data=b'{"tool_name": "Bash"}', argv=("PreToolUse",)):
    return codex_hook.run(list(argv), io.BytesIO(data), environ)


def test_hook_denies_on_any_failure():
    code, out, err = _run_hook({})
    assert code == codex_hook.EXIT_DENY and out == "" and err == codex_hook.DENY_TEXT
    code, _out, _err = _run_hook({"HUB_CODEX_SOCK": "hub-test-nope", "HUB_CODEX_PID": str(os.getpid())})
    assert code == codex_hook.EXIT_DENY
    assert _run_hook({}, argv=())[0] == codex_hook.EXIT_DENY


async def _serve(answer: bytes | None, delay: float = 0.0):
    name = f"hub-test-codex-hook-{os.getpid()}-{id(answer)}-{delay}"
    seen = []

    async def handle(reader, writer):
        seen.append(json.loads(await reader.readline()))
        await asyncio.sleep(delay)
        if answer is not None:
            writer.write(answer)
            await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(handle, path="\0" + name)
    return server, name, seen


async def test_hook_passes_host_answer_and_checks_shape():
    allow = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}
    server, name, seen = await _serve(json.dumps(allow).encode() + b"\n")
    env = {"HUB_CODEX_SOCK": name, "HUB_CODEX_PID": str(os.getpid())}
    code, out, err = await asyncio.to_thread(_run_hook, env)
    assert code == 0 and json.loads(out) == allow and err == ""
    assert seen == [{"event": "PreToolUse", "input": {"tool_name": "Bash"}}]
    code, _out, _err = await asyncio.to_thread(_run_hook, {**env, "HUB_CODEX_PID": str(os.getpid() + 1)})
    assert code == codex_hook.EXIT_DENY and len(seen) == 1  # не тот собеседник
    server.close()
    await server.wait_closed()
    # Ответ без решения — запрет (Codex счёл бы его «нет решения» и пропустил инструмент).
    server, name, _seen = await _serve(b'{"hookSpecificOutput": {"hookEventName": "PreToolUse"}}\n')
    code, _out, _err = await asyncio.to_thread(
        _run_hook, {"HUB_CODEX_SOCK": name, "HUB_CODEX_PID": str(os.getpid())}
    )
    assert code == codex_hook.EXIT_DENY
    server.close()
    await server.wait_closed()
    # Хост молчит дольше `HUB_CODEX_HOOK_WAIT_S` — запрет раньше таймаута Codex.
    server, name, _seen = await _serve(json.dumps(allow).encode() + b"\n", delay=3)
    env = {"HUB_CODEX_SOCK": name, "HUB_CODEX_PID": str(os.getpid()), "HUB_CODEX_HOOK_WAIT_S": "0.5"}
    code, _out, _err = await asyncio.to_thread(_run_hook, env)
    assert code == codex_hook.EXIT_DENY
    server.close()
    await server.wait_closed()


def test_hook_main_exits_2_with_reason():
    proc = subprocess.run(
        [sys.executable, "-m", "hub.work.codex_hook", "PreToolUse"],
        input=b"{}",
        capture_output=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("HUB_CODEX")},
        timeout=60,
    )
    assert proc.returncode == 2 and codex_hook.DENY_TEXT in proc.stderr.decode()
