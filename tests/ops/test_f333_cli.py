"""Ф33-3: `hub backup --snapshot-dir` сквозь настоящий `hub core` подпроцессом (снимок делает ядро, в
настоящий /dev/shm) и `hub backup --hold-outbox` без ядра."""

import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from .test_cli_ops import _run_cli, core_env  # noqa: F401 — фикстура ядра подпроцессом

_SHM = Path("/dev/shm")  # noqa: S108 — память, как у боевого снимка


@pytest.mark.skipif(not _SHM.is_dir(), reason="нет /dev/shm")
def test_snapshot_cli_through_real_core(core_env, capsys) -> None:  # noqa: F811
    data_dir, _config = core_env
    dest = _SHM / f"hub-restic-t{os.getpid()}"
    dest.mkdir(mode=0o700)
    try:
        _run_cli(["backup", "--snapshot-dir", str(dest)])
        body = json.loads(capsys.readouterr().out)
        assert body["ok"] is True
        names = {p.name for p in dest.iterdir()}
        assert "manifest.json" in names
        dbs = [p for p in dest.iterdir() if p.suffix == ".db"]
        assert len(dbs) == 2
        for p in dbs:
            conn = sqlite3.connect(p.as_uri() + "?mode=ro", uri=True)
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            conn.close()
        manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["data_dir"] == str(data_dir)

        # Повтор в непустой каталог — отказ ядра, код возврата не ноль.
        with pytest.raises(SystemExit) as exc:
            _run_cli(["backup", "--snapshot-dir", str(dest)])
        assert exc.value.code == 1
        assert "HTTP 400" in capsys.readouterr().err
    finally:
        shutil.rmtree(dest, ignore_errors=True)


def test_hold_outbox_cli_refuses_empty_dir(tmp_path) -> None:
    with pytest.raises(SystemExit) as exc:
        _run_cli(["backup", "--hold-outbox", str(tmp_path)])
    assert "нет файлов БД" in str(exc.value.code)


def test_backup_modes_are_exclusive(tmp_path) -> None:
    with pytest.raises(SystemExit) as exc:
        _run_cli(["backup", "--snapshot-dir", "/dev/shm/hub-restic-1", "--hold-outbox", str(tmp_path)])
    assert exc.value.code == 2
