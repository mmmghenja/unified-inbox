"""CLI дежурного агента и юнит. Спецификация: docs/architecture.md, B10, поправки П7,
К4, мелочи (юнит в `hub-units-install.sh`, рестарт в `hub-deploy.sh`). Ф57-1: модели нет — команд `hub agent
check` и `hub auth deepseek` больше нет (`test_f57_rules_only.py`).

`hub agent setup` — против НАСТОЯЩЕГО ядра дочерним процессом (свободный порт, временные каталоги — не боевые
8787 и `~/.config/hub`), как `tests/hub_mcp/test_mcp_cli.py`.
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from hub import cli as hub_cli
from hub.agent import files

_STARTUP_TIMEOUT_S = 15.0
_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(predicate, what: str) -> None:
    deadline = time.monotonic() + _STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise TimeoutError(f"{what} не дождались за {_STARTUP_TIMEOUT_S} с")


def _connects_unix(path: Path) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(str(path))
    except OSError:
        return False
    finally:
        probe.close()
    return True


@pytest.fixture
def real_core(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    data_dir.mkdir()
    config_dir.mkdir()
    port = _free_port()
    env = {**os.environ, "HUB_DATA_DIR": str(data_dir), "HUB_CONFIG_DIR": str(config_dir)}
    proc = subprocess.Popen(  # noqa: S603 — фиксированный локальный интерпретатор
        [sys.executable, "-c", "from hub.cli import main; main()", "core", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait(lambda: (config_dir / "admin.key").exists(), "admin.key")
        _wait(lambda: _connects_unix(data_dir / "internal.sock"), "internal.sock")
        monkeypatch.setenv("HUB_DATA_DIR", str(data_dir))
        monkeypatch.setenv("HUB_CONFIG_DIR", str(config_dir))
        yield data_dir, config_dir, port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _run_cli(argv: list[str]) -> None:
    old_argv = sys.argv
    sys.argv = ["hub", *argv]
    try:
        hub_cli.main()
    finally:
        sys.argv = old_argv


def _api(port: int, token: str, path: str) -> httpx.Response:
    with httpx.Client(base_url=f"http://127.0.0.1:{port}/api/v1", trust_env=False) as c:
        return c.get(path, headers={"Authorization": f"Bearer {token}"})


def _mode(path: Path) -> str:
    return oct(path.stat().st_mode & 0o777)


# --- Тест 17: hub agent setup — права файлов, повторный setup отзывает прежний токен ----------------------


def test_7b_17_agent_setup_files_rotation_and_state_dir(real_core, capsys):
    data_dir, config_dir, port = real_core
    _run_cli(["agent", "setup"])
    out = capsys.readouterr().out
    token_file = files.token_path(config_dir)
    assert _mode(token_file.parent) == "0o700" and _mode(token_file) == "0o600"
    assert _mode(files.token_meta_path(config_dir)) == "0o600"
    assert _mode(files.config_path(config_dir)) == "0o600"
    assert _mode(files.data_agent_dir(data_dir)) == "0o700"  # К4: каталог состояния — сразу
    first = files.read_secret(token_file)
    assert first is not None and first not in out
    written = json.loads(files.config_path(config_dir).read_text())
    # Ф57-1: ключей модели (и DeepSeek) в умолчаниях нет; команда проверки модели не упоминается
    assert not set(files.LEGACY_CONFIG_KEYS) & set(written)
    assert not [key for key in written if key.startswith("llm_")]
    assert "hub agent check" not in out and "ollama" not in out.lower()
    assert "systemctl --user enable --now hub-agent.service" in out
    assert written["skip_channel_titles"] == ["#youtrack"]  # решение владельца
    assert _api(port, first, "/agent/items").status_code == 200
    first_id = json.loads(files.token_meta_path(config_dir).read_text())["token_id"]

    _run_cli(["agent", "setup"])
    out = capsys.readouterr().out
    assert f"id={first_id}) отозван" in out
    second = files.read_secret(token_file)
    assert second != first
    assert _api(port, first, "/agent/items").status_code == 401
    assert _api(port, second, "/agent/items").status_code == 200

    capsys.readouterr()
    _run_cli(["token", "list"])
    tokens = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    live = [t for t in tokens if not t["revoked"]]
    assert [(t["name"], t["kind"], t["scopes"]) for t in live] == [
        ("agent:triage", "background", ["read", "triage"])
    ]


def test_7b_17_agent_setup_keeps_existing_config(real_core, capsys):
    _data_dir, config_dir, _port = real_core
    files.private_dir(files.config_agent_dir(config_dir))
    files.atomic_write(files.config_path(config_dir), json.dumps({"llm_model": "qwen3.5:9b"}))
    _run_cli(["agent", "setup"])
    capsys.readouterr()
    assert json.loads(files.config_path(config_dir).read_text()) == {"llm_model": "qwen3.5:9b"}


def test_7b_agent_status_without_texts(real_core, capsys):
    data_dir, _config_dir, _port = real_core
    files.save_state(
        data_dir,
        {
            "seq": 0,
            "last_ok_ts": int(time.time() * 1000),
            "calls": [int(time.time() * 1000)],
            "counters": {"triaged": 3},
            "errors": [{"ts": 1, "where": "provider", "cls": "retryable", "code": 503}],
            # 7b-2, L4: поля бюджета из state.json 7b — игнорируются, строки «расход» нет.
            "day": "2026-09-24",
            "spent_usd": 0.5,
        },
    )
    _run_cli(["agent", "status"])
    out = capsys.readouterr().out
    assert "курсор: 0 (max_seq 0; отставание 0)" in out
    assert '"triaged": 3' in out and "HTTP 503" in out
    assert "расход" not in out and "$" not in out and "ОСТАНОВЛЕН" not in out
    assert "LLM" not in out  # Ф57-1: модели нет — ни вызовов за час, ни «последнего ответа»


# --- B10: юнит, установка, деплой ---------------------------------------------------------------------


def test_7b_unit_file_and_install_scripts():
    unit = (_ROOT / "deploy" / "systemd" / "hub-agent.service").read_text(encoding="utf-8")
    for line in (
        "After=hub-core.service",
        "ExecStart=%h/projects/hub-prod/.venv/bin/hub agent run",
        "Restart=on-failure",
        "RestartPreventExitStatus=78",
        "TimeoutStopSec=30",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=read-only",
        "ReadWritePaths=%h/.local/share/hub/agent",
        "PrivateTmp=yes",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "WantedBy=default.target",
    ):
        assert f"\n{line}\n" in unit, line
    assert "/home/" not in unit
    install = (_ROOT / "scripts" / "hub-units-install.sh").read_text(encoding="utf-8")
    assert "hub-agent.service" in install
    mkdir_line = next(ln for ln in install.splitlines() if ln.startswith("mkdir"))
    assert '"$HOME/.local/share/hub/agent"' in mkdir_line
    assert not [ln for ln in install.splitlines() if ln.startswith("systemctl") and "hub-agent" in ln]
    deploy = (_ROOT / "scripts" / "hub-deploy.sh").read_text(encoding="utf-8")
    assert "systemctl --user is-active --quiet hub-agent.service" in deploy
    assert "systemctl --user restart hub-agent.service" in deploy
