"""Ревью Ф74 (находки 1 и 2, воспроизведения — `.runs/critic-f74/`): гард сессии хаба видит все файлы патча
Codex/OpenCode, в том числе заголовок с отступом, а патч командой `apply_patch` в Bash судится по файлам."""

import os

import pytest

from hub.engine import codex_tools, oc_tools
from hub.work import guard

WD = "/home/owner/projects/x"
HOME = "/home/owner"
INDENTED = (
    "*** Begin Patch\n"
    "*** Add File: ok.txt\n"
    "+hi\n"
    "   *** Add File: /home/owner/.claude/settings.json\n"
    "+{}\n"
    "\t*** Delete File: CLAUDE.md\n"
    "*** End Patch"
)


def test_indented_headers_are_files():
    payload = codex_tools.from_hook({"tool_name": "apply_patch", "tool_input": {"command": INDENTED}}, cwd=WD)
    assert payload["files"] == [
        os.path.join(WD, "ok.txt"),
        "/home/owner/.claude/settings.json",
        os.path.join(WD, "CLAUDE.md"),
    ]


def test_unknown_star_line_is_unparsed():
    patch = "*** Begin Patch\n*** Add File: ok.txt\n+hi\n  *** Rename File: x\n*** End Patch"
    payload = codex_tools.from_hook({"tool_name": "apply_patch", "tool_input": {"command": patch}}, cwd=WD)
    assert payload.get("unparsed")
    oc = oc_tools.to_claude("apply_patch", {"patchText": patch})
    assert oc.get("unparsed")


def test_plain_patch_stays_parsed():
    patch = "*** Begin Patch\n*** Update File: a.py\n@@\n-x\n+y\n*** End of File\n*** End Patch"
    payload = codex_tools.from_hook({"tool_name": "apply_patch", "tool_input": {"command": patch}}, cwd=WD)
    assert payload["files"] == [os.path.join(WD, "a.py")] and not payload.get("unparsed")


def _decide(command):
    return guard.decide(
        {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": WD},
        workdir=WD,
        home=HOME,
    )


def test_bash_apply_patch_to_home_claude_denied():
    cmd = "apply_patch '*** Begin Patch\n*** Add File: /home/owner/.claude/settings.json\n+{}\n*** End Patch'"
    with pytest.raises(guard.Verdict) as exc:
        _decide(cmd)
    assert exc.value.kind == "deny"


def test_bash_apply_patch_inside_workdir_passes():
    assert _decide("apply_patch '*** Begin Patch\n*** Add File: notes.txt\n+hi\n*** End Patch'") is None


def test_bash_apply_patch_from_stdin_is_card():
    with pytest.raises(guard.Verdict) as exc:
        _decide("applypatch <<'EOF'\n*** Begin Patch\n*** Add File: a\n+b\n*** End Patch\nEOF")
    assert exc.value.kind == "ask"
