"""Файлы и картинки во вход постоянной сессии хаба (Ф41-3в, docs/architecture.md).

Владелец загружает файл в сессию (`POST /sessions/{id}/files`), потом прикладывает его к реплике (`files` в
`say`); хост забирает байты приложенного (`GET /work/host/session-files/{id}`) и подаёт модели.

Строка `held_file` — в файле сессии (`hub_session.store`), id — случайные 128 бит hex. Байты —
`<data_dir>/held-files/<sha[:2]>/<sha>` (0600, каталоги 0700), один файл на содержимое: удаляется, когда на
его `sha256` не осталось нестёртых строк ни в `main`, ни в `work`. Удаление и появление файла склада идут
только в потоке-писателе (`commit`, `_purge`) — гонки «стёрли то, что только что приложили» нет.

Граница: имя приходит из запроса и дальше попадает в ленту, в текст реплики и (на хосте) в путь рабочей
папки. Здесь оно чистится (`clean_name`); хост чистит ещё раз сам и пишет приёмами Ф22-1. Тип — только по
сигнатуре байтов. В аудит и журнал — без имён и содержимого.

Функции с `conn` зовутся внутри `Db.write()`, кроме помеченных «читатель». Отказы — `SessionError` с готовым
русским текстом.

23.10: файлы реплик разговора «Мой агент» — `main.talk_file` (раздел «разговор с агентом» внизу),
тот же склад байтов и то же правило ссылок. Загружает и прикладывает только владелец (кука), байты приложенного
забирает агент владельца (`GET /owner_channel/talk/files/{id}`) и показывает интерфейс. Неприложенные стираются через
24 ч, приложенные — через 30 дней после загрузки."""

import contextlib
import logging
import os
import re
import secrets
import time
import unicodedata
from pathlib import Path

from . import held as held_mod
from . import media as media_mod
from . import uploads as uploads_mod
from .hub_sessions import SessionError

_logger = logging.getLogger(__name__)

MIB = 1024 * 1024
MAX_BYTES = 20 * MIB  # один файл; больше — 413
MAX_PENDING = 30  # неприложенных файлов на сессию; больше — 409
SAY_MAX_FILES = 10
SAY_MAX_BYTES = 30 * MIB
TTL_MS = 24 * 60 * 60 * 1000
TALK_TTL_MS = 30 * TTL_MS  # приложенный к реплике разговора с агентом
NAME_MAX = 200
EXT_MAX = 16
DIR_NAME = "held-files"
TMP_PREFIX = ".tmp-"
TMP_MAX_AGE_S = 3600
KINDS = ("image", "pdf", "file")
FILES_REFUSED = "files: файл не найден или уже отправлен"

_IMAGE = frozenset({"png", "jpeg", "gif", "webp"})
_ID_RE = re.compile(r"[0-9a-f]{32}")
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_DROP = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
_COLS = ("id", "name", "mime", "size", "kind")
_EDGE = ". " + chr(0xA0)  # точки, пробелы и неразрывный пробел по краям имени


def _sql(schema: str, template: str, **parts: str) -> str:
    """Схема (`main`/`work`) и доверенные фрагменты подставляются здесь; значения — только параметрами."""
    return template.format(schema=schema, **parts)


def new_id() -> str:
    return secrets.token_hex(16)


def valid_id(file_id) -> bool:
    return isinstance(file_id, str) and _ID_RE.fullmatch(file_id) is not None


def clean_name(raw: str | None) -> str:
    """Базовое имя: без пути (оба вида разделителей), без управляющих и невидимых символов (Cc — в том числе
    перевод строки, Cf, Cs, Zl, Zp), без ведущих точек и пробелов, без точек и пробелов в конце, не длиннее
    200 (режется основа, короткое расширение остаётся); пустое — `file`."""
    value = unicodedata.normalize("NFC", raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    value = "".join(ch for ch in value if unicodedata.category(ch) not in _DROP)
    value = value.strip(_EDGE)
    if len(value) > NAME_MAX:
        stem, dot, ext = value.rpartition(".")
        if stem and 1 <= len(ext) <= EXT_MAX:
            value = stem[: NAME_MAX - len(ext) - 1].rstrip(_EDGE) + dot + ext
        else:
            value = value[:NAME_MAX]
        value = value.strip(_EDGE)
    return value or "file"


def kind_of(detected: str) -> str:
    if detected in _IMAGE:
        return "image"
    return "pdf" if detected == "pdf" else "file"


def mime_of(detected: str) -> str:
    return uploads_mod.mime_of(detected)


# --- склад --------------------------------------------------------------------------------------------------


def root(data_dir: Path) -> Path:
    return Path(data_dir) / DIR_NAME


def file_path(data_dir: Path, sha256: str) -> Path:
    if _SHA_RE.fullmatch(sha256 or "") is None:
        raise ValueError("sha256")
    return root(data_dir) / sha256[:2] / sha256


def _ensure_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)


def tmp_path(data_dir: Path) -> Path:
    """Временный файл приёма — в корне склада (тот же раздел диска, что и место назначения)."""
    _ensure_dir(root(data_dir))
    return root(data_dir) / f"{TMP_PREFIX}{secrets.token_hex(16)}"


def _refs(conn, sha256: str) -> int:
    total = 0
    for schema in held_mod.STORES:
        total += conn.execute(
            _sql(schema, "SELECT COUNT(*) FROM {schema}.held_file WHERE sha256 = ? AND purged_ts IS NULL"),
            (sha256,),
        ).fetchone()[0]
    # 23.10: тот же склад у файлов разговора «Мой агент».
    total += conn.execute(
        "SELECT COUNT(*) FROM main.talk_file WHERE sha256 = ? AND purged_ts IS NULL", (sha256,)
    ).fetchone()[0]
    return total


def _release_bytes(conn, sha256: str) -> None:
    """Правило ссылок: после изменения строк в ЭТОЙ транзакции — нет нестёртых строк на `sha256`, файл
    удаляется."""
    if _refs(conn, sha256):
        return
    try:
        file_path(media_mod.data_dir_of(conn), sha256).unlink(missing_ok=True)
    except (OSError, ValueError) as exc:
        _logger.warning("файлы сессий: файл не удалён (%s)", type(exc).__name__)


def _purge(conn, schema: str, where: str, params: tuple, now_ms: int) -> int:
    rows = conn.execute(
        _sql(
            schema,
            "SELECT id, sha256 FROM {schema}.held_file WHERE purged_ts IS NULL AND {where}",
            where=where,
        ),
        params,
    ).fetchall()
    for file_id, _sha in rows:
        conn.execute(
            _sql(schema, "UPDATE {schema}.held_file SET purged_ts = ? WHERE id = ?"), (now_ms, file_id)
        )
    for sha in {r[1] for r in rows}:
        _release_bytes(conn, sha)
    return len(rows)


# --- загрузка и удаление (владелец) -----------------------------------------------------------------------------


def pending_count(conn, store: str, session_id: int) -> int:
    return conn.execute(
        _sql(
            store,
            "SELECT COUNT(*) FROM {schema}.held_file WHERE session_id = ? AND say_id IS NULL "
            "AND purged_ts IS NULL",
        ),
        (session_id,),
    ).fetchone()[0]


def check_room(conn, store: str | None, session_id: int) -> None:
    """Читатель и писатель: неприложенных файлов у сессии меньше 30, иначе 409."""
    if store is not None and pending_count(conn, store, session_id) >= MAX_PENDING:
        raise SessionError(409, f"неприложенных файлов уже {MAX_PENDING} — отправьте или уберите лишние")


def commit(
    conn,
    store: str,
    session_id: int,
    *,
    file_id: str,
    tmp: Path,
    sha256: str,
    size: int,
    detected: str,
    name: str,
    now_ms: int,
) -> dict:
    """Принятый временный файл -> склад (по sha256; такой уже лежит — временный удаляется) и строка."""
    check_room(conn, store, session_id)
    final = file_path(media_mod.data_dir_of(conn), sha256)
    _ensure_dir(final.parent)
    if final.is_file() and not final.is_symlink() and final.stat().st_size == size:
        tmp.unlink(missing_ok=True)
    else:
        os.replace(tmp, final)
    row = {"id": file_id, "name": name, "size": size, "mime": mime_of(detected), "kind": kind_of(detected)}
    conn.execute(
        _sql(
            store,
            "INSERT INTO {schema}.held_file (id, session_id, sha256, size, mime, kind, name, created_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ),
        (file_id, session_id, sha256, size, row["mime"], row["kind"], name, now_ms),
    )
    return row


def remove(conn, store: str | None, session_id: int, file_id: str) -> dict:
    """Убрать неприложенный файл: строка удаляется, байты — по правилу ссылок. Нет такого у ЭТОЙ сессии
    (или уже стёрт) — 404; приложен к реплике — 409."""
    row = None
    if store is not None and valid_id(file_id):
        row = conn.execute(
            _sql(
                store,
                "SELECT sha256, say_id, size, kind FROM {schema}.held_file WHERE id = ? AND session_id = ? "
                "AND purged_ts IS NULL",
            ),
            (file_id, session_id),
        ).fetchone()
    if row is None:
        raise SessionError(404, "not found")
    if row[1] is not None:
        raise SessionError(409, "файл уже отправлен с репликой")
    conn.execute(_sql(store, "DELETE FROM {schema}.held_file WHERE id = ?"), (file_id,))
    _release_bytes(conn, row[0])
    return {"size": row[2], "kind": row[3]}


# --- реплика ------------------------------------------------------------------------------------------------


def check_ids(file_ids) -> list[str]:
    """Список id из `say`: до 10, без повторов, каждый — id файла. Иначе 422."""
    if len(file_ids) > SAY_MAX_FILES:
        raise SessionError(422, f"files: не больше {SAY_MAX_FILES} файлов в реплике")
    if len(set(file_ids)) != len(file_ids):
        raise SessionError(422, "files: файл указан дважды")
    if not all(valid_id(i) for i in file_ids):
        raise SessionError(422, FILES_REFUSED)
    return list(file_ids)


def pick(conn, store: str, session_id: int, file_ids: list[str]) -> list[dict]:
    """Файлы ЭТОЙ сессии по списку id: не приложенные и не стёртые — иначе 422 (чужой файл, файл другой
    сессии и несуществующий неотличимы). В сумме не больше 30 МиБ. Порядок — порядок загрузки (он же у
    выдачи хосту, `of_says`), а не порядок в запросе."""
    found: list[tuple[int, dict]] = []
    for file_id in check_ids(file_ids):
        row = conn.execute(
            _sql(
                store,
                "SELECT id, name, mime, size, kind, rowid FROM {schema}.held_file WHERE id = ? "
                "AND session_id = ? AND say_id IS NULL AND purged_ts IS NULL",
            ),
            (file_id, session_id),
        ).fetchone()
        if row is None:
            raise SessionError(422, FILES_REFUSED)
        found.append((row[5], dict(zip(_COLS, row[:5], strict=True))))
    files = [item for _rowid, item in sorted(found, key=lambda pair: pair[0])]
    if sum(f["size"] for f in files) > SAY_MAX_BYTES:
        raise SessionError(422, f"files: в сумме не больше {SAY_MAX_BYTES // MIB} МиБ")
    return files


def attach(conn, store: str, session_id: int, files: list[dict], say_id: int) -> None:
    for item in files:
        done = conn.execute(
            _sql(
                store,
                "UPDATE {schema}.held_file SET say_id = ? WHERE id = ? AND session_id = ? AND say_id IS NULL "
                "AND purged_ts IS NULL",
            ),
            (say_id, item["id"], session_id),
        ).rowcount
        if done != 1:
            raise SessionError(422, FILES_REFUSED)


def of_says(conn, store: str, say_ids: list[int]) -> dict[int, list[dict]]:
    """Файлы реплик для хоста: `{say_id: [{id, name, mime, size, kind}]}` в порядке загрузки. Стёртые не
    отдаются (байтов уже нет)."""
    found: dict[int, list[dict]] = {}
    for say_id in say_ids:
        rows = conn.execute(
            _sql(
                store,
                "SELECT id, name, mime, size, kind FROM {schema}.held_file WHERE say_id = ? "
                "AND purged_ts IS NULL ORDER BY rowid",
            ),
            (say_id,),
        ).fetchall()
        if rows:
            found[say_id] = [dict(zip(_COLS, r, strict=True)) for r in rows]
    return found


def release(conn, store: str, say_id: int, now_ms: int) -> None:
    """Реплика доставлена или снята: отсюда считаются 24 часа до стирания байтов её файлов."""
    conn.execute(
        _sql(store, "UPDATE {schema}.held_file SET released_ts = ? WHERE say_id = ? AND released_ts IS NULL"),
        (now_ms, say_id),
    )


# --- выдача хосту -------------------------------------------------------------------------------------------


def host_file(conn, file_id: str) -> tuple[Path, int]:
    """Читатель: `(путь, размер)` файла для хоста — только приложенный к реплике, не стёртый, незакрытой
    сессии, чья строка лежит в файле своей сессии. Иначе 404."""
    if valid_id(file_id):
        for schema in held_mod.STORES:
            row = conn.execute(
                _sql(
                    schema,
                    "SELECT f.sha256, f.size FROM {schema}.held_file f JOIN main.hub_session s "
                    "ON s.id = f.session_id AND s.store = '{schema}' WHERE f.id = ? AND f.say_id IS NOT NULL "
                    "AND f.purged_ts IS NULL AND s.closed_ts IS NULL",
                ),
                (file_id,),
            ).fetchone()
            if row is not None:
                return file_path(media_mod.data_dir_of(conn), row[0]), row[1]
    raise SessionError(404, "not found")


# --- чистка -------------------------------------------------------------------------------------------------


def purge_session(conn, store: str | None, session_id: int, now_ms: int) -> int:
    """Сессия закрыта — байты всех её файлов стираются сразу."""
    if store is None:
        return 0
    return _purge(conn, store, "session_id = ?", (session_id,), now_ms)


def cleanup(conn, *, now_ms: int) -> int:
    """Часовой проход (там же, где `uploads.cleanup`): неприложенные — через 24 ч после загрузки;
    приложенные — через 24 ч после того, как реплика доставлена или снята; файлы закрытых сессий — сразу
    (страховка: закрытие стирает само). Плюс брошенные временные файлы приёма и байты без строки (откат
    загрузки) старше часа. Возвращает число стёртых строк."""
    cutoff = now_ms - TTL_MS
    purged = 0
    for schema in held_mod.STORES:
        purged += _purge(
            conn,
            schema,
            "((say_id IS NULL AND created_ts < ?) OR (say_id IS NOT NULL AND released_ts IS NOT NULL "
            "AND released_ts < ?) OR session_id IN (SELECT id FROM main.hub_session "
            "WHERE closed_ts IS NOT NULL))",
            (cutoff, cutoff),
            now_ms,
        )
    purged += _talk_purge(
        conn,
        "(talk_id IS NULL AND created_ts < ?) OR (talk_id IS NOT NULL AND created_ts < ?)",
        (cutoff, now_ms - TALK_TTL_MS),
        now_ms,
    )
    _sweep_tmp(media_mod.data_dir_of(conn))
    _sweep_orphans(conn)
    return purged


def _sweep_orphans(conn) -> None:
    """Ревью Ф41-3в: байты без строки. `commit` кладёт файл в склад ДО вставки строки; если транзакция
    загрузки потом откатилась, файл остался бы навсегда. Файл склада старше часа, на `sha256` которого нет
    нестёртых строк ни в одном из двух файлов, удаляется (проход идёт в потоке-писателе, как и `commit`)."""
    base = root(media_mod.data_dir_of(conn))
    limit = time.time() - TMP_MAX_AGE_S
    try:
        shelves = [d for d in os.listdir(base) if len(d) == 2]
    except OSError:
        return
    for shelf in shelves:
        try:
            names = os.listdir(base / shelf)
        except OSError:
            continue
        for name in names:
            if _SHA_RE.fullmatch(name) is None or not name.startswith(shelf):
                continue
            path = base / shelf / name
            with contextlib.suppress(OSError):
                if path.lstat().st_mtime < limit and not _refs(conn, name):
                    path.unlink()


def _sweep_tmp(data_dir: Path) -> None:
    base = root(data_dir)
    try:
        names = os.listdir(base)
    except OSError:
        return
    limit = time.time() - TMP_MAX_AGE_S
    for name in names:
        if not name.startswith(TMP_PREFIX):
            continue
        with contextlib.suppress(OSError):
            path = base / name
            st = path.lstat()
            if st.st_mtime < limit:
                path.unlink()


# --- разговор с агентом (23.10) -----------------------------------------------------------------------------

_TALK_COLS = "id, name, mime, size, kind"


def _talk_purge(conn, where: str, params: tuple, now_ms: int) -> int:
    rows = conn.execute(
        f"SELECT id, sha256 FROM main.talk_file WHERE purged_ts IS NULL AND ({where})",  # noqa: S608
        params,
    ).fetchall()
    for file_id, _sha in rows:
        conn.execute("UPDATE main.talk_file SET purged_ts = ? WHERE id = ?", (now_ms, file_id))
    for sha in {r[1] for r in rows}:
        _release_bytes(conn, sha)
    return len(rows)


def talk_room(conn) -> None:
    """Читатель и писатель: неприложенных файлов разговора меньше 30, иначе 409."""
    count = conn.execute(
        "SELECT COUNT(*) FROM main.talk_file WHERE talk_id IS NULL AND purged_ts IS NULL"
    ).fetchone()[0]
    if count >= MAX_PENDING:
        raise SessionError(409, f"неприложенных файлов уже {MAX_PENDING} — отправьте или уберите лишние")


def talk_commit(
    conn, *, file_id: str, tmp: Path, sha256: str, size: int, detected: str, name: str, now_ms: int
) -> dict:
    """Принятый временный файл -> склад и строка `talk_file` (как `commit` у сессии)."""
    talk_room(conn)
    final = file_path(media_mod.data_dir_of(conn), sha256)
    _ensure_dir(final.parent)
    if final.is_file() and not final.is_symlink() and final.stat().st_size == size:
        tmp.unlink(missing_ok=True)
    else:
        os.replace(tmp, final)
    row = {"id": file_id, "name": name, "size": size, "mime": mime_of(detected), "kind": kind_of(detected)}
    conn.execute(
        "INSERT INTO main.talk_file (id, sha256, size, mime, kind, name, created_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (file_id, sha256, size, row["mime"], row["kind"], name, now_ms),
    )
    return row


def talk_remove(conn, file_id: str) -> None:
    """Убрать неприложенный файл разговора. Нет или стёрт — 404; приложен — 409."""
    row = None
    if valid_id(file_id):
        row = conn.execute(
            "SELECT sha256, talk_id FROM main.talk_file WHERE id = ? AND purged_ts IS NULL", (file_id,)
        ).fetchone()
    if row is None:
        raise SessionError(404, "not found")
    if row[1] is not None:
        raise SessionError(409, "файл уже отправлен с репликой")
    conn.execute("DELETE FROM main.talk_file WHERE id = ?", (file_id,))
    _release_bytes(conn, row[0])


def talk_pick(conn, file_ids) -> list[dict]:
    """Неприложенные файлы разговора по списку id (до 10, в сумме до 30 МиБ), в порядке загрузки. Иначе
    422."""
    found: list[tuple[int, dict]] = []
    for file_id in check_ids(file_ids):
        row = conn.execute(
            f"SELECT {_TALK_COLS}, rowid FROM main.talk_file WHERE id = ? AND talk_id IS NULL "  # noqa: S608
            "AND purged_ts IS NULL",
            (file_id,),
        ).fetchone()
        if row is None:
            raise SessionError(422, FILES_REFUSED)
        found.append((row[5], dict(zip(_COLS, row[:5], strict=True))))
    files = [item for _rowid, item in sorted(found, key=lambda pair: pair[0])]
    if sum(f["size"] for f in files) > SAY_MAX_BYTES:
        raise SessionError(422, f"files: в сумме не больше {SAY_MAX_BYTES // MIB} МиБ")
    return files


def talk_attach(conn, files: list[dict], talk_id: int) -> None:
    for item in files:
        done = conn.execute(
            "UPDATE main.talk_file SET talk_id = ? WHERE id = ? AND talk_id IS NULL AND purged_ts IS NULL",
            (talk_id, item["id"]),
        ).rowcount
        if done != 1:
            raise SessionError(422, FILES_REFUSED)


def of_talks(conn, talk_ids: list[int]) -> dict[int, list[dict]]:
    """Читатель: файлы реплик разговора `{talk_id: [{id, name, mime, size, kind, gone}]}` в порядке загрузки;
    стёртые — с `gone: true` (имя помнится, байтов нет)."""
    found: dict[int, list[dict]] = {}
    if not talk_ids:
        return found
    marks = ",".join("?" * len(talk_ids))
    rows = conn.execute(
        f"SELECT talk_id, {_TALK_COLS}, purged_ts FROM main.talk_file "  # noqa: S608
        f"WHERE talk_id IN ({marks}) ORDER BY rowid",
        tuple(talk_ids),
    ).fetchall()
    for row in rows:
        item = dict(zip(_COLS, row[1:6], strict=True))
        item["gone"] = row[6] is not None
        found.setdefault(row[0], []).append(item)
    return found


def talk_file(conn, file_id: str) -> tuple[Path, int, str, str, str]:
    """Читатель: `(путь, размер, mime, kind, имя)` приложенного к реплике и не стёртого файла разговора.
    Иначе 404."""
    if valid_id(file_id):
        row = conn.execute(
            "SELECT sha256, size, mime, kind, name FROM main.talk_file WHERE id = ? AND talk_id IS NOT NULL "
            "AND purged_ts IS NULL",
            (file_id,),
        ).fetchone()
        if row is not None:
            return file_path(media_mod.data_dir_of(conn), row[0]), row[1], row[2], row[3], row[4]
    raise SessionError(404, "not found")
