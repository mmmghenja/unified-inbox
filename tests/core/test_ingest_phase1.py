"""Тесты 1-4 куска 1a фазы 1 (раздел A спеки `docs/architecture.md`): `attrs` в `ChannelRef`,
события `channel`/`channel_migrate`/`account_meta`. Стиль и фикстуры — `tests/core/test_ingest.py` (тот же
`db` из `tests/core/conftest.py`/локальный, `apply_batch` напрямую, никакого HTTP)."""

import json

import pytest

from hub.core.db import Db
from hub.core.ingest import apply_batch


@pytest.fixture
def db(tmp_path):
    instance = Db(tmp_path)
    instance.start()
    yield instance
    instance.close()


def _insert_account(db, *, source: str = "tg", db_name: str = "main") -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.account (source, db) VALUES (?, ?)", (source, db_name)).lastrowid
        )
    )


def _insert_project(db, *, name: str = "P") -> int:
    return db.write_sync(
        lambda conn: conn.execute("INSERT INTO main.project (name) VALUES (?)", (name,)).lastrowid
    )


def _add_rule(db, *, account_id: int, project_id: int, match: dict, priority: int = 0) -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.map_rule (account_id, match_json, project_id, priority) "
                "VALUES (?, ?, ?, ?)",
                (account_id, json.dumps(match), project_id, priority),
            ).lastrowid
        )
    )


def _upsert(ext_id, ts, *, channel_ext="c1", channel_kind="dm", attrs=None, text="hi", **extra):
    channel = {"ext_id": channel_ext, "kind": channel_kind}
    if attrs is not None:
        channel["attrs"] = attrs
    event = {
        "type": "upsert",
        "channel": channel,
        "ext_id": ext_id,
        "ts": ts,
        "is_me": False,
        "kind": "text",
        "text": text,
        "mentions_me": False,
    }
    event.update(extra)
    return event


def _channel_event(channel_ext, *, kind="dm", title=None, attrs=None):
    channel = {"ext_id": channel_ext, "kind": kind}
    if title is not None:
        channel["title"] = title
    if attrs is not None:
        channel["attrs"] = attrs
    return {"type": "channel", "channel": channel}


def _migrate_event(old_ext_id, new_ext_id, *, kind="group", title=None, attrs=None):
    channel = {"ext_id": new_ext_id, "kind": kind}
    if title is not None:
        channel["title"] = title
    if attrs is not None:
        channel["attrs"] = attrs
    return {"type": "channel_migrate", "old_ext_id": old_ext_id, "channel": channel}


def _account_meta_event(key, value):
    return {"type": "account_meta", "key": key, "value": value}


def _apply(db, account_id, events, now_ms=1000):
    return db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=events, cursor=None, now_ms=now_ms)
    )


def _channel_row(db, ext_id, *, schema="main"):
    with db.reader() as conn:
        return conn.execute(
            f"SELECT id, project_id, map_reason, seq, meta_json, archived FROM {schema}.channel "
            "WHERE ext_id = ?",
            (ext_id,),
        ).fetchone()


# --- Тест 1: attrs -------------------------------------------------------------------------------------


def test_1a_01_new_channel_with_attrs_is_laid_out_in_same_batch(db):
    account_id = _insert_account(db)
    project_id = _insert_project(db)
    _add_rule(db, account_id=account_id, project_id=project_id, match={"attr": "tg_folders", "has": 3})

    result = _apply(db, account_id, [_upsert("m1", 1000, attrs={"tg_folders": [3, 7]})])
    assert result.applied == 1
    assert result.dead == 0

    row = _channel_row(db, "c1")
    assert row is not None
    assert row[1] == project_id
    assert "правило #" in row[2]


def test_1a_01_repeat_same_attrs_is_noop_without_seq_growth(db):
    account_id = _insert_account(db)
    _apply(db, account_id, [_upsert("m1", 1000, attrs={"tg_folders": [3]})])
    with db.reader() as conn:
        seq_before = conn.execute("SELECT seq FROM main.channel WHERE ext_id = 'c1'").fetchone()[0]

    result = _apply(db, account_id, [_upsert("m2", 1001, attrs={"tg_folders": [3]})])
    assert result.dead == 0
    with db.reader() as conn:
        seq_after = conn.execute("SELECT seq FROM main.channel WHERE ext_id = 'c1'").fetchone()[0]
    # Сообщение всё равно новое (m2) -> канал.seq мог бы вырасти от last_msg_ts/my_msg_count, но НЕ от attrs:
    # проверяем именно через отдельное событие `channel` с теми же attrs — там seq не растёт вовсе.
    result2 = _apply(db, account_id, [_channel_event("c1", attrs={"tg_folders": [3]})])
    assert result2 == (0, 1, 0, 0)
    with db.reader() as conn:
        seq_final = conn.execute("SELECT seq FROM main.channel WHERE ext_id = 'c1'").fetchone()[0]
    assert seq_final == seq_after
    assert seq_after >= seq_before


def test_1a_01_attrs_change_bumps_seq_and_project(db):
    account_id = _insert_account(db)
    project_a = _insert_project(db, name="A")
    project_b = _insert_project(db, name="B")
    _add_rule(db, account_id=account_id, project_id=project_a, match={"attr": "tg_folders", "has": 1})
    _add_rule(db, account_id=account_id, project_id=project_b, match={"attr": "tg_folders", "has": 2})

    _apply(db, account_id, [_upsert("m1", 1000, attrs={"tg_folders": [1]})])
    row = _channel_row(db, "c1")
    assert row[1] == project_a
    seq_before = row[3]

    result = _apply(db, account_id, [_channel_event("c1", attrs={"tg_folders": [2]})])
    assert result.applied == 1

    row2 = _channel_row(db, "c1")
    assert row2[1] == project_b
    assert row2[3] > seq_before


def test_1a_01_attrs_too_big_is_dead_letter_neighbors_applied(db):
    account_id = _insert_account(db)
    huge = {"tg_folders": list(range(2000))}  # заведомо > 4 КиБ в JSON
    events = [_upsert("bad", 1000, attrs=huge), _upsert("good", 1001)]

    result = _apply(db, account_id, events)
    assert result.dead == 1
    assert result.applied == 1
    with db.reader() as conn:
        msg = conn.execute("SELECT 1 FROM main.message WHERE ext_id = 'good'").fetchone()
    assert msg is not None


# --- Тест 2: событие channel ----------------------------------------------------------------------------


def test_1a_02_channel_event_unknown_channel_is_noop_and_not_created(db):
    account_id = _insert_account(db)
    result = _apply(db, account_id, [_channel_event("ghost", title="X")])
    assert result == (0, 1, 0, 0)
    with db.reader() as conn:
        row = conn.execute("SELECT 1 FROM main.channel WHERE ext_id = 'ghost'").fetchone()
    assert row is None


def test_1a_02_channel_event_known_channel_title_goes_to_history(db):
    account_id = _insert_account(db)
    _apply(db, account_id, [_upsert("m1", 1000, channel_kind="group")], now_ms=1000)
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.channel SET title = 'старое имя' WHERE ext_id = 'c1'")
    )

    result = _apply(db, account_id, [_channel_event("c1", kind="group", title="новое имя")], now_ms=2000)
    assert result.applied == 1

    with db.reader() as conn:
        title, history_json = conn.execute(
            "SELECT title, title_history_json FROM main.channel WHERE ext_id = 'c1'"
        ).fetchone()
    assert title == "новое имя"
    history = json.loads(history_json)
    assert history == [{"title": "старое имя", "ts": 2000}]


# --- Тест 3: channel_migrate -----------------------------------------------------------------------------


@pytest.mark.parametrize("account_kind", ["main", "work"])
def test_1a_03_channel_migrate_new_channel_inherits_manual_project(db, account_kind):
    account_id = _insert_account(db, db_name=account_kind)
    schema = account_kind
    project_id = _insert_project(db, name="работа")

    _apply(db, account_id, [_upsert("m1", 1000, channel_ext="old1", channel_kind="group")])
    with db.reader() as conn:
        old_channel_id = conn.execute(f"SELECT id FROM {schema}.channel WHERE ext_id = 'old1'").fetchone()[0]

    def _set_manual(conn):
        conn.execute(
            f"UPDATE {schema}.channel SET project_id = ?, map_source = 'manual', map_reason = 'manual', "
            "muted = 1 WHERE id = ?",
            (project_id, old_channel_id),
        )

    db.write_sync(_set_manual)

    result = _apply(db, account_id, [_migrate_event("old1", "new1", kind="channel")], now_ms=5000)
    assert result.applied == 1

    with db.reader() as conn:
        new_row = conn.execute(
            f"SELECT project_id, map_source, map_reason, muted FROM {schema}.channel WHERE ext_id = 'new1'"
        ).fetchone()
        old_row = conn.execute(
            f"SELECT archived, meta_json FROM {schema}.channel WHERE ext_id = 'old1'"
        ).fetchone()
        alias = conn.execute(
            f"SELECT channel_id FROM {schema}.channel_alias WHERE account_id = ? AND old_ext_id = 'old1'",
            (account_id,),
        ).fetchone()
        new_channel_id = conn.execute(f"SELECT id FROM {schema}.channel WHERE ext_id = 'new1'").fetchone()[0]

    assert new_row == (project_id, "manual", "manual", 1)
    assert old_row[0] == 1
    assert json.loads(old_row[1])["migrated_to"] == "new1"
    assert alias[0] == new_channel_id

    # Повтор -> no-op
    result2 = _apply(db, account_id, [_migrate_event("old1", "new1", kind="channel")], now_ms=6000)
    assert result2 == (0, 1, 0, 0)


def test_1a_03_channel_migrate_without_old_channel_just_creates_new(db):
    account_id = _insert_account(db)
    result = _apply(db, account_id, [_migrate_event("gone", "new2", kind="channel")])
    assert result.applied == 1
    with db.reader() as conn:
        row = conn.execute("SELECT 1 FROM main.channel WHERE ext_id = 'new2'").fetchone()
        alias = conn.execute("SELECT 1 FROM main.channel_alias WHERE old_ext_id = 'gone'").fetchone()
    assert row is not None
    assert alias is not None


# --- Тест 4: account_meta ---------------------------------------------------------------------------------


@pytest.mark.parametrize("account_kind", ["main", "work"])
def test_1a_04_account_meta_write_and_noop_on_repeat(db, account_kind):
    account_id = _insert_account(db, db_name=account_kind)
    value = [{"id": 3, "title": "Работа"}, {"id": 7, "title": "Личное"}]

    result = _apply(db, account_id, [_account_meta_event("tg_folders", value)])
    assert result.applied == 1

    with db.reader() as conn:
        stored = conn.execute(
            f"SELECT value_json FROM {account_kind}.account_meta WHERE account_id = ? AND key = 'tg_folders'",
            (account_id,),
        ).fetchone()
    assert json.loads(stored[0]) == value

    result2 = _apply(db, account_id, [_account_meta_event("tg_folders", value)])
    assert result2 == (0, 1, 0, 0)


def test_1a_04_account_meta_for_work_account_only_in_work_file(db):
    account_id = _insert_account(db, db_name="work")
    _apply(db, account_id, [_account_meta_event("tg_folders", [{"id": 1, "title": "X"}])])

    with db.reader() as conn:
        work_row = conn.execute(
            "SELECT 1 FROM work.account_meta WHERE account_id = ? AND key = 'tg_folders'", (account_id,)
        ).fetchone()
        main_row = conn.execute("SELECT COUNT(*) FROM main.account_meta").fetchone()[0]
    assert work_row is not None
    assert main_row == 0


# --- Приёмка архитектором после opus-критика (22.09.2026) ------------------------------------------------


def test_1a_crit5_migrate_after_new_supergroup_message_keeps_manual_project(db):
    """Сообщение новой супергруппы пришло РАНЬШЕ события миграции (порядок апдейтов не гарантирован): новый
    канал уже `auto`, и размеченный руками чат уезжал в «Неразобранное»."""
    account_id = _insert_account(db)
    project_id = _insert_project(db)
    _apply(db, account_id, [_upsert("1", 1000, channel_ext="old", channel_kind="group")])
    old_id = _channel_row(db, "old")[0]
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET project_id = ?, map_source = 'manual', map_reason = 'manual' "
            "WHERE id = ?",
            (project_id, old_id),
        )
    )
    _apply(db, account_id, [_upsert("1", 2000, channel_ext="new", channel_kind="group")])
    _apply(db, account_id, [_migrate_event("old", "new")])

    with db.reader() as conn:
        row = conn.execute("SELECT project_id, map_source FROM main.channel WHERE ext_id = 'new'").fetchone()
    assert row == (project_id, "manual")


def test_1a_crit5_migrate_does_not_override_manual_mapping_of_new_channel(db):
    account_id = _insert_account(db)
    p_old = _insert_project(db, name="old")
    p_new = _insert_project(db, name="new")
    _apply(db, account_id, [_upsert("1", 1000, channel_ext="old", channel_kind="group")])
    _apply(db, account_id, [_upsert("1", 2000, channel_ext="new", channel_kind="group")])
    db.write_sync(
        lambda conn: conn.executemany(
            "UPDATE main.channel SET project_id = ?, map_source = 'manual' WHERE ext_id = ?",
            [(p_old, "old"), (p_new, "new")],
        )
    )
    _apply(db, account_id, [_migrate_event("old", "new")])

    with db.reader() as conn:
        assert conn.execute("SELECT project_id FROM main.channel WHERE ext_id = 'new'").fetchone() == (p_new,)


def test_1a_crit9_work_sequence_seed_is_not_duplicated(tmp_path):
    """У sqlite_sequence нет UNIQUE — `INSERT OR IGNORE` плодил дубли на каждой shared-миграции. Свежий
    work-файл проходит две shared-версии (0001 и 0004), значит засев выполняется дважды."""
    instance = Db(tmp_path)
    instance.start()
    try:
        with instance.reader() as conn:
            dupes = conn.execute(
                "SELECT name, COUNT(*) FROM work.sqlite_sequence GROUP BY name HAVING COUNT(*) > 1"
            ).fetchall()
            seeded = conn.execute("SELECT COUNT(*) FROM work.sqlite_sequence").fetchone()[0]
        assert seeded > 0
        assert dupes == []
    finally:
        instance.close()


# --- Ф10-5 (ревью, находка 4): миграция беседы-треда почты в беседу трекера наследует прочитанность --------


def _read_marker(db, ext_id):
    with db.reader() as conn:
        row = conn.execute("SELECT last_read_ext FROM main.channel WHERE ext_id = ?", (ext_id,)).fetchone()
    return row[0]


def _read_event(channel_ext, up_to, *, kind):
    return {"type": "read", "channel": {"ext_id": channel_ext, "kind": kind}, "up_to_ext_id": up_to}


@pytest.mark.parametrize(
    ("old_kind", "new_kind", "inherited"),
    [("mail_thread", "issue", "<a1@x>"), ("group", "channel", None)],
)
def test_f10_5_migrate_inherits_read_by_ext_id_only_for_mail_thread(db, old_kind, new_kind, inherited):
    """Беседа-тред (a1 прочитано, a3 нет) -> беседа задачи с a1, a2 (новое, в старой беседе его нет), a3:
    маркер новой — на a1 (a2 и a3 непрочитаны). Для Telegram (`group`) совпадение `ext_id` не значит
    «то же сообщение» — прочитанность не наследуется."""
    account_id = _insert_account(db, source="mail")
    _apply(
        db,
        account_id,
        [
            _upsert("<a1@x>", 1000, channel_ext="thr", channel_kind=old_kind),
            _upsert("<a3@x>", 3000, channel_ext="thr", channel_kind=old_kind),
            _read_event("thr", "<a1@x>", kind=old_kind),
        ],
    )
    assert _read_marker(db, "thr") == "<a1@x>"
    _apply(
        db,
        account_id,
        [
            _upsert("<a1@x>", 1000, channel_ext="jira:k", channel_kind=new_kind),
            _upsert("<a2@x>", 2000, channel_ext="jira:k", channel_kind=new_kind),
            _upsert("<a3@x>", 3000, channel_ext="jira:k", channel_kind=new_kind),
            _migrate_event("thr", "jira:k", kind=new_kind),
        ],
        now_ms=5000,
    )
    assert _read_marker(db, "jira:k") == inherited
    assert _channel_row(db, "thr")[5] == 1  # старая беседа в архиве
