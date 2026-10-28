"""Ф14-4 (docs/architecture.md и E1–E3 + «Поправка архитектора»): голосовое из браузера —
ядро. `POST /uploads?voice=1` (только UI, webm/ogg, ≤ 20 МиБ), вид `voice` в `upload.kind` (миграция 0024),
голосовое — одно в сообщении, строка `media` заготовки `kind='voice'`, задание несёт `kind: "voice"`,
CSP `media-src 'self' blob:`; старая строка `upload` без `kind` — прежний вид."""

import hashlib
import json

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import db as db_module
from hub.core import media, security, uploads
from hub.core.db import Db

from .conftest import BASE_URL, csrf_headers, insert_account, insert_channel, login_via_code
from .test_send_files import (
    PDF,
    PNG,
    _auth,
    _connector_secret,
    _media_rows,
    _message,
    _outbox,
    _send,
    _upload_count,
    iclient,  # noqa: F401 — фикстура
)

WEBM = b"\x1a\x45\xdf\xa3" + b"\x9f\x42\x86\x81\x01\x42\x82\x84webm" + b"\x00" * 200
OGG = b"OggS" + b"\x00" * 300
MIB = 1024 * 1024


def _setup(db, *, source="telegram"):
    account_id = insert_account(db, source=source)
    channel_id = insert_channel(db, account_id, ext_id="c1")
    return account_id, channel_id


async def _voice(client, cookie, channel_id, body, *, name=None):
    headers = {**csrf_headers(cookie), "Content-Type": "audio/webm"}
    if name is not None:
        headers["X-Filename"] = name
    url = f"/api/v1/uploads?channel_id={channel_id}&voice=1"
    return await client.post(url, content=body, headers=headers)


def _upload_row(db, upload_id):
    with db.reader() as conn:
        return conn.execute(
            "SELECT mime, detected, name, kind FROM main.upload WHERE id = ?", (upload_id,)
        ).fetchone()


# --- E1: загрузка -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "mime", "name", "detected"),
    [(WEBM, "audio/webm", "voice.webm", "webm"), (OGG, "audio/ogg", "voice.ogg", "ogg")],
)
async def test_voice_upload_webm_ogg(client, db, clock, body, mime, name, detected):
    cookie, _ = await login_via_code(client, db, clock)
    _account_id, channel_id = _setup(db)
    r = await _voice(client, cookie, channel_id, body)
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["kind"] == "voice"
    assert got["mime"] == mime
    assert got["name"] == name
    assert _upload_row(db, got["upload_id"]) == (mime, detected, name, "voice")
    # `get` (постановка) берёт вид строки.
    with db.reader() as conn:
        assert uploads.get(conn, upload_id=got["upload_id"], channel_id=channel_id)["kind"] == "voice"


async def test_voice_upload_keeps_given_name(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    _account_id, channel_id = _setup(db)
    r = await _voice(client, cookie, channel_id, WEBM, name="my%20rec.webm")
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "my rec.webm"


async def test_plain_webm_upload_is_still_video_document(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    _account_id, channel_id = _setup(db)
    r = await client.post(
        f"/api/v1/uploads?channel_id={channel_id}",
        content=WEBM,
        headers={**csrf_headers(cookie), "X-Filename": "clip.webm"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "document"
    assert r.json()["mime"] == "video/webm"
    assert _upload_row(db, r.json()["upload_id"])[3] is None


async def test_voice_upload_png_is_422_and_no_file(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    account_id, channel_id = _setup(db)
    r = await _voice(client, cookie, channel_id, PNG)
    assert r.status_code == 422
    assert "webm/ogg" in r.json()["detail"]
    assert _upload_count(db) == 0
    assert not media.file_path(db.data_dir, account_id, hashlib.sha256(PNG).hexdigest()).exists()
    leftovers = [p for p in media.account_dir(db.data_dir, account_id).iterdir()]
    assert leftovers == []


async def test_voice_upload_over_20_mib_is_413_before_body(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    _account_id, channel_id = _setup(db)

    async def body():
        yield b"x"

    r = await client.post(
        f"/api/v1/uploads?channel_id={channel_id}&voice=1",
        content=body(),
        headers={**csrf_headers(cookie), "Content-Length": str(20 * MIB + 1)},
    )
    assert r.status_code == 413
    assert _upload_count(db) == 0


async def test_voice_upload_bearer_is_rejected(app, db):
    _account_id, channel_id = _setup(db)
    token = db.write_sync(
        lambda conn: security.create_api_token(
            conn, name="mcp:all", kind="interactive", scopes=["read", "draft", "attach"], project_ids=None,
            expires_ts=None,
        )
    ).token  # fmt: skip
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as bearer:
        r = await bearer.post(
            f"/api/v1/uploads?channel_id={channel_id}&voice=1", content=WEBM, headers=_auth(token)
        )
        assert r.status_code in (403, 422)
        # Без `voice` тот же токен файл загрузить может (Ф14-3) — отказ именно из-за голосового.
        ok = await bearer.post(f"/api/v1/uploads?channel_id={channel_id}", content=WEBM, headers=_auth(token))
        assert ok.status_code == 200, ok.text
    assert _upload_count(db) == 1


# --- E2: постановка ---------------------------------------------------------------------------------------


async def test_voice_plus_other_file_is_422(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    _account_id, channel_id = _setup(db)
    voice_id = (await _voice(client, cookie, channel_id, WEBM)).json()["upload_id"]
    pdf = await client.post(
        f"/api/v1/uploads?channel_id={channel_id}",
        content=PDF,
        headers={**csrf_headers(cookie), "X-Filename": "a.pdf"},
    )
    r = await _send(client, cookie, channel_id, files=[voice_id, pdf.json()["upload_id"]], key="mix")
    assert r.status_code == 422
    assert "отдельным сообщением" in r.json()["detail"]
    other_voice = (await _voice(client, cookie, channel_id, OGG)).json()["upload_id"]
    r = await _send(client, cookie, channel_id, files=[voice_id, other_voice], key="two")
    assert r.status_code == 422
    assert _upload_count(db) == 3  # ничего не ушло


@pytest.mark.parametrize("source", ["telegram", "slack", "mail", "fake"])
async def test_single_voice_empty_text_is_queued(client, iclient, db, clock, source):  # noqa: F811
    cookie, _ = await login_via_code(client, db, clock)
    account_id, channel_id = _setup(db, source=source)
    voice_id = (await _voice(client, cookie, channel_id, WEBM)).json()["upload_id"]
    r = await _send(client, cookie, channel_id, text="", files=[voice_id], key="v1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "queued"
    row = _outbox(db, body["outbox_id"])
    (item,) = json.loads(row["file_json"])
    assert item["kind"] == "voice"
    assert item["mime"] == "audio/webm"
    assert item["name"] == "voice.webm"
    message = _message(db, body["message_id"])
    assert json.loads(message["attachments_json"]) == [
        {"type": "voice", "name": "voice.webm", "size": len(WEBM), "mime": "audio/webm"}
    ]
    (media_row,) = _media_rows(db, body["message_id"])
    assert media_row["kind"] == "voice"
    assert media_row["detected"] == "webm"
    # A4: задание коннектору несёт `kind: "voice"`.
    secret = _connector_secret(db, account_id)
    lease = await iclient.post("/internal/v1/outbox/lease", headers=_auth(secret), json={"lease_ms": 60_000})
    (task,) = lease.json()
    assert task["files"][0]["kind"] == "voice"
    assert task["files"][0]["mime"] == "audio/webm"


# --- E3: CSP ----------------------------------------------------------------------------------------------


async def test_csp_allows_blob_media(client):
    r = await client.get("/api/v1/healthz")
    csp = r.headers["content-security-policy"]
    assert "media-src 'self' blob:;" in csp
    # Остальное не расширилось: blob — только у медиа.
    assert csp.count("blob:") == 1


# --- миграция 0024 ----------------------------------------------------------------------------------------


def _kind_column(conn, schema):
    return {r[1]: r for r in conn.execute(f"PRAGMA {schema}.table_info(upload)")}.get("kind")


def test_migration_0024_old_rows_keep_computed_kind(tmp_path, monkeypatch):
    original = db_module._migrations

    def _before(*, main: bool):
        return {v: files for v, files in original(main=main).items() if v < 24}

    monkeypatch.setattr(db_module, "_migrations", _before)
    old = Db(tmp_path)
    old.start()
    channels = {}
    try:
        for schema in ("main", "work"):
            acc = old.write_sync(
                lambda conn, schema=schema: conn.execute(
                    "INSERT INTO main.account (source, db) VALUES ('telegram', ?)", (schema,)
                ).lastrowid
            )

            def _fill(conn, schema=schema, acc=acc):
                channel_id = conn.execute(
                    f"INSERT INTO {schema}.channel (account_id, ext_id, kind, first_seen_ts) "  # noqa: S608
                    "VALUES (?, 'c1', 'dm', 0)",
                    (acc,),
                ).lastrowid
                for upload_id, detected, size in (("p1", "png", 100), ("d1", "webm", 100)):
                    conn.execute(
                        f"INSERT INTO {schema}.upload (id, channel_id, account_id, sha256, size, mime, "  # noqa: S608
                        "detected, name, created_ts) VALUES (?, ?, ?, 'x', ?, 'm', ?, 'n', 0)",
                        (f"{schema}-{upload_id}", channel_id, acc, size, detected),
                    )
                return channel_id

            channels[schema] = old.write_sync(_fill)
        with old.reader() as conn:
            assert _kind_column(conn, "main") is None
            assert _kind_column(conn, "work") is None
    finally:
        old.close()

    monkeypatch.setattr(db_module, "_migrations", original)
    new = Db(tmp_path)
    new.start()
    try:
        with new.reader() as conn:
            for schema, channel_id in channels.items():
                col = _kind_column(conn, schema)
                assert col is not None and col[2] == "TEXT" and col[3] == 0
                photo = uploads.get(conn, upload_id=f"{schema}-p1", channel_id=channel_id)
                doc = uploads.get(conn, upload_id=f"{schema}-d1", channel_id=channel_id)
                assert photo is not None and photo["kind"] == "photo"
                assert doc is not None and doc["kind"] == "document"
    finally:
        new.close()
