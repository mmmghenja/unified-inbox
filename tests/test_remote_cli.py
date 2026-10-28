"""Ф10-7 (docs/architecture.md, п. 1, 5, поправки П2–П3): `hub core
--remote-port` поднимает второй слушатель ОДНИМ сервером; занятый удалённый порт не роняет ядро (громкая
строка, `listening:false`); одинаковые порты — отказ на старте; `hub remote status|off` через админ-сокет.

`hub core` — отдельным процессом на свободных портах и временных каталогах (как `tests/test_cli.py`).
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

_STARTUP_TIMEOUT_S = 10.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_listening_socket(path: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(path))
        except OSError:
            time.sleep(0.02)
        else:
            return
        finally:
            probe.close()
    raise TimeoutError(f"по {path} никто не слушает спустя {timeout} с")


def _wait_http(url: str, timeout: float) -> httpx.Response:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return httpx.get(url, timeout=2.0, trust_env=False)
        except httpx.HTTPError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def _run_cli(argv: list[str]) -> None:
    old_argv = sys.argv
    sys.argv = ["hub", *argv]
    try:
        hub_cli.main()
    finally:
        sys.argv = old_argv


class _Core:
    def __init__(self, tmp_path: Path, port: int, remote_port: int) -> None:
        self.data_dir = tmp_path / "data"
        self.config_dir = tmp_path / "config"
        self.data_dir.mkdir()
        self.config_dir.mkdir()
        env = {**os.environ, "HUB_DATA_DIR": str(self.data_dir), "HUB_CONFIG_DIR": str(self.config_dir)}
        self.proc = subprocess.Popen(  # noqa: S603 — фиксированный локальный интерпретатор
            [
                sys.executable,
                "-c",
                "from hub.cli import main; main()",
                "core",
                "--port",
                str(port),
                "--remote-port",
                str(remote_port),
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def stop(self) -> str:
        self.proc.terminate()
        try:
            out, _ = self.proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            out, _ = self.proc.communicate(timeout=5)
        return out.decode("utf-8", errors="replace")


@pytest.fixture
def cli_env(monkeypatch):
    def _use(core: _Core) -> None:
        monkeypatch.setenv("HUB_DATA_DIR", str(core.data_dir))
        monkeypatch.setenv("HUB_CONFIG_DIR", str(core.config_dir))

    return _use


def test_remote_listener_up_disabled_is_421_and_cli_status_off(tmp_path, cli_env, capsys) -> None:
    port, remote_port = _free_port(), _free_port()
    core = _Core(tmp_path, port, remote_port)
    try:
        _wait_for_listening_socket(core.data_dir / "internal.sock", _STARTUP_TIMEOUT_S)
        assert _wait_http(f"http://127.0.0.1:{port}/api/v1/healthz", _STARTUP_TIMEOUT_S).status_code == 200
        # Второй сокет того же сервера: доступ не включён -> 421 на всё, даже с «локальным» Host.
        r = httpx.get(f"http://127.0.0.1:{remote_port}/api/v1/healthz", trust_env=False)
        assert r.status_code == 421
        r = httpx.get(
            f"http://127.0.0.1:{remote_port}/api/v1/healthz",
            headers={"Host": f"127.0.0.1:{port}"},
            trust_env=False,
        )
        assert r.status_code == 421
        # proxy_headers выключены: X-Forwarded-* не делают запрос «локальным» и не ломают ответ.
        r = httpx.get(
            f"http://127.0.0.1:{port}/api/v1/healthz",
            headers={"X-Forwarded-For": "8.8.8.8", "X-Forwarded-Proto": "https"},
            trust_env=False,
        )
        assert r.status_code == 200

        cli_env(core)
        _run_cli(["remote", "status"])
        status = json.loads(capsys.readouterr().out)
        assert status["listening"] is True
        assert status["enabled"] is False
        assert status["password_set"] is False
        _run_cli(["remote", "off"])
        assert "отозвано телефонных сессий: 0" in capsys.readouterr().out
    finally:
        core.stop()


def test_busy_remote_port_does_not_stop_core(tmp_path, cli_env, capsys) -> None:
    port = _free_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    remote_port = blocker.getsockname()[1]
    core = _Core(tmp_path, port, remote_port)
    try:
        _wait_for_listening_socket(core.data_dir / "internal.sock", _STARTUP_TIMEOUT_S)
        assert _wait_http(f"http://127.0.0.1:{port}/api/v1/healthz", _STARTUP_TIMEOUT_S).status_code == 200
        cli_env(core)
        _run_cli(["remote", "status"])
        assert json.loads(capsys.readouterr().out)["listening"] is False
    finally:
        out = core.stop()
        blocker.close()
    assert f"удалённый слушатель 127.0.0.1:{remote_port} НЕ поднят" in out


def test_same_port_and_remote_port_refuses_to_start(tmp_path) -> None:
    port = _free_port()
    core = _Core(tmp_path, port, port)
    try:
        code = core.proc.wait(timeout=_STARTUP_TIMEOUT_S)
    finally:
        out = core.stop() if core.proc.poll() is None else core.proc.stdout.read().decode("utf-8", "replace")
    assert code != 0
    assert "совпадает с --port" in out
    assert not (core.data_dir / "internal.sock").exists()


def test_remote_port_defaults_to_zero() -> None:
    """П2: без флага удалённого слушателя нет (тесты и стенд не должны занимать 8790)."""
    args = hub_cli._build_parser().parse_args(["core"])  # noqa: SLF001
    assert args.remote_port == 0


def test_e2e_phone_login_through_remote_socket_and_cli_off_closes_it(tmp_path, cli_env, capsys) -> None:
    """П1 на настоящем `hub core`: одно состояние на публичное и внутреннее приложения — `hub remote off`
    через админ-сокет сразу закрывает вход паролем на удалённом сокете (браузер телефона помнит пароль)."""
    port, remote_port = _free_port(), _free_port()
    core = _Core(tmp_path, port, remote_port)
    host = "hub.test.example:8787"
    password = "correct horse battery"
    try:
        _wait_for_listening_socket(core.data_dir / "internal.sock", _STARTUP_TIMEOUT_S)
        _wait_http(f"http://127.0.0.1:{port}/api/v1/healthz", _STARTUP_TIMEOUT_S)
        cli_env(core)
        _run_cli(["ui", "--print-url"])
        code = capsys.readouterr().out.strip().split("#code=", 1)[1]

        origin = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=origin, trust_env=False) as laptop:
            r = laptop.post("/api/v1/auth/session", json={"code": code}, headers={"Origin": origin})
            assert r.status_code == 200, r.text
            headers = {"Origin": origin, "X-Hub-CSRF": r.json()["csrf"]}
            r = laptop.put("/api/v1/remote/password", json={"password": password}, headers=headers)
            assert r.status_code == 200, r.text
            r = laptop.put("/api/v1/remote", json={"enabled": True, "hosts": [host]}, headers=headers)
            assert r.status_code == 200, r.text
            assert r.json()["listening"] is True

        with httpx.Client(
            base_url=f"http://127.0.0.1:{remote_port}", headers={"Host": host}, trust_env=False
        ) as phone:

            def login() -> httpx.Response:
                return phone.post(
                    "/api/v1/auth/password", json={"password": password}, headers={"Origin": f"http://{host}"}
                )

            assert login().status_code == 200
            me = phone.get("/api/v1/auth/me")
            assert me.status_code == 200
            assert me.json()["remote"] is True

            _run_cli(["remote", "off"])
            assert "отозвано телефонных сессий: 1" in capsys.readouterr().out
            assert login().status_code == 421
            assert phone.get("/api/v1/auth/me").status_code == 421
    finally:
        core.stop()
