"""Ф71-1: гард и правила под Cursor (П2, П7, П8, Р6) — настройки Cursor под защитой, `cursor-agent` из Bash —
вложенный агент, сервер плагина узнаётся по имени, файлы реплики — на диск."""

import pytest

from hub.work import guard, policy, say_files


def _verdict(event: dict, workdir: str, home: str):
    try:
        guard.decide(event, workdir=workdir, home=home, free=frozenset())
    except guard.Verdict as verdict:
        return verdict
    return None


@pytest.fixture
def dirs(tmp_path):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    return str(workdir), str(home)


def test_cursor_settings_are_guarded(dirs):
    workdir, home = dirs
    project = _verdict(
        {"tool_name": "Write", "tool_input": {"file_path": f"{workdir}/.cursor/hooks.json"}, "cwd": workdir},
        workdir,
        home,
    )
    assert project is not None and project.key == "project_rules" and project.kind == "ask"
    rm = _verdict(
        {"tool_name": "Bash", "tool_input": {"command": f"rm -- {workdir}/.cursor/x.json"}, "cwd": workdir},
        workdir,
        home,
    )
    assert rm is not None and rm.key == "project_rules"
    own = _verdict(
        {"tool_name": "Write", "tool_input": {"file_path": f"{home}/.cursor/mcp.json"}, "cwd": workdir},
        workdir,
        home,
    )
    assert own is not None and own.key == "home_claude" and own.kind == "deny"
    plain = _verdict(
        {"tool_name": "Write", "tool_input": {"file_path": f"{workdir}/app.py"}, "cwd": workdir},
        workdir,
        home,
    )
    assert plain is None


@pytest.mark.parametrize(
    "command", ["cursor-agent -p hi", "agent -p hi", "~/.local/bin/cursor-agent --resume x"]
)
def test_nested_cursor_agent_is_caught(dirs, command):
    workdir, home = dirs
    verdict = _verdict(
        {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": workdir}, workdir, home
    )
    assert verdict is not None and verdict.key == "nested_agent"


def test_canonical_server_knows_cursor_plugin_servers():
    assert policy.canonical_server("plugin-s12-hub-all") == "hub-all"
    assert policy.canonical_server("plugin-atlassian-atlassian") == "atlassian"
    assert policy.canonical_server("plugin_atlassian_atlassian") == "atlassian"  # Claude — как раньше
    assert policy.canonical_server("claude_ai_globex-gitlab") == "globex-gitlab"


def test_say_files_go_to_disk_for_cursor():
    image = {"kind": "image", "mime": "image/png", "size": 10}
    assert say_files.inline("cursor", image, oc_image=True) is None
    assert say_files.inline("claude", image, oc_image=False) == "image"
