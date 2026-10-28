"""CLI-пломбировка `hub backup`/`hub watch` (кусок 7) — часть теста 7 (код возврата `hub watch`) и
раздела 7.2/7.3 («`hub backup` … код возврата ≠ 0 при любой ошибке»). Юнит-уровень: подменяем
`watch.run_watch`/сетевые вызовы, реальный `notify-send` и реальный `hub core` не поднимаем здесь — это
уже делает `tests/ops/test_diag.py` (SIGUSR1) и `tests/test_cli.py` (общий стенд `hub core` подпроцессом).

Не трогаем `tests/test_cli.py` (запрещено условиями задачи) — этот файл самостоятельный.
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

import hub.watch as watch_mod
from hub import cli as hub_cli

_STARTUP_TIMEOUT_S = 10.0


def test_cmd_watch_exits_with_run_watch_code(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    data_dir.mkdir()
    config_dir.mkdir()
    monkeypatch.setenv("HUB_DATA_DIR", str(data_dir))
    monkeypatch.setenv("HUB_CONFIG_DIR", str(config_dir))

    captured = {}

    def _fake_run_watch(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(watch_mod, "run_watch", _fake_run_watch)

    with pytest.raises(SystemExit) as exc_info:
        hub_cli._cmd_watch(argparse.Namespace(port=18787))  # noqa: SLF001 -- проверяем саму подкоманду

    assert exc_info.value.code == 0
    assert captured["port"] == 18787
    assert captured["data_dir"] == data_dir
    assert captured["config_dir"] == config_dir
    assert "notifier" not in captured  # CLI не подсовывает свой notifier -- боевой путь зовёт notify-send


def test_cmd_backup_set_recipient_writes_file(tmp_path, monkeypatch, capsys) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("HUB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("HUB_DATA_DIR", str(tmp_path / "data"))

    hub_cli._cmd_backup(  # noqa: SLF001
        argparse.Namespace(set_recipient="age1" + "y" * 58)
    )

    recipient_path = config_dir / "backup.recipient"
    assert recipient_path.exists()
    assert oct(recipient_path.stat().st_mode & 0o777) == "0o600"
    assert recipient_path.read_text(encoding="utf-8").strip() == "age1" + "y" * 58
    assert "recipient записан" in capsys.readouterr().out


# --- сквозной прогон hub backup через реально запущенный hub core (подпроцесс) --------------------------


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


def _run_cli(argv: list[str]) -> None:
    old_argv = sys.argv
    sys.argv = ["hub", *argv]
    try:
        hub_cli.main()
    finally:
        sys.argv = old_argv


@pytest.fixture
def core_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    data_dir.mkdir()
    config_dir.mkdir()

    env = {**os.environ, "HUB_DATA_DIR": str(data_dir), "HUB_CONFIG_DIR": str(config_dir)}
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "from hub.cli import main; main()", "core", "--port", str(_free_port())],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
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


_HAS_AGE = shutil.which("age") is not None and shutil.which("age-keygen") is not None


@pytest.mark.skipif(not _HAS_AGE, reason="age/age-keygen не установлены")
def test_hub_backup_cli_end_to_end(core_env, tmp_path, capsys) -> None:
    _data_dir, config_dir = core_env
    identity_path = tmp_path / "identity.txt"
    r = subprocess.run(  # noqa: S603, S607
        ["age-keygen", "-o", str(identity_path)], check=True, capture_output=True, text=True, timeout=30
    )
    line = next(ln for ln in r.stderr.splitlines() if "age1" in ln)
    recipient = "age1" + line.split("age1", 1)[1].strip()

    _run_cli(["backup", "--set-recipient", recipient])
    capsys.readouterr()

    _run_cli(["backup"])
    out = capsys.readouterr().out
    body = json.loads(out)
    assert body["ok"] is True
    assert {f["name"] for f in body["files"]} == {"main", "work"}


def test_cli_clients_do_not_import_sqlite3() -> None:
    """Общее правило CLI (спека куска 7): `hub backup`/`hub watch` — тонкие клиенты, слой БД не импортируют.
    Отдельный интерпретатор: в процессе теста `sqlite3` уже загружен другими модулями."""
    code = (
        "import sys; import hub.cli, hub.watch, hub.backup_recipient; "
        "bad = [m for m in ('sqlite3', 'hub.core.db', 'hub.core.backup') if m in sys.modules]; "
        "print(bad); sys.exit(1 if bad else 0)"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)  # noqa: S603
    assert r.returncode == 0, r.stdout + r.stderr


def test_watch_with_wrong_admin_key_reports_unauthorized_not_core_down(core_env, monkeypatch) -> None:
    """Находка 7 opus-критика: живой core + негодный ключ -> `watch_unauthorized`; раньше это выглядело как
    вечный `core_down`. healthz подменён (порт фикстура не отдаёт), сокет и 401 — настоящие."""
    data_dir, config_dir = core_env

    class _Ok:
        status_code = 200

    monkeypatch.setattr(watch_mod.httpx, "get", lambda *_a, **_kw: _Ok())
    sent: list[tuple[str, str]] = []
    code = watch_mod.run_watch(
        port=1,
        data_dir=data_dir,
        config_dir=config_dir,
        admin_secret="huba_wrong-key-for-this-core",  # noqa: S106 — заведомо негодный ключ
        now_ms=0,
        notifier=lambda title, text: sent.append((title, text)),
    )
    assert code == 0
    assert [title for title, _ in sent] == ["hub: watch_unauthorized"]
