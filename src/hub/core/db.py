"""Слой БД: два файла SQLite, один поток-писатель на процесс, читатели read-only.

Почему два файла и одна очередь записи — docs/architecture.md) и docs/architecture.md.
Коротко: `hub.db` (main) и `hub-work.db` (work, корпоративные источники, ATTACH) — триггеры и FTS external
content не работают через границу файлов, поэтому у каждого файла своя полная копия «общих» таблиц, а
ссылки через границу (account_id, project_id, api_token_id) — без FOREIGN KEY, их проверяет код.
"""

import asyncio
import concurrent.futures
import contextlib
import json
import logging
import queue
import sqlite3
import threading
import time
import weakref
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TypeVar

_logger = logging.getLogger(__name__)

WORK_ID_BASE = 2**40  # id >= WORK_ID_BASE лежит в hub-work.db; sqlite_sequence там засеян этим значением.

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_F57_BACKLOG_KEY = "f57_mail_backlog_projects"  # пишет `0086_main.sql`, снимает `Db._f57_mail_backlog`
_F66_BRANCHES_KEY = "f66_talk_branches"  # пишет `0098_main.sql`, снимает `Db._f66_talk_branches`
_PRAGMAS = (
    # auto_vacuum ДО journal_mode: если сначала включить WAL, auto_vacuum=INCREMENTAL молча не применяется
    # (проверено эмпирически на sqlite3 из stdlib) — порядок здесь важен, не только «до первой таблицы».
    "PRAGMA auto_vacuum=INCREMENTAL",
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA journal_size_limit=67108864",
    "PRAGMA foreign_keys=ON",
)
# Таблицы из *_shared.sql со своим AUTOINCREMENT id (то, что реально нужно развести по диапазонам id).
# `agent_item` — 0009_shared.sql (спека 7b, П3): засев идёт по этому списку, а не по AUTOINCREMENT.
_SHARED_TABLES_WITH_OWN_ID = (
    "contact",
    "channel",
    "thread",
    "outbox",
    "message",
    "dead_letter",
    "agent_item",
    "media",  # Ф8-5 П1: 0011_shared.sql
    "agent_run",  # Ф8-6 A1: 0013_shared.sql
    # Ф9-1 (9a), «Мелочи»: рабочие сессии — 0014_shared.sql.
    "work_task",
    "work_event",
    "work_request",
    "work_say",
    # Ф9-3 (9c), раздел B: чат оркестратора — 0015_shared.sql (строки — всегда в work).
    "orch_conversation",
    "orch_message",
    "person_fact",  # Ф24-1 A1: факты о людях — 0038_shared.sql
    # Ф41-2б (2.1): лента, запросы и реплики постоянной сессии хаба — 0057_shared.sql.
    "held_event",
    "held_request",
    "held_say",
    "decision_log",  # Ф76-1: журнал решений по карточкам — 0106_shared.sql
    "digest",  # Ф86-1: сводка дня проекта — 0109_shared.sql
)

T = TypeVar("T")
_WriteFn = Callable[[sqlite3.Connection], T]


def db_of(id_: int) -> str:
    """К какому файлу относится строка с этим id: 'main' или 'work'."""
    return "work" if id_ >= WORK_ID_BASE else "main"


def next_seq(conn: sqlite3.Connection) -> int:
    """Следующее значение глобального seq. Вызывать только в потоке-писателе, внутри транзакции write()."""
    row = conn.execute("UPDATE seq_counter SET value = value + 1 WHERE id = 1 RETURNING value").fetchone()
    return row[0]


def reconcile_person_seq(conn: sqlite3.Connection) -> None:
    """Ф21-1 П9: ATTACH в WAL не атомарен между файлами — `work.contact.person_id` мог закоммититься, а строка
    `main.person` (и счётчик AUTOINCREMENT) — нет. Поднять `sqlite_sequence` для `person` до наибольшего
    `person_id` контактов обоих файлов, чтобы новый человек не получил id «висящей» ссылки. Внутри транзакции
    вызывающего; без таблиц людей (прежние версии схемы в тестах) — ничего."""
    tables = {r[0] for r in conn.execute("SELECT name FROM main.sqlite_master WHERE type = 'table'")}
    if "person" not in tables:
        return
    used = 0
    for schema in ("main", "work"):
        columns = {r[1] for r in conn.execute(f"PRAGMA {schema}.table_info(contact)")}
        if "person_id" in columns:
            used = max(
                used,
                conn.execute(
                    f"SELECT COALESCE(MAX(person_id), 0) FROM {schema}.contact"  # noqa: S608
                ).fetchone()[0],
            )
    if not used:
        return
    row = conn.execute("SELECT seq FROM main.sqlite_sequence WHERE name = 'person'").fetchone()
    if row is None:
        conn.execute("INSERT INTO main.sqlite_sequence (name, seq) VALUES ('person', ?)", (used,))
    elif row[0] < used:
        conn.execute("UPDATE main.sqlite_sequence SET seq = ? WHERE name = 'person'", (used,))


def _register_functions(conn: sqlite3.Connection) -> None:
    """Ф8-3 П5: режим беседы — функция SQLite (`attention.register`). Импорт внутри: `attention` тянет
    `threads`, а тот — `next_seq` из этого модуля (цикл импорта на верхнем уровне)."""
    from . import attention

    attention.register(conn)


def _set_pragmas(conn: sqlite3.Connection) -> None:
    for pragma in _PRAGMAS:
        conn.execute(pragma)


def _statements(sql: str) -> Iterator[str]:
    """Порезать скрипт на операторы: executescript() сам коммитит и рвёт транзакцию миграции."""
    buf = ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            yield buf
            buf = ""
    if buf.strip():
        raise ValueError("незавершённый оператор в конце миграции")


def _seed_work_sequence(conn: sqlite3.Connection) -> None:
    """id в work глобально уникальны: подкручиваем sqlite_sequence, чтобы первый insert дал WORK_ID_BASE+1."""
    for table in _SHARED_TABLES_WITH_OWN_ID:
        # У sqlite_sequence нет UNIQUE — `INSERT OR IGNORE` ничего не игнорировал, и каждая shared-миграция
        # плодила бы дубли строк (находка 9 критика 1a, проявилась на 0004). Вставка — только если нет.
        conn.execute(
            "INSERT INTO sqlite_sequence (name, seq) SELECT ?, ? "
            "WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = ?)",
            (table, WORK_ID_BASE, table),
        )


def _migrations(*, main: bool) -> dict[int, list[Path]]:
    """Версия -> файлы. `NNNN_main.sql` — только hub.db, `NNNN_shared.sql` — оба файла; main раньше shared."""
    kinds = ("main", "shared") if main else ("shared",)
    found: dict[int, list[Path]] = {}
    for kind in kinds:
        for path in sorted(_MIGRATIONS_DIR.glob(f"[0-9][0-9][0-9][0-9]_{kind}.sql")):
            found.setdefault(int(path.name[:4]), []).append(path)
    return dict(sorted(found.items()))


def _init_file(path: Path, *, main: bool) -> None:
    """Создать/смигрировать один файл БД.

    Версия — `PRAGMA user_version` самого файла: main и work мигрируют независимо, каждая версия применяется
    одной транзакцией — при сбое файл целиком остаётся на прошлой версии.
    """
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        _set_pragmas(conn)
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        for version, files in _migrations(main=main).items():
            if version <= current:
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                for file in files:
                    for statement in _statements(file.read_text(encoding="utf-8")):
                        conn.execute(statement)
                if not main:
                    _seed_work_sequence(conn)
                conn.execute(f"PRAGMA user_version={version:d}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
    finally:
        conn.close()
    path.chmod(0o600)


_READER_POOL_MAX = 16  # столько простаивающих читателей держим; больше — закрываем после выдачи


class _ReaderConnection(sqlite3.Connection):
    """Read-only соединение из пула `Db.reader()`. 19.10, нагрузка ядра на стационарнике: открытие соединения
    (разбор схемы двух файлов, ATTACH, функции) давало ~63 % процессора ядра при сотнях чтений в секунду.

    Снимок читателя держит любой незакрытый оператор: курсор, не дочитанный до конца и ещё живой у
    вызывающего, оставляет читающую транзакцию открытой, и следующий взявший соединение видел бы старые
    данные (проверено пробой: `in_transaction` при этом False, `BEGIN`/`ROLLBACK` снимок не снимают).
    Поэтому соединение помнит свои курсоры и при возврате в пул закрывает их — закрытие курсора сбрасывает
    оператор и снимок."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._cursors: weakref.WeakSet[sqlite3.Cursor] = weakref.WeakSet()

    def cursor(self, *args, **kwargs):  # type: ignore[override]
        cur = super().cursor(*args, **kwargs)
        self._cursors.add(cur)
        return cur

    def execute(self, *args, **kwargs):  # type: ignore[override]
        cur = super().execute(*args, **kwargs)
        self._cursors.add(cur)
        return cur

    def executemany(self, *args, **kwargs):  # type: ignore[override]
        cur = super().executemany(*args, **kwargs)
        self._cursors.add(cur)
        return cur

    def reset_for_pool(self) -> None:
        """Закрыть все живые курсоры и откатить забытую транзакцию — следующий читатель начнёт со свежего
        снимка."""
        for cur in list(self._cursors):
            cur.close()
        self._cursors.clear()
        if self.in_transaction:
            self.rollback()


class DbClosed(RuntimeError):
    """Запись в уже закрытый `Db` (приёмка 2b, архитектор): явное исключение, а не молчаливый `None` — общий
    примитив записи не должен маскировать ошибки вызывающих. Фоновая работа, пережившая остановку (поток
    ретенции), ловит его сама."""


class Db:
    """Один писатель на процесс (очередь + выделенный поток) плюс read-only читатели."""

    def __init__(self, data_dir: Path, clock: Callable[[], float] = time.time) -> None:
        self.data_dir = data_dir
        self.main_path = data_dir / "hub.db"
        self.work_path = data_dir / "hub-work.db"
        self.clock = clock  # тесты подменяют часы; в слое БД time.time() напрямую не звать (раздел 8 плана)
        self._queue: queue.Queue[tuple[_WriteFn, concurrent.futures.Future, bool] | None] | None = None
        self._thread: threading.Thread | None = None
        self._on_commit: list[Callable[[], None]] = []
        self._started = False
        # Поправки после opus-критика кода (run1) + приёмка архитектора: проверка «закрыт ли» и постановка в
        # очередь — под ОДНИМ замком с `close()`; иначе задача, поставленная после сигнала остановки писателя,
        # ждала бы вечно. Запись в закрытый `Db` — `DbClosed` (см. `_submit`).
        self._closed = False
        self._submit_lock = threading.Lock()
        self._readers: list[_ReaderConnection] = []  # простаивающие читатели (`reader()`), LIFO
        self._readers_lock = threading.Lock()

    def on_commit(self, callback: Callable[[], None]) -> None:
        """Зарегистрировать колбэк, который поток-писатель вызывает ПОСЛЕ каждого успешного COMMIT
        транзакции `write`/`write_sync` — и никогда при откате. Порядок в `_run_writer`: COMMIT -> колбэки
        -> `future.set_result`. Исключение колбэка логируется и глотается: оно не должно ни ронять
        поток-писатель, ни превращать успешную запись в ошибку для вызывающего (см. `feed.Notifier`).
        """
        self._on_commit.append(callback)

    def start(self) -> None:
        """Открыть/создать оба файла, применить прагмы и миграции, выверить seq, поднять поток-писатель."""
        _init_file(self.main_path, main=True)
        _init_file(self.work_path, main=False)
        self._reconcile_seq()
        self._f57_mail_backlog()
        self._f66_talk_branches()
        self._queue = queue.Queue()
        self._thread = threading.Thread(target=self._run_writer, name="hub-db-writer", daemon=True)
        self._thread.start()
        self._started = True

    def close(self) -> None:
        """Дождаться очереди записи и закрыть поток-писатель."""
        with self._submit_lock:
            self._closed = True
            if self._queue is not None:
                self._queue.put(None)
        if self._thread is not None:
            self._thread.join()
        self._queue = None
        self._thread = None
        with self._readers_lock:
            readers, self._readers = self._readers, []
        for conn in readers:
            conn.close()

    def _connect_writer(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.main_path, isolation_level=None, check_same_thread=False)
        _set_pragmas(conn)
        conn.execute("ATTACH DATABASE ? AS work", (str(self.work_path),))
        # Ретенция (план 7, спека phase2-02 раздел F.4): без secure_delete очищенный текст остаётся байтами
        # на освобождённых страницах файла work — PRAGMA только per-connection, не сохраняется в файле.
        conn.execute("PRAGMA work.secure_delete = ON")
        # Ф8-3 П5: режим беседы — функция SQLite и в писателе (админские ручки читают через него), и в
        # читателях.
        _register_functions(conn)
        return conn

    def _f57_mail_backlog(self) -> None:
        """Ф57-1 (ревью): разовый шаг после миграций. `0086_main` оставил в `meta` проекты, где почта была в
        «разобрать»; их старые чужие письма (в обоих файлах — миграция main файла work не видит) помечаются
        рассылкой, иначе после смены режима на «реагирую» они разом встали бы в «Нужна реакция». Ключ
        снимается в той же транзакции; повтор после сбоя между файлами безвреден."""
        conn = self._connect_writer()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT value FROM main.meta WHERE key = ?", (_F57_BACKLOG_KEY,)).fetchone()
            if row is not None:
                try:
                    ids = [
                        i
                        for i in json.loads(row[0] or "[]")
                        if isinstance(i, int) and not isinstance(i, bool)
                    ]
                except ValueError:
                    ids = []
                if ids:
                    marks = ",".join("?" * len(ids))
                    for schema in ("main", "work"):
                        conn.execute(
                            f"UPDATE {schema}.message SET bulk = 1 WHERE is_me = 0 AND bulk = 0 "  # noqa: S608
                            f"AND channel_id IN (SELECT id FROM {schema}.channel WHERE kind = 'mail_thread' "
                            f"AND project_id IN ({marks}))",
                            ids,
                        )
                conn.execute("DELETE FROM main.meta WHERE key = ?", (_F57_BACKLOG_KEY,))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _f66_talk_branches(self) -> None:
        """Ф66-2 (A.1): разовый шаг после миграций. `0098_main` оставил в `meta` ключ, если в разговоре владельца с
        агентом были реплики: им проставляется ветка по `ref` (расчёт карточки — код очереди, в SQL его нет).
        Ключ снимается в той же транзакции; сбой расчёта — реплики остаются в «Общем», ключ всё равно
        снимается (не ошибка, A.1)."""
        conn = self._connect_writer()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT 1 FROM main.meta WHERE key = ?", (_F66_BRANCHES_KEY,)).fetchone()
            if row is not None:
                from . import owner_talk  # импорт внутри: модуль разговора опирается на код очереди

                conn.execute("SAVEPOINT f66")
                try:
                    owner_talk.backfill(conn, now_ms=int(self.clock() * 1000))
                    conn.execute("RELEASE f66")
                except Exception:
                    _logger.exception("Ф66-2: ветки старых реплик разговора не проставлены")
                    conn.execute("ROLLBACK TO f66")
                    conn.execute("RELEASE f66")
                conn.execute("DELETE FROM main.meta WHERE key = ?", (_F66_BRANCHES_KEY,))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _reconcile_seq(self) -> None:
        """На старте: value = max(текущий счётчик, max(seq) в main.message, max(seq) в work.message).

        Переживаем сбой, где work закоммитился, а main (с seq_counter) — нет: тогда счётчик в main отстаёт
        от уже использованных значений в work, и без этой выверки next_seq() выдал бы повтор.
        """
        conn = self._connect_writer()
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT value FROM seq_counter WHERE id = 1").fetchone()[0]
            # seq носят сообщения, каналы, (Ф9-1, 9a) задачи и запросы рабочих сессий и (Ф9-3, 9c) разговор и
            # сообщения чата оркестратора — выверяем по всем.
            # Таблицы рабочих сессий есть не во всех версиях схемы (тесты поднимают прежние) — только
            # существующие.
            used = []
            for schema in ("main", "work"):
                existing = {
                    r[0]
                    for r in conn.execute(
                        f"SELECT name FROM {schema}.sqlite_master WHERE type = 'table'"  # noqa: S608
                    )
                }
                for table in (
                    "message",
                    "channel",
                    "work_task",
                    "work_request",
                    "orch_conversation",
                    "orch_message",
                    "join_request",  # Ф17-1 П6: заявки на вступление (только main)
                    "leave_request",  # Ф17-2 П11: заявки на выход (только main)
                    "button_press",  # Ф44-1c П3: нажатия кнопок ботов (только main)
                    "human_request",  # Ф54-1: заявки «нужен человек» (только main)
                    "group_op",  # Ф62-6: операции над группами WhatsApp (только main)
                ):
                    if table in existing:
                        used.append(
                            conn.execute(
                                f"SELECT COALESCE(MAX(seq), 0) FROM {schema}.{table}"  # noqa: S608
                            ).fetchone()[0]
                        )
            target = max(current, *used)
            if target != current:
                conn.execute("UPDATE seq_counter SET value = ? WHERE id = 1", (target,))
            reconcile_person_seq(conn)
            conn.execute("COMMIT")
        finally:
            conn.close()

    def _run_writer(self) -> None:
        assert self._queue is not None
        conn = self._connect_writer()
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    return
                fn, future, raw = item
                if not future.set_running_or_notify_cancel():
                    continue
                if raw:
                    # Обслуживание (раздел F.4 спеки phase2-02): `PRAGMA wal_checkpoint(TRUNCATE)` не может
                    # завершиться внутри открытой транзакции ТОГО ЖЕ соединения (держит собственный снимок) —
                    # выполняется в autocommit, БЕЗ BEGIN/COMMIT, но всё так же через очередь писателя (та же
                    # сериализация, тот же connection).
                    try:
                        result = fn(conn)
                    except BaseException as exc:  # noqa: BLE001 — исключение fn пробрасываем вызывающему
                        future.set_exception(exc)
                    else:
                        future.set_result(result)
                    continue
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    result = fn(conn)
                    conn.execute("COMMIT")
                except BaseException as exc:  # noqa: BLE001 — исключение fn пробрасываем вызывающему
                    with contextlib.suppress(sqlite3.Error):
                        conn.execute("ROLLBACK")
                    future.set_exception(exc)
                else:
                    for callback in self._on_commit:
                        try:
                            callback()
                        except Exception:  # noqa: BLE001 — колбэк не должен ронять писатель или запись
                            _logger.exception("Db.on_commit: колбэк упал")
                    future.set_result(result)
        finally:
            conn.close()

    def _submit(self, fn: _WriteFn, *, raw: bool = False) -> concurrent.futures.Future:
        if not self._started:
            raise RuntimeError("Db.start() не вызван")
        future: concurrent.futures.Future = concurrent.futures.Future()
        with self._submit_lock:
            if self._closed:
                # Писателя уже нет (или сигнал остановки уже в очереди) — задача не выполнится никогда:
                # явная ошибка, а не молчаливый `None` и не вечное ожидание `future.result()`.
                future.set_exception(DbClosed("Db закрыт"))
                return future
            self._queue.put((fn, future, raw))
        return future

    async def write(self, fn: _WriteFn) -> T:
        """fn(conn) — в потоке-писателе, внутри BEGIN IMMEDIATE … COMMIT; исключение — откат и проброс."""
        return await asyncio.wrap_future(self._submit(fn))

    def write_sync(self, fn: _WriteFn) -> T:
        """То же самое, но синхронно — для CLI и тестов без event loop."""
        return self._submit(fn).result()

    def maintenance_sync(self, fn: _WriteFn) -> T:
        """`fn(conn)` В ТОМ ЖЕ потоке-писателе, но БЕЗ обёртки `BEGIN IMMEDIATE`/`COMMIT` (autocommit) —
        для операций обслуживания, которые не могут завершиться внутри открытой транзакции (раздел F.4 спеки
        phase2-02: `PRAGMA wal_checkpoint(TRUNCATE)` требует отсутствия открытого снимка на этом же
        соединении). `fn` сам отвечает за то, чтобы не оставить соединение в незавершённой транзакции."""
        return self._submit(fn, raw=True).result()

    def _open_reader(self) -> _ReaderConnection:
        conn = sqlite3.connect(
            f"file:{self.main_path}?mode=ro", uri=True, check_same_thread=False, factory=_ReaderConnection
        )
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("ATTACH DATABASE ? AS work", (f"file:{self.work_path}?mode=ro",))
            _register_functions(conn)  # Ф8-3 П5
            conn.reset_for_pool()
        except BaseException:
            conn.close()
            raise
        return conn

    @contextlib.contextmanager
    def reader(self) -> Iterator[sqlite3.Connection]:
        """Read-only соединение: main + work (тоже read-only), запись через него падает. Соединения берутся из
        пула и возвращаются в него (см. `_ReaderConnection`): одно соединение за раз отдаётся одному
        вызывающему, между выдачами живые курсоры закрываются — снимок всегда свежий."""
        with self._readers_lock:
            conn = self._readers.pop() if self._readers else None
        if conn is None:
            conn = self._open_reader()
        try:
            yield conn
        finally:
            keep = False
            try:
                conn.reset_for_pool()
                keep = True
            except sqlite3.Error:
                _logger.warning("читатель не вернулся в пул: сброс не удался", exc_info=True)
            if keep:
                with self._readers_lock:
                    keep = not self._closed and len(self._readers) < _READER_POOL_MAX
                    if keep:
                        self._readers.append(conn)
            if not keep:
                conn.close()
