"""Бэкап БД: `VACUUM INTO` (отдельное read-only соединение — писатель работает параллельно) -> шифрование
`age` -> ретенция. Спецификация: docs/architecture.md Граница «открытый текст БД на
диске» — эта граница именно здесь: `tmp`-файл с открытым текстом БД существует на диске между VACUUM INTO и
успешным `age`, удаляется в `finally` ВСЕГДА.

Приватный ключ `age` хаб не видит, не хранит и не просит — только ПУБЛИЧНЫЙ recipient в
`<config_dir>/backup.recipient` (`hub backup --set-recipient` пишет его; recipient не секрет, но файл всё
равно 0600). Без валидного recipient бэкап не делается НИКОГДА (`RecipientNotConfigured`).

Вызывается из `routes/internal.py::post_admin_backup` через `asyncio.to_thread` (весь код здесь —
блокирующий: `sqlite3`, `subprocess.run`, файловый ввод-вывод).
"""

import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..backup_recipient import read_recipient
from .db import Db

_logger = logging.getLogger(__name__)

BACKUPS_DIR_NAME = "backups"
WORK_DIR_NAME = ".work"
KEEP_SETS = 7
# Модульная переменная, не аргумент по умолчанию: тесты подменяют `backup.AGE_TIMEOUT_S` на сценарии «age
# зависает» (спека, тест 3) — имя ищется в globals() в момент вызова. 60 с, а не 600 из первой редакции спеки:
# шифрование локального файла — секунды, таймаут здесь про ЗАВИСШИЙ age, и он обязан быть заметно меньше
# `TimeoutStopSec` юнита core (180 с) — иначе `systemctl restart` посреди бэкапа кончается SIGKILL, мимо
# `finally`, с открытой копией БД на диске (находка 1 opus-критика куска 7).
AGE_TIMEOUT_S = 60.0

# Не более одного бэкапа на процесс. Замок живёт ЗДЕСЬ, в блокирующем коде, а не флагом в HTTP-ручке: поток
# `asyncio.to_thread` не отменяется вместе с запросом, и флаг, сброшенный в `finally` отменённой ручки,
# пустил бы второй бэкап, пока первый ещё пишет tmp (а вход второго вычищает «осиротевшие» `*.tmp`).
_RUN_LOCK = threading.Lock()

_SET_RE = re.compile(r"^(?P<name>[a-z]+)-(?P<label>\d{8}-\d{6})\.age$")


class BackupError(Exception):
    """Базовая ошибка бэкапа."""


class RecipientNotConfigured(BackupError):
    """Файла `backup.recipient` нет, либо в нём нет валидной строки `age1...` — 409."""


class AgeNotInstalled(BackupError):
    """Бинарник `age` не найден через `shutil.which` — 409."""


class BackupBusy(BackupError):
    """Бэкап уже идёт в этом процессе — 409."""


class BackupFailed(BackupError):
    """VACUUM/age упали, истёк таймаут, или отвалился комплект — 500. Сообщение НЕ содержит путей и
    содержимого stderr `age` (спека, раздел 7.2, пункт 5)."""


@dataclass(frozen=True)
class _DbFile:
    name: str  # 'main' | 'work' — те же логические имена, что в db.db_of()
    path: Path


def _db_files(db: Db) -> list[_DbFile]:
    """Порядок копирования — work ПЕРВЫМ, main вторым, и это существенно: два файла снимаются не одним
    снимком (`VACUUM` нельзя выполнить внутри транзакции), а ссылки через границу файлов идут из work в main
    (account_id, project_id, api_token_id) и `seq_counter` живёт в main. Копия main, снятая ПОЗЖЕ копии
    work, содержит родителей всех строк work и счётчик не меньше использованного в work; обратный порядок
    давал в восстановленном комплекте каналы work без аккаунта в main (находка 5 opus-критика)."""
    return [_DbFile("work", db.work_path), _DbFile("main", db.main_path)]


def _member_names(db: Db) -> frozenset[str]:
    return frozenset(f.name for f in _db_files(db))


def _timestamp_label(now_ms: int) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime(now_ms / 1000))


def _vacuum_into(src_path: Path, dest_path: Path) -> None:
    """ОТДЕЛЬНОЕ read-only соединение (не через `Db.reader()` — тот ATTACH-ит оба файла разом, здесь нужен
    ровно один по разу на файл). `VACUUM INTO` не пишет в источник — писатель `Db` продолжает работать
    (WAL, `busy_timeout`) параллельно."""
    conn = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True, isolation_level=None)
    try:
        conn.execute("VACUUM INTO ?", (str(dest_path),))
    finally:
        conn.close()


def _encrypt(age_bin: str, recipient: str, tmp_path: Path, dest_path: Path) -> None:
    subprocess.run(  # noqa: S603 — без shell, путь к бинарнику из shutil.which, аргументы не от пользователя
        [age_bin, "-r", recipient, "-o", str(dest_path), str(tmp_path)],
        check=True,
        timeout=AGE_TIMEOUT_S,
        capture_output=True,  # stderr age (может содержать детали ключа/пути) никогда никуда не прокидывается
    )


def _secure_dir(path: Path) -> None:
    """Каталог 0700, НЕ симлинк. `mkdir(mode=..., exist_ok=True)` прав существующего каталога не меняет, а
    подложенный симлинк принял бы молча — и открытый текст БД уехал бы за пределы `data_dir`."""
    if path.is_symlink():
        raise BackupFailed("unsafe backups dir")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise BackupFailed("unsafe backups dir")
    os.chmod(path, 0o700)


def cleanup_plaintext(data_dir: Path) -> None:
    """Убрать всё содержимое `backups/.work` — открытые копии БД от прогона, убитого мимо `finally`
    (SIGKILL, отключение питания). Зовётся при СТАРТЕ core (`runner`) и на входе в каждый бэкап ДО проверок
    recipient/age: уборка не должна зависеть от того, настроен ли бэкап сейчас. Каталогов не создаёт."""
    work_dir = data_dir / BACKUPS_DIR_NAME / WORK_DIR_NAME
    if work_dir.is_symlink():
        work_dir.unlink()
        return
    if not work_dir.is_dir():
        return
    for p in work_dir.iterdir():
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)


def _scan_sets(backups_dir: Path) -> dict[str, set[str]]:
    """label -> набор имён (`main`/`work`), для которых есть `.age` в `backups_dir` (только верхний
    уровень — `.work` туда не входит)."""
    sets: dict[str, set[str]] = {}
    for p in backups_dir.iterdir():
        if not p.is_file():
            continue
        m = _SET_RE.match(p.name)
        if m is None:
            continue
        sets.setdefault(m.group("label"), set()).add(m.group("name"))
    return sets


def _apply_retention(
    backups_dir: Path, *, member_names: frozenset[str], protect: str | None = None, keep: int = KEEP_SETS
) -> tuple[int, int]:
    """Оставить `keep` последних ПОЛНЫХ комплектов (метка времени, у которой есть `.age` для каждого имени
    из `member_names`); неполные комплекты считаются сбойными и удаляются независимо от возраста. Метка —
    `YYYYMMDD-HHMMSS` в UTC, лексикографический порядок строк совпадает с хронологическим.
    `protect` — метка комплекта ТЕКУЩЕГО прогона: он остаётся безусловно и занимает одно из `keep` мест,
    даже если часы скакнули назад и по метке он «старее» прежних (иначе свежий бэкап удалялся бы собственной
    ретенцией при ответе `ok` — находка 3 opus-критика).
    Возвращает (число оставленных комплектов, число удалённых комплектов).
    """
    sets = _scan_sets(backups_dir)
    complete_labels = sorted((lbl for lbl, names in sets.items() if names == member_names), reverse=True)
    if protect is not None and protect in complete_labels:
        complete_labels.remove(protect)
        complete_labels.insert(0, protect)
    keep_labels = set(complete_labels[:keep])

    removed = 0
    for label, names in sets.items():
        if label in keep_labels:
            continue
        for name in names:
            (backups_dir / f"{name}-{label}.age").unlink(missing_ok=True)
        removed += 1

    return len(keep_labels), removed


def run_backup_sync(*, db: Db, config_dir: Path, now_ms: int) -> dict:
    """Весь блокирующий ход бэкапа (спека, раздел 7.2, пункты 1-6, кроме записи в `audit` — та делается
    вызывающим внутри `Db.write()`, эта функция БД на запись не трогает вовсе). Бросает
    `RecipientNotConfigured` / `AgeNotInstalled` / `BackupBusy` / `BackupFailed`; в последнем случае уже
    подчистила и `tmp`, и неполный комплект."""
    if not _RUN_LOCK.acquire(blocking=False):
        raise BackupBusy
    try:
        return _run_backup_locked(db=db, config_dir=config_dir, now_ms=now_ms)
    finally:
        _RUN_LOCK.release()


def _run_backup_locked(*, db: Db, config_dir: Path, now_ms: int) -> dict:
    # ДО проверок recipient/age: ответ 409 не должен оставлять осиротевшую открытую копию лежать дальше.
    cleanup_plaintext(db.data_dir)

    recipient = read_recipient(config_dir)
    if recipient is None:
        raise RecipientNotConfigured
    age_bin = shutil.which("age")
    if age_bin is None:
        raise AgeNotInstalled

    backups_dir = db.data_dir / BACKUPS_DIR_NAME
    work_dir = backups_dir / WORK_DIR_NAME
    try:
        _secure_dir(backups_dir)
        _secure_dir(work_dir)
    except BackupFailed as exc:
        _logger.warning("backup failed: %s", exc)
        raise

    label = _timestamp_label(now_ms)
    member_names = _member_names(db)
    produced: list[dict] = []
    failure: str | None = None

    # Каталог прогона — `mkdtemp` (0700, создан этим процессом): `VACUUM INTO` создаёт файл сам, с правами
    # 0644 независимо от umask, и до `chmod` его закрывает именно этот каталог, а не чьи-то прежние права.
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=work_dir))
    try:
        for f in _db_files(db):
            tmp_path = run_dir / f"{f.name}-{label}.tmp"
            dest_path = backups_dir / f"{f.name}-{label}.age"
            try:
                _vacuum_into(f.path, tmp_path)
                os.chmod(tmp_path, 0o600)
                _encrypt(age_bin, recipient, tmp_path, dest_path)
                os.chmod(dest_path, 0o600)
                produced.append({"name": f.name, "bytes": dest_path.stat().st_size})
            except subprocess.TimeoutExpired:
                failure = "age timeout"
            except subprocess.CalledProcessError:
                failure = "age failed"
            except Exception as exc:  # noqa: BLE001 — sqlite3.Error из VACUUM, OSError и всё прочее: любой сбой
                # обязан пройти через уборку неполного комплекта ниже; наружу — только имя класса, без путей.
                failure = exc.__class__.__name__
            finally:
                # tmp — ВСЕГДА, что бы ни случилось (успех, ошибка age, таймаут, исключение): открытый текст
                # БД не должен пережить попытку бэкапа, даже неудачную.
                tmp_path.unlink(missing_ok=True)
            if failure is not None:
                dest_path.unlink(missing_ok=True)  # недописанный `.age` упавшего файла — явно
                break
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)  # вместе с побочными файлами SQLite, если такие остались

    if failure is None:
        kept, removed = _apply_retention(backups_dir, member_names=member_names, protect=label)
        if all((backups_dir / f"{name}-{label}.age").is_file() for name in member_names):
            return {"ok": True, "files": produced, "kept": kept, "removed": removed}
        failure = "set missing after retention"  # `ok` без комплекта на диске не отвечаем никогда

    # Неполный комплект этого прогона — сбойный, убираем явно; заодно подметаем неполные комплекты,
    # оставшиеся от возможных прошлых падений (жёсткое SIGKILL между VACUUM и retention).
    for item in produced:
        (backups_dir / f"{item['name']}-{label}.age").unlink(missing_ok=True)
    _apply_retention(backups_dir, member_names=member_names)
    # Причина — в журнал и (у вызывающего) в audit: это вид сбоя, не путь и не stderr age. Без неё опечатка в
    # recipient давала голый 500, по которому нечего чинить (находка 8 opus-критика).
    _logger.warning("backup failed: %s", failure)
    raise BackupFailed(failure)
