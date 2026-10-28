"""Учёт нагрузки ноутбука (Ф52-1, docs/architecture.md).

Сессии на этой машине записывают, что у них идёт тяжёлое (прогон тестов, сборка, стенд), и смотрят, что идёт
у других. Хаб ничего не решает и никого не блокирует: он показывает загрузку машины и заявленное.

Главное требование: зависших записей не бывает. Запись привязана к процессу — живёт, пока жив `pid` с тем же
временем старта и пока не прошёл срок; чистка идёт при каждом чтении (`snapshot`) и каждой записи (`add`).
Основной путь — обёртка `hub load run … -- <команда>`: запись привязана к pid самой обёртки, она же снимает
запись при любом исходе; убита `kill -9` — снимет чистка.

Только чтение `/proc`, без внешних команд и ожиданий. Время — мс часов ядра.
"""

import os
from pathlib import Path

from .. import hostid
from . import subs

WHO_MAX = 80
WHAT_MAX = 120
MINUTES_MIN = 1
MINUTES_MAX = 240
CORES_MAX = 4096
MEM_MB_MAX = 16 * 1024 * 1024
MAX_CLAIMS = 50
# Ревью Ф52-1: bearer-токен держит не больше стольких живых записей (CLI через админ-сокет — без предела).
TOKEN_MAX_CLAIMS = 10
# Срок записи: заявленные минуты × 1,5 плюс запас — на случай, когда процесс жив, а работа давно кончилась.
EXPIRE_FACTOR = 1.5
EXPIRE_SLACK_MS = 5 * 60_000
_MINUTE_MS = 60_000
_COLUMNS = "id, who, what, cores, mem_mb, pid, started_ts, expires_ts, host"


class LoadError(Exception):
    """Отказ записи: `status` — код HTTP, `detail` — причина для вызывающего."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def proc_start(pid: int) -> int | None:
    """Время старта живого процесса `pid` (поле 22 `/proc/<pid>/stat`) или `None` — процесса нет."""
    return subs.read_proc_start(pid)


# --- машина -------------------------------------------------------------------------------------------------


def _loadavg() -> tuple[float | None, float | None]:
    try:
        parts = Path("/proc/loadavg").read_text("ascii").split()
        return float(parts[0]), float(parts[1])
    except OSError, ValueError, IndexError:
        return None, None


def _meminfo() -> tuple[int | None, int | None]:
    """(всего, доступно) в МБ из `/proc/meminfo` (значения там в кБ)."""
    found: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text("ascii").splitlines():
            name, _, rest = line.partition(":")
            if name in ("MemTotal", "MemAvailable"):
                found[name] = int(rest.split()[0]) // 1024
    except OSError, ValueError, IndexError:
        return None, None
    return found.get("MemTotal"), found.get("MemAvailable")


def system() -> dict:
    """Снимок машины: ядра, средняя загрузка за 1 и 5 минут, память. Чего не прочитать — `None`."""
    load1, load5 = _loadavg()
    mem_total, mem_available = _meminfo()
    return {
        "cores": os.cpu_count(),
        "load1": load1,
        "load5": load5,
        "mem_total_mb": mem_total,
        "mem_available_mb": mem_available,
    }


def machine_limits() -> tuple[int, int]:
    """Ревью Ф52-1: больше, чем есть у машины, заявить нельзя — (ядра, память в МБ). Чего не прочитать —
    общий потолок поля."""
    total_mb, _ = _meminfo()
    return min(os.cpu_count() or CORES_MAX, CORES_MAX), min(total_mb or MEM_MB_MAX, MEM_MB_MAX)


# --- записи -------------------------------------------------------------------------------------------------


def kernel_host() -> str:
    """Метка машины ядра ('' — назвать нельзя)."""
    return hostid.host_id() or ""


def is_foreign(host: str | None) -> bool:
    """Запись с другой машины (Ф33-6): пусто или своя метка — машина ядра."""
    return bool(host) and host != kernel_host()


def stale_ids(conn, now_ms: int) -> list[int]:
    """Записи к удалению: срок прошёл, процесса нет или у pid другое время старта. Только чтение.
    Запись другой машины (Ф33-6) — только по сроку: её pid в `/proc` ядра не ищем."""
    stale = []
    for claim_id, pid, pid_start, expires_ts, host in conn.execute(
        "SELECT id, pid, pid_start, expires_ts, host FROM main.load_claim"
    ).fetchall():
        if expires_ts <= now_ms or (not is_foreign(host) and proc_start(pid) != pid_start):
            stale.append(claim_id)
    return stale


def purge(conn, now_ms: int) -> int:
    """Чистка: удалить отжившие записи; возвращает, сколько удалено."""
    ids = stale_ids(conn, now_ms)
    conn.executemany("DELETE FROM main.load_claim WHERE id = ?", [(i,) for i in ids])
    return len(ids)


def _claim(row) -> dict:
    claim_id, who, what, cores, mem_mb, pid, started_ts, expires_ts, host = row
    minutes = round((expires_ts - started_ts - EXPIRE_SLACK_MS) / (_MINUTE_MS * EXPIRE_FACTOR))
    return {
        "id": claim_id,
        "who": who,
        "what": what,
        "cores": cores,
        "mem_mb": mem_mb,
        "pid": pid,
        "started_ts": started_ts,
        "expires_ts": expires_ts,
        "minutes": minutes,
        "host": host or kernel_host(),
    }


def read(conn, now_ms: int, *, skip=()) -> dict:
    """Ответ `GET /load` без чистки (годится читающее соединение); `skip` — id записей, которые чистка уже
    сочла отжившими: в ответ они не попадают."""
    skipped = set(skip)
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM main.load_claim ORDER BY started_ts, id"  # noqa: S608 — константа модуля
    ).fetchall()
    claims = [_claim(row) for row in rows if row[0] not in skipped]
    return {
        "now_ts": now_ms,
        "host": kernel_host(),
        "system": system(),
        "claims": claims,
        "claimed": {
            "cores": sum(c["cores"] for c in claims),
            "mem_mb": sum(c["mem_mb"] for c in claims),
        },
    }


def snapshot(conn, now_ms: int) -> dict:
    """Сначала чистка, затем `{"now_ts", "system": {...}, "claims": [...], "claimed": {"cores", "mem_mb"}}`.
    Пишущее соединение (внутри `Db.write`)."""
    purge(conn, now_ms)
    return read(conn, now_ms)


def _text(value, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LoadError(422, f"{name}: нужен непустой текст")
    value = " ".join(value.split())
    if len(value) > limit:
        raise LoadError(422, f"{name}: не длиннее {limit} символов")
    return value


def _int(value, name: str, low: int, high: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise LoadError(422, f"{name}: целое от {low} до {high}")
    return value


def live_ids(conn) -> set[int]:
    return {row[0] for row in conn.execute("SELECT id FROM main.load_claim").fetchall()}


def add(conn, *, who, what, cores, mem_mb, pid, minutes, now_ms: int, owned=None, host=None) -> dict:
    """Новая запись (после чистки). `pid` — живой процесс этой машины: время его старта читает ядро.
    `host` — метка машины клиента (Ф33-6): чужая — pid в `/proc` ядра не ищем, пределы ядер и памяти
    общие (машину клиента ядро не видит).
    `owned` — id записей того же bearer-токена (ведёт маршрут; `None` — предел на токен не действует).
    Отказы: 422 — неверные поля, больше ядер или памяти, чем у машины, процесса нет; 409 — записей уже
    `MAX_CLAIMS` либо у токена уже `TOKEN_MAX_CLAIMS` живых."""
    who = _text(who, "who", WHO_MAX)
    what = _text(what, "what", WHAT_MAX)
    if host is not None and (not isinstance(host, str) or not hostid.HOST_RE.fullmatch(host)):
        raise LoadError(422, "host: 16 hex-символов")
    foreign = is_foreign(host)
    cores_max, mem_mb_max = (CORES_MAX, MEM_MB_MAX) if foreign else machine_limits()
    cores = _int(cores, "cores", 1, cores_max)
    mem_mb = _int(mem_mb, "mem_mb", 0, mem_mb_max)
    minutes = _int(minutes, "minutes", MINUTES_MIN, MINUTES_MAX)
    pid = _int(pid, "pid", 1, 2**31 - 1)
    start = 0 if foreign else proc_start(pid)
    if start is None:
        raise LoadError(422, f"pid: процесса {pid} нет")
    purge(conn, now_ms)
    if conn.execute("SELECT COUNT(*) FROM main.load_claim").fetchone()[0] >= MAX_CLAIMS:
        raise LoadError(409, f"записей уже {MAX_CLAIMS} — больше не принимаю")
    if owned is not None and len(set(owned) & live_ids(conn)) >= TOKEN_MAX_CLAIMS:
        raise LoadError(409, f"у этого токена уже {TOKEN_MAX_CLAIMS} записей — сними лишние")
    expires_ts = now_ms + int(minutes * EXPIRE_FACTOR * _MINUTE_MS) + EXPIRE_SLACK_MS
    claim_id = conn.execute(
        "INSERT INTO main.load_claim "
        "(who, what, cores, mem_mb, pid, pid_start, started_ts, expires_ts, host) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (who, what, cores, mem_mb, pid, start, now_ms, expires_ts, host if foreign else ""),
    ).lastrowid
    return _claim((claim_id, who, what, cores, mem_mb, pid, now_ms, expires_ts, host if foreign else ""))


def done(conn, claim_id: int) -> bool:
    """Снять запись; нет такой — тоже успех (`False`)."""
    return conn.execute("DELETE FROM main.load_claim WHERE id = ?", (claim_id,)).rowcount > 0
