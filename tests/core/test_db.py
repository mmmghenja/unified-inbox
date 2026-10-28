import asyncio
import re
import shutil
import sqlite3
import stat

import pytest

from hub.core.db import WORK_ID_BASE, Db, db_of, next_seq


def _latest(*, main: bool) -> int:
    from hub.core import db as db_module

    return max(db_module._migrations(main=main))


def _insert_channel(conn: sqlite3.Connection, schema: str, ext_id: str = "c1") -> int:
    cur = conn.execute(
        f"INSERT INTO {schema}.channel (account_id, ext_id, kind, first_seen_ts) VALUES (1, ?, 'dm', 0)",
        (ext_id,),
    )
    return cur.lastrowid


def _insert_message(
    conn: sqlite3.Connection, schema: str, channel_id: int, *, ext_id: str | None = None, text: str = "hi"
) -> int:
    seq = next_seq(conn)
    cur = conn.execute(
        f"INSERT INTO {schema}.message (seq, first_seq, account_id, channel_id, ext_id, kind, text, ts) "
        "VALUES (?, ?, 1, ?, ?, 'text', ?, 0)",
        (seq, seq, channel_id, ext_id, text),
    )
    return cur.lastrowid


# 1. Прагмы у обоих свежих файлов.
def test_fresh_files_have_expected_pragmas(tmp_path):
    db = Db(tmp_path)
    db.start()
    try:
        for path in (db.main_path, db.work_path):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            conn = sqlite3.connect(path)
            try:
                assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
                assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
            finally:
                conn.close()
    finally:
        db.close()


# 2. Повторный start() на существующих файлах — без ошибок, версия та же.
def test_restart_is_idempotent(tmp_path):
    db = Db(tmp_path)
    db.start()
    db.close()

    db2 = Db(tmp_path)
    db2.start()
    try:
        with db2.reader() as conn:
            # main: 0001_main+shared, потом 0002_main (login_code, кусок 4a) — последняя версия на сейчас.
            # версии — какие бы ни были последними у каждого файла: тест не должен краснеть от новой миграции
            assert conn.execute("PRAGMA main.user_version").fetchone()[0] == _latest(main=True)
            assert conn.execute("PRAGMA work.user_version").fetchone()[0] == _latest(main=False)
    finally:
        db2.close()


# 3. write коммитит; исключение в fn откатывает всё и пробрасывается; следующая запись после сбоя работает.
def test_write_commits_and_rolls_back(db):
    channel_id = db.write_sync(lambda conn: _insert_channel(conn, "main"))

    def _boom(conn: sqlite3.Connection) -> None:
        _insert_message(conn, "main", channel_id, ext_id="will-rollback")
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        db.write_sync(_boom)

    with db.reader() as conn:
        count = conn.execute("SELECT COUNT(*) FROM main.message WHERE ext_id = 'will-rollback'").fetchone()[0]
    assert count == 0

    # следующая запись после сбоя работает как обычно
    message_id = db.write_sync(lambda conn: _insert_message(conn, "main", channel_id, ext_id="after-boom"))
    assert message_id


# 4. 200 конкурентных write из asyncio — все применились, seq строго уникальны.
async def test_concurrent_writes_get_unique_seq(db):
    channel_id = db.write_sync(lambda conn: _insert_channel(conn, "main"))

    def _insert(i: int):
        def _fn(conn: sqlite3.Connection) -> int:
            return _insert_message(conn, "main", channel_id, ext_id=f"m{i}")

        return _fn

    results = await asyncio.gather(*(db.write(_insert(i)) for i in range(200)))
    assert len(results) == 200

    with db.reader() as conn:
        seqs = [row[0] for row in conn.execute("SELECT seq FROM main.message ORDER BY id")]
    assert len(seqs) == 200
    assert len(set(seqs)) == 200
    assert seqs == sorted(seqs)  # порядок коммита == порядок id == возрастание seq


# 5. Reader не может писать; reader видит закоммиченное писателем, в т.ч. в work.
def test_reader_is_read_only_and_sees_committed_data(db):
    db.write_sync(lambda conn: _insert_channel(conn, "main", ext_id="main-chan"))
    db.write_sync(lambda conn: _insert_channel(conn, "work", ext_id="work-chan"))

    with db.reader() as conn:
        assert conn.execute("SELECT ext_id FROM main.channel").fetchone()[0] == "main-chan"
        assert conn.execute("SELECT ext_id FROM work.channel").fetchone()[0] == "work-chan"
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO main.channel (account_id, ext_id, kind, first_seen_ts) VALUES (1, 'x', 'dm', 0)"
            )


# 6. id в work >= 2**40; db_of верна для обоих.
def test_work_ids_are_offset(db):
    main_id = db.write_sync(lambda conn: _insert_channel(conn, "main", ext_id="m1"))
    work_id = db.write_sync(lambda conn: _insert_channel(conn, "work", ext_id="w1"))

    assert main_id < WORK_ID_BASE
    assert work_id >= WORK_ID_BASE
    assert db_of(main_id) == "main"
    assert db_of(work_id) == "work"


# 7. Выверка seq на старте: руками вставить в work.message seq больше счётчика, перезапустить.
def test_seq_reconciliation_after_restart(tmp_path):
    db = Db(tmp_path)
    db.start()
    channel_id = db.write_sync(lambda conn: _insert_channel(conn, "work"))
    counter_before = db.write_sync(lambda conn: conn.execute("SELECT value FROM seq_counter").fetchone()[0])
    db.close()

    # руками, в обход writer'а — имитация сбоя "work закоммитился, main нет"
    raw = sqlite3.connect(db.work_path)
    try:
        rogue_seq = counter_before + 1000
        raw.execute(
            "INSERT INTO message (seq, first_seq, account_id, channel_id, ext_id, kind, text, ts) "
            "VALUES (?, ?, 1, ?, 'rogue', 'text', 'x', 0)",
            (rogue_seq, rogue_seq, channel_id),
        )
        raw.commit()
    finally:
        raw.close()

    db2 = Db(tmp_path)
    db2.start()
    try:
        new_seq = db2.write_sync(next_seq)
        assert new_seq > rogue_seq
    finally:
        db2.close()


# 8. FTS: вставка/префиксный поиск/регистр/UPDATE/deleted=1/DELETE — в обеих БД.
@pytest.mark.parametrize("schema", ["main", "work"])
def test_fts_lifecycle(db, schema):
    channel_id = db.write_sync(lambda conn: _insert_channel(conn, schema))

    def _search(term: str) -> list[str]:
        with db.reader() as conn:
            rows = conn.execute(
                f"SELECT text FROM {schema}.message_fts WHERE message_fts MATCH ?", (term,)
            ).fetchall()
        return [r[0] for r in rows]

    message_id = db.write_sync(lambda conn: _insert_message(conn, schema, channel_id, text="Привет, мир"))
    assert _search("прив*")  # префиксный поиск, регистр другой
    assert _search("ПРИВЕТ")  # без учёта регистра (unicode61)

    def _update(conn: sqlite3.Connection) -> None:
        conn.execute(f"UPDATE {schema}.message SET text = ? WHERE id = ?", ("гудбай", message_id))

    db.write_sync(_update)
    assert not _search("прив*")
    assert _search("гудбай")

    def _soft_delete(conn: sqlite3.Connection) -> None:
        conn.execute(f"UPDATE {schema}.message SET deleted = 1 WHERE id = ?", (message_id,))

    db.write_sync(_soft_delete)
    assert not _search("гудбай")

    # восстановим и проверим настоящий DELETE
    def _undelete_and_reindex(conn: sqlite3.Connection) -> None:
        conn.execute(
            f"UPDATE {schema}.message SET deleted = 0, text = ? WHERE id = ?", ("опять тут", message_id)
        )

    db.write_sync(_undelete_and_reindex)
    assert _search("опять")

    db.write_sync(lambda conn: conn.execute(f"DELETE FROM {schema}.message WHERE id = ?", (message_id,)))
    assert not _search("опять")


# 9. audit: UPDATE и DELETE падают.
def test_audit_is_append_only(db):
    audit_id = db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO audit (ts, actor, action) VALUES (0, 'test', 'noop')").lastrowid
        )
    )

    with pytest.raises(sqlite3.IntegrityError):
        db.write_sync(lambda conn: conn.execute("UPDATE audit SET actor = 'x' WHERE id = ?", (audit_id,)))

    with pytest.raises(sqlite3.IntegrityError):
        db.write_sync(lambda conn: conn.execute("DELETE FROM audit WHERE id = ?", (audit_id,)))


# 10. UNIQUE(channel_id, ext_id): дубль падает; две строки с ext_id NULL допустимы.
def test_message_unique_channel_ext_id(db):
    channel_id = db.write_sync(lambda conn: _insert_channel(conn, "main"))
    db.write_sync(lambda conn: _insert_message(conn, "main", channel_id, ext_id="dup"))

    with pytest.raises(sqlite3.IntegrityError):
        db.write_sync(lambda conn: _insert_message(conn, "main", channel_id, ext_id="dup"))

    # NULL в ext_id не конфликтует — строки "отправляется"
    db.write_sync(lambda conn: _insert_message(conn, "main", channel_id, ext_id=None))
    db.write_sync(lambda conn: _insert_message(conn, "main", channel_id, ext_id=None))


# Сломанная миграция откатывается целиком: файл остаётся на прошлой версии, без половины таблиц.
def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    from hub.core import db as db_module

    first = Db(tmp_path / "data")
    first.data_dir.mkdir()
    first.start()
    first.close()

    # Ломаем заведомо следующую версию (9999), чтобы проверять откат НЕ уже применённой миграции и не
    # зависеть от того, сколько настоящих миграций есть на момент теста.
    latest_main = _latest(main=True)
    migrations = tmp_path / "migrations"
    shutil.copytree(db_module._MIGRATIONS_DIR, migrations)
    broken = "CREATE TABLE half_done (x);\nINSERT INTO no_such_table VALUES (1);\n"
    (migrations / "9999_shared.sql").write_text(broken)
    monkeypatch.setattr(db_module, "_MIGRATIONS_DIR", migrations)

    with pytest.raises(sqlite3.OperationalError):
        Db(tmp_path / "data").start()

    conn = sqlite3.connect(first.main_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == latest_main
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name = 'half_done'").fetchone()[0] == 0
    finally:
        conn.close()


def test_main_base_that_skipped_0058_shared_gets_held_file(tmp_path, monkeypatch):
    """Выкладка 08.10.2026: `0058_main.sql` и `0058_shared.sql` приехали РАЗНЫМИ деплоями — версия основной
    базы уже была 58, и `0058_shared.sql` она пропустила (таблицы `held_file` нет, чистка падает). Починка —
    `0059_shared.sql` с тем же определением под `IF NOT EXISTS`. Тест воспроизводит оба деплоя каталогами
    миграций: сначала всё до 58 БЕЗ `0058_shared.sql`, потом настоящий каталог целиком (от будущих миграций
    тест не зависит)."""
    from hub.core import db as db_module

    real_dir = db_module._MIGRATIONS_DIR
    first_deploy = tmp_path / "migrations-first-deploy"
    first_deploy.mkdir()
    for path in real_dir.glob("[0-9][0-9][0-9][0-9]_*.sql"):
        if int(path.name[:4]) <= 58 and path.name != "0058_shared.sql":
            shutil.copy(path, first_deploy / path.name)
    monkeypatch.setattr(db_module, "_MIGRATIONS_DIR", first_deploy)
    data = tmp_path / "data"
    data.mkdir()
    first = Db(data)
    first.start()
    first.close()
    conn = sqlite3.connect(first.main_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 58
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name = 'held_file'").fetchone()[0] == 0
    finally:
        conn.close()

    monkeypatch.setattr(db_module, "_MIGRATIONS_DIR", real_dir)
    second = Db(data)
    second.start()
    second.close()
    for path in (second.main_path, second.work_path):
        conn = sqlite3.connect(path)
        try:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(held_file)")]
            assert "sha256" in columns and "released_ts" in columns, path.name
            indexes = {row[1] for row in conn.execute("PRAGMA index_list(held_file)")}
            assert {"idx_held_file_session", "idx_held_file_sha", "idx_held_file_say"} <= indexes
        finally:
            conn.close()


def test_one_migration_file_per_number_from_0059():
    """Версия базы — одно число на файл базы: пара `NNNN_main.sql` + `NNNN_shared.sql` применяется целиком
    только если приехала ОДНИМ деплоем. Приехала порознь — вторая половина молча пропускается (так 08.10.2026
    основная база прода осталась без `held_file`). Поэтому с номера 59 один номер — один файл; нужны правки и
    main, и shared — это два соседних номера."""
    from hub.core import db as db_module

    seen: dict[int, list[str]] = {}
    for path in db_module._MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"):
        seen.setdefault(int(path.name[:4]), []).append(path.name)
    doubled = {number: sorted(names) for number, names in seen.items() if number >= 59 and len(names) > 1}
    assert not doubled, f"один номер — один файл миграции: {doubled}"


def _schema_sql(conn) -> dict[str, str]:
    """Текст схемы `hub_session` и её индексов из `sqlite_master` без пробелов и кавычек имени: ALTER ADD
    COLUMN дописывает столбцы в конец текста, RENAME берёт имя в кавычки — смысл тот же."""
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE tbl_name = 'hub_session' AND sql IS NOT NULL"
    ).fetchall()
    return {name: re.sub(r"\s+", "", sql).replace('"', "") for name, sql in rows}


def test_0102_hub_session_accepts_cursor_and_keeps_rows_ids_indexes(tmp_path, monkeypatch):
    """Ф71-1 (Р8): `0102_main.sql` пересоздаёт `hub_session` ради CHECK движка `cursor`. База версии 101 с
    сессиями (открытой, закрытой и удалённой последней — счётчик AUTOINCREMENT выше наибольшего id) после
    миграции несёт те же строки и id, все четыре индекса, прежний счётчик: новая сессия получает СЛЕДУЮЩИЙ id,
    а не id удалённой. `cursor` принимается, неизвестный движок — нет."""
    from hub.core import db as db_module

    real_dir = db_module._MIGRATIONS_DIR
    before = tmp_path / "migrations-101"
    before.mkdir()
    for path in real_dir.glob("[0-9][0-9][0-9][0-9]_*.sql"):
        if int(path.name[:4]) <= 101:
            shutil.copy(path, before / path.name)
    monkeypatch.setattr(db_module, "_MIGRATIONS_DIR", before)
    data = tmp_path / "data"
    data.mkdir()
    first = Db(data)
    first.start()
    first.close()
    insert = (
        "INSERT INTO hub_session (project_id, role, role_key, role_no, engine, model, mode, workdir, "
        "created_ts, updated_ts, closed_ts, created_by, seq, turns, sys_pin) "
        "VALUES (?, ?, ?, 1, ?, ?, 'auto', '/w', 1, 2, ?, 'ui', ?, ?, ?)"
    )
    conn = sqlite3.connect(first.main_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 101
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, (1, None, None, "cursor", None, None, 0, 0, None))
        conn.execute(insert, (1, "р", "р", "claude", "opus", None, 5, 3, '{"a": 1}'))
        conn.execute(insert, (2, "о", "о", "opencode", "x/y", 7, 6, 0, None))
        conn.execute(insert, (2, None, None, "claude", None, None, 0, 0, None))
        conn.execute("DELETE FROM hub_session WHERE id = 3")
        conn.commit()
        rows_before = conn.execute("SELECT * FROM hub_session ORDER BY id").fetchall()
        info_before = conn.execute("PRAGMA table_info(hub_session)").fetchall()
        sql_before = _schema_sql(conn)
    finally:
        conn.close()

    # Ф73-1: второй деплой — каталог до 102 включительно (следующие миграции добавляют столбцы `hub_session`
    # — сравнение схемы «до/после» относится только к пересозданию 0102).
    upto = tmp_path / "migrations-102"
    upto.mkdir()
    for path in real_dir.glob("[0-9][0-9][0-9][0-9]_*.sql"):
        if int(path.name[:4]) <= 102:
            shutil.copy(path, upto / path.name)
    monkeypatch.setattr(db_module, "_MIGRATIONS_DIR", upto)
    second = Db(data)
    second.start()
    second.close()
    conn = sqlite3.connect(second.main_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 102
        # П10: столбцы целиком (тип, NOT NULL, DEFAULT, ключ) и текст схемы таблицы и индексов — те же;
        # отличие одно: CHECK движка принимает `cursor`.
        assert conn.execute("PRAGMA table_info(hub_session)").fetchall() == info_before
        sql_after = _schema_sql(conn)
        assert sql_before["hub_session"].count("('claude','opencode')") == 1
        sql_before["hub_session"] = sql_before["hub_session"].replace(
            "('claude','opencode')", "('claude','opencode','cursor')"
        )
        assert sql_after == sql_before
        assert conn.execute("SELECT * FROM hub_session ORDER BY id").fetchall() == rows_before
        indexes = {row[1]: row[2] for row in conn.execute("PRAGMA index_list(hub_session)")}
        wanted = {
            "hub_session_open",
            "hub_session_open_role",
            "hub_session_open_engine_sid",
            "hub_session_seq",
        }
        assert wanted <= set(indexes)
        assert indexes["hub_session_open_role"] == 1 and indexes["hub_session_open_engine_sid"] == 1
        assert (
            conn.execute("SELECT name FROM sqlite_master WHERE name = 'hub_session_new'").fetchone() is None
        )
        assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'hub_session'").fetchall() == [(3,)]
        cur = conn.execute(insert, (1, None, None, "cursor", "grok-4.7", None, 0, 0, None))
        assert cur.lastrowid == 4
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, (1, None, None, "bogus", None, None, 0, 0, None))
        with pytest.raises(sqlite3.IntegrityError):  # уникальность линии роли пережила пересоздание
            conn.execute(insert, (1, "р", "р", "cursor", None, None, 0, 0, None))
    finally:
        conn.close()


def test_0103_hub_session_mcp_reload_column(tmp_path, monkeypatch):
    """Ф73-1: `0103_main.sql` добавляет `hub_session.mcp_reload_ts` — у строк версии 102 он `NULL`, запись
    сессии читается целиком (`held.get`), значение пишется и читается."""
    from hub.core import db as db_module
    from hub.core import held as held_mod

    real_dir = db_module._MIGRATIONS_DIR
    before = tmp_path / "migrations-102"
    before.mkdir()
    for path in real_dir.glob("[0-9][0-9][0-9][0-9]_*.sql"):
        if int(path.name[:4]) <= 102:
            shutil.copy(path, before / path.name)
    monkeypatch.setattr(db_module, "_MIGRATIONS_DIR", before)
    data = tmp_path / "data"
    data.mkdir()
    first = Db(data)
    first.start()
    first.close()
    conn = sqlite3.connect(first.main_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 102
        assert "mcp_reload_ts" not in [r[1] for r in conn.execute("PRAGMA table_info(hub_session)")]
        conn.execute(
            "INSERT INTO hub_session (project_id, engine, mode, workdir, created_ts, updated_ts, created_by) "
            "VALUES (1, 'claude', 'manual', '/w', 1, 1, 'ui')"
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(db_module, "_MIGRATIONS_DIR", real_dir)
    second = Db(data)
    second.start()
    second.close()
    conn = sqlite3.connect(second.main_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 103
        rec = held_mod.get(conn, 1)
        assert rec is not None and rec.mcp_reload_ts is None and rec.workdir == "/w"
        conn.execute("UPDATE hub_session SET mcp_reload_ts = 42 WHERE id = 1")
        assert held_mod.get(conn, 1).mcp_reload_ts == 42
    finally:
        conn.close()


def test_0104_hub_session_engine_without_check_keeps_rows_ids_indexes(tmp_path, monkeypatch):
    """Ф74-5 (план 6.2, В6): `0104_main.sql` пересоздаёт `hub_session` без CHECK движка — `codex` (и любой
    следующий движок профиля) принимается; строки, id, индексы, уникальность линии роли и счётчик
    AUTOINCREMENT те же; отличие схемы — только снятый CHECK."""
    from hub.core import db as db_module

    real_dir = db_module._MIGRATIONS_DIR
    before = tmp_path / "migrations-103"
    before.mkdir()
    for path in real_dir.glob("[0-9][0-9][0-9][0-9]_*.sql"):
        if int(path.name[:4]) <= 103:
            shutil.copy(path, before / path.name)
    monkeypatch.setattr(db_module, "_MIGRATIONS_DIR", before)
    data = tmp_path / "data"
    data.mkdir()
    first = Db(data)
    first.start()
    first.close()
    insert = (
        "INSERT INTO hub_session (project_id, role, role_key, role_no, engine, model, mode, workdir, "
        "created_ts, updated_ts, closed_ts, created_by, seq, turns, sys_pin, mcp_reload_ts) "
        "VALUES (?, ?, ?, 1, ?, ?, 'auto', '/w', 1, 2, ?, 'ui', ?, ?, ?, ?)"
    )
    conn = sqlite3.connect(first.main_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 103
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, (1, None, None, "codex", None, None, 0, 0, None, None))
        conn.execute(insert, (1, "р", "р", "claude", "opus", None, 5, 3, '{"a": 1}', 9))
        conn.execute(insert, (2, "о", "о", "cursor", "grok-4.7", 7, 6, 0, None, None))
        conn.execute(insert, (2, None, None, "opencode", None, None, 0, 0, None, None))
        conn.execute("DELETE FROM hub_session WHERE id = 3")
        conn.commit()
        rows_before = conn.execute("SELECT * FROM hub_session ORDER BY id").fetchall()
        info_before = conn.execute("PRAGMA table_info(hub_session)").fetchall()
        sql_before = _schema_sql(conn)
    finally:
        conn.close()

    upto = tmp_path / "migrations-104"
    upto.mkdir()
    for path in real_dir.glob("[0-9][0-9][0-9][0-9]_*.sql"):
        if int(path.name[:4]) <= 104:
            shutil.copy(path, upto / path.name)
    monkeypatch.setattr(db_module, "_MIGRATIONS_DIR", upto)
    second = Db(data)
    second.start()
    second.close()
    conn = sqlite3.connect(second.main_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 104
        assert conn.execute("PRAGMA table_info(hub_session)").fetchall() == info_before
        sql_after = _schema_sql(conn)
        check = "CHECK(engineIN('claude','opencode','cursor'))"
        assert sql_before["hub_session"].count(check) == 1
        sql_before["hub_session"] = sql_before["hub_session"].replace(check, "")
        assert sql_after == sql_before
        assert conn.execute("SELECT * FROM hub_session ORDER BY id").fetchall() == rows_before
        indexes = {row[1]: row[2] for row in conn.execute("PRAGMA index_list(hub_session)")}
        wanted = {
            "hub_session_open",
            "hub_session_open_role",
            "hub_session_open_engine_sid",
            "hub_session_seq",
        }
        assert wanted <= set(indexes)
        assert indexes["hub_session_open_role"] == 1 and indexes["hub_session_open_engine_sid"] == 1
        assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'hub_session'").fetchall() == [(3,)]
        cur = conn.execute(insert, (1, None, None, "codex", "gpt-6.1-sol", None, 0, 0, None, None))
        assert cur.lastrowid == 4
        with pytest.raises(sqlite3.IntegrityError):  # уникальность линии роли пережила пересоздание
            conn.execute(insert, (1, "р", "р", "codex", None, None, 0, 0, None, None))
    finally:
        conn.close()
