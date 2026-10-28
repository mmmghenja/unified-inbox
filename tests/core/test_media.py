"""Ф8-5 (docs/architecture.md): строки `media` при приёме (T1, П6, П10–П14), очередь без HTTP (T3,
П15), миграция и точка отсчёта (T7), сигнатуры и уборка временных файлов (П5)."""

import json

import pytest

from hub.core import media, retention
from hub.core.db import WORK_ID_BASE
from hub.core.ingest import apply_batch

MIB = 1024 * 1024
START = 1_000_000


def _account(db, *, source="telegram", db_name="main", status="ok"):
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, status) VALUES (?, ?, ?)", (source, db_name, status)
            ).lastrowid
        )
    )


def _set_start(db, ts=START):
    db.write_sync(lambda conn: media.ensure_start_ts(conn, ts))


def _upsert(ext_id, ts, attachments, *, channel_ext="c1", channel_kind="dm", text=None, **extra):
    event = {
        "type": "upsert",
        "channel": {"ext_id": channel_ext, "kind": channel_kind},
        "ext_id": ext_id,
        "ts": ts,
        "is_me": False,
        "kind": "file",
        "text": text,
        "attachments": attachments,
        "mentions_me": False,
    }
    event.update(extra)
    return event


def _apply(db, account_id, events, now_ms=START + 10):
    return db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=events, cursor=None, now_ms=now_ms)
    )


def _rows(db, schema="main"):
    with db.reader() as conn:
        cur = conn.execute(
            "SELECT md.id, m.ext_id, md.idx, md.kind, md.status, md.size, md.ref_json, md.src_id, "
            "md.sha256, md.priority, md.attempts, md.lease_gen, md.not_before_ts, md.width "
            f"FROM {schema}.media md JOIN {schema}.message m ON m.id = md.message_id "
            "ORDER BY m.ext_id, md.idx"
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def _msg(db, ext_id, schema="main"):
    with db.reader() as conn:
        return conn.execute(
            f"SELECT id, seq, attachments_json FROM {schema}.message WHERE ext_id = ?", (ext_id,)
        ).fetchone()


def _set_ready(db, media_id, sha, schema="main"):
    db.write_sync(
        lambda conn: conn.execute(
            f"UPDATE {schema}.media SET status = 'ready', sha256 = ?, stored_size = 3, detected = 'jpeg' "
            "WHERE id = ?",
            (sha, media_id),
        )
    )


# --- T1 ------------------------------------------------------------------------------------------------


def test_t1_statuses_on_ingest(db):
    account_id = _account(db)
    _set_start(db)
    result = _apply(
        db,
        account_id,
        [
            _upsert("1", START + 1, [{"type": "photo", "width": 800, "height": 600}]),
            _upsert("2", START + 2, [{"type": "video", "size": 25 * MIB, "mime": "video/mp4"}]),
            _upsert("3", START + 3, [{"type": "video", "size": 5 * MIB}]),
            _upsert("4", START - 5, [{"type": "photo"}]),  # старше точки отсчёта (догон/стартовая загрузка)
            _upsert("5", START + 5, [{"type": "location"}]),
            _upsert("6", START + 6, [{"type": "document"}]),  # размер неизвестен
        ],
    )
    assert result.dead == 0
    rows = {(r["ext_id"], r["idx"]): r for r in _rows(db)}
    assert rows[("1", 0)]["status"] == "pending"
    assert rows[("1", 0)]["width"] == 800
    assert rows[("2", 0)]["status"] == "deferred"
    assert rows[("3", 0)]["status"] == "pending"
    assert rows[("4", 0)]["status"] == "deferred"
    assert ("5", 0) not in rows
    assert rows[("6", 0)]["status"] == "deferred"


def test_t1_media_ref_goes_to_row_not_attachments(db):
    account_id = _account(db, source="slack")
    _set_start(db)
    ref = {"file_id": "F1", "url": "https://files.slack.com/files-pri/T-F1/x.png"}
    _apply(db, account_id, [_upsert("1.0", START + 1, [{"type": "photo", "src_id": "F1", "media_ref": ref}])])
    _mid, _seq, attachments_json = _msg(db, "1.0")
    assert "media_ref" not in attachments_json
    assert "files.slack.com" not in attachments_json
    (row,) = _rows(db)
    assert json.loads(row["ref_json"]) == ref
    assert row["src_id"] == "F1"


def test_t1_repeat_and_edit_do_not_duplicate_or_reset_ready(db):
    account_id = _account(db)
    _set_start(db)
    att = [{"type": "photo", "src_id": "p1"}, {"type": "document", "size": 10, "src_id": "d1"}]
    _apply(db, account_id, [_upsert("1", START + 1, att)])
    rows = _rows(db)
    assert len(rows) == 2
    _set_ready(db, rows[0]["id"], "a" * 64)
    _mid, seq_before, _ = _msg(db, "1")

    again = _apply(db, account_id, [_upsert("1", START + 1, att)])
    assert again.applied == 0 and again.noop == 1
    assert _msg(db, "1")[1] == seq_before

    edit = _upsert("1", START + 1, att, text="подпись", edited_ts=START + 50)
    edit["type"] = "edit"
    _apply(db, account_id, [edit])
    rows_after = _rows(db)
    assert len(rows_after) == 2
    assert rows_after[0]["status"] == "ready"
    assert rows_after[0]["sha256"] == "a" * 64


def test_t1_src_id_change_resets_row_and_releases_file(db, tmp_path):
    account_id = _account(db)
    _set_start(db)
    _apply(db, account_id, [_upsert("1", START + 1, [{"type": "photo", "src_id": "old"}])])
    _apply(db, account_id, [_upsert("2", START + 2, [{"type": "photo", "src_id": "same"}])])
    rows = _rows(db)
    shared_sha = "b" * 64
    only_sha = "c" * 64
    _set_ready(db, rows[0]["id"], only_sha)
    _set_ready(db, rows[1]["id"], shared_sha)
    # ещё одна строка на shared_sha — файл общий
    _apply(db, account_id, [_upsert("3", START + 3, [{"type": "photo", "src_id": "same3"}])])
    third = [r for r in _rows(db) if r["ext_id"] == "3"][0]
    _set_ready(db, third["id"], shared_sha)
    for sha in (only_sha, shared_sha):
        path = media.file_path(tmp_path, account_id, sha)
        media.ensure_dir(path.parent)
        path.write_bytes(b"xyz")
    lease_gen_before = rows[0]["lease_gen"]

    _, seq_before, _ = _msg(db, "1")
    edit = _upsert("1", START + 1, [{"type": "photo", "src_id": "new"}], edited_ts=START + 60)
    edit["type"] = "edit"
    _apply(db, account_id, [edit])
    edit2 = _upsert("2", START + 2, [{"type": "photo", "src_id": "other"}], edited_ts=START + 60)
    edit2["type"] = "edit"
    _apply(db, account_id, [edit2])

    by_ext = {r["ext_id"]: r for r in _rows(db)}
    assert by_ext["1"]["status"] == "pending"
    assert by_ext["1"]["sha256"] is None
    assert by_ext["1"]["src_id"] == "new"
    assert by_ext["1"]["lease_gen"] == lease_gen_before + 1
    assert _msg(db, "1")[1] > seq_before
    assert not media.file_path(tmp_path, account_id, only_sha).exists()  # ссылок не осталось
    assert media.file_path(tmp_path, account_id, shared_sha).exists()  # на него ещё ссылается «3»


def test_p11_crooked_fields_ignored_not_dead_letter(db):
    account_id = _account(db)
    _set_start(db)
    result = _apply(
        db,
        account_id,
        [
            _upsert(
                "1", START + 1, [{"type": "photo", "size": "огромный", "media_ref": "строка", "width": True}]
            )
        ],
    )
    assert result.dead == 0
    (row,) = _rows(db)
    assert row["size"] is None and row["ref_json"] is None and row["width"] is None


def test_p11_huge_ints_ignored_not_dead_letter(db):
    """Ревью Ф8-5, находка 1: целое больше int64 в полях вложения не делает событие ядовитым."""
    account_id = _account(db)
    _set_start(db)
    result = _apply(
        db,
        account_id,
        [_upsert("1", START + 1, [{"type": "photo", "size": 2**64, "width": 10**30}])],
    )
    assert result.dead == 0
    (row,) = _rows(db)
    assert row["size"] is None and row["width"] is None


def test_p11_noop_branch_still_creates_rows(db):
    account_id = _account(db)
    _set_start(db)
    ev = _upsert("1", START + 1, [{"type": "photo"}])
    _apply(db, account_id, [ev])
    db.write_sync(lambda conn: conn.execute("DELETE FROM main.media"))
    _, seq_before, _ = _msg(db, "1")
    result = _apply(db, account_id, [ev])
    assert len(_rows(db)) == 1
    assert result.applied == 1  # П8: строка появилась — новый seq сообщения
    assert _msg(db, "1")[1] > seq_before


def test_p11_stale_edit_does_not_roll_back_replaced_file(db):
    account_id = _account(db)
    _set_start(db)
    fresh = _upsert("1", START + 1, [{"type": "photo", "src_id": "new"}], edited_ts=START + 100)
    fresh["type"] = "edit"
    _apply(db, account_id, [fresh])
    stale = _upsert("1", START + 1, [{"type": "photo", "src_id": "old"}], edited_ts=START + 50)
    stale["type"] = "edit"
    _apply(db, account_id, [stale])
    (row,) = _rows(db)
    assert row["src_id"] == "new"


def test_p11_purged_message_gets_no_rows(db, monkeypatch):
    # Ф8-4 П9: очистка при приёме — только при включённой ретенции (снята константой).
    monkeypatch.setattr(retention, "RETENTION_ENABLED", True)
    account_id = _account(db, db_name="work")
    _set_start(db, 0)
    # сообщение старше порога ретенции work ложится сразу очищенным
    _apply(db, account_id, [_upsert("x", 10 * 24 * 3600 * 1000, [{"type": "photo"}])], now_ms=10)
    now = 200 * 24 * 3600 * 1000
    _apply(db, account_id, [_upsert("y", now - 1000, [{"type": "photo"}], channel_ext="c2")], now_ms=now)
    _apply(db, account_id, [_upsert("z", 1000, [{"type": "photo"}], channel_ext="c3")], now_ms=now)
    exts = {r["ext_id"] for r in _rows(db, "work")}
    assert "z" not in exts
    assert "y" in exts


def test_p12_rules_muted_channel_tg_channel_and_disk(db, monkeypatch):
    account_id = _account(db)
    _set_start(db)
    _apply(
        db,
        account_id,
        [
            _upsert("1", START + 1, [{"type": "photo"}], channel_ext="ch", channel_kind="channel"),
            _upsert(
                "2", START + 2, [{"type": "video", "size": MIB}], channel_ext="ch", channel_kind="channel"
            ),
            _upsert(
                "3", START + 3, [{"type": "document", "size": MIB}], channel_ext="g", channel_kind="group"
            ),
        ],
    )
    by_ext = {r["ext_id"]: r["status"] for r in _rows(db)}
    assert by_ext == {"1": "pending", "2": "deferred", "3": "pending"}

    db.write_sync(lambda conn: conn.execute("UPDATE main.channel SET muted = 1 WHERE ext_id = 'g'"))
    _apply(
        db, account_id, [_upsert("4", START + 4, [{"type": "photo"}], channel_ext="g", channel_kind="group")]
    )
    assert {r["ext_id"]: r["status"] for r in _rows(db)}["4"] == "deferred"

    monkeypatch.setattr(media, "free_bytes", lambda _d: 4 * 1024**3)
    _apply(db, account_id, [_upsert("5", START + 5, [{"type": "photo"}])])
    assert {r["ext_id"]: r["status"] for r in _rows(db)}["5"] == "deferred"


def test_p12_slack_channel_video_is_automatic(db):
    account_id = _account(db, source="slack")
    _set_start(db)
    _apply(
        db, account_id, [_upsert("1.0", START + 1, [{"type": "video", "size": MIB}], channel_kind="channel")]
    )
    assert _rows(db)[0]["status"] == "pending"


def test_p6_known_photo_over_20mib_deferred(db):
    account_id = _account(db)
    _set_start(db)
    _apply(db, account_id, [_upsert("1", START + 1, [{"type": "photo", "size": 21 * MIB}])])
    assert _rows(db)[0]["status"] == "deferred"


def test_p13_p2_p14_no_rows(db):
    tg = _account(db)
    other = _account(db, source="fake")
    _set_start(db)
    _apply(
        db,
        tg,
        [
            _upsert("1", START + 1, [{"type": "photo", "ttl": True}]),
            _upsert("2", START + 2, [{"type": "document", "external": True, "size": 1}]),
        ],
    )
    _apply(db, other, [_upsert("m", START + 1, [{"type": "document", "size": 1}])])
    assert _rows(db) == []


def test_p10_tombstone_gone_but_ready_stays(db):
    account_id = _account(db, source="slack")
    _set_start(db)
    _apply(
        db,
        account_id,
        [
            _upsert("1.0", START + 1, [{"type": "photo", "src_id": "F1"}]),
            _upsert("2.0", START + 2, [{"type": "photo", "src_id": "F2"}]),
        ],
    )
    rows = _rows(db)
    _set_ready(db, rows[1]["id"], "d" * 64)
    for ext in ("1.0", "2.0"):
        ev = _upsert(
            ext, START + 1, [{"type": "photo", "src_id": f"F{ext[0]}", "gone": True}], edited_ts=START + 9
        )
        ev["type"] = "edit"
        _apply(db, account_id, [ev])
    by_ext = {r["ext_id"]: r["status"] for r in _rows(db)}
    assert by_ext == {"1.0": "gone", "2.0": "ready"}
    # tombstone без строки — строки не заводит
    _apply(db, account_id, [_upsert("3.0", START + 3, [{"type": "photo", "gone": True}])])
    assert len(_rows(db)) == 2


def test_work_account_rows_in_work_with_high_ids(db):
    account_id = _account(db, source="slack", db_name="work")
    _set_start(db)
    _apply(db, account_id, [_upsert("1.0", START + 1, [{"type": "photo"}])])
    (row,) = _rows(db, "work")
    assert row["id"] > WORK_ID_BASE
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.media").fetchone()[0] == 0


# --- T7 ------------------------------------------------------------------------------------------------


def test_t7_migration_both_schemas_and_start_ts_once(db):
    with db.reader() as conn:
        for schema in ("main", "work"):
            cols = {r[1] for r in conn.execute(f"PRAGMA {schema}.table_info(media)")}
            assert {"account_id", "src_id", "ref_json", "lease_gen", "sha256", "detected"} <= cols
        assert media.get_start_ts(conn) is None
    assert db.write_sync(lambda conn: media.ensure_start_ts(conn, 111)) == 111
    assert db.write_sync(lambda conn: media.ensure_start_ts(conn, 222)) == 111
    with db.reader() as conn:
        assert media.get_start_ts(conn) == 111


def test_t7_existing_work_db_seeded_on_upgrade(tmp_path):
    """Файл work, созданный ДО миграции медиа, после неё выдаёт id медиа > 2^40."""
    from hub.core import db as db_mod

    real = db_mod._migrations

    def _without_media(*, main):
        return {v: files for v, files in real(main=main).items() if v < 11}

    old = db_mod.Db(tmp_path)
    db_mod._migrations = _without_media
    try:
        old.start()
    finally:
        db_mod._migrations = real
    old.close()

    new = db_mod.Db(tmp_path)
    new.start()
    try:
        account_id = _account(new, source="slack", db_name="work")
        _set_start(new)
        _apply(new, account_id, [_upsert("1.0", START + 1, [{"type": "photo"}])])
        assert _rows(new, "work")[0]["id"] > WORK_ID_BASE
    finally:
        new.close()


# --- T3 и П15 без HTTP -----------------------------------------------------------------------------------


def _pending(db, account_id, n=1, *, ts0=START + 1):
    _apply(db, account_id, [_upsert(str(i), ts0 + i, [{"type": "photo"}]) for i in range(n)])
    return _rows(db)


def _lease(db, account_id, now, limit=1):
    return db.write_sync(lambda conn: media.lease(conn, account_id=account_id, now_ms=now, limit=limit))


def _report(db, account_id, media_id, lease_gen, outcome, now, *, error=None, retry_after_s=None, size=None):
    return db.write_sync(
        lambda conn: media.report(
            conn,
            media_id=media_id,
            account_id=account_id,
            lease_gen=lease_gen,
            outcome=outcome,
            error=error,
            retry_after_s=retry_after_s,
            now_ms=now,
            size=size,
        )
    )


def test_t3_report_gone_temporary_backoff_and_failed(db):
    account_id = _account(db)
    _set_start(db)
    _pending(db, account_id)
    now = START + 1000
    (job,) = db.write_sync(lambda conn: media.lease(conn, account_id=account_id, now_ms=now))
    mid = job["media_id"]

    expected_backoff = [60_000, 300_000, 1_800_000, 1_800_000]
    for attempt, backoff in enumerate(expected_backoff, start=1):
        _report(db, account_id, mid, job["lease_gen"], "temporary", now, error="x")
        (row,) = _rows(db)
        assert row["status"] == "pending" and row["attempts"] == attempt
        assert row["not_before_ts"] == now + backoff
        # до not_before задания нет
        assert _lease(db, account_id, now) == []
        now += backoff
        (job,) = _lease(db, account_id, now)
    db.write_sync(
        lambda conn: media.report(
            conn,
            media_id=mid,
            account_id=account_id,
            lease_gen=job["lease_gen"],
            outcome="temporary",
            error=None,
            retry_after_s=None,
            now_ms=now,
        )
    )
    assert _rows(db)[0]["status"] == "failed"


def test_t3_gone_and_flood_not_counted(db):
    account_id = _account(db)
    _set_start(db)
    _pending(db, account_id, 2)
    now = START + 1000
    jobs = db.write_sync(lambda conn: media.lease(conn, account_id=account_id, now_ms=now, limit=5))
    assert len(jobs) == 2
    a, b = jobs
    db.write_sync(
        lambda conn: media.report(
            conn,
            media_id=a["media_id"],
            account_id=account_id,
            lease_gen=a["lease_gen"],
            outcome="gone",
            error=None,
            retry_after_s=None,
            now_ms=now,
        )
    )
    db.write_sync(
        lambda conn: media.report(
            conn,
            media_id=b["media_id"],
            account_id=account_id,
            lease_gen=b["lease_gen"],
            outcome="temporary",
            error="flood",
            retry_after_s=42,
            now_ms=now,
        )
    )
    by_id = {r["id"]: r for r in _rows(db)}
    assert by_id[a["media_id"]]["status"] == "gone"
    assert by_id[b["media_id"]]["status"] == "pending"
    assert by_id[b["media_id"]]["attempts"] == 0
    assert by_id[b["media_id"]]["not_before_ts"] == now + 42_000
    # отчёт по уже закрытому лизу — устарел
    with pytest.raises(media.StaleLease):
        db.write_sync(
            lambda conn: media.report(
                conn,
                media_id=a["media_id"],
                account_id=account_id,
                lease_gen=a["lease_gen"],
                outcome="gone",
                error=None,
                retry_after_s=None,
                now_ms=now,
            )
        )


def test_t3_expired_lease_back_to_pending_attempt_counted(db):
    account_id = _account(db)
    _set_start(db)
    _pending(db, account_id)
    now = START + 1000
    (job,) = db.write_sync(lambda conn: media.lease(conn, account_id=account_id, now_ms=now))
    with db.reader() as conn:
        assert not media.has_leasable(conn, account_id=account_id, now_ms=now + 1000)
        assert media.has_leasable(conn, account_id=account_id, now_ms=now + media.LEASE_MS)
    later = now + media.LEASE_MS
    (again,) = db.write_sync(lambda conn: media.lease(conn, account_id=account_id, now_ms=later))
    assert again["media_id"] == job["media_id"]
    assert again["lease_gen"] == job["lease_gen"] + 1
    assert _rows(db)[0]["attempts"] == 1


def test_p15_order_priority_then_fresh_message(db):
    account_id = _account(db)
    _set_start(db)
    _apply(
        db,
        account_id,
        [_upsert(str(i), START + i, [{"type": "photo"}]) for i in (1, 2, 3)]
        + [_upsert("old", START - 100, [{"type": "photo"}])],
    )
    old_id = _msg(db, "old")[0]
    db.write_sync(lambda conn: media.request_fetch(conn, message_id=old_id, idx=0, now_ms=START + 5))
    jobs = db.write_sync(lambda conn: media.lease(conn, account_id=account_id, now_ms=START + 10, limit=10))
    assert [j["message"]["ext_id"] for j in jobs] == ["old", "3", "2", "1"]
    assert jobs[0]["max_bytes"] == media.CLICK_MAX_BYTES
    assert jobs[1]["max_bytes"] == media.AUTO_MAX_BYTES


def test_p15_limited_account_gets_nothing(db):
    account_id = _account(db)
    _set_start(db)
    _pending(db, account_id)
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.account SET status = 'limited' WHERE id = ?", (account_id,))
    )
    with db.reader() as conn:
        assert not media.has_leasable(conn, account_id=account_id, now_ms=START + 10)
    assert db.write_sync(lambda conn: media.lease(conn, account_id=account_id, now_ms=START + 10)) == []


def test_p8_lease_and_report_bump_message_seq(db):
    account_id = _account(db)
    _set_start(db)
    _pending(db, account_id)
    seq0 = _msg(db, "0")[1]
    (job,) = db.write_sync(lambda conn: media.lease(conn, account_id=account_id, now_ms=START + 10))
    seq1 = _msg(db, "0")[1]
    db.write_sync(
        lambda conn: media.report(
            conn,
            media_id=job["media_id"],
            account_id=account_id,
            lease_gen=job["lease_gen"],
            outcome="auth",
            error=None,
            retry_after_s=None,
            now_ms=START + 11,
        )
    )
    seq2 = _msg(db, "0")[1]
    assert seq0 < seq1 < seq2
    assert _rows(db)[0]["status"] == "failed"


# --- сигнатуры и уборка ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"\xff\xd8\xff\xe0\x00\x10JFIF", "jpeg"),
        (b"\x89PNG\r\n\x1a\n\x00\x00", "png"),
        (b"GIF89a\x01\x00", "gif"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "webp"),
        (b"%PDF-1.7\n", "pdf"),
        (b"OggS\x00\x02", "ogg"),
        (b"\x00\x00\x00\x18ftypmp42\x00\x00", "mp4"),
        (b"\x00\x00\x00\x18ftypisom\x00\x00", "mp4"),
        (b"\x00\x00\x00\x18ftypM4A \x00\x00", "m4a"),
        (b"\x00\x00\x00\x18ftypheic\x00\x00", "other"),
        (b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\x82\x84webm", "webm"),
        (b"ID3\x04\x00", "mp3"),
        (b"\xff\xfb\x90\x00", "mp3"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'>", "other"),
        (b"<!doctype html><script>", "other"),
        (b"", "other"),
    ],
)
def test_detect(head, expected):
    assert media.detect(head) == expected


def test_p5_cleanup_tmp(tmp_path):
    root = tmp_path / "media"
    keep = root / "1" / "ab" / ("ab" + "0" * 62)
    keep.parent.mkdir(parents=True)
    keep.write_bytes(b"k")
    tmp_core = root / "1" / ".tmp-123"
    tmp_core.write_bytes(b"t")
    conn_tmp = root / ".tmp-conn" / "1" / "part"
    conn_tmp.parent.mkdir(parents=True)
    conn_tmp.write_bytes(b"p")
    media.cleanup_tmp(tmp_path)
    assert keep.exists()
    assert not tmp_core.exists()
    assert not conn_tmp.exists()
    media.cleanup_tmp(tmp_path / "nope")  # нет каталога — не падает


def test_contract5_report_too_big_with_size(db):
    """Контракт 5b, п.5: `too_big` с `size` — `deferred` с этим размером в пределах 300 МиБ, иначе `too_big`;
    без размера — по `priority` задания."""
    account_id = _account(db)
    _set_start(db)
    _pending(db, account_id, 3)
    now = START + 1000
    a, b, c = _lease(db, account_id, now, limit=5)
    _report(db, account_id, a["media_id"], a["lease_gen"], "too_big", now, size=50 * MIB)
    _report(db, account_id, b["media_id"], b["lease_gen"], "too_big", now, size=301 * MIB)
    _report(db, account_id, c["media_id"], c["lease_gen"], "too_big", now)
    by_id = {r["id"]: r for r in _rows(db)}
    assert (by_id[a["media_id"]]["status"], by_id[a["media_id"]]["size"]) == ("deferred", 50 * MIB)
    assert (by_id[b["media_id"]]["status"], by_id[b["media_id"]]["size"]) == ("too_big", 301 * MIB)
    assert by_id[c["media_id"]]["status"] == "deferred"  # задание без priority — «больше 20 МиБ»


def test_contract1_flags_stay_in_attachments_json(db):
    """Контракт 5b, п.1: `ttl`/`external`/`gone` строк не заводят, но остаются во вложении (UI рисует
    надпись); `src_id` — строка верхнего уровня вложения; `media_ref` вынимается."""
    account_id = _account(db, source="slack")
    _set_start(db)
    att = [
        {"type": "photo", "ttl": True},
        {"type": "document", "external": True, "src_id": "F2"},
        {"type": "photo", "gone": True, "src_id": "F3"},
        {"type": "photo", "src_id": "F4", "media_ref": {"file_id": "F4", "url": "https://files.slack.com/x"}},
    ]
    _apply(db, account_id, [_upsert("1.0", START + 1, att)])
    stored = json.loads(_msg(db, "1.0")[2])
    assert stored[0]["ttl"] is True and stored[1]["external"] is True and stored[2]["gone"] is True
    assert "media_ref" not in stored[3] and stored[3]["src_id"] == "F4"
    rows = _rows(db)
    assert [(r["idx"], r["src_id"]) for r in rows] == [(3, "F4")]


# --- старт ядра и hub-watch -------------------------------------------------------------------------------


async def test_core_start_cleans_tmp_and_sets_start_ts_once(tmp_path, monkeypatch):
    """П5 + A1/П15: старт `hub core` убирает недописанные загрузки и ставит `media_start_ts` часами `Db` —
    один раз: второй старт точку отсчёта не двигает."""
    from hub.core import runner as runner_mod
    from hub.core.db import Db

    data = tmp_path / "data"
    cfg = tmp_path / "cfg"
    data.mkdir()
    cfg.mkdir()
    stale = data / "media" / "1" / ".tmp-dead"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"x")

    def boom(*_a, **_k):
        raise RuntimeError("стоп после старта БД")

    monkeypatch.setattr(runner_mod, "create_public_app", boom)
    for clock_s in (1.0, 5.0):
        with pytest.raises(RuntimeError, match="стоп после старта БД"):
            await runner_mod._serve_forever(
                data, cfg, host="127.0.0.1", port=0, clock=lambda s=clock_s: s, ui_dir=None
            )
    assert not stale.exists()
    check = Db(data)
    check.start()
    try:
        with check.reader() as conn:
            assert media.get_start_ts(conn) == 1000
    finally:
        check.close()


def test_watch_disk_low_problem():
    from hub.watch import evaluate

    base = {"accounts": [], "dead_letter_total": 0, "outbox_unknown": 0}
    ok = evaluate({**base, "media": {"total_bytes": 0, "free_bytes": 6 * 1024**3}}, now_ms=1)
    assert [p.key for p in ok] == []
    (problem,) = evaluate(
        {**base, "media": {"total_bytes": 2 * 1024**3, "free_bytes": 3 * 1024**3}}, now_ms=1
    )
    assert problem.key == "disk_low"
    assert problem.text == "медиа: 2.0 ГБ, свободно 3.0 ГБ"


# --- Ф32-2 п.5б + П5: поздний отчёт после истёкшего лиза попытку второй раз не считает ---------------------


def test_f322_late_temporary_after_expired_lease_not_counted_twice(db):
    account_id = _account(db)
    _set_start(db)
    _pending(db, account_id, 2)
    now = START + 1000
    job, other = _lease(db, account_id, now, limit=2)
    later = now + media.LEASE_MS
    # лиз обоих истёк: истёкший лиз засчитан попыткой, `lease_gen` прежний
    db.write_sync(lambda conn: media._expire_leases(conn, "main", account_id, later))  # noqa: SLF001
    row = next(r for r in _rows(db) if r["id"] == job["media_id"])
    assert (row["status"], row["attempts"], row["lease_gen"]) == ("pending", 1, job["lease_gen"])

    _report(db, account_id, job["media_id"], job["lease_gen"], "temporary", later + 5, error="timeout")
    row = next(r for r in _rows(db) if r["id"] == job["media_id"])
    assert (row["status"], row["attempts"]) == ("pending", 1)  # не 2
    assert row["not_before_ts"] == later + 5 + media._backoff_ms(1)  # noqa: SLF001

    # поздний `ready` того же поколения по-прежнему принимается (П4 Ф8-5): лиз жив, пока строку не перехватили
    with db.reader() as conn:
        assert media.check_lease(
            conn, media_id=other["media_id"], account_id=account_id, lease_gen=other["lease_gen"]
        )


def test_f322_late_temporary_keeps_failed(db):
    account_id = _account(db)
    _set_start(db)
    _pending(db, account_id)
    now = START + 1000
    (job,) = _lease(db, account_id, now)
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.media SET attempts = ? WHERE id = ?", (media.MAX_ATTEMPTS - 1, job["media_id"])
        )
    )
    later = now + media.LEASE_MS
    db.write_sync(lambda conn: media._expire_leases(conn, "main", account_id, later))  # noqa: SLF001
    assert (_rows(db)[0]["status"], _rows(db)[0]["attempts"]) == ("failed", media.MAX_ATTEMPTS)
    for retry_after_s in (None, 30):  # и с FloodWait: `failed` не оживает
        _report(
            db,
            account_id,
            job["media_id"],
            job["lease_gen"],
            "temporary",
            later + 5,
            error="timeout",
            retry_after_s=retry_after_s,
        )
        row = _rows(db)[0]
        assert (row["status"], row["attempts"]) == ("failed", media.MAX_ATTEMPTS)


def test_f322_temporary_within_lease_still_counted(db):
    """Регресс: отчёт по живому лизу (строка `fetching`) попытку считает, как раньше."""
    account_id = _account(db)
    _set_start(db)
    _pending(db, account_id)
    now = START + 1000
    (job,) = _lease(db, account_id, now)
    _report(db, account_id, job["media_id"], job["lease_gen"], "temporary", now + 5, error="timeout")
    row = _rows(db)[0]
    assert (row["status"], row["attempts"]) == ("pending", 1)
