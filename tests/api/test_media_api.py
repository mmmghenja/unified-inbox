"""Ф8-5 (docs/architecture.md): публичные ручки медиа — T4 (`GET /media/{id}`: тип по сигнатуре,
заголовки ровно по разу, скоуп проекта, не `ready` -> 404), T5 (`POST …/fetch`), П5 (нет файла -> `deferred`),
П8 (`seq` и ответ `fetch`), П9 (Range, `Content-Disposition`), П14, A3 (`media` во всех выдачах сообщений),
`/status` (П12). Скрытое сообщение -> 404 — в Ф8-4 (поправка П0)."""

import hashlib

from hub.core import media
from hub.core.ingest import apply_batch

from .conftest import (
    csrf_headers,
    insert_account,
    insert_channel,
    insert_project,
    login_via_code,
    make_bearer_token,
)

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 100 + b"end"
HTML = b"<!doctype html><script>alert(document.cookie)</script>"
SVG = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
MIB = 1024 * 1024


def _setup(db, *, source="telegram", project=True):
    account_id = insert_account(db, source=source)
    project_id = insert_project(db, name="P") if project else None
    channel_id = insert_channel(db, account_id, project_id=project_id, ext_id="c1")
    return account_id, project_id, channel_id


def _ingest(db, account_id, ext_id, attachments, *, ts=10_000, thread_ext_id=None):
    event = {
        "type": "upsert",
        "channel": {"ext_id": "c1", "kind": "dm"},
        "ext_id": ext_id,
        "ts": ts,
        "is_me": False,
        "kind": "file",
        "attachments": attachments,
        "mentions_me": False,
        "thread_ext_id": thread_ext_id,
    }
    result = db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=[event], cursor=None, now_ms=ts)
    )
    assert result.dead == 0
    with db.reader() as conn:
        return conn.execute("SELECT id FROM main.message WHERE ext_id = ?", (ext_id,)).fetchone()[0]


def _media_id(db, message_id, idx=0):
    with db.reader() as conn:
        row = conn.execute(
            "SELECT id FROM main.media WHERE message_id = ? AND idx = ?", (message_id, idx)
        ).fetchone()
    return row[0] if row else None


def _make_ready(db, account_id, media_id, body):
    sha = hashlib.sha256(body).hexdigest()
    path = media.file_path(db.data_dir, account_id, sha)
    media.ensure_dir(path.parent)
    path.write_bytes(body)
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.media SET status = 'ready', sha256 = ?, stored_size = ?, detected = ? WHERE id = ?",
            (sha, len(body), media.detect(body[:64]), media_id),
        )
    )
    return path


def _row(db, media_id):
    with db.reader() as conn:
        cur = conn.execute("SELECT * FROM main.media WHERE id = ?", (media_id,))
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, cur.fetchone(), strict=True))


def _seq(db, message_id):
    with db.reader() as conn:
        return conn.execute("SELECT seq FROM main.message WHERE id = ?", (message_id,)).fetchone()[0]


def _count(headers, name):
    return sum(1 for key, _ in headers.multi_items() if key.lower() == name)


# --- T4 ------------------------------------------------------------------------------------------------


async def test_t4_jpeg_inline_headers_once(client, db, clock):
    account_id, _p, _c = _setup(db)
    db.write_sync(lambda conn: media.ensure_start_ts(conn, 0))
    mid = _ingest(db, account_id, "1", [{"type": "photo", "mime": "image/png", "name": "x.png"}])
    media_id = _media_id(db, mid)
    _make_ready(db, account_id, media_id, JPEG)
    await login_via_code(client, db, clock)

    r = await client.get(f"/api/v1/media/{media_id}")
    assert r.status_code == 200
    assert r.content == JPEG
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["content-disposition"] == "inline"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-security-policy"] == "default-src 'none'; sandbox; frame-ancestors 'none'"
    assert r.headers["cache-control"] == "private, max-age=86400"
    for name in (
        "content-type",
        "content-disposition",
        "x-content-type-options",
        "content-security-policy",
        "cache-control",
        "referrer-policy",
        "cross-origin-resource-policy",
    ):
        assert _count(r.headers, name) == 1, name


async def test_t4_html_disguised_as_png_is_download(client, db, clock):
    account_id, _p, _c = _setup(db)
    mid = _ingest(db, account_id, "1", [{"type": "photo", "mime": "image/png", "name": "../../x.png"}])
    db.write_sync(lambda conn: media.request_fetch(conn, message_id=mid, idx=0, now_ms=1))
    media_id = _media_id(db, mid)
    _make_ready(db, account_id, media_id, HTML)
    await login_via_code(client, db, clock)
    r = await client.get(f"/api/v1/media/{media_id}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"] == "attachment; filename=\"x.png\"; filename*=UTF-8''x.png"
    assert _count(r.headers, "content-security-policy") == 1


async def test_t4_svg_and_unicode_name_attachment(client, db, clock):
    account_id, _p, _c = _setup(db)
    name = "отчёт" + chr(0x202E) + "gpj.svg"  # невидимая подмена направления текста
    mid = _ingest(db, account_id, "1", [{"type": "document", "mime": "image/svg+xml", "name": name}])
    db.write_sync(lambda conn: media.request_fetch(conn, message_id=mid, idx=0, now_ms=1))
    media_id = _media_id(db, mid)
    _make_ready(db, account_id, media_id, SVG)
    await login_via_code(client, db, clock)
    r = await client.get(f"/api/v1/media/{media_id}")
    assert r.headers["content-type"] == "application/octet-stream"
    cd = r.headers["content-disposition"]
    assert cd.startswith("attachment; filename=\"_____gpj.svg\"; filename*=UTF-8''")
    assert "%E2%80%AE" not in cd  # невидимая подмена направления вырезана
    assert "%D0%BE%D1%82" in cd


async def test_t4_project_scope_and_not_ready(client, db, clock):
    account_id, project_id, _c = _setup(db)
    other_project = insert_project(db, name="Other")
    mid = _ingest(db, account_id, "1", [{"type": "photo"}, {"type": "photo"}])
    db.write_sync(lambda conn: media.request_fetch(conn, message_id=mid, idx=0, now_ms=1))
    db.write_sync(lambda conn: media.request_fetch(conn, message_id=mid, idx=1, now_ms=1))
    ready_id = _media_id(db, mid, 0)
    pending_id = _media_id(db, mid, 1)
    _make_ready(db, account_id, ready_id, JPEG)

    own = make_bearer_token(db, scopes=("read",), project_ids=[project_id])
    foreign = make_bearer_token(db, scopes=("read",), project_ids=[other_project], name="foreign")
    r = await client.get(f"/api/v1/media/{ready_id}", headers={"Authorization": f"Bearer {own}"})
    assert r.status_code == 200 and r.content == JPEG  # D: проектный токен читает медиа своего проекта
    r = await client.get(f"/api/v1/media/{ready_id}", headers={"Authorization": f"Bearer {foreign}"})
    assert r.status_code == 404
    r = await client.get(f"/api/v1/media/{pending_id}", headers={"Authorization": f"Bearer {own}"})
    assert r.status_code == 404
    r = await client.get("/api/v1/media/999999", headers={"Authorization": f"Bearer {own}"})
    assert r.status_code == 404
    r = await client.get(f"/api/v1/media/{ready_id}")
    assert r.status_code == 401


async def test_p9_range_request(client, db, clock):
    account_id, _p, _c = _setup(db)
    mid = _ingest(db, account_id, "1", [{"type": "photo"}])
    db.write_sync(lambda conn: media.request_fetch(conn, message_id=mid, idx=0, now_ms=1))
    media_id = _media_id(db, mid)
    _make_ready(db, account_id, media_id, JPEG)
    await login_via_code(client, db, clock)
    r = await client.get(f"/api/v1/media/{media_id}", headers={"Range": "bytes=0-3"})
    assert r.status_code == 206
    assert r.content == JPEG[:4]
    assert r.headers["content-range"] == f"bytes 0-3/{len(JPEG)}"
    assert _count(r.headers, "cache-control") == 1


async def test_p5_missing_file_404_and_deferred(client, db, clock):
    account_id, _p, _c = _setup(db)
    mid = _ingest(db, account_id, "1", [{"type": "photo"}])
    db.write_sync(lambda conn: media.request_fetch(conn, message_id=mid, idx=0, now_ms=1))
    media_id = _media_id(db, mid)
    path = _make_ready(db, account_id, media_id, JPEG)
    path.unlink()
    seq_before = _seq(db, mid)
    await login_via_code(client, db, clock)
    r = await client.get(f"/api/v1/media/{media_id}")
    assert r.status_code == 404
    row = _row(db, media_id)
    assert row["status"] == "deferred" and row["sha256"] is None
    assert _seq(db, mid) > seq_before


async def test_p9_other_api_responses_keep_default_headers(client, db, clock):
    await login_via_code(client, db, clock)
    r = await client.get("/api/v1/projects")
    assert r.headers["cache-control"] == "no-store"
    assert "media-src 'self'" in r.headers["content-security-policy"]
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert _count(r.headers, "content-security-policy") == 1
    assert _count(r.headers, "cache-control") == 1


# --- T5 ------------------------------------------------------------------------------------------------


async def test_t5_fetch_old_attachment_without_row(client, db, clock):
    account_id, _p, _c = _setup(db)
    # сообщение без строк media (точки отсчёта нет — пришло «до куска»)
    mid = _ingest(db, account_id, "1", [{"type": "location"}, {"type": "video", "size": 5 * MIB}])
    db.write_sync(lambda conn: conn.execute("DELETE FROM main.media"))
    seq_before = _seq(db, mid)
    cookie, _csrf = await login_via_code(client, db, clock)
    r = await client.post(f"/api/v1/messages/{mid}/media/1/fetch", headers=csrf_headers(cookie))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["message_id"] == mid
    (entry,) = body["media"]
    assert entry["idx"] == 1 and entry["status"] == "pending" and entry["kind"] == "video"
    assert set(entry) == {
        "id",
        "idx",
        "kind",
        "status",
        "mime",
        "size",
        "name",
        "width",
        "height",
        "duration_s",
        "detected",
        "transcript",  # Ф14-6 C: расшифровка голосового (у этого вложения — нет)
    }
    assert entry["transcript"] is None
    row = _row(db, entry["id"])
    assert (row["priority"], row["attempts"]) == (1, 0)
    assert _seq(db, mid) > seq_before
    with db.reader() as conn:
        audit = conn.execute("SELECT result FROM main.audit WHERE action = 'media_fetch'").fetchall()
    assert audit == [("ok",)]

    # геопозиция — качать нечего; индекс мимо — 404
    r = await client.post(f"/api/v1/messages/{mid}/media/0/fetch", headers=csrf_headers(cookie))
    assert r.status_code == 409
    r = await client.post(f"/api/v1/messages/{mid}/media/5/fetch", headers=csrf_headers(cookie))
    assert r.status_code == 404
    # без CSRF — middleware
    r = await client.post(
        f"/api/v1/messages/{mid}/media/1/fetch", headers={"Origin": "http://127.0.0.1:8787"}
    )
    assert r.status_code == 403


async def test_t5_fetch_bearer_forbidden(client, db):
    account_id, project_id, _c = _setup(db)
    mid = _ingest(db, account_id, "1", [{"type": "photo"}])
    token = make_bearer_token(db, scopes=("read", "send", "draft"), project_ids=[project_id])
    r = await client.post(
        f"/api/v1/messages/{mid}/media/0/fetch", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 403
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.media WHERE priority = 1").fetchone()[0] == 0


async def test_t5_fetch_too_big_and_reset_failed(client, db, clock):
    account_id, _p, _c = _setup(db)
    db.write_sync(lambda conn: media.ensure_start_ts(conn, 0))
    mid = _ingest(
        db, account_id, "1", [{"type": "video", "size": 301 * MIB}, {"type": "document", "size": 1}]
    )
    big_id = _media_id(db, mid, 0)
    doc_id = _media_id(db, mid, 1)
    assert _row(db, big_id)["status"] == "deferred"
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.media SET status = 'failed', attempts = 5 WHERE id = ?", (doc_id,)
        )
    )
    cookie, _ = await login_via_code(client, db, clock)
    r = await client.post(f"/api/v1/messages/{mid}/media/0/fetch", headers=csrf_headers(cookie))
    assert r.status_code == 200
    assert _row(db, big_id)["status"] == "too_big"
    r = await client.post(f"/api/v1/messages/{mid}/media/1/fetch", headers=csrf_headers(cookie))
    row = _row(db, doc_id)
    assert (row["status"], row["attempts"], row["priority"]) == ("pending", 0, 1)
    statuses = {m["idx"]: m["status"] for m in r.json()["media"]}
    assert statuses == {0: "too_big", 1: "pending"}


async def test_t5_fetch_new_row_too_big_when_size_known(client, db, clock):
    account_id, _p, _c = _setup(db)
    mid = _ingest(db, account_id, "1", [{"type": "video", "size": 400 * MIB}])
    db.write_sync(lambda conn: conn.execute("DELETE FROM main.media"))
    cookie, _ = await login_via_code(client, db, clock)
    r = await client.post(f"/api/v1/messages/{mid}/media/0/fetch", headers=csrf_headers(cookie))
    assert r.json()["media"][0]["status"] == "too_big"


async def test_p14_fetch_other_source_409(client, db, clock):
    account_id, _p, _c = _setup(db, source="fake")
    mid = _ingest(db, account_id, "1", [{"type": "document", "size": 10}])
    cookie, _ = await login_via_code(client, db, clock)
    r = await client.post(f"/api/v1/messages/{mid}/media/0/fetch", headers=csrf_headers(cookie))
    assert r.status_code == 409


# --- A3: `media` во всех выдачах, без служебного -------------------------------------------------------


async def test_a3_media_in_feed_thread_and_sync(client, db, clock):
    account_id, _p, channel_id = _setup(db, source="slack")
    db.write_sync(lambda conn: media.ensure_start_ts(conn, 0))
    ref = {"file_id": "F1", "url": "https://files.slack.com/files-pri/T-F1/a.png"}
    _ingest(db, account_id, "1.0", [{"type": "photo", "src_id": "F1", "media_ref": ref, "width": 10}])
    _ingest(db, account_id, "2.0", [{"type": "photo"}], ts=10_001, thread_ext_id="1.0")
    await login_via_code(client, db, clock)

    r = await client.get(f"/api/v1/channels/{channel_id}/messages")
    (msg,) = r.json()["messages"]
    (entry,) = msg["media"]
    assert entry["status"] == "pending" and entry["width"] == 10
    assert "files.slack.com" not in r.text and "ref" not in entry and "src_id" not in entry
    assert "media_ref" not in (msg["attachments_json"] or "")

    r = await client.get(f"/api/v1/channels/{channel_id}/threads/1.0/messages")
    body = r.json()
    assert body["messages"][0]["media"][0]["kind"] == "photo"
    assert body["parent"]["media"][0]["id"] == entry["id"]

    r = await client.get("/api/v1/sync", params={"since": 0})
    synced = {m["ext_id"]: m for m in r.json()["messages"]}
    assert synced["1.0"]["media"][0]["id"] == entry["id"]
    assert "files.slack.com" not in r.text


async def test_status_media_only_for_ui(client, db, clock):
    _setup(db)
    await login_via_code(client, db, clock)
    r = await client.get("/api/v1/status")
    body = r.json()["media"]
    assert set(body) >= {"bytes_by_account", "total_bytes", "pending", "failed", "free_bytes", "line"}
    assert body["line"].startswith("медиа: ")
    token = make_bearer_token(db, scopes=("read",))
    client.cookies.clear()
    r = await client.get("/api/v1/status", headers={"Authorization": f"Bearer {token}"})
    assert "media" not in r.json()
