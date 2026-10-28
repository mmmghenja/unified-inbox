"""Ф14-9 (docs/architecture.md, тесты T1–T2): `hub core --remote-port` с парой
`<config_dir>/tls/` поднимает удалённый слушатель по https (свой `_Server`, тот же `public_app`), локальный —
http как был; битая пара — удалённый не слушает, ошибка в статусе, локальный и ядро живы.

`hub core` — отдельным процессом на свободных портах и временных каталогах (как `tests/test_remote_cli.py`);
«lifespan один раз» — в процессе теста через `runner._serve_forever`.
"""

import asyncio
import json
import os
import signal
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from hub import cli as hub_cli
from hub.core import runner
from hub.dev.selfsigned import generate, write_pair

_STARTUP_TIMEOUT_S = 10.0
PASSWORD = "correct horse battery"


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
    """`hub core` дочерним процессом; каталоги готовятся ДО старта (пара читается только на старте)."""

    def __init__(self, tmp_path: Path) -> None:
        self.data_dir = tmp_path / "data"
        self.config_dir = tmp_path / "config"
        self.data_dir.mkdir()
        self.config_dir.mkdir()
        self.tls_dir = self.config_dir / "tls"
        self.port = _free_port()
        self.remote_port = _free_port()
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        env = {**os.environ, "HUB_DATA_DIR": str(self.data_dir), "HUB_CONFIG_DIR": str(self.config_dir)}
        self.proc = subprocess.Popen(  # noqa: S603 — фиксированный локальный интерпретатор
            [
                sys.executable,
                "-c",
                "from hub.cli import main; main()",
                "core",
                "--port",
                str(self.port),
                "--remote-port",
                str(self.remote_port),
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        _wait_for_listening_socket(self.data_dir / "internal.sock", _STARTUP_TIMEOUT_S)
        _wait_http(f"http://127.0.0.1:{self.port}/api/v1/healthz", _STARTUP_TIMEOUT_S)

    def terminate(self) -> tuple[int, str]:
        assert self.proc is not None
        self.proc.terminate()
        try:
            out, _ = self.proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            out, _ = self.proc.communicate(timeout=5)
        return self.proc.returncode, out.decode("utf-8", errors="replace")


@pytest.fixture
def core(tmp_path, monkeypatch):
    c = _Core(tmp_path)
    monkeypatch.setenv("HUB_DATA_DIR", str(c.data_dir))
    monkeypatch.setenv("HUB_CONFIG_DIR", str(c.config_dir))
    yield c
    if c.proc is not None and c.proc.poll() is None:
        c.terminate()


def _remote_status(capsys) -> dict:
    _run_cli(["remote", "status"])
    return json.loads(capsys.readouterr().out)


def _enable_remote(core: _Core, capsys, host: str) -> None:
    _run_cli(["ui", "--print-url"])
    code = capsys.readouterr().out.strip().split("#code=", 1)[1]
    origin = f"http://127.0.0.1:{core.port}"
    with httpx.Client(base_url=origin, trust_env=False) as laptop:
        r = laptop.post("/api/v1/auth/session", json={"code": code}, headers={"Origin": origin})
        assert r.status_code == 200, r.text
        headers = {"Origin": origin, "X-Hub-CSRF": r.json()["csrf"]}
        r = laptop.put("/api/v1/remote/password", json={"password": PASSWORD}, headers=headers)
        assert r.status_code == 200, r.text
        r = laptop.put("/api/v1/remote", json={"enabled": True, "hosts": [host]}, headers=headers)
        assert r.status_code == 200, r.text


# === T1: валидная пара — удалённый по https, локальный по http ===


def test_t1_remote_listener_is_https_local_stays_http(core, capsys) -> None:
    ca = write_pair(core.tls_dir, ["hub.test", "127.0.0.1"])
    core.start()
    host = f"hub.test:{core.remote_port}"
    verify = ssl.create_default_context(cafile=str(ca))
    remote_url = f"https://127.0.0.1:{core.remote_port}"

    # До включения доступа — ответ ядра (421) уже по TLS.
    r = httpx.get(f"{remote_url}/api/v1/healthz", headers={"Host": host}, verify=verify, trust_env=False)
    assert r.status_code == 421

    status = _remote_status(capsys)
    assert status["listening"] is True
    assert status["tls"]["error"] is None
    assert status["tls"]["names"] == ["hub.test", "127.0.0.1"]
    assert status["tls"]["not_after_ms"] > time.time() * 1000

    _enable_remote(core, capsys, host)
    with httpx.Client(base_url=remote_url, headers={"Host": host}, verify=verify, trust_env=False) as phone:
        assert phone.get("/api/v1/healthz").status_code == 200
        r = phone.post(
            "/api/v1/auth/password", json={"password": PASSWORD}, headers={"Origin": f"https://{host}"}
        )
        assert r.status_code == 200, r.text
        assert "secure" in [p.strip().lower() for p in r.headers["set-cookie"].split(";")]
        me = phone.get("/api/v1/auth/me")
        assert me.status_code == 200 and me.json()["remote"] is True
        # Origin http:// на https-слушателе — 403 (решение 5).
        r = phone.post(
            "/api/v1/auth/password", json={"password": PASSWORD}, headers={"Origin": f"http://{host}"}
        )
        assert r.status_code == 403

    # http на удалённый порт — обрыв, не ответ ядра.
    with pytest.raises(httpx.HTTPError):
        httpx.get(
            f"http://127.0.0.1:{core.remote_port}/api/v1/healthz", headers={"Host": host}, trust_env=False
        )

    # Локальный — http как был.
    assert httpx.get(f"http://127.0.0.1:{core.port}/api/v1/healthz", trust_env=False).status_code == 200

    # SIGTERM гасит все серверы: выход 0 (а не убит сигналом), следующий старт на тех же каталогах проходит.
    code, out = core.terminate()
    assert code == 0, out[-2000:]
    core.start()
    code, out = core.terminate()
    assert code == 0, out[-2000:]


# === T2: битая пара — удалённый не слушает, локальный жив ===


def _pair_key_from_other_cert(tls_dir: Path) -> None:
    write_pair(tls_dir, ["hub.test"])
    _chain, other_key, _ca = generate(["hub.test"])
    (tls_dir / "privkey.pem").write_bytes(other_key)


def _pair_only_cert(tls_dir: Path) -> None:
    write_pair(tls_dir, ["hub.test"])
    (tls_dir / "privkey.pem").unlink()


def _pair_garbage(tls_dir: Path) -> None:
    tls_dir.mkdir(mode=0o700)
    (tls_dir / "fullchain.pem").write_text("мусор\n")
    (tls_dir / "privkey.pem").write_text("мусор\n")


@pytest.mark.parametrize(
    ("make_pair", "error"),
    [
        (_pair_key_from_other_cert, "ключ не от этого сертификата"),
        (_pair_only_cert, "нет файла privkey.pem"),
        (_pair_garbage, "сертификат или ключ не загружаются"),
    ],
)
def test_t2_broken_pair_remote_not_listening(core, capsys, make_pair, error) -> None:
    make_pair(core.tls_dir)
    core.start()
    status = _remote_status(capsys)
    assert status["listening"] is False
    assert status["tls"]["error"].startswith(error)
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", core.remote_port), timeout=2).close()
    assert httpx.get(f"http://127.0.0.1:{core.port}/api/v1/healthz", trust_env=False).status_code == 200
    assert core.proc is not None and core.proc.poll() is None
    code, out = core.terminate()
    assert code == 0
    assert f"удалённый слушатель 127.0.0.1:{core.remote_port} НЕ поднят" in out
    assert error in out


def test_t2_no_pair_is_http_as_before(core, capsys) -> None:
    core.start()
    status = _remote_status(capsys)
    assert status["listening"] is True
    assert status["tls"] is None
    r = httpx.get(f"http://127.0.0.1:{core.remote_port}/api/v1/healthz", trust_env=False)
    assert r.status_code == 421  # ответ ядра по http: доступ не включён


# === T1: lifespan приложения — один раз, при двух публичных серверах ===


async def test_t1_lifespan_runs_once_with_tls_server(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    data_dir.mkdir()
    config_dir.mkdir()
    ca = write_pair(config_dir / "tls", ["hub.test", "127.0.0.1"])
    port, remote_port = _free_port(), _free_port()

    entered: list[int] = []
    real_create = runner.create_public_app

    def counting_create(*args, **kwargs):
        app = real_create(*args, **kwargs)
        real_lifespan = app.router.lifespan_context

        def lifespan(a):
            entered.append(1)
            return real_lifespan(a)

        app.router.lifespan_context = lifespan
        return app

    monkeypatch.setattr(runner, "create_public_app", counting_create)
    task = asyncio.ensure_future(
        runner._serve_forever(  # noqa: SLF001
            data_dir,
            config_dir,
            host="127.0.0.1",
            port=port,
            clock=time.time,
            ui_dir=None,
            remote_port=remote_port,
        )
    )
    verify = ssl.create_default_context(cafile=str(ca))
    try:
        async with httpx.AsyncClient(trust_env=False, verify=verify) as client:
            deadline = time.monotonic() + _STARTUP_TIMEOUT_S
            while True:
                try:
                    r = await client.get(
                        f"https://127.0.0.1:{remote_port}/api/v1/healthz",
                        headers={"Host": f"hub.test:{remote_port}"},
                    )
                    break
                except httpx.HTTPError:
                    if time.monotonic() > deadline or task.done():
                        raise
                    await asyncio.sleep(0.05)
            assert r.status_code == 421
            r = await client.get(f"http://127.0.0.1:{port}/api/v1/healthz")
            assert r.status_code == 200
    finally:
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(task, timeout=15)
    assert entered == [1]
