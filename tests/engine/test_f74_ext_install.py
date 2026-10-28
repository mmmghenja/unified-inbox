"""Ф74-7 / 74-3: установщики хаба во внешние Codex и Cursor — слияние с чужими записями, повторный запуск
ничего не меняет, MCP `hub-all` не дублируется (есть — не трогается); сами скрипты — на подменённом HOME."""

import json
import os
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from hub.engine import ext_install

REPO = Path(__file__).resolve().parents[2]
GUARD_CODEX = {
    "matcher": ".*",
    "hooks": [{"type": "command", "command": "python3 /home/u/engine-guard.py codex", "timeout": 10}],
}
GUARD_CURSOR = {"command": "python3 /home/u/engine-guard.py cursor", "failClosed": True}


@pytest.fixture
def prod(tmp_path):
    """Дерево «боевого» хаба: шаблоны хуков из репозитория, `.venv/bin` с интерпретатором тестов."""
    root = tmp_path / "hub-prod"
    shutil.copytree(REPO / "deploy" / "codex", root / "deploy" / "codex")
    shutil.copytree(REPO / "deploy" / "cursor", root / "deploy" / "cursor")
    bin_dir = root / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    (bin_dir / "python").chmod(0o755)
    for name in ("hub", "hub-ext-hook"):
        (bin_dir / name).write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / name).chmod(0o755)
    return root


def hook_commands(hooks: dict, engine: str) -> dict[str, list[str]]:
    out = {}
    for event, entries in hooks["hooks"].items():
        cmds = []
        for e in entries:
            for h in e.get("hooks", [e]) if engine == "codex" else [e]:
                cmds.append(h["command"])
        out[event] = cmds
    return out


def test_codex_merge_keeps_guard_and_is_idempotent(tmp_path, prod, monkeypatch):
    home = tmp_path / "home"
    codex = home / ".codex"
    codex.mkdir(parents=True)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    (codex / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [GUARD_CODEX]}, "x": 1}))
    config = '# конфиг владельца\nmodel = "gpt-6.1-sol"\n\n[mcp_servers.ru-edit]\ncommand = "ru"\n'
    (codex / "config.toml").write_text(config)
    os.chmod(codex / "config.toml", 0o600)
    report = ext_install.install("codex", home=home, hub_prod=prod, stamp="T1")
    assert any("добавлен" in line for line in report)
    text = (codex / "config.toml").read_text()
    assert text.startswith(config)
    data = tomllib.loads(text)
    hook_bin = str(prod / ".venv" / "bin" / "hub")
    assert data["mcp_servers"]["hub-all"] == {
        "command": hook_bin,
        "args": ["mcp", "--profile", "all"],
        "tool_timeout_sec": 180,
    }
    assert stat.S_IMODE((codex / "config.toml").stat().st_mode) == 0o600
    hooks = json.loads((codex / "hooks.json").read_text())
    assert hooks["x"] == 1 and hooks["hooks"]["PreToolUse"][0] == GUARD_CODEX
    cmds = hook_commands(hooks, "codex")
    hook = str(prod / ".venv" / "bin" / "hub-ext-hook")
    assert cmds["PreToolUse"] == [GUARD_CODEX["hooks"][0]["command"], f"{hook} codex pre"]
    assert set(cmds) == {"SessionStart", "PreToolUse", "PostToolUse", "Stop", "SessionEnd"}
    assert (codex / "hooks.json.bak-T1").exists() and (codex / "config.toml.bak-T1").exists()
    snapshot = {p.name: p.read_text() for p in codex.iterdir()}
    report = ext_install.install("codex", home=home, hub_prod=prod, stamp="T2")
    assert {p.name: p.read_text() for p in codex.iterdir()} == snapshot
    assert any("есть — не трогаю" in line for line in report) and any(
        "уже на месте" in line for line in report
    )


def test_codex_existing_hub_all_left_alone(tmp_path, prod, monkeypatch):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    config = (
        '[mcp_servers.hub-all]\ncommand = "/x/hub"\nargs = ["mcp", "--profile", "all"]\nenabled = false\n'
    )
    (home / ".codex" / "config.toml").write_text(config)
    ext_install.install("codex", home=home, hub_prod=prod, stamp="T1")
    assert (home / ".codex" / "config.toml").read_text() == config
    assert not (home / ".codex" / "config.toml.bak-T1").exists()


def test_cursor_merge_replaces_only_own_entries(tmp_path, prod):
    home = tmp_path / "home"
    cur = home / ".cursor"
    cur.mkdir(parents=True)
    old_own = {"command": "/old/hub-ext-hook cursor stop", "timeout": 5}
    (cur / "hooks.json").write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {
                    "preToolUse": [GUARD_CURSOR],
                    "stop": [old_own],
                    "afterFileEdit": [{"command": "/old/hub-ext-hook cursor gone"}, GUARD_CURSOR],
                },
            }
        )
    )
    (cur / "mcp.json").write_text(
        json.dumps({"mcpServers": {"hub-all": {"command": "/x/hub", "args": ["mcp"]}}})
    )
    before_mcp = (cur / "mcp.json").read_text()
    ext_install.install("cursor", home=home, hub_prod=prod, stamp="T1")
    assert (cur / "mcp.json").read_text() == before_mcp
    hooks = json.loads((cur / "hooks.json").read_text())["hooks"]
    hook = str(prod / ".venv" / "bin" / "hub-ext-hook")
    assert hooks["preToolUse"] == [
        GUARD_CURSOR,
        {"matcher": "MCP:hub_", "command": f"{hook} cursor pre", "timeout": 30},
    ]
    assert hooks["stop"] == [{"command": f"{hook} cursor stop", "timeout": 86400, "loop_limit": None}]
    assert hooks["afterFileEdit"] == [GUARD_CURSOR]


def test_cursor_adds_hub_all_when_absent(tmp_path, prod):
    home = tmp_path / "home"
    ext_install.install("cursor", home=home, hub_prod=prod, stamp="T1")
    mcp = json.loads((home / ".cursor" / "mcp.json").read_text())
    assert mcp == {
        "mcpServers": {
            "hub-all": {"command": str(prod / ".venv/bin/hub"), "args": ["mcp", "--profile", "all"]}
        }
    }
    assert stat.S_IMODE((home / ".cursor" / "mcp.json").stat().st_mode) == 0o600


def test_codex_group_with_foreign_and_own_hook_keeps_foreign():
    mixed = {
        "matcher": ".*",
        "hooks": [GUARD_CODEX["hooks"][0], {"type": "command", "command": "/o/hub-ext-hook codex pre"}],
    }
    merged = ext_install.merge_hooks({"hooks": {"PreToolUse": [mixed]}}, {"hooks": {}}, "/n/hook", "codex")
    assert merged["hooks"]["PreToolUse"] == [{"matcher": ".*", "hooks": [GUARD_CODEX["hooks"][0]]}]


def test_hooks_not_object_is_an_error():
    with pytest.raises(ValueError):
        ext_install.merge_hooks({"hooks": []}, {"hooks": {}}, "/h", "cursor")


# --- сами скрипты на подменённом HOME -----------------------------------------------------------------------


def fake_home(tmp_path) -> tuple[Path, dict]:
    home = tmp_path / "home"
    (home / ".config" / "hub" / "mcp").mkdir(parents=True)
    (home / ".config" / "hub" / "mcp" / "all.json").write_text("{}")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    codex.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "codex-cli 0.163.0"; exit 0; fi\n'
        'if [ "$1" = "mcp" ]; then\n'
        '  grep -o "mcp_servers\\.[A-Za-z0-9_-]*" "$CODEX_HOME/config.toml"; exit 0\n'
        "fi\n"
        "exit 1\n"
    )
    codex.chmod(0o755)
    env = {
        "HOME": str(home),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HUB_CONFIG_DIR": str(home / ".config" / "hub"),
    }
    return home, env


@pytest.mark.parametrize("engine", ["codex", "cursor"])
def test_install_scripts_on_fake_home(tmp_path, prod, engine):
    home, env = fake_home(tmp_path)
    env["HUB_PROD"] = str(prod)
    script = REPO / "scripts" / f"{engine}-hub-install.sh"
    first = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=60)
    assert first.returncode == 0, first.stdout + first.stderr
    base = home / f".{engine}"
    snapshot = {p.name: p.read_text() for p in base.iterdir()}
    assert "hooks.json" in snapshot
    second = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=60)
    assert second.returncode == 0, second.stdout + second.stderr
    assert {p.name: p.read_text() for p in base.iterdir()} == snapshot


def test_codex_script_rolls_back_when_codex_does_not_see_hub_all(tmp_path, prod):
    home, env = fake_home(tmp_path)
    env["HUB_PROD"] = str(prod)
    (tmp_path / "bin" / "codex").write_text(
        '#!/bin/sh\n[ "$1" = "--version" ] && echo v && exit 0\necho none\n'
    )
    (home / ".codex").mkdir()
    (home / ".codex" / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [GUARD_CODEX]}}))
    before = (home / ".codex" / "hooks.json").read_text()
    res = subprocess.run(
        ["bash", str(REPO / "scripts" / "codex-hub-install.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert res.returncode == 1 and "Откат" in res.stdout
    assert (home / ".codex" / "hooks.json").read_text() == before
    assert not (home / ".codex" / "config.toml").exists()
