"""Тест 12 куска 1a фазы 1 (раздел D спеки `docs/architecture.md`): SIGTERM на живом процессе
`hub connector` (fake, временные каталоги, свой порт core) -> буфер дослан (пуст, ждать нечего), код выхода
0. Стенд — тот же приём, что `tests/ops/test_diag.py` (`core_proc`): реальные подпроцессы, реальный сигнал,
а не функция в процессе теста.
"""

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

_STARTUP_TIMEOUT_S = 10.0


def _wait_for_file(path: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise TimeoutError(f"{path} не появился за {timeout} с")


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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def core_proc(tmp_path):
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    data_dir.mkdir()
    config_dir.mkdir()
    port = _free_port()

    env = {**os.environ, "HUB_DATA_DIR": str(data_dir), "HUB_CONFIG_DIR": str(config_dir)}
    proc = subprocess.Popen(  # noqa: S603 — фиксированный локальный интерпретатор
        [sys.executable, "-c", "from hub.cli import main; main()", "core", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_file(config_dir / "admin.key", _STARTUP_TIMEOUT_S)
        _wait_for_listening_socket(data_dir / "internal.sock", _STARTUP_TIMEOUT_S)
        yield proc, data_dir, config_dir, port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _add_fake_account(config_dir: Path, data_dir: Path) -> int:
    admin_secret = (config_dir / "admin.key").read_text(encoding="utf-8").strip()
    transport = httpx.HTTPTransport(uds=str(data_dir / "internal.sock"))
    with httpx.Client(
        transport=transport,
        base_url="http://hub-internal/internal/v1",
        headers={"Authorization": f"Bearer {admin_secret}"},
    ) as client:
        r = client.post(
            "/admin/accounts", json={"source": "fake", "title": None, "db": "main", "config": None}
        )
        assert r.status_code == 200, r.text
        body = r.json()

    account_id = body["account_id"]
    connectors_dir = config_dir / "connectors"
    connectors_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_path = connectors_dir / f"{account_id}.key"
    key_path.write_text(body["connector_secret"], encoding="utf-8")
    key_path.chmod(0o600)
    return account_id


def test_1a_12_sigterm_on_live_connector_process_exits_zero(core_proc) -> None:
    _core, data_dir, config_dir, _port = core_proc
    account_id = _add_fake_account(config_dir, data_dir)

    env = {**os.environ, "HUB_DATA_DIR": str(data_dir), "HUB_CONFIG_DIR": str(config_dir)}
    connector_proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "from hub.cli import main; main()", "connector", str(account_id)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        # Коннектор жив достаточно долго, чтобы завести фоновые задачи run() (пульс, толкатель, планировщик).
        time.sleep(1.0)
        assert connector_proc.poll() is None, connector_proc.stdout.read()

        connector_proc.send_signal(signal.SIGTERM)
        try:
            code = connector_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            connector_proc.kill()
            connector_proc.wait(timeout=5)
            raise AssertionError("hub connector не завершился за 10с после SIGTERM") from None

        assert code == 0, connector_proc.stdout.read()
    finally:
        if connector_proc.poll() is None:
            connector_proc.terminate()
            try:
                connector_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                connector_proc.kill()
                connector_proc.wait(timeout=5)
