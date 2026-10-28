"""Настоящее ядро дочерним процессом (свободный порт, временные каталоги — не боевые 8787 и `~/.config/hub`),
как в `tests/hub_mcp/test_mcp_cli.py`."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

_STARTUP_TIMEOUT_S = 15.0


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


def _connects_tcp(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def api_get(port: int, token: str, path: str) -> httpx.Response:
    with httpx.Client(base_url=f"http://127.0.0.1:{port}/api/v1", trust_env=False) as c:
        return c.get(path, headers={"Authorization": f"Bearer {token}"})


def mode_of(path: Path) -> str:
    return oct(path.stat().st_mode & 0o777)


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
        _wait(lambda: _connects_tcp(port), f"порт {port}")
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
