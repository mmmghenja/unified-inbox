"""Ф33-3 (К4, К5): открытый снимок для restic — `core/snapshot.py` и ручка `/admin/snapshot`.

Обе БД копируются `VACUUM INTO` и открываются; живые SQLite сессий мессенджеров (в том числе в WAL с ещё не
перенесёнными страницами) — backup API, без файлов `-wal`; `state.json` почты — как есть; любой сбой
оставляет каталог снимка пустым; каталог — только свой, 0700, пустой, в «памяти» (корень подменён).
"""

import json
import sqlite3
import stat
from pathlib import Path

import pytest

from hub.core import snapshot

from .conftest import insert_account


@pytest.fixture
def shm(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "shm"
    root.mkdir()
    monkeypatch.setattr(snapshot, "SNAPSHOT_ROOT", root)
    return root


def _dest(shm: Path, name: str = "hub-restic-1") -> Path:
    d = shm / name
    d.mkdir(mode=0o700)
    return d


def _wal_db(path: Path, rows: int) -> sqlite3.Connection:
    """База в WAL с незакрытым писателем: строки лежат только в `-wal` (автоперенос выключен)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE t (v TEXT)")
    for i in range(rows):
        conn.execute("INSERT INTO t (v) VALUES (?)", (f"row-{i}",))
    return conn


def _plain_db(path: Path, rows: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sessions (dc INTEGER, key BLOB)")
    conn.executemany("INSERT INTO sessions VALUES (?, ?)", [(i, b"k") for i in range(rows)])
    conn.commit()
    conn.close()


def _open_count(path: Path, table: str) -> int:
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
    finally:
        conn.close()


def test_snapshot_copies_both_dbs_and_messenger_sessions(db, shm, config_dir) -> None:
    account_id = insert_account(db, source="fake")
    data = db.data_dir
    _plain_db(data / "telegram" / "1" / "tg.session", 3)
    wa_writer = _wal_db(data / "whatsapp" / "2" / "session.db", 5)
    (data / "mail" / "3").mkdir(parents=True)
    (data / "mail" / "3" / "state.json").write_text('{"uidnext": 42}', encoding="utf-8")
    (data / "telegram" / "1" / "link").symlink_to(data / "mail" / "3" / "state.json")
    try:
        assert (data / "whatsapp" / "2" / "session.db-wal").stat().st_size > 0
        dest = _dest(shm)
        result = snapshot.make_snapshot_sync(db=db, config_dir=config_dir, dest=dest, now_ms=1_000)
    finally:
        wa_writer.close()

    assert result["ok"] is True
    assert _open_count(dest / db.main_path.name, "main.account") >= 1
    conn = sqlite3.connect((dest / db.main_path.name).as_uri() + "?mode=ro", uri=True)
    assert conn.execute("SELECT id FROM account WHERE id = ?", (account_id,)).fetchone() is not None
    conn.close()
    assert _open_count(dest / db.work_path.name, "sqlite_master") > 0
    assert _open_count(dest / "telegram" / "1" / "tg.session", "sessions") == 3
    assert _open_count(dest / "whatsapp" / "2" / "session.db", "t") == 5  # строки были только в WAL
    assert not list(dest.rglob("*-wal")) and not list(dest.rglob("*-shm"))
    assert (dest / "mail" / "3" / "state.json").read_text(encoding="utf-8") == '{"uidnext": 42}'
    assert not (dest / "telegram" / "1" / "link").exists()  # симлинки не копируются

    manifest = json.loads((dest / snapshot.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["data_dir"] == str(data)
    assert manifest["config_dir"] == str(config_dir)
    assert manifest["ts"] == 1_000
    for p in dest.rglob("*"):
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode == (0o700 if p.is_dir() else 0o600), (p, oct(mode))


def test_snapshot_sessions_before_dbs(db, shm, config_dir, monkeypatch) -> None:
    """Финальное ревью Ф33 (17.10): сессии мессенджеров копируются раньше БД — курсор сессии в копии не новее
    базы (повтор доставки отсеет `ext_id`, пропуска нет)."""
    _plain_db(db.data_dir / "telegram" / "1" / "tg.session", 1)
    (db.data_dir / "mail" / "3").mkdir(parents=True)
    (db.data_dir / "mail" / "3" / "state.json").write_text("{}", encoding="utf-8")
    order: list[str] = []
    real_vacuum, real_copy, real_file = snapshot._vacuum_into, snapshot._sqlite_copy, snapshot.shutil.copyfile

    def vacuum(src, dest):
        order.append(f"db:{Path(src).name}")
        return real_vacuum(src, dest)

    def copy(src, dest):
        order.append(f"session:{Path(src).name}")
        return real_copy(src, dest)

    def copyfile(src, dest, **kw):
        order.append(f"session:{Path(src).name}")
        return real_file(src, dest, **kw)

    monkeypatch.setattr(snapshot, "_vacuum_into", vacuum)
    monkeypatch.setattr(snapshot, "_sqlite_copy", copy)
    monkeypatch.setattr(snapshot.shutil, "copyfile", copyfile)
    snapshot.make_snapshot_sync(db=db, config_dir=config_dir, dest=_dest(shm), now_ms=1)
    kinds = [item.split(":")[0] for item in order]
    assert kinds == ["session", "session", "db", "db"], order


def test_snapshot_without_messenger_dirs(db, shm, config_dir) -> None:
    dest = _dest(shm)
    snapshot.make_snapshot_sync(db=db, config_dir=config_dir, dest=dest, now_ms=1)
    names = sorted(p.name for p in dest.iterdir())
    assert names == sorted([db.main_path.name, db.work_path.name, snapshot.MANIFEST_NAME])


def test_snapshot_failure_leaves_dir_empty(db, shm, config_dir, monkeypatch) -> None:
    _plain_db(db.data_dir / "telegram" / "1" / "tg.session", 1)

    def boom(src, dest):
        Path(dest).write_bytes(b"partial")
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(snapshot, "_sqlite_copy", boom)
    dest = _dest(shm)
    with pytest.raises(snapshot.SnapshotFailed, match="OperationalError"):
        snapshot.make_snapshot_sync(db=db, config_dir=config_dir, dest=dest, now_ms=1)
    assert dest.is_dir() and list(dest.iterdir()) == []


def test_snapshot_no_space_writes_nothing(db, shm, config_dir, monkeypatch) -> None:
    class _Usage:
        free = 1

    monkeypatch.setattr(snapshot.shutil, "disk_usage", lambda _p: _Usage())
    dest = _dest(shm)
    with pytest.raises(snapshot.SnapshotFailed, match="no space"):
        snapshot.make_snapshot_sync(db=db, config_dir=config_dir, dest=dest, now_ms=1)
    assert list(dest.iterdir()) == []


@pytest.mark.parametrize("case", ["outside", "prefix", "mode", "nonempty", "symlink", "missing"])
def test_snapshot_rejects_bad_dir(db, shm, config_dir, tmp_path, case) -> None:
    if case == "outside":
        dest = tmp_path / "hub-restic-1"
        dest.mkdir(mode=0o700)
    elif case == "prefix":
        dest = _dest(shm, "other-1")
    elif case == "mode":
        dest = _dest(shm)
        dest.chmod(0o755)
    elif case == "nonempty":
        dest = _dest(shm)
        (dest / "x").write_text("x", encoding="utf-8")
    elif case == "symlink":
        real = tmp_path / "real"
        real.mkdir(mode=0o700)
        dest = shm / "hub-restic-1"
        dest.symlink_to(real)
    else:
        dest = shm / "hub-restic-1"
    with pytest.raises(snapshot.SnapshotBadDir):
        snapshot.make_snapshot_sync(db=db, config_dir=config_dir, dest=dest, now_ms=1)
    if case == "nonempty":
        assert (dest / "x").exists()  # чужое содержимое не трогается
    if case == "symlink":
        assert list((tmp_path / "real").iterdir()) == []


async def test_admin_snapshot_route(internal_client, raw_client, db, shm) -> None:
    dest = _dest(shm)
    r = await internal_client.post("/internal/v1/admin/snapshot", json={"dir": str(dest)})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert (dest / db.main_path.name).is_file()

    r = await raw_client.post("/internal/v1/admin/snapshot", json={"dir": str(dest)})
    assert r.status_code == 401

    r = await internal_client.post("/internal/v1/admin/snapshot", json={"dir": str(dest)})
    assert r.status_code == 400  # уже не пустой


async def test_admin_snapshot_route_failure_audited(internal_client, db, shm, monkeypatch) -> None:
    def boom(src, dest):
        raise OSError("disk")

    monkeypatch.setattr(snapshot, "_vacuum_into", boom)
    dest = _dest(shm)
    r = await internal_client.post("/internal/v1/admin/snapshot", json={"dir": str(dest)})
    assert r.status_code == 500
    assert list(dest.iterdir()) == []
    with db.reader() as conn:
        row = conn.execute("SELECT result, detail_json FROM main.audit WHERE action = 'snapshot'").fetchone()
    assert row[0] == "error"
    assert json.loads(row[1]) == {"reason": "OSError"}


def test_snapshot_busy(db, shm, config_dir) -> None:
    assert snapshot._LOCK.acquire(blocking=False)
    try:
        with pytest.raises(snapshot.SnapshotBusy):
            snapshot.make_snapshot_sync(db=db, config_dir=config_dir, dest=_dest(shm), now_ms=1)
    finally:
        snapshot._LOCK.release()


def test_default_root_is_dev_shm() -> None:
    assert snapshot.SNAPSHOT_ROOT == Path("/dev/shm")
