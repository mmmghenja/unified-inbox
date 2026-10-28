import os
import shutil
import tempfile
from pathlib import Path

import pytest

from hub.core import held, session_host, transcripts
from hub.core.db import Db

# Временные папки тестов — в памяти (/dev/shm), не на диске ноутбука (владелец 14.10): каждый тест заводит свои
# базы SQLite, и на диске один файл в 31 тест писал ~195 МБ, полный прогон — десятки гигабайт; очередь NVMe
# забивалась, ноутбук вис при свободных памяти и процессоре. В памяти записи на диск нет, а папки прошедших
# тестов удаляются сразу (`tmp_path_retention_policy = "failed"` в pyproject). Подпроцессы тестов наследуют
# TMPDIR. Вернуть на диск — `HUB_TESTS_DISK_TMP=1`.
# Пути короткие: адрес unix-сокета — не длиннее 108 байт, а тесты кладут `internal.sock` в tmp_path; с
# `/dev/shm/<длинное имя>/pytest-of-…/pytest-N/popen-gwN/<имя теста>/data/internal.sock` предел превышался.
# Маленький /dev/shm (раннер CI — 64 МБ) — на диск: загрузки файлов в тестах требуют свободных 5 ГиБ
# (`media.MIN_FREE_BYTES`), иначе ядро отвечает 507.
_SHM = Path("/dev/shm")
_SHM_MIN_FREE = 8 * 1024**3


def _shm_roomy() -> bool:
    try:
        return shutil.disk_usage(_SHM).free >= _SHM_MIN_FREE
    except OSError:
        return False


_SHM_TMP = _SHM.is_dir() and not os.environ.get("HUB_TESTS_DISK_TMP") and _shm_roomy()
if _SHM_TMP:
    _tmp = _SHM / f"t{os.getuid()}"
    _tmp.mkdir(mode=0o700, exist_ok=True)
    os.environ["TMPDIR"] = str(_tmp)
    tempfile.tempdir = str(_tmp)

_OWN_BASETEMP = pytest.StashKey[str]()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    """Своя короткая базовая папка прогона `/dev/shm/ht<pid>` (вместо `/tmp/pytest-of-<user>/pytest-N`).
    Рабочие xdist получают её от главного процесса готовой. Папки умерших прогонов убираются здесь же."""
    if not _SHM_TMP or config.option.basetemp is not None or hasattr(config, "workerinput"):
        return
    for old in _SHM.glob("ht[0-9]*"):
        pid = old.name[2:]
        if pid.isdigit() and old.owner() == Path.home().owner() and not _pid_alive(int(pid)):
            shutil.rmtree(old, ignore_errors=True)
    config.option.basetemp = str(_SHM / f"ht{os.getpid()}")
    config.stash[_OWN_BASETEMP] = config.option.basetemp


def pytest_sessionfinish(session, exitstatus):
    """Прошёл без падений — базовая папка прогона уходит из памяти; упал — остаётся до следующего прогона."""
    own = session.config.stash.get(_OWN_BASETEMP, None)
    if own and exitstatus == 0:
        shutil.rmtree(own, ignore_errors=True)


@pytest.fixture
def db(tmp_path):
    instance = Db(tmp_path)
    instance.start()
    yield instance
    instance.close()


@pytest.fixture(autouse=True)
def _transcribe_worker_unseen():
    """Ф14-6b: метка «служба расшифровки жива» — в памяти процесса; каждый тест начинает как после рестарта
    ядра (служба не приходила)."""
    transcripts.reset_worker_seen()
    yield
    transcripts.reset_worker_seen()


@pytest.fixture(autouse=True)
def _held_registry_empty():
    """Ф41-2а (2.1, П2б): реестр незакрытых сессий хаба — модульная ссылка в памяти процесса; каждый тест
    начинает как процесс без приложения (реестр пуст), сборка приложения ставит реестр своей базы."""
    held.reset()
    yield
    held.reset()


@pytest.fixture(autouse=True)
def _session_host_registry_empty():
    """Ф33-1: реестр сессий другой машины — тоже модульная ссылка в памяти процесса; тест начинает с пустого
    (все сессии свои), сборка приложения ставит реестр своей базы."""
    session_host.reset()
    yield
    session_host.reset()
