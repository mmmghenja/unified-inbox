"""Стенд ядра для тестов коннекторов (вынесен из `tests/connectors/slack/conftest.py`: им пользуются тесты
Slack, почты и Mattermost). `core_env`/`sync_admin_client` — тот же приём, что
`tests/connectors/telegram/conftest.py`: настоящий `hub core` в отдельном ПРОЦЕССЕ (реальный unix-сокет,
синхронный `httpx.Client`, как использует настоящий CLI) — для тестов `hub auth …` и `hub connector`.
`internal_app`/`admin_client`/`make_core_client` — стенд с НАСТОЯЩИМ ядром в процессе (`ASGITransport`, без
реального unix-сокета) для тестов коннектора (сверка, живые апдейты). Модуль не conftest: фикстуры подключает
conftest пакета присваиванием (`db = core_stand.db`)."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from hub.connectors.base import CoreClient
from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.db import Db
from hub.core.feed import Notifier

ADMIN_SECRET = ADMIN_PREFIX + "slack-test-admin-secret-0123456789abcdefgh"
INTERNAL_BASE_URL = "http://hub-internal"
_STARTUP_TIMEOUT_S = 10.0


class FakeClock:
    """Часы приложения — секунды, как `time.time`. Тесты сами их двигают (`advance_ms`)."""

    def __init__(self, start_s: float = 1_700_000_000.0) -> None:
        self.value = start_s

    def __call__(self) -> float:
        return self.value

    def advance_ms(self, ms: int) -> None:
        self.value += ms / 1000


@pytest.fixture
def db(tmp_path):
    instance = Db(tmp_path)
    instance.start()
    yield instance
    instance.close()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def notifier() -> Notifier:
    return Notifier()


@pytest.fixture
def internal_app(db, clock, notifier):
    db.on_commit(notifier.notify)
    return create_internal_app(db, clock=clock, notifier=notifier, admin_key_hash=hash_secret(ADMIN_SECRET))


@pytest.fixture
def internal_transport(internal_app):
    return httpx.ASGITransport(app=internal_app)


@pytest.fixture
async def admin_client(internal_transport):
    headers = {"Authorization": f"Bearer {ADMIN_SECRET}"}
    async with httpx.AsyncClient(
        transport=internal_transport, base_url=INTERNAL_BASE_URL, headers=headers
    ) as c:
        yield c


def make_core_client(internal_transport, secret: str) -> CoreClient:
    return CoreClient(secret=secret, transport=internal_transport)


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
def core_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    data_dir.mkdir()
    config_dir.mkdir()

    env = {**os.environ, "HUB_DATA_DIR": str(data_dir), "HUB_CONFIG_DIR": str(config_dir)}
    proc = subprocess.Popen(  # noqa: S603 — фиксированный локальный интерпретатор
        [sys.executable, "-c", "from hub.cli import main; main()", "core", "--port", str(_free_port())],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_file(config_dir / "admin.key", _STARTUP_TIMEOUT_S)
        _wait_for_listening_socket(data_dir / "internal.sock", _STARTUP_TIMEOUT_S)
        monkeypatch.setenv("HUB_DATA_DIR", str(data_dir))
        monkeypatch.setenv("HUB_CONFIG_DIR", str(config_dir))
        yield data_dir, config_dir
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def sync_admin_client(config_dir: Path, data_dir: Path) -> httpx.Client:
    """Синхронный `httpx.Client` поверх unix-сокета `hub core` — тот же приём, что `cli._admin_client`."""
    admin_secret = (config_dir / "admin.key").read_text(encoding="utf-8").strip()
    transport = httpx.HTTPTransport(uds=str(data_dir / "internal.sock"))
    return httpx.Client(
        transport=transport,
        base_url="http://hub-internal/internal/v1",
        headers={"Authorization": f"Bearer {admin_secret}"},
    )
