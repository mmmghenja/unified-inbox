"""Ретенция work-БД (спека `docs/architecture.md`). Только work — main не
трогается ни разу."""

import json

import pytest

from hub.core import retention
from hub.core.ingest import apply_batch

_DAY_MS = 24 * 60 * 60 * 1000


@pytest.fixture(autouse=True)
def _retention_enabled(monkeypatch):
    """Ф8-4 П9: ретенция снята константой `RETENTION_ENABLED`; её собственные тесты включают её обратно."""
    monkeypatch.setattr(retention, "RETENTION_ENABLED", True)


def _insert_account(db, *, source="slack", db_name="work"):
    return db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.account (source, db) VALUES (?, ?)", (source, db_name)).lastrowid
        )
    )


def _upsert(ext_id, ts, *, channel_ext="c1", text="уникальноеслово DEV-123 https://x.example/1", **extra):
    event = {
        "type": "upsert",
        "channel": {"ext_id": channel_ext, "kind": "channel"},
        "ext_id": ext_id,
        "ts": ts,
        "is_me": False,
        "kind": "text",
        "text": text,
        "mentions_me": False,
        "attachments": [{"type": "photo", "name": "секретное_имя.jpg", "size": 123}],
        "refs": ["DEV-123", "https://x.example/1"],
    }
    event.update(extra)
    return event


def _apply(db, account_id, events, now_ms):
    return db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=events, cursor=None, now_ms=now_ms)
    )


def _seed_anchor(db, account_id, now_ms):
    """Свежее сообщение (ts ~ now_ms) — держит `retention.reference_ts` привязанным к реальному «сейчас»
    (раздел F.3: `min(now, MAX(ts)+1 день)`). Без него в тестах, где единственное сообщение — старое, опорное
    время застревало бы на этом старом ts НАВСЕГДА, и 91-дневное сообщение никогда бы не считалось старше
    90 дней ОТНОСИТЕЛЬНО опорного времени (что математически верно для формулы, но не то, что проверяет тест:
    реальная система всегда получает свежие сообщения, отсюда MAX(ts) ~ now)."""
    _apply(db, account_id, [_upsert("anchor", now_ms, channel_ext="anchor-channel")], now_ms=now_ms)


def _message_row(db, ext_id, *, schema="work"):
    with db.reader() as conn:
        return conn.execute(
            f"SELECT text, entities_json, forward_json, attachments_json, refs_json, purged, seq "
            f"FROM {schema}.message WHERE ext_id = ?",
            (ext_id,),
        ).fetchone()


def test_clean_message_content_keeps_task_key_drops_url_and_names():
    content = {
        "text": "текст",
        "entities_json": json.dumps([{"type": "bold"}]),
        "forward_json": json.dumps({"from_name": "X"}),
        "attachments_json": json.dumps([{"type": "photo", "name": "a.jpg", "size": 1}]),
        "refs_json": json.dumps(["DEV-123", "https://x.example/1"]),
    }
    retention.clean_message_content(content)
    assert content["text"] is None
    assert content["entities_json"] is None
    assert content["forward_json"] is None
    assert json.loads(content["attachments_json"]) == [{"type": "photo"}]
    assert json.loads(content["refs_json"]) == ["DEV-123"]


def test_clean_message_content_empty_results_become_null():
    content = {
        "text": "текст",
        "entities_json": None,
        "forward_json": None,
        "attachments_json": json.dumps([]),
        "refs_json": json.dumps(["https://x.example/1"]),
    }
    retention.clean_message_content(content)
    assert content["attachments_json"] is None
    assert content["refs_json"] is None


def test_91_days_old_message_purged_by_background_run(db):
    account_id = _insert_account(db)
    now0 = 100 * _DAY_MS
    _apply(db, account_id, [_upsert("m91", now0 - 91 * _DAY_MS)], now_ms=now0 - 91 * _DAY_MS)
    _apply(db, account_id, [_upsert("m89", now0 - 89 * _DAY_MS)], now_ms=now0)  # свежее, не должно тронуться
    _seed_anchor(db, account_id, now0)

    seq_before_run = _message_row(db, "m91")[6]

    result = retention.run_once(db, now0)
    assert result is not None
    assert result["messages"] == 1

    row91 = _message_row(db, "m91")
    text, entities, forward, attachments, refs, purged, seq_after_run = row91
    assert text is None
    assert entities is None
    assert forward is None
    assert json.loads(attachments) == [{"type": "photo"}]
    assert json.loads(refs) == ["DEV-123"]
    assert purged == 1
    assert seq_after_run == seq_before_run  # раздел F.3: seq НЕ растёт от периодического прогона

    row89 = _message_row(db, "m89")
    assert row89[0] is not None  # 89 дней — не тронуто
    assert row89[5] == 0

    # FTS не находит очищенный текст
    with db.reader() as conn:
        found = conn.execute(
            "SELECT rowid FROM work.message_fts WHERE message_fts MATCH 'уникальноеслово'"
        ).fetchall()
    ext_ids_found = set()
    if found:
        with db.reader() as conn:
            for (rid,) in found:
                ext_ids_found.add(
                    conn.execute("SELECT ext_id FROM work.message WHERE id = ?", (rid,)).fetchone()[0]
                )
    assert "m91" not in ext_ids_found
    assert "m89" in ext_ids_found


def test_repeated_run_is_noop_without_audit(db):
    account_id = _insert_account(db)
    now0 = 100 * _DAY_MS
    _apply(db, account_id, [_upsert("m1", now0 - 91 * _DAY_MS)], now_ms=now0 - 91 * _DAY_MS)
    _seed_anchor(db, account_id, now0)

    result1 = retention.run_once(db, now0)
    assert result1 is not None

    with db.reader() as conn:
        audit_count_before = conn.execute(
            "SELECT COUNT(*) FROM main.audit WHERE action = 'retention_purge'"
        ).fetchone()[0]
        seq_before = conn.execute("SELECT seq FROM work.message WHERE ext_id = 'm1'").fetchone()[0]

    result2 = retention.run_once(db, now0 + 1000)
    assert result2 is None

    with db.reader() as conn:
        audit_count_after = conn.execute(
            "SELECT COUNT(*) FROM main.audit WHERE action = 'retention_purge'"
        ).fetchone()[0]
        seq_after = conn.execute("SELECT seq FROM work.message WHERE ext_id = 'm1'").fetchone()[0]
    assert audit_count_after == audit_count_before == 1
    assert seq_after == seq_before


def test_reingest_of_purged_row_does_not_resurrect_text(db):
    account_id = _insert_account(db)
    now0 = 100 * _DAY_MS
    old_ts = now0 - 91 * _DAY_MS
    _apply(db, account_id, [_upsert("m1", old_ts)], now_ms=old_ts)
    _seed_anchor(db, account_id, now0)
    retention.run_once(db, now0)

    with db.reader() as conn:
        seq_before = conn.execute("SELECT seq FROM work.message WHERE ext_id = 'm1'").fetchone()[0]

    # тот же самый сырой upsert приходит снова (повторное событие/сверка) — текст не должен вернуться
    result = _apply(db, account_id, [_upsert("m1", old_ts)], now_ms=now0 + 1000)
    assert result.applied == 0
    with db.reader() as conn:
        text, seq_after = conn.execute(
            "SELECT text, seq FROM work.message WHERE ext_id = 'm1'"
        ).fetchone()
    assert text is None
    assert seq_after == seq_before


def test_insert_of_100_day_old_history_message_lands_already_purged(db):
    account_id = _insert_account(db)
    now0 = 100 * _DAY_MS
    result = _apply(db, account_id, [_upsert("hist", now0 - 100 * _DAY_MS)], now_ms=now0)
    assert result.applied == 1
    row = _message_row(db, "hist")
    assert row[0] is None  # text
    assert row[5] == 1  # purged


def test_clock_jump_forward_one_year_purges_nothing(db):
    account_id = _insert_account(db)
    now0 = 100 * _DAY_MS
    _apply(db, account_id, [_upsert("recent", now0 - _DAY_MS)], now_ms=now0)

    result = retention.run_once(db, now0 + 365 * _DAY_MS)
    assert result is None
    row = _message_row(db, "recent")
    assert row[0] is not None
    assert row[5] == 0


def test_main_account_never_touched(db):
    account_id = _insert_account(db, source="tg", db_name="main")
    now0 = 100 * _DAY_MS
    _apply(db, account_id, [_upsert("m1", now0 - 200 * _DAY_MS)], now_ms=now0 - 200 * _DAY_MS)
    result = retention.run_once(db, now0)
    assert result is None
    row = _message_row(db, "m1", schema="main")
    assert row[0] is not None
    assert row[5] == 0


def test_outbox_and_dead_letter_purged(db):
    from hub.core import outbox as outbox_mod

    account_id = _insert_account(db)
    token_id = db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.api_token (name, kind, hash) VALUES ('t', 'ui', 'x')"
        ).lastrowid
    )
    now0 = 100 * _DAY_MS
    old_ts = now0 - 91 * _DAY_MS
    _apply(db, account_id, [_upsert("p1", old_ts)], now_ms=old_ts)
    _seed_anchor(db, account_id, now0)
    channel_id = db.write_sync(
        lambda conn: conn.execute("SELECT id FROM work.channel WHERE ext_id = 'c1'").fetchone()[0]
    )

    def _enqueue(conn):
        return outbox_mod.enqueue(
            conn,
            channel_id=channel_id,
            text="старое сообщение",
            origin="ui",
            api_token_id=token_id,
            idempotency_key="k1",
            now_ms=old_ts,
        )

    enq = db.write_sync(_enqueue)

    def _dead_letter(conn):
        conn.execute(
            "INSERT INTO work.dead_letter (account_id, payload, error, ts) VALUES (?, '{}', 'x', ?)",
            (account_id, old_ts),
        )

    db.write_sync(_dead_letter)

    result = retention.run_once(db, now0)
    assert result is not None
    assert result["outbox"] == 1
    assert result["dead_letter"] == 1

    with db.reader() as conn:
        outbox_text = conn.execute(
            "SELECT text FROM work.outbox WHERE id = ?", (enq.outbox_id,)
        ).fetchone()[0]
        dead_count = conn.execute("SELECT COUNT(*) FROM work.dead_letter").fetchone()[0]
    assert outbox_text == ""
    assert dead_count == 0


def test_auto_vacuum_is_incremental(db):
    with db.reader() as conn:
        mode = conn.execute("PRAGMA work.auto_vacuum").fetchone()[0]
    assert mode == 2


def test_word_physically_absent_from_work_db_and_wal_bytes(db):
    """Тест 9, самая важная часть: уникальное слово отсутствует в БАЙТАХ файлов `hub-work.db` и его `-wal`
    после прогона — не только в выдаче API/FTS. Проверяет весь раздел F.4: `secure_delete` (db.py), FTS
    `optimize`, `incremental_vacuum` (с `.fetchall()`), `wal_checkpoint(TRUNCATE)`.

    Поправки после opus-критика кода (R2): точная (верхний регистр) форма слова ничего не говорит про FTS —
    `unicode61` хранит токены в НИЖНЕМ регистре с префиксным сжатием, и без `optimize` этот тест был бы слеп
    к оставшемуся FTS-хвосту (см. `test_ret3_committed_byte_test_is_blind_to_fts` в `test_critic_2b.py`, где
    `optimize` намеренно выключен и это видно). Здесь дополнительно ищем хвост нижнего регистра — то, что
    реально лежит в служебных таблицах FTS."""
    needle = "СОВЕРШЕННОУНИКАЛЬНОЕСЛОВОДЛЯБАЙТОВОГОТЕСТА"
    account_id = _insert_account(db)
    now0 = 100 * _DAY_MS
    old_ts = now0 - 91 * _DAY_MS
    _apply(db, account_id, [_upsert("secret", old_ts, text=f"текст с {needle} внутри")], now_ms=old_ts)
    _seed_anchor(db, account_id, now0)

    result = retention.run_once(db, now0)
    assert result is not None
    assert result["messages"] == 1

    work_db_path = db.data_dir / "hub-work.db"
    work_wal_path = db.data_dir / "hub-work.db-wal"
    needle_bytes = needle.encode("utf-8")
    lower_tail_bytes = needle.lower()[-12:].encode("utf-8")

    db_bytes = work_db_path.read_bytes()
    assert needle_bytes not in db_bytes
    assert lower_tail_bytes not in db_bytes

    if work_wal_path.exists():
        wal_bytes = work_wal_path.read_bytes()
        assert needle_bytes not in wal_bytes
        assert lower_tail_bytes not in wal_bytes


def test_freelist_fully_reclaimed_after_large_purge(db):
    """Приёмка 2b (архитектор): `incremental_vacuum` строк не возвращает — прежний цикл «пока есть строки»
    выходил после первой порции (2000 страниц). После прогона свободных страниц в work не остаётся."""
    account_id = _insert_account(db)
    now0 = 100 * _DAY_MS
    old_ts = now0 - 91 * _DAY_MS
    big = "х" * 4000
    events = [_upsert(f"m{i}", old_ts + i, text=f"{big} {i}") for i in range(1500)]
    for start in range(0, len(events), 100):
        _apply(db, account_id, events[start : start + 100], now_ms=old_ts)
    _seed_anchor(db, account_id, now0)

    result = retention.run_once(db, now0)
    assert result is not None and result["messages"] == 1500
    with db.reader() as conn:
        assert conn.execute("PRAGMA work.freelist_count").fetchone()[0] == 0
