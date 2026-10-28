"""Ф74-1 (6.2 В5): запрет вложенного запуска агента в гарде собран из профилей движков — набор имён и
регулярка страховки по тексту те же, что до профиля (Ф74-5: плюс `codex`)."""

import re

from hub.work import guard

OLD_NESTED = ("claude", "opencode", "cursor-agent", "agent")
OLD_RE = re.compile(
    r"(?<![\w.])(?:claude(?!-\d+/)(?!(?<=/claude)-\d+(?![\w-]))|opencode|cursor-agent)(?![\w./])"
)

SAMPLES = [
    "claude -p hi",
    "$(which claude)",
    "echo opencode | bash",
    "C=cursor-agent; $C",
    "cat .claude/settings.json",
    "cat opencode.json",
    "ls /tmp/claude-1000/x",
    "git checkout origin/claude/ABC-101",
    "agent --resume x",
    "codex exec hi",
    "npx claude",
    "my-agent run",
    "claude-code",
    "/usr/bin/claude-12",
]


def test_nested_agents_same_set():
    # Ф74-5: Codex включён — `codex` из сессии тоже вложенный агент; прежние имена не тронуты.
    assert guard.NESTED_AGENTS == (*OLD_NESTED, "codex")


def test_nested_re_same_pattern_and_matches():
    for text in SAMPLES:
        expected = bool(OLD_RE.search(text)) or text.startswith("codex ")
        assert bool(guard._NESTED_RE.search(text)) == expected, text


def test_nested_codex_is_caught():
    for command in ("codex exec hi", "~/.local/bin/codex resume x", "C=codex; $C app-server"):
        try:
            guard.decide(
                {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/tmp"},
                workdir="/tmp",
                home="/nonexistent-home",
            )
        except guard.Verdict as verdict:
            assert verdict.key == "nested_agent", command
        else:
            raise AssertionError(command)
