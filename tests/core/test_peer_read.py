"""Ф11-7 (docs/architecture.md): событие ядра `peer_read` — «собеседник прочитал мои
сообщения до этого id»; колонка `channel.peer_read_sort` (миграция 0021, обе схемы); поле в выдаче беседы;
fake-коннектор умеет слать событие (для стенда)."""

import pytest

from hub.connectors.fake import FakeSource
from hub.core import db as db_module
from hub.core import queries
from hub.core.db import Db
from hub.core.ingest import apply_batch
from hub.core.security import Principal

UI = Principal(token_id=1, kind="ui", scopes=frozenset({"read"}), project_ids=None)


def _insert_account(db, *, source: str = "telegram", db_name: str = "main") -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.account (source, db) VALUES (?, ?)", (source, db_name)).lastrowid
        )
    )


def _upsert(ext_id: str, ts: int, *, channel_ext: str = "c1", is_me: bool = True) -> dict:
    return {
        "type": "upsert",
        "channel": {"ext_id": channel_ext, "kind": "dm"},
        "ext_id": ext_id,
        "ts": ts,
        "is_me": is_me,
        "kind": "text",
        "text": "hi",
        "mentions_me": False,
    }


def _peer_read(up_to: str, *, channel_ext: str = "c1") -> dict:
    return {"type": "peer_read", "channel": {"ext_id": channel_ext, "kind": "dm"}, "up_to_ext_id": up_to}


def _apply(db, account_id, events):
    return db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=events, cursor=None, now_ms=1000)
    )


def _channel(db, schema: str, ext_id: str = "c1") -> tuple[int, int, int | None]:
    with db.reader() as conn:
        return conn.execute(
            f"SELECT id, seq, peer_read_sort FROM {schema}.channel WHERE ext_id = ?",  # noqa: S608
            (ext_id,),
        ).fetchone()


# --- 1. ядро: только вперёд, назад — no-op без seq; чужой канал/аккаунт — как у `read` ----------------------


@pytest.mark.parametrize("schema", ["main", "work"])
def test_peer_read_moves_forward_only(db, schema):
    account_id = _insert_account(db, db_name=schema)
    _apply(db, account_id, [_upsert("10", 100), _upsert("12", 120)])
    _cid, seq0, marker0 = _channel(db, schema)
    assert marker0 is None

    result = _apply(db, account_id, [_peer_read("10")])
    assert (result.applied, result.noop, result.dead) == (1, 0, 0)
    _cid, seq1, marker1 = _channel(db, schema)
    assert marker1 == 10
    assert seq1 > seq0
    assert result.max_seq == seq1

    # Маркер дальше сообщений, которые хаб уже знает (догон раньше самого сообщения), — тоже вперёд.
    result = _apply(db, account_id, [_peer_read("15")])
    assert result.applied == 1
    _cid, seq2, marker2 = _channel(db, schema)
    assert marker2 == 15
    assert seq2 > seq1

    with db.reader() as conn:
        counter_before = conn.execute("SELECT value FROM main.seq_counter").fetchone()[0]
    for back in ("15", "12", "3"):
        result = _apply(db, account_id, [_peer_read(back)])
        assert (result.applied, result.noop) == (0, 1)
    with db.reader() as conn:
        counter_after = conn.execute("SELECT value FROM main.seq_counter").fetchone()[0]
    assert counter_after == counter_before
    assert _channel(db, schema)[1:] == (seq2, 15)


def test_peer_read_unknown_channel_other_account_and_non_numeric_are_noop(db):
    mine = _insert_account(db)
    other = _insert_account(db)
    _apply(db, mine, [_upsert("10", 100)])

    # Беседа неизвестна ядру — канал не создаётся (как у `read`).
    result = _apply(db, mine, [_peer_read("10", channel_ext="nope")])
    assert (result.applied, result.noop, result.dead) == (0, 1, 0)
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.channel WHERE ext_id = 'nope'").fetchone()[0] == 0

    # Тот же ext_id беседы, но другой аккаунт — чужой маркер не трогается.
    result = _apply(db, other, [_peer_read("10")])
    assert (result.applied, result.noop) == (0, 1)
    assert _channel(db, "main")[2] is None

    # Нечисловой id — позиции нет, no-op.
    result = _apply(db, mine, [_peer_read("m1")])
    assert (result.applied, result.noop, result.dead) == (0, 1, 0)
    assert _channel(db, "main")[2] is None


# --- 3 (выдача): `peer_read_sort` в словаре беседы — список, одна беседа, /sync -----------------------------


def test_peer_read_sort_in_channel_dicts(db):
    tg = _insert_account(db)
    slack = _insert_account(db, source="slack", db_name="work")
    _apply(db, tg, [_upsert("10", 100), _peer_read("10")])
    _apply(db, slack, [_upsert("1700000000.000100", 100, channel_ext="C1")])
    tg_cid = _channel(db, "main")[0]
    slack_cid = _channel(db, "work", "C1")[0]

    with db.reader() as conn:
        assert queries.get_channel(conn, UI, tg_cid)["peer_read_sort"] == 10
        assert queries.get_channel(conn, UI, slack_cid)["peer_read_sort"] is None
        listed = {c["id"]: c for c in queries.list_channels(conn, UI, -2)}
        assert listed[tg_cid]["peer_read_sort"] == 10
        assert listed[slack_cid]["peer_read_sort"] is None
        changes = queries.changes_since(conn, UI, since=0, limit=500)
    by_id = {c["id"]: c for c in changes.channels}
    assert by_id[tg_cid]["peer_read_sort"] == 10
    assert by_id[slack_cid]["peer_read_sort"] is None


# --- 2. миграция 0021 на копии БД предыдущей версии, обе схемы ---------------------------------------------


def test_migration_peer_read_sort_on_previous_version_both_files(tmp_path, monkeypatch):
    original = db_module._migrations

    def _before(*, main: bool):
        # Всё, что раньше 0021 (номер 0020 — параллельный кусок Ф12-2; проверяем колонку, не номер версии).
        return {v: files for v, files in original(main=main).items() if v < 21}

    monkeypatch.setattr(db_module, "_migrations", _before)
    old = Db(tmp_path)
    old.start()
    try:
        for schema, source in (("main", "telegram"), ("work", "slack")):
            acc = _insert_account(old, source=source, db_name=schema)
            old.write_sync(
                lambda conn, schema=schema, acc=acc: conn.execute(
                    f"INSERT INTO {schema}.channel (account_id, ext_id, kind, title, first_seen_ts) "  # noqa: S608
                    "VALUES (?, 'c1', 'dm', 'Вася', 0)",
                    (acc,),
                )
            )
        with old.reader() as conn:
            for schema in ("main", "work"):
                cols = {r[1] for r in conn.execute(f"PRAGMA {schema}.table_info(channel)")}
                assert "peer_read_sort" not in cols
    finally:
        old.close()

    monkeypatch.setattr(db_module, "_migrations", original)
    new = Db(tmp_path)
    new.start()
    try:
        with new.reader() as conn:
            for schema in ("main", "work"):
                cols = {r[1]: r for r in conn.execute(f"PRAGMA {schema}.table_info(channel)")}
                assert "peer_read_sort" in cols
                assert cols["peer_read_sort"][2] == "INTEGER"
                assert cols["peer_read_sort"][3] == 0  # NULL допустим
                rows = conn.execute(
                    f"SELECT title, peer_read_sort FROM {schema}.channel"  # noqa: S608
                ).fetchall()
                assert rows == [("Вася", None)]
    finally:
        new.close()


# --- 5. fake-коннектор умеет слать `peer_read` --------------------------------------------------------------


def test_fake_source_emits_peer_read(db):
    source = FakeSource()
    source.peer_read("c1", up_to_ext_id="10")
    events = source.events_since(0)
    assert events == [
        {"type": "peer_read", "channel": {"ext_id": "c1", "kind": "dm", "title": None}, "up_to_ext_id": "10"}
    ]
    account_id = _insert_account(db, source="fake")
    _apply(db, account_id, [_upsert("10", 100)])
    result = _apply(db, account_id, events)
    assert result.applied == 1
    assert _channel(db, "main")[2] == 10
