"""Ф56-5 (2.4): `hub term-agent token|key|revoke` и `hub term list` на настоящем `hub core` во временных
каталогах и на свободном порту (как `tests/test_f52_load_cli.py`). Терминалы здесь не создаются: сервер tmux
не поднимается (сокета нет — список пуст)."""

import json
import os
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from hub.term import agent_key

_HUB = [sys.executable, "-c", "from hub.cli import main; main()"]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError
        time.sleep(0.05)


def _listening(path: Path) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(str(path))
    except OSError:
        return False
    finally:
        probe.close()
    return True


@pytest.fixture
def core(tmp_path, term_root):
    data_dir, config_dir = tmp_path / "data", tmp_path / "config"
    data_dir.mkdir()
    config_dir.mkdir()
    port = _free_port()
    env = {
        **os.environ,
        "HUB_DATA_DIR": str(data_dir),
        "HUB_CONFIG_DIR": str(config_dir),
        "HUB_TERM_SYSTEMD": "0",
        "HUB_TERM_SOCKET": str(term_root / "s"),
        "HUB_TERM_UNIT": "hub-term-pytest",
    }
    proc = subprocess.Popen([*_HUB, "core", "--port", str(port)], env=env)  # noqa: S603
    try:
        _wait(lambda: (config_dir / "admin.key").exists() and _listening(data_dir / "internal.sock"))
        yield env, config_dir, port
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _hub(env, *argv) -> subprocess.CompletedProcess:
    return subprocess.run([*_HUB, *argv], env=env, capture_output=True, text=True, timeout=30)  # noqa: S603


def _get_term(port: int, key: str) -> httpx.Response:
    return httpx.get(f"http://127.0.0.1:{port}/api/v1/term", headers={"Authorization": f"Bearer {key}"})


def test_term_agent_key_lifecycle_and_list(core):
    env, config_dir, port = core
    r = _hub(env, "term-agent", "key")
    assert r.returncode == agent_key.EXIT_NO_KEY and "ключа нет" in r.stderr and r.stdout == ""

    r = _hub(env, "term-agent", "token")
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("ключ выпущен #") and "hub term-agent revoke" in r.stdout
    assert "hub_" not in r.stdout  # значение ключа не печатается при выпуске
    path = config_dir / "term-agent.key"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    stored = json.loads(path.read_text())
    first = _hub(env, "term-agent", "key").stdout.strip()
    assert first == stored["key"] and first.startswith("hub_")
    assert _get_term(port, first).json() == []

    # Повторный выпуск отзывает прежний.
    r = _hub(env, "term-agent", "token", "--ttl-days", "30")
    assert r.returncode == 0 and f"прежний ключ #{stored['token_id']} отозван" in r.stdout
    second = _hub(env, "term-agent", "key").stdout.strip()
    assert second != first
    assert _get_term(port, first).status_code == 401 and _get_term(port, second).status_code == 200

    assert _hub(env, "term", "list").stdout.strip() == "терминалов нет"

    r = _hub(env, "term-agent", "revoke")
    assert r.returncode == 0 and "отозван" in r.stdout and not path.exists()
    assert _get_term(port, second).status_code == 401
    assert _hub(env, "term-agent", "key").returncode == agent_key.EXIT_NO_KEY
    assert _hub(env, "term-agent", "revoke").stdout.strip() == "ключа нет"


def test_format_list():
    assert agent_key.format_list([]) == "терминалов нет"
    text = agent_key.format_list(
        [
            {"id": 3, "title": "bash в ~", "cols": 80, "rows": 24, "created_ts": 0, "owner": "ui"},
            {"id": 5, "title": "htop в ~/x", "cols": 100, "rows": 30, "created_ts": 0, "owner": "token:9"},
        ]
    )
    lines = text.splitlines()
    assert lines[0].startswith("#3  bash в ~  80×24  владелец") and "агент (token:9)" in lines[1]
