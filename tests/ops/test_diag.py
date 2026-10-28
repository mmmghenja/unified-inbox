"""Тест 1 (кусок 7): SIGUSR1 -> дамп задач в stderr, процесс жив, повторный сигнал тоже работает, никаких
текстов приложения в дампе. Спецификация: docs/architecture.md

`hub core` — дочерним процессом (как в `tests/test_cli.py`): сигнал шлём настоящему процессу, а не функции
в процессе теста — иначе проверяли бы не то же самое, что увидит journald на боевой машине.

«Нет текста маркера» проверяется не тривиально: пока сигнал приходит, во внутреннем сокете висит открытый
long-poll (`GET /outbox/wait`) от «коннектора» с заведомо приметным `source` в аккаунте — если бы дамп
печатал `repr()` локальных переменных (в т.ч. `ConnectorPrincipal.source`), маркер попал бы в stderr. Наш
`_dump()` зовёт только `task.print_stack()`/`faulthandler.dump_traceback()`, которые печатают файл/строку/
функцию, но не значения — тест ловит регрессию, если кто-то потом добавит вывод локальных переменных.
"""

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

_STARTUP_TIMEOUT_S = 10.0
_MARKER_SOURCE = "hub_test_marker_source_zzz_should_never_leak"


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


def _read_available_output(proc: subprocess.Popen, *, timeout: float) -> str:
    """Собрать всё, что процесс успел написать в stdout/stderr за `timeout` секунд, не дожидаясь его
    завершения (он продолжает жить)."""
    import selectors

    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    chunks: list[str] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for key, _ in sel.select(timeout=0.1):
            line = key.fileobj.readline()
            if line:
                chunks.append(line)
    sel.close()
    return "".join(chunks)


def test_sigusr1_dumps_tasks_process_stays_alive(core_proc) -> None:
    proc, data_dir, config_dir, port = core_proc
    admin_secret = (config_dir / "admin.key").read_text(encoding="utf-8").strip()
    transport = httpx.HTTPTransport(uds=str(data_dir / "internal.sock"))

    # Аккаунт с приметным source — держим long-poll открытым, чтобы во время сигнала в кадре одной из
    # задач реально лежал маркер (в ConnectorPrincipal.source), а не только в файле БД.
    with httpx.Client(
        transport=transport,
        base_url="http://hub-internal/internal/v1",
        headers={"Authorization": f"Bearer {admin_secret}"},
    ) as admin_client:
        r = admin_client.post(
            "/admin/accounts", json={"source": _MARKER_SOURCE, "title": None, "db": "main", "config": None}
        )
        assert r.status_code == 200, r.text
        connector_secret = r.json()["connector_secret"]

    long_poll_done = threading.Event()

    def _long_poll() -> None:
        transport2 = httpx.HTTPTransport(uds=str(data_dir / "internal.sock"))
        with httpx.Client(
            transport=transport2,
            base_url="http://hub-internal/internal/v1",
            headers={"Authorization": f"Bearer {connector_secret}"},
            timeout=10.0,
        ) as client:
            client.get("/outbox/wait", params={"timeout_s": 3.0})
        long_poll_done.set()

    poll_thread = threading.Thread(target=_long_poll, daemon=True)
    poll_thread.start()
    time.sleep(0.3)  # дать long-poll точно зайти в ожидание к моменту сигнала

    proc.send_signal(signal.SIGUSR1)
    output_1 = _read_available_output(proc, timeout=1.5)

    assert f"hub task dump core pid={proc.pid}" in output_1, output_1
    assert "tasks=" in output_1
    task_count = int(output_1.split("tasks=")[1].split()[0].split("\n")[0])
    assert task_count >= 2, output_1
    assert "--- task " in output_1
    assert _MARKER_SOURCE not in output_1, "дамп не должен печатать значения переменных"

    # /healthz всё ещё 200 — сигнал не уронил процесс.
    r = httpx.get(f"http://127.0.0.1:{port}/api/v1/healthz", timeout=5.0)
    assert r.status_code == 200

    # Второй сигнал тоже работает.
    proc.send_signal(signal.SIGUSR1)
    output_2 = _read_available_output(proc, timeout=1.5)
    assert f"hub task dump core pid={proc.pid}" in output_2, output_2
    assert _MARKER_SOURCE not in output_2

    r = httpx.get(f"http://127.0.0.1:{port}/api/v1/healthz", timeout=5.0)
    assert r.status_code == 200

    poll_thread.join(timeout=5.0)
    assert long_poll_done.is_set()
