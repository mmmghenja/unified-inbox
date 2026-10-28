"""`hub.engine.oc_tools.to_claude`: каждая строка таблицы, MCP-префиксы, пути до гарда (Ф16, 3.3, П7)."""

import json
from pathlib import Path

import pytest

from hub.engine import oc_tools

DATA = json.loads((Path(__file__).with_name("data") / "oc-tools-1.18.json").read_text(encoding="utf-8"))

PATCH = """*** Begin Patch
*** Update File: src/a.py
@@
-x
+y
*** Add File: /abs/b.txt
+new
*** Delete File: c.md
*** Update File: src/a.py
*** Move to: d/e.py
*** End Patch
"""

ROWS = [
    (
        "bash",
        {"command": "ls -la", "timeout": 5000, "description": "список"},
        {"tool_name": "Bash", "tool_input": {"command": "ls -la", "timeout": 5000, "description": "список"}},
    ),
    (
        "read",
        {"filePath": "/w/a.py", "offset": 3, "limit": 10},
        {"tool_name": "Read", "tool_input": {"file_path": "/w/a.py", "offset": 3, "limit": 10}},
    ),
    (
        "edit",
        {"filePath": "/w/a.py", "oldString": "a", "newString": "b", "replaceAll": True},
        {
            "tool_name": "Edit",
            "tool_input": {"file_path": "/w/a.py", "old_string": "a", "new_string": "b", "replace_all": True},
        },
    ),
    (
        "write",
        {"filePath": "/w/n.txt", "content": "x"},
        {"tool_name": "Write", "tool_input": {"file_path": "/w/n.txt", "content": "x"}},
    ),
    (
        "grep",
        {"pattern": "КЛЁН", "path": "/w", "include": "*.{ts,tsx}"},
        {"tool_name": "Grep", "tool_input": {"pattern": "КЛЁН", "path": "/w", "glob": "*.{ts,tsx}"}},
    ),
    (
        "glob",
        {"pattern": "**/*.py", "path": "/w"},
        {"tool_name": "Glob", "tool_input": {"pattern": "**/*.py", "path": "/w"}},
    ),
    ("list", {"path": "/w/src"}, {"tool_name": "Glob", "tool_input": {"pattern": "*", "path": "/w/src"}}),
    (
        "webfetch",
        {"url": "https://example.org", "format": "markdown"},
        {"tool_name": "WebFetch", "tool_input": {"url": "https://example.org"}},
    ),
    ("websearch", {"query": "opencode"}, {"tool_name": "WebSearch", "tool_input": {"query": "opencode"}}),
    (
        "task",
        {"description": "разведка", "prompt": "найди", "subagent_type": "explore"},
        {
            "tool_name": "Task",
            "tool_input": {"description": "разведка", "prompt": "найди", "subagent_type": "explore"},
        },
    ),
    (
        "todowrite",
        {"todos": [{"content": "a", "status": "pending", "priority": "low"}]},
        {
            "tool_name": "TodoWrite",
            "tool_input": {"todos": [{"content": "a", "status": "pending", "priority": "low"}]},
        },
    ),
    ("todoread", {}, {"tool_name": "TodoWrite", "tool_input": {}}),
    (
        "question",
        {"questions": [{"question": "Как?", "header": "Как", "options": []}]},
        {
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [{"question": "Как?", "header": "Как", "options": []}]},
        },
    ),
    ("skill", {"name": "deploy"}, {"tool_name": "Skill", "tool_input": {"skill": "deploy"}}),
]


@pytest.mark.parametrize(("tool", "args", "expected"), ROWS, ids=[r[0] for r in ROWS])
def test_table_row(tool, args, expected):
    assert oc_tools.to_claude(tool, args, mcp_servers=["hub"]) == expected


def test_every_table_row_is_tested():
    assert {r[0] for r in ROWS} | {"apply_patch", "patch"} == set(oc_tools.TOOLS)


def test_table_covers_every_schema_key_of_p8():
    """Схемы 1.18.31 (проба P8): каждый ключ каждого встроенного инструмента известен таблице — новый ключ
    после обновления OpenCode (при обновлении файла данных) не пройдёт мимо гарда незамеченным."""
    for tool, schema in DATA["tools"].items():
        if tool == "invalid":
            continue
        assert tool in oc_tools.TOOLS, tool
        props = set((schema or {}).get("properties") or {})
        assert props <= set(oc_tools.TOOLS[tool]["keys"]), (tool, props - set(oc_tools.TOOLS[tool]["keys"]))


def test_bash_workdir_becomes_cwd_of_guard_event():
    payload = oc_tools.to_claude("bash", {"command": "git status", "workdir": "../other"})
    assert payload == {"tool_name": "Bash", "tool_input": {"command": "git status"}, "cwd": "../other"}
    [event] = oc_tools.guard_events(payload, cwd="/w/proj", session_id="ses_1")
    assert event == {
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
        "cwd": "/w/other",
        "session_id": "ses_1",
    }
    [event] = oc_tools.guard_events(
        oc_tools.to_claude("bash", {"command": "ls"}), cwd="/w/proj", session_id="s"
    )
    assert event["cwd"] == "/w/proj"


@pytest.mark.parametrize("tool", ["apply_patch", "patch"])
def test_patch_edit_per_file(tool):
    payload = oc_tools.to_claude(tool, {"patchText": PATCH})
    assert payload["tool_name"] == "Edit"
    assert payload["files"] == ["src/a.py", "/abs/b.txt", "c.md", "d/e.py"]
    assert payload["tool_input"] == {"file_path": "src/a.py"}
    assert "unparsed" not in payload
    events = oc_tools.guard_events(payload, cwd="/w", session_id="s")
    assert [(e["tool_name"], e["tool_input"]["file_path"]) for e in events] == [
        ("Edit", "src/a.py"),
        ("Edit", "/abs/b.txt"),
        ("Edit", "c.md"),
        ("Edit", "d/e.py"),
    ]


def test_patch_without_files_is_unparsed():
    payload = oc_tools.to_claude("apply_patch", {"patchText": "просто текст"})
    assert payload["unparsed"] is True and payload["files"] == []
    assert oc_tools.to_claude("apply_patch", {})["unparsed"] is True


@pytest.mark.parametrize("tool", ["read", "edit", "write"])
def test_missing_path_is_unparsed(tool):
    assert oc_tools.to_claude(tool, {"content": "x"} if tool == "write" else {})["unparsed"] is True


def test_unknown_keys_marked_for_builtin_tools():
    payload = oc_tools.to_claude("read", {"filePath": "/w/a", "extraPath": "/etc/shadow"})
    assert payload["unknown_keys"] == ["extraPath"]
    assert payload["tool_input"] == {"file_path": "/w/a"}
    assert "unknown_keys" not in oc_tools.to_claude("read", {"filePath": "/w/a"})
    assert oc_tools.to_claude("grep", {"pattern": "x", "exclude": "*.md"})["unknown_keys"] == ["exclude"]


@pytest.mark.parametrize(
    ("tool", "servers", "name"),
    [
        ("hub-all_hub_draft", ["hub-all", "hub"], "mcp__hub-all__hub_draft"),
        ("hub_hub_draft", ["hub"], "mcp__hub__hub_draft"),
        ("globex-gitlab_gl_merge_mr", ["globex", "globex-gitlab"], "mcp__globex-gitlab__gl_merge_mr"),
        (
            "playwright-show_browser_click",
            ["playwright", "playwright-show"],
            "mcp__playwright-show__browser_click",
        ),
        ("echo_echo", ["echo"], "mcp__echo__echo"),
    ],
)
def test_mcp_longest_prefix(tool, servers, name):
    payload = oc_tools.to_claude(tool, {"channel_id": 5, "text": "привет"}, mcp_servers=servers)
    assert payload == {"tool_name": name, "tool_input": {"channel_id": 5, "text": "привет"}}


def test_unknown_tool_passes_as_is():
    assert oc_tools.to_claude("external_directory", {"x": 1}, mcp_servers=["hub"]) == {
        "tool_name": "external_directory",
        "tool_input": {"x": 1},
    }
    # Сервер без инструмента после префикса — не MCP.
    assert oc_tools.to_claude("hub_", {}, mcp_servers=["hub"])["tool_name"] == "hub_"
    assert oc_tools.to_claude("read", None) == {"tool_name": "Read", "tool_input": {}, "unparsed": True}
