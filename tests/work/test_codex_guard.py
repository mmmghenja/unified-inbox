"""Ф74-5 (спека, 3): гард под Codex — `.codex/` проекта и `~/.codex/**` под защитой правил, секрет входа
`~/.codex/auth.json` закрыт и на чтение (инструменты с путём, слова команды, шаблоны, симлинк), картинки
реплики — блоком data:-URL, PDF — на диск."""

import os

import pytest

from hub.work import guard, say_files


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
    (home / ".codex").mkdir(parents=True)
    return str(workdir), str(home)


def _ev(tool: str, tool_input: dict, cwd: str) -> dict:
    return {"tool_name": tool, "tool_input": tool_input, "cwd": cwd}


def test_codex_settings_are_guarded(dirs):
    workdir, home = dirs
    project = _verdict(_ev("Write", {"file_path": f"{workdir}/.codex/config.toml"}, workdir), workdir, home)
    assert project is not None and project.key == "project_rules" and project.kind == "ask"
    own = _verdict(_ev("Write", {"file_path": f"{home}/.codex/hooks.json"}, workdir), workdir, home)
    assert own is not None and own.key == "home_claude" and own.kind == "deny"
    redirect = _verdict(
        _ev("Bash", {"command": f"echo x > {home}/.codex/config.toml"}, workdir), workdir, home
    )
    assert redirect is not None and redirect.key == "home_claude"
    assert _verdict(_ev("Write", {"file_path": f"{workdir}/app.py"}, workdir), workdir, home) is None


@pytest.mark.parametrize(
    "event",
    [
        lambda h, w: _ev("Read", {"file_path": f"{h}/.codex/auth.json"}, w),
        lambda h, w: _ev("Bash", {"command": f"cat {h}/.codex/auth.json"}, w),
        lambda h, w: _ev("Bash", {"command": "cat ~/.codex/auth.json"}, w),
        lambda h, w: _ev("Bash", {"command": "cat $HOME/.codex/*.json"}, w),
        lambda h, w: _ev("Bash", {"command": f"grep -r token {h}/.codex"}, w),
        lambda h, w: _ev("Bash", {"command": "python3 -c \"open('.codex/auth.json')\""}, h),
        lambda h, w: _ev("Grep", {"pattern": "x", "path": f"{h}/.codex"}, w),
        lambda h, w: _ev("Glob", {"pattern": f"{h}/.codex/**"}, w),
    ],
)
def test_codex_auth_is_closed_for_reading(dirs, event):
    workdir, home = dirs
    verdict = _verdict(event(home, workdir), workdir, home)
    assert verdict is not None and verdict.key == "home_claude" and verdict.kind == "deny"


def test_codex_auth_via_symlink(dirs):
    workdir, home = dirs
    link = os.path.join(workdir, "a.json")
    os.symlink(os.path.join(home, ".codex", "auth.json"), link)
    verdict = _verdict(_ev("Read", {"file_path": link}, workdir), workdir, home)
    assert verdict is not None and verdict.key == "home_claude"


def test_codex_other_reads_pass(dirs):
    workdir, home = dirs
    assert _verdict(_ev("Read", {"file_path": f"{home}/.codex/config.toml"}, workdir), workdir, home) is None
    assert _verdict(_ev("Bash", {"command": "cat README.md"}, workdir), workdir, home) is None


def test_say_files_for_codex():
    image = {"kind": "image", "mime": "image/png", "size": 10, "name": "a.png"}
    pdf = {"kind": "pdf", "mime": "application/pdf", "size": 10, "name": "a.pdf"}
    assert say_files.inline("codex", image, oc_image=False) == "image"
    assert say_files.inline("codex", pdf, oc_image=False) is None
    block = say_files.block("codex", "image", image, b"\x89PNG")
    assert block == {"type": "image", "url": "data:image/png;base64,iVBORw=="}
