"""Ф74-2 (docs/architecture.md): `hub engine mcp-export` — набор MCP Claude Code в
формате Cursor, OpenCode, Codex тем же конвертером, что у сессий хаба. Секреты — только в файл `--out` (0600),
в stdout — маска. Домашний каталог — подменный."""

import json
import stat
import sys
import tomllib

import pytest

from hub import cli as hub_cli
from hub.engine import mcp_export

SECRET = "sk-very-secret-123"


@pytest.fixture
def home(tmp_path):
    claude = {
        "mcpServers": {
            "searxng": {"command": "uvx", "args": ["mcp-searxng"], "env": {"API_KEY": SECRET}},
            "ru edit": {"type": "http", "url": "https://x.example/mcp", "headers": {"Authorization": SECRET}},
            "hub-all": {"command": "/opt/hub", "args": ["mcp", "--profile", "all"]},  # хаба в выгрузке нет
        }
    }
    (tmp_path / ".claude.json").write_text(json.dumps(claude))
    return tmp_path


def test_stdout_masks_secrets_in_every_format(home):
    cursor = json.loads(mcp_export.export("cursor", home=home))
    assert set(cursor["mcpServers"]) == {"searxng", "ru edit"}
    assert cursor["mcpServers"]["searxng"]["env"] == {"API_KEY": mcp_export.MASK}
    opencode = json.loads(mcp_export.export("opencode", home=home))
    assert opencode["mcp"]["searxng"]["environment"] == {"API_KEY": mcp_export.MASK}
    assert opencode["mcp"]["ru edit"]["headers"] == {"Authorization": mcp_export.MASK}
    text = mcp_export.export("codex", home=home)
    codex = tomllib.loads(text)["mcp_servers"]
    assert codex["searxng"] == {
        "command": "uvx",
        "args": ["mcp-searxng"],
        "env": {"API_KEY": mcp_export.MASK},
    }
    assert codex["ru edit"] == {
        "url": "https://x.example/mcp",
        "http_headers": {"Authorization": mcp_export.MASK},
    }
    for out in (json.dumps(cursor), json.dumps(opencode), text):
        assert SECRET not in out and "hub-all" not in out


def test_out_file_has_real_values_and_mode_0600(home, tmp_path):
    out = tmp_path / "codex-mcp.toml"
    out.write_text("old")
    out.chmod(0o644)
    line = mcp_export.export("codex", home=home, out=out)
    assert SECRET not in line and "searxng" in line
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    codex = tomllib.loads(out.read_text())["mcp_servers"]
    assert codex["searxng"]["env"]["API_KEY"] == SECRET


def test_cli(monkeypatch, home, capsys, tmp_path):
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        sys, "argv", ["hub", "engine", "mcp-export", "--format", "opencode", "--dir", str(home)]
    )
    hub_cli.main()
    assert json.loads(capsys.readouterr().out)["mcp"]["searxng"]["environment"]["API_KEY"] == mcp_export.MASK
    monkeypatch.setattr(
        sys, "argv", ["hub", "engine", "mcp-export", "--format", "cursor", "--dir", "/nonexistent"]
    )
    with pytest.raises(SystemExit):
        hub_cli.main()
