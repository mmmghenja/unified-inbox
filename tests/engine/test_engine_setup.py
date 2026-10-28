"""`hub engine setup` (Ф16, П8; кусок 16-3): путь к `opencode`, версия, каталоги node и имена MCP ->
`<config_dir>/engine.json` (0600). Настоящий `opencode` не запускается: `which` и запуск подменены."""

import json
import subprocess

import pytest

from hub import cli
from hub.engine import setup as engine_setup

# Вывод `opencode mcp list` 1.18.31 (цвета терминала как у настоящего; команды и адреса — не разбираются).
MCP_LIST = (
    "\x1b[0m\n┌  MCP Servers\n│\n"
    "●  ○ chrome-devtools \x1b[90mdisabled\n│      \x1b[90m/usr/bin/x --headless\n│\n"
    "●  ✓ playwright \x1b[90mconnected\n│      \x1b[90m/usr/bin/y\n│\n"
    "●  ✗ globex-gitlab \x1b[90mfailed\n│      SSE error: Invalid content type\n│      https://example.test/mcp?t=1\n│\n"
    "●  ✓ hub-all \x1b[90mconnected\n│      \x1b[90m/opt/hub mcp --profile all\n│\n"
    "●  ✓ hub \x1b[90mconnected\n│\n"
    "└  5 server(s)\n"
)


class Runner:
    def __init__(self, version="1.18.31\n", mcp=MCP_LIST, version_rc=0, mcp_rc=0) -> None:
        self.calls: list[tuple[list[str], str]] = []
        self.version, self.mcp, self.version_rc, self.mcp_rc = version, mcp, version_rc, mcp_rc

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs.get("cwd")))
        if argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, self.version_rc, self.version, "")
        if argv[1:] == ["mcp", "list"]:
            return subprocess.CompletedProcess(argv, self.mcp_rc, self.mcp, "")
        raise AssertionError(argv)


@pytest.fixture
def layout(tmp_path):
    """`~/.local/bin/opencode` -> установка в nvm v22 (как у владельца), node — из nvm v24."""
    home = tmp_path / "home"
    install = home / ".nvm" / "versions" / "node" / "v22.14.0"
    real = install / "lib" / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
    real.parent.mkdir(parents=True)
    real.write_text("")
    (install / "bin").mkdir()
    node_bin = home / ".nvm" / "versions" / "node" / "v24.21.0" / "bin"
    node_bin.mkdir(parents=True)
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    (local_bin / "opencode").symlink_to(real)
    paths = {"opencode": str(local_bin / "opencode"), "node": str(node_bin / "node")}
    return home, paths, [str(local_bin), str(install / "bin"), str(node_bin)]


def test_parse_mcp_list_names_and_states_only():
    assert engine_setup.parse_mcp_list(MCP_LIST) == [
        ("chrome-devtools", "disabled"),
        ("playwright", "connected"),
        ("globex-gitlab", "failed"),
        ("hub-all", "connected"),
        ("hub", "connected"),
    ]


def test_setup_writes_engine_json_0600(tmp_path, layout):
    home, paths, dirs = layout
    config_dir = tmp_path / "config"
    runner = Runner()
    text = engine_setup.run_setup(config_dir, which=paths.get, runner=runner, home=home)
    path = config_dir / "engine.json"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {
        "opencode": {
            "path": paths["opencode"],
            "version": "1.18.31",
            "path_extra": dirs,
            # `hub` — имя собственного MCP хаба в приватных серверах, его не выключаем
            "disable_mcp": ["chrome-devtools", "playwright", "globex-gitlab", "hub-all"],
        }
    }
    assert "https://" not in path.read_text(encoding="utf-8")  # из вывода берутся только имена
    assert [argv[1:] for argv, _ in runner.calls] == [["--version"], ["mcp", "list"]]
    assert all(cwd == str(home) for _, cwd in runner.calls)
    assert "opencode: " + paths["opencode"] + " (1.18.31)" in text and "hub-all" in text


def test_setup_keeps_roles_and_warns_on_other_version(tmp_path, layout):
    home, paths, _ = layout
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    roles = {"work": {"variant": "high"}}
    (config_dir / "engine.json").write_text(json.dumps({"opencode": {"path": "/old", "roles": roles}}))
    text = engine_setup.run_setup(config_dir, which=paths.get, runner=Runner(version="1.20.0"), home=home)
    data = json.loads((config_dir / "engine.json").read_text(encoding="utf-8"))["opencode"]
    assert data["roles"] == roles and data["path"] == paths["opencode"] and data["version"] == "1.20.0"
    assert "хаб проверен на 1.18.x и 1.19.x" in text and "переопределения ролей" in text


def test_setup_errors(tmp_path, layout):
    home, paths, _ = layout
    config_dir = tmp_path / "config"
    with pytest.raises(engine_setup.SetupError, match="opencode не найден"):
        engine_setup.run_setup(config_dir, which=lambda name: None, runner=Runner(), home=home)
    with pytest.raises(engine_setup.SetupError, match="--version"):
        engine_setup.run_setup(config_dir, which=paths.get, runner=Runner(version="?"), home=home)
    with pytest.raises(engine_setup.SetupError, match="mcp list"):
        engine_setup.run_setup(config_dir, which=paths.get, runner=Runner(mcp_rc=1), home=home)
    assert not (config_dir / "engine.json").exists()


def test_cli_engine_setup_uses_config_dir_without_core(tmp_path, monkeypatch, capsys):
    seen = {}

    def fake(config_dir):
        seen["dir"] = config_dir
        return "готово"

    monkeypatch.setenv("HUB_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(engine_setup, "run_setup", fake)
    monkeypatch.setattr(cli, "_admin_client", lambda: pytest.fail("setup не ходит в ядро"))
    monkeypatch.setattr("sys.argv", ["hub", "engine", "setup"])
    cli.main()
    assert seen["dir"] == tmp_path / "cfg" and capsys.readouterr().out.strip() == "готово"


def test_setup_keeps_other_engine_sections(tmp_path, layout):
    """Ф71-1: `hub engine setup` переписывает только секцию `opencode` — путь `cursor` остаётся."""
    home, paths, _ = layout
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    cursor = {"path": "/opt/cursor-agent"}
    (config_dir / "engine.json").write_text(json.dumps({"opencode": {"path": "/old"}, "cursor": cursor}))
    engine_setup.run_setup(config_dir, which=paths.get, runner=Runner(), home=home)
    data = json.loads((config_dir / "engine.json").read_text(encoding="utf-8"))
    assert data["cursor"] == cursor and data["opencode"]["path"] == paths["opencode"]


def test_setup_writes_codex_section_and_works_without_opencode(tmp_path, layout):
    """Ф74-5: `codex` (PATH или `~/.local/bin/codex`) — секция `codex` с путём и версией; OpenCode не найден —
    его секция не трогается, а не отказ всей настройки."""
    home, paths, _ = layout
    codex = home / ".local" / "bin" / "codex"
    codex.write_text("#!/bin/sh\n")
    codex.chmod(0o700)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "engine.json").write_text(json.dumps({"cursor": {"path": "/c"}}))

    class Both(Runner):
        def __call__(self, argv, **kwargs):
            if argv[0] == str(codex):
                self.calls.append((list(argv), kwargs.get("cwd")))
                return subprocess.CompletedProcess(argv, 0, "codex-cli 0.163.0\n", "")
            return super().__call__(argv, **kwargs)

    text = engine_setup.run_setup(config_dir, which=lambda name: None, runner=Both(), home=home)
    data = json.loads((config_dir / "engine.json").read_text(encoding="utf-8"))
    assert data == {"cursor": {"path": "/c"}, "codex": {"path": str(codex), "version": "0.163.0"}}
    assert f"codex: {codex} (0.163.0)" in text
    other = engine_setup.run_setup(config_dir, which=paths.get, runner=Both(), home=home)
    data = json.loads((config_dir / "engine.json").read_text(encoding="utf-8"))
    assert set(data) == {"cursor", "codex", "opencode"} and "codex: " in other

    class Old(Both):
        def __call__(self, argv, **kwargs):
            if argv[0] == str(codex):
                return subprocess.CompletedProcess(argv, 0, "codex-cli 0.170.2\n", "")
            return super().__call__(argv, **kwargs)

    warned = engine_setup.run_setup(config_dir, which=lambda name: None, runner=Old(), home=home)
    assert "хаб проверен на 0.163.x" in warned
