"""Тест 1 (кусок 5а): реальный unix-сокет во временном каталоге — права сокета 0600, каталога 0700; запрос
по сокету проходит; второй `hub core` на том же каталоге отказывается стартовать; «мёртвый» файл сокета от
упавшего процесса не мешает старту. Спецификация: docs/architecture.md.

`_acquire_lock`/`_prepare_socket_path`/`_socket_is_alive` — те самые функции, которые использует
`runner.run_core` — тестируются здесь напрямую на настоящих файлах/сокетах, без поднятия полного `hub core`
(два реальных `uvicorn.Server` в фоне усложнили бы тест сигналами останова без дополнительной выгоды: сам
механизм защиты — файловые примитивы ОС, а не поведение, зависящее от event loop). Один тест — "смоук" —
поднимает настоящий `uvicorn.Server` на unix-сокете и делает по нему запрос (см.
`test_real_request_over_unix_socket_succeeds`).
"""

import asyncio
import os
import socket
from pathlib import Path

import httpx
import pytest
import uvicorn

from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import hash_secret
from hub.core.db import Db
from hub.core.feed import Notifier
from hub.core.runner import _acquire_lock, _prepare_socket_path, _socket_is_alive

from .conftest import ADMIN_SECRET, admin_headers

_MAX_UDS_PATH_BYTES = 100  # реальный лимит sun_path ~108 байт; берём с запасом


@pytest.fixture
def short_dirs(tmp_path_factory, monkeypatch) -> tuple[Path, Path]:
    """Каталоги данных/конфига с гарантированно коротким путём до `internal.sock` (лимит AF_UNIX). Если
    путь от `tmp_path_factory` длинный — переходим в него и дальше работаем относительными путями."""
    data_dir = tmp_path_factory.mktemp("d")
    config_dir = tmp_path_factory.mktemp("c")
    if len(str(data_dir / "internal.sock").encode("utf-8")) < _MAX_UDS_PATH_BYTES:
        return data_dir, config_dir
    monkeypatch.chdir(data_dir.parent)
    return Path(data_dir.name), Path(config_dir.name)


def test_second_core_on_same_data_dir_refuses_to_start(short_dirs) -> None:
    data_dir, _config_dir = short_dirs
    lock_fd = _acquire_lock(data_dir)
    try:
        with pytest.raises(RuntimeError, match="уже запущен"):
            _acquire_lock(data_dir)
    finally:
        os.close(lock_fd)


def test_dead_socket_file_does_not_block_start(short_dirs) -> None:
    data_dir, _config_dir = short_dirs
    socket_path = data_dir / "internal.sock"

    # «Мёртвый» файл: создан (например, предыдущим упавшим core), но никто по нему не слушает.
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(socket_path))
    dead.close()  # файл остался на диске, слушателя больше нет
    assert socket_path.exists()
    assert not _socket_is_alive(socket_path)

    prepared = _prepare_socket_path(data_dir)
    assert prepared == socket_path
    assert not socket_path.exists()  # мёртвый файл убран, путь свободен для нового bind


def test_live_socket_blocks_second_start(short_dirs) -> None:
    data_dir, _config_dir = short_dirs
    socket_path = data_dir / "internal.sock"

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    try:
        assert _socket_is_alive(socket_path)
        with pytest.raises(RuntimeError, match="уже слушает"):
            _prepare_socket_path(data_dir)
    finally:
        listener.close()


async def test_real_request_over_unix_socket_succeeds(short_dirs) -> None:
    data_dir, _config_dir = short_dirs
    socket_path = _prepare_socket_path(data_dir)

    db = Db(data_dir)
    db.start()
    notifier = Notifier()
    db.on_commit(notifier.notify)
    app = create_internal_app(
        db, clock=lambda: 1_700_000_000.0, notifier=notifier, admin_key_hash=hash_secret(ADMIN_SECRET)
    )

    config = uvicorn.Config(app, uds=str(socket_path), log_level="warning")
    sock = config.bind_socket()
    os.chmod(socket_path, 0o600)

    server = uvicorn.Server(config)
    serve_task = asyncio.ensure_future(server.serve(sockets=[sock]))
    try:
        await asyncio.sleep(0.05)  # дать серверу дойти до accept()

        assert oct(socket_path.stat().st_mode & 0o777) == "0o600"
        assert oct(data_dir.stat().st_mode & 0o777) == "0o700"

        transport = httpx.AsyncHTTPTransport(uds=str(socket_path))
        async with httpx.AsyncClient(transport=transport, base_url="http://hub-internal") as client:
            r = await client.post("/internal/v1/admin/login-code", headers=admin_headers())
        assert r.status_code == 200
        assert "code" in r.json()
    finally:
        server.should_exit = True
        await asyncio.wait_for(serve_task, timeout=5)
        db.close()
