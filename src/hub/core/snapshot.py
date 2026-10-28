"""Ф33-3 (К4, К5): открытый ежечасный снимок для restic — обе БД через `VACUUM INTO` (отдельное read-only
соединение, писатель работает параллельно — тот же приём, что `backup._vacuum_into`), сессии мессенджеров
(`<data_dir>/telegram/`, `whatsapp/`, `mail/`) — живые SQLite копируются `sqlite3` backup API, прочие файлы
(`state.json` почты) — обычной копией. Плюс `manifest.json` (пути каталогов данных и конфига на момент
снимка — по ним `scripts/hub-restore.sh` раскладывает копию обратно).

Каталог снимка создаёт и ВСЕГДА удаляет вызывающий (`scripts/hub-offsite-backup.sh`:
`/dev/shm/hub-restic-<pid>`, 0700, `trap … EXIT`); здесь он только проверяется (в памяти, свой, 0700,
пустой, не симлинк) и наполняется.
Любой сбой — содержимое каталога вычищается до ответа: открытый текст БД не переживает неудачную попытку.
Шифрование — дело restic (Ф33); `age`-копии `backup.py` идут своим путём раз в сутки.

Блокирующий код целиком: зовётся из `routes/internal.py::post_admin_snapshot` через `asyncio.to_thread`.
"""

import json
import logging
import os
import shutil
import sqlite3
import stat
import threading
import time
from pathlib import Path

from .db import Db

_logger = logging.getLogger(__name__)

# Модульные переменные, а не константы в коде функций: тесты подменяют корень (их tmp_path — в /dev/shm, но
# не прямо в нём).
SNAPSHOT_ROOT = Path("/dev/shm")  # noqa: S108 — память, не /tmp: свой каталог 0700 проверяется
SNAPSHOT_PREFIX = "hub-restic-"
MANIFEST_NAME = "manifest.json"
MANIFEST_FORMAT = 1
# Каталоги сессий мессенджеров в `<data_dir>` (К4): копируются в снимок по тем же относительным путям.
SESSION_DIRS = ("telegram", "whatsapp", "mail")
# Спутники SQLite не копируются: backup API читает базу вместе с её WAL и пишет цельный файл.
_SQLITE_SIDECARS = ("-wal", "-shm", "-journal")
_SQLITE_HEADER = b"SQLite format 3\x00"
# Запас свободного места поверх оценки размера копий.
FREE_MARGIN_BYTES = 64 * 1024**2
SQLITE_BUSY_TIMEOUT_S = 30.0

_LOCK = threading.Lock()


class SnapshotError(Exception):
    """Базовая ошибка снимка."""


class SnapshotBadDir(SnapshotError):
    """Каталог снимка не годится (не в `/dev/shm`, чужой, не 0700, не пустой, симлинк) — 400."""


class SnapshotBusy(SnapshotError):
    """Снимок уже идёт в этом процессе — 409."""


class SnapshotFailed(SnapshotError):
    """VACUUM/backup/копия упали или не хватило места — 500. Сообщение — вид сбоя, без путей."""


def _check_dest(dest: Path) -> None:
    if not dest.is_absolute() or not dest.name.startswith(SNAPSHOT_PREFIX):
        raise SnapshotBadDir("snapshot dir must be <root>/hub-restic-*")
    if os.path.realpath(dest.parent) != os.path.realpath(SNAPSHOT_ROOT):
        raise SnapshotBadDir("snapshot dir must be in memory (/dev/shm)")
    try:
        st = os.lstat(dest)
    except FileNotFoundError as exc:
        raise SnapshotBadDir("snapshot dir does not exist") from exc
    if not stat.S_ISDIR(st.st_mode):
        raise SnapshotBadDir("snapshot dir is not a directory")
    if st.st_uid != os.getuid():
        raise SnapshotBadDir("snapshot dir is not ours")
    if st.st_mode & 0o077:
        raise SnapshotBadDir("snapshot dir must be 0700")
    if any(dest.iterdir()):
        raise SnapshotBadDir("snapshot dir is not empty")


def _wipe(dest: Path) -> None:
    """Содержимое каталога снимка — прочь (сам каталог — вызывающего)."""
    try:
        children = list(dest.iterdir())
    except OSError:
        return
    for p in children:
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)


def _is_sqlite(path: Path) -> bool:
    with path.open("rb") as f:
        return f.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER


def _vacuum_into(src: Path, dest: Path) -> None:
    conn = sqlite3.connect(src.as_uri() + "?mode=ro", uri=True, isolation_level=None)
    try:
        conn.execute("VACUUM INTO ?", (str(dest),))
    finally:
        conn.close()


def _sqlite_copy(src: Path, dest: Path) -> None:
    """К4: живой SQLite (Telethon пишет свою сессию, мост WhatsApp — в WAL) — backup API одним шагом
    (`pages=-1`): копия согласована на момент чтения, вместе с ещё не перенесённым WAL."""
    source = sqlite3.connect(src.as_uri() + "?mode=ro", uri=True, timeout=SQLITE_BUSY_TIMEOUT_S)
    try:
        target = sqlite3.connect(dest, isolation_level=None)
        try:
            source.backup(target)
            # Копия наследует режим WAL источника (заголовок файла) — и тогда живёт двумя файлами. В снимок —
            # один цельный файл: режим отката; программа-владелец при открытии вернёт свой режим сама.
            target.execute("PRAGMA journal_mode=DELETE")
        finally:
            target.close()
    finally:
        source.close()


def _session_files(data_dir: Path) -> list[Path]:
    """Обычные файлы каталогов сессий (относительно `data_dir`), без симлинков и спутников SQLite."""
    out: list[Path] = []
    for name in SESSION_DIRS:
        top = data_dir / name
        if top.is_symlink() or not top.is_dir():
            continue
        for root, dirs, files in os.walk(top, followlinks=False):
            root_path = Path(root)
            dirs[:] = sorted(d for d in dirs if not (root_path / d).is_symlink())
            for fname in sorted(files):
                p = root_path / fname
                if fname.endswith(_SQLITE_SIDECARS):
                    continue
                st = os.lstat(p)
                if not stat.S_ISREG(st.st_mode):
                    continue
                out.append(p.relative_to(data_dir))
    return out


def _estimate_bytes(paths: list[Path]) -> int:
    total = 0
    for p in paths:
        for candidate in (p, p.with_name(p.name + "-wal")):
            try:
                total += candidate.stat().st_size
            except OSError:
                pass
    return total


def _ensure_parent(dest_root: Path, rel: Path) -> None:
    cur = dest_root
    for part in rel.parent.parts:
        cur = cur / part
        if not cur.exists():
            cur.mkdir(mode=0o700)


def make_snapshot_sync(*, db: Db, config_dir: Path, dest: Path, now_ms: int) -> dict:
    """Наполнить `dest` снимком. Бросает `SnapshotBadDir` / `SnapshotBusy` / `SnapshotFailed`; в последнем
    случае каталог уже пуст."""
    if not _LOCK.acquire(blocking=False):
        raise SnapshotBusy
    try:
        _check_dest(dest)
        try:
            return _fill(db=db, config_dir=config_dir, dest=dest, now_ms=now_ms)
        except Exception as exc:  # noqa: BLE001 — любой сбой: каталог чистится, наружу только вид сбоя
            _wipe(dest)
            reason = str(exc) if isinstance(exc, SnapshotFailed) else exc.__class__.__name__
            _logger.warning("snapshot failed: %s", reason)
            raise SnapshotFailed(reason) from exc
    finally:
        _LOCK.release()


def _fill(*, db: Db, config_dir: Path, dest: Path, now_ms: int) -> dict:
    started = time.monotonic()
    data_dir = db.data_dir
    sessions = _session_files(data_dir)
    # Порядок БД — work, затем main (как `backup._db_files`): ссылки идут из work в main, а копия main, снятая
    # позже, содержит родителей всех строк копии work.
    dbs = [db.work_path, db.main_path]
    need = _estimate_bytes(dbs) + _estimate_bytes([data_dir / rel for rel in sessions]) + FREE_MARGIN_BYTES
    if shutil.disk_usage(dest).free < need:
        raise SnapshotFailed("no space")

    files: list[str] = []
    # Правка финального ревью Ф33 (17.10): сначала сессии мессенджеров, потом БД. Курсор сессии, снятый раньше
    # базы, не новее её: после восстановления источник доотдаст сообщения, которые база уже знает, — их отсеет
    # `ext_id`; обратный порядок дал бы сессию новее базы — пропуск сообщений между двумя копиями.
    for rel in sessions:
        src = data_dir / rel
        _ensure_parent(dest, rel)
        target = dest / rel
        if _is_sqlite(src):
            _sqlite_copy(src, target)
        else:
            shutil.copyfile(src, target, follow_symlinks=False)
        os.chmod(target, 0o600)
        files.append(str(rel))

    for src in dbs:
        target = dest / src.name
        _vacuum_into(src, target)
        os.chmod(target, 0o600)
        files.append(src.name)

    manifest = {
        "format": MANIFEST_FORMAT,
        "ts": now_ms,
        "data_dir": str(data_dir),
        "config_dir": str(config_dir),
        "files": files,
    }
    manifest_path = dest / MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(manifest_path, 0o600)

    total = sum((dest / f).stat().st_size for f in files)
    return {"ok": True, "files": len(files), "bytes": total, "ms": int((time.monotonic() - started) * 1000)}
