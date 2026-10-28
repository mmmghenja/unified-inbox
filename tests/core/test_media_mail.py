"""Ф11-3a A1/A2 (docs/architecture.md), тест 2: почта — источник медиа; вложения писем только
по клику (`deferred`), старое письмо без `type` получает строку по клику с видом из `mime`."""

import json

from hub.core import media
from hub.core.ingest import apply_batch

START = 1_000_000


def _account(db, source="mail"):
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, status) VALUES (?, 'main', 'ok')", (source,)
            ).lastrowid
        )
    )


def _upsert(ext_id, ts, attachments):
    return {
        "type": "upsert",
        "channel": {"ext_id": "777", "kind": "mail_thread"},
        "ext_id": ext_id,
        "ts": ts,
        "is_me": False,
        "kind": "text",
        "text": "Письмо",
        "attachments": attachments,
        "mentions_me": False,
    }


def _apply(db, account_id, events):
    return db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=events, cursor=None, now_ms=START + 10)
    )


def _rows(db):
    with db.reader() as conn:
        cur = conn.execute(
            "SELECT md.id, m.ext_id, md.idx, md.kind, md.status, md.priority, md.mime, md.name "
            "FROM main.media md JOIN main.message m ON m.id = md.message_id ORDER BY m.ext_id, md.idx"
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def _message_id(db, ext_id):
    with db.reader() as conn:
        return conn.execute("SELECT id FROM main.message WHERE ext_id = ?", (ext_id,)).fetchone()[0]


def test_mail_is_media_source_and_kind_rule():
    assert "mail" in media.MEDIA_SOURCES
    assert media.mail_attachment_kind("image/PNG") == "photo"
    assert media.mail_attachment_kind("image/jpeg; name=a.jpg") == "photo"
    assert media.mail_attachment_kind("image/svg+xml") == "document"
    assert media.mail_attachment_kind(None) == "document"


def test_new_letter_attachments_are_deferred_logo_not_fetched(db):
    account_id = _account(db)
    db.write_sync(lambda conn: media.ensure_start_ts(conn, START))
    small_logo = {"type": "photo", "name": "logo.png", "mime": "image/png", "size": 900}
    pdf = {"type": "document", "name": "счет.pdf", "mime": "application/pdf", "size": 1000}
    _apply(db, account_id, [_upsert("<a@x>", START + 1, [small_logo, pdf])])
    rows = _rows(db)
    assert [(r["idx"], r["kind"], r["status"]) for r in rows] == [
        (0, "photo", "deferred"),
        (1, "document", "deferred"),
    ]
    # логотип нового письма сам не качается: у аккаунта нечего выдавать загрузчику
    with db.reader() as conn:
        assert not media.has_leasable(conn, account_id=account_id, now_ms=START + 20)
    assert db.write_sync(lambda conn: media.lease(conn, account_id=account_id, now_ms=START + 20)) == []


def test_initial_status_mail_always_deferred():
    status = media.initial_status(
        kind="photo",
        size=10,
        msg_ts=START + 1,
        start_ts=START,
        source="mail",
        channel_kind="mail_thread",
        muted=False,
        archived=False,
        disk_ok=True,
    )
    assert status == "deferred"


def test_old_letter_without_type_fetch_creates_pending_row_by_mime(db):
    account_id = _account(db)
    db.write_sync(lambda conn: media.ensure_start_ts(conn, START))
    old = [
        {"name": "фото.jpg", "mime": "image/jpeg", "size": 2000},
        {"name": "договор.docx", "mime": "application/vnd.openxmlformats", "size": 3000},
    ]
    _apply(db, account_id, [_upsert("<old@x>", START - 100, old)])
    # письмо пришло до Ф11-3a: строк `media` у него нет
    db.write_sync(lambda conn: conn.execute("DELETE FROM main.media"))
    message_id = _message_id(db, "<old@x>")
    with db.reader() as conn:
        stored = json.loads(
            conn.execute("SELECT attachments_json FROM main.message WHERE id = ?", (message_id,)).fetchone()[
                0
            ]
        )
    assert all("type" not in a for a in stored)

    out = db.write_sync(
        lambda conn: media.request_fetch(conn, message_id=message_id, idx=0, now_ms=START + 5)
    )
    assert [(m["idx"], m["kind"], m["status"]) for m in out] == [(0, "photo", "pending")]
    out = db.write_sync(
        lambda conn: media.request_fetch(conn, message_id=message_id, idx=1, now_ms=START + 6)
    )
    assert [(m["idx"], m["kind"], m["status"]) for m in out] == [
        (0, "photo", "pending"),
        (1, "document", "pending"),
    ]
    rows = _rows(db)
    assert all(r["priority"] == 1 for r in rows)
    (job, _job2) = db.write_sync(
        lambda conn: media.lease(conn, account_id=account_id, now_ms=START + 10, limit=5)
    )
    assert job["message"]["ext_id"] == "<old@x>"
    assert job["max_bytes"] == media.CLICK_MAX_BYTES
    assert {job["name"], _job2["name"]} == {"фото.jpg", "договор.docx"}


def test_deferred_mail_row_fetch_goes_pending(db):
    account_id = _account(db)
    db.write_sync(lambda conn: media.ensure_start_ts(conn, START))
    _apply(
        db,
        account_id,
        [_upsert("<n@x>", START + 1, [{"type": "document", "name": "a.pdf", "mime": "application/pdf"}])],
    )
    message_id = _message_id(db, "<n@x>")
    out = db.write_sync(
        lambda conn: media.request_fetch(conn, message_id=message_id, idx=0, now_ms=START + 5)
    )
    assert out[0]["status"] == "pending"


def test_type_rule_only_for_mail_source(db):
    """Вложение без `type` у Telegram по-прежнему строки не получает (вывод из mime — только почта)."""
    account_id = _account(db, source="telegram")
    db.write_sync(lambda conn: media.ensure_start_ts(conn, START))
    _apply(db, account_id, [_upsert("1", START + 1, [{"name": "a.jpg", "mime": "image/jpeg"}])])
    assert _rows(db) == []
