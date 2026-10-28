"""Ф10-4 (docs/architecture.md): отправка картинок и файлов — ядро (раздел A, D и поправки
П1–П16). Загрузка `POST /uploads?channel_id=`, `files` в `POST /messages/send`, `outbox.enqueue` с файлом,
поле `file` задания, ручка байтов коннектору, эхо, учёт ссылок на файл склада, очистка, «удалить у себя»,
миграция 0018."""

import asyncio
import hashlib
import json

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import connector_auth, hiding, media, runner, uploads
from hub.core import db as db_module
from hub.core import outbox as outbox_mod
from hub.core.app_internal import create_internal_app
from hub.core.db import Db
from hub.core.feed import Notifier
from hub.core.ingest import apply_batch

from .conftest import (
    csrf_headers,
    insert_account,
    insert_channel,
    login_via_code,
    make_bearer_token,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200 + b"png-end"
PDF = b"%PDF-1.7\n" + b"x" * 300
MIB = 1024 * 1024


# --- обвязка -----------------------------------------------------------------------------------------------


def _setup(db, *, source="telegram", db_name="main"):
    account_id = insert_account(db, source=source, db_name=db_name)
    channel_id = insert_channel(db, account_id, ext_id="c1", db_name=db_name)
    return account_id, channel_id


def _schema(row_id: int) -> str:
    return "work" if row_id >= 2**40 else "main"


async def _upload(client, cookie, channel_id, body=PNG, *, name="photo.png", content_type="image/png"):
    return await client.post(
        f"/api/v1/uploads?channel_id={channel_id}",
        content=body,
        headers={
            **csrf_headers(cookie),
            "Content-Type": content_type,
            "X-Filename": name,
        },
    )


async def _send(client, cookie, channel_id, *, text="", files=None, key="k1"):
    payload = {"channel_id": channel_id, "text": text, "idempotency_key": key}
    if files is not None:
        payload["files"] = files
    return await client.post("/api/v1/messages/send", json=payload, headers=csrf_headers(cookie))


def _outbox(db, outbox_id):
    with db.reader() as conn:
        cur = conn.execute(f"SELECT * FROM {_schema(outbox_id)}.outbox WHERE id = ?", (outbox_id,))
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, cur.fetchone(), strict=True))


def _message(db, message_id):
    with db.reader() as conn:
        cur = conn.execute(f"SELECT * FROM {_schema(message_id)}.message WHERE id = ?", (message_id,))
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, cur.fetchone(), strict=True))


def _media_rows(db, message_id):
    schema = _schema(message_id)
    with db.reader() as conn:
        cur = conn.execute(f"SELECT * FROM {schema}.media WHERE message_id = ? ORDER BY idx", (message_id,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


def _upload_count(db, schema="main"):
    with db.reader() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {schema}.upload").fetchone()[0]  # noqa: S608


def _connector_secret(db, account_id):
    secret = connector_auth.generate_connector_secret()
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.connector_secret (account_id, hash, created_ts) VALUES (?, ?, 0)",
            (account_id, connector_auth.hash_secret(secret)),
        )
    )
    return secret


@pytest.fixture
async def iclient(db, clock):
    app = create_internal_app(
        db, clock=clock, notifier=Notifier(), admin_key_hash=connector_auth.hash_secret("admin")
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://hub-internal") as c:
        yield c


def _auth(secret):
    return {"Authorization": f"Bearer {secret}"}


async def _upload_and_send(client, db, clock, *, source="telegram", text="", body=PNG, db_name="main"):
    cookie, _ = await login_via_code(client, db, clock)
    account_id, channel_id = _setup(db, source=source, db_name=db_name)
    r = await _upload(client, cookie, channel_id, body)
    assert r.status_code == 200, r.text
    upload_id = r.json()["upload_id"]
    s = await _send(client, cookie, channel_id, text=text, files=[upload_id])
    assert s.status_code == 200, s.text
    return cookie, account_id, channel_id, upload_id, s.json()


# --- A2: загрузка ------------------------------------------------------------------------------------------


async def test_upload_png_type_by_signature_not_content_type(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    account_id, channel_id = _setup(db)
    r = await _upload(client, cookie, channel_id, PNG, name="shot.png", content_type="text/html")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mime"] == "image/png"
    assert body["kind"] == "photo"
    assert body["size"] == len(PNG)
    assert body["name"] == "shot.png"
    assert len(body["upload_id"]) >= 20
    sha = hashlib.sha256(PNG).hexdigest()
    path = media.file_path(db.data_dir, account_id, sha)
    assert path.read_bytes() == PNG
    assert path.stat().st_mode & 0o777 == 0o600
    with db.reader() as conn:
        row = conn.execute(
            "SELECT channel_id, account_id, sha256, size, mime, detected, name FROM main.upload WHERE id = ?",
            (body["upload_id"],),
        ).fetchone()
    assert row == (channel_id, account_id, sha, len(PNG), "image/png", "png", "shot.png")


async def test_upload_html_bytes_are_document_octet_stream(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    _account_id, channel_id = _setup(db)
    r = await _upload(client, cookie, channel_id, b"<html><script>x</script>", content_type="image/png")
    assert r.status_code == 200
    assert r.json()["mime"] == "application/octet-stream"
    assert r.json()["kind"] == "document"


async def test_upload_big_photo_is_document(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    _account_id, channel_id = _setup(db)
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (10 * MIB)
    r = await _upload(client, cookie, channel_id, big)
    assert r.status_code == 200
    assert r.json()["mime"] == "image/png"
    assert r.json()["kind"] == "document"


async def test_upload_over_50_mib_is_413_without_reading_body(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    _account_id, channel_id = _setup(db)

    async def body():
        yield b"x"

    r = await client.post(
        f"/api/v1/uploads?channel_id={channel_id}",
        content=body(),
        headers={**csrf_headers(cookie), "Content-Length": str(50 * MIB + 1)},
    )
    assert r.status_code == 413
    assert _upload_count(db) == 0


async def test_upload_exactly_50_mib_is_accepted(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    _account_id, channel_id = _setup(db)
    r = await _upload(client, cookie, channel_id, b"%PDF-" + b"\x00" * (50 * MIB - 5))
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "document"


async def test_upload_without_content_length_is_411(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    _account_id, channel_id = _setup(db)

    async def body():
        yield PNG

    r = await client.post(
        f"/api/v1/uploads?channel_id={channel_id}", content=body(), headers=csrf_headers(cookie)
    )
    assert "content-length" not in {k.lower() for k in r.request.headers}
    assert r.status_code == 411
    assert _upload_count(db) == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("..%2F..%2Fx", "x"),
        ("..%5C..%5Cwin.txt", "win.txt"),
        ("a%0Ab%1B%7Fc.txt", "abc.txt"),
        ("evil%E2%80%AEgnp.exe", "evilgnp.exe"),  # U+202E — подмена направления текста
        ("%D0%BE%D1%82%D1%87%D1%91%D1%82.pdf", "отчёт.pdf"),
        ("", "file"),
        ("..", "file"),
        ("a" * 300, "a" * 200),
    ],
)
async def test_upload_name_is_cleaned(client, db, clock, raw, expected):
    cookie, _ = await login_via_code(client, db, clock)
    _account_id, channel_id = _setup(db)
    r = await _upload(client, cookie, channel_id, PDF, name=raw)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == expected


async def test_upload_bearer_is_403(client, db):
    _account_id, channel_id = _setup(db)
    token = make_bearer_token(db, scopes=("read", "send", "draft"))
    r = await client.post(
        f"/api/v1/uploads?channel_id={channel_id}",
        content=PNG,
        headers={"Authorization": f"Bearer {token}", "X-Filename": "a.png"},
    )
    assert r.status_code == 403
    assert _upload_count(db) == 0
    with db.reader() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM main.audit WHERE action = 'ui_only_denied'").fetchone()[0] == 1
        )


async def test_upload_foreign_source_channel_is_422_and_unknown_channel_404(client, db, clock):
    # Ф11-3b A7: почта теперь принимает файлы — «не принимает» проверяется на источнике без отправки файлов.
    cookie, _ = await login_via_code(client, db, clock)
    _account_id, other_channel = _setup(db, source="pyrus")
    r = await _upload(client, cookie, other_channel, PNG)
    assert r.status_code == 422
    r = await _upload(client, cookie, 999_999, PNG)
    assert r.status_code == 404
    assert _upload_count(db) == 0


async def test_upload_same_bytes_twice_dedups_file(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    account_id, channel_id = _setup(db)
    a = (await _upload(client, cookie, channel_id, PNG)).json()
    b = (await _upload(client, cookie, channel_id, PNG)).json()
    assert a["upload_id"] != b["upload_id"]
    assert _upload_count(db) == 2
    account_dir = media.account_dir(db.data_dir, account_id)
    files = [p for p in account_dir.rglob("*") if p.is_file()]
    assert len(files) == 1  # временных файлов не осталось, склад — один файл


async def test_upload_goes_to_work_schema_for_work_channel(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    account_id, channel_id = _setup(db, source="slack", db_name="work")
    r = await _upload(client, cookie, channel_id, PNG)
    assert r.status_code == 200
    assert _upload_count(db, "work") == 1
    assert _upload_count(db, "main") == 0
    assert media.file_path(db.data_dir, account_id, hashlib.sha256(PNG).hexdigest()).is_file()


# --- A3/A4: отправка с файлом ------------------------------------------------------------------------------


async def test_send_with_upload_writes_file_json_placeholder_and_ready_media(client, db, clock):
    _cookie, account_id, channel_id, upload_id, body = await _upload_and_send(client, db, clock, text="")
    assert body["status"] == "queued"
    row = _outbox(db, body["outbox_id"])
    sha = hashlib.sha256(PNG).hexdigest()
    assert json.loads(row["file_json"]) == [
        {
            "sha256": sha,
            "size": len(PNG),
            "mime": "image/png",
            "name": "photo.png",
            "kind": "photo",
            "upload_id": upload_id,
        }
    ]
    assert row["text"] == ""
    message = _message(db, body["message_id"])
    assert message["kind"] == "file"  # П16
    assert json.loads(message["attachments_json"]) == [
        {"type": "photo", "name": "photo.png", "size": len(PNG), "mime": "image/png"}
    ]
    (media_row,) = _media_rows(db, body["message_id"])
    assert media_row["idx"] == 0
    assert media_row["status"] == "ready"
    assert media_row["sha256"] == sha
    assert media_row["size"] == len(PNG)
    assert media_row["stored_size"] == len(PNG)
    assert media_row["mime"] == "image/png"
    assert media_row["detected"] == "png"
    assert media_row["kind"] == "photo"
    assert media_row["account_id"] == account_id
    assert media_row["fetched_ts"] is not None
    assert _upload_count(db) == 0  # П1: строка загрузки удалена, ссылка — строка media


async def test_placeholder_media_is_served_inline_png(client, db, clock):
    _cookie, _account_id, _channel_id, _upload_id, body = await _upload_and_send(client, db, clock)
    (media_row,) = _media_rows(db, body["message_id"])
    r = await client.get(f"/api/v1/media/{media_row['id']}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.headers["content-disposition"] == "inline"
    assert r.content == PNG


async def test_send_empty_text_without_file_is_422(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    _account_id, channel_id = _setup(db)
    r = await _send(client, cookie, channel_id, text="  ")
    assert r.status_code == 422
    r = await _send(client, cookie, channel_id, text="", files=[])
    assert r.status_code == 422


async def test_send_bearer_with_files_is_422(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    _account_id, channel_id = _setup(db)
    upload_id = (await _upload(client, cookie, channel_id)).json()["upload_id"]
    client.cookies.clear()
    token = make_bearer_token(db, scopes=("read", "send", "draft"))
    r = await client.post(
        "/api/v1/messages/send",
        json={"channel_id": channel_id, "text": "x", "idempotency_key": "b1", "files": [upload_id]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422
    assert _upload_count(db) == 1


async def test_send_telegram_photo_and_document_is_422(client, db, clock):
    # Ф11-3b решение 3: несколько файлов — можно, но альбом Telegram — только картинки или только файлы.
    cookie, _ = await login_via_code(client, db, clock)
    _account_id, channel_id = _setup(db)
    a = (await _upload(client, cookie, channel_id)).json()["upload_id"]
    b = (await _upload(client, cookie, channel_id, PDF)).json()["upload_id"]
    r = await _send(client, cookie, channel_id, text="x", files=[a, b])
    assert r.status_code == 422
    assert "альбом" in r.json()["detail"]
    assert _upload_count(db) == 2


async def test_send_unknown_used_or_foreign_upload_is_422(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    account_id, channel_id = _setup(db)
    other_channel = insert_channel(db, account_id, ext_id="c2")
    foreign = (await _upload(client, cookie, other_channel)).json()["upload_id"]

    assert (await _send(client, cookie, channel_id, files=["nope"], key="a")).status_code == 422
    assert (await _send(client, cookie, channel_id, files=[foreign], key="b")).status_code == 422
    assert _upload_count(db) == 1  # чужая загрузка не тронута

    own = (await _upload(client, cookie, channel_id)).json()["upload_id"]
    assert (await _send(client, cookie, channel_id, files=[own], key="c")).status_code == 200
    # Уже использованная с НОВЫМ ключом — 422 (строки загрузки нет).
    assert (await _send(client, cookie, channel_id, files=[own], key="d")).status_code == 422


async def test_send_idempotent_repeat_with_used_upload_returns_same(client, db, clock):
    cookie, _account_id, channel_id, upload_id, first = await _upload_and_send(
        client, db, clock, text="подпись"
    )
    again = await _send(client, cookie, channel_id, text="подпись", files=[upload_id], key="k1")
    assert again.status_code == 200, again.text  # П10: повтор по ключу раньше проверки загрузки
    assert again.json()["outbox_id"] == first["outbox_id"]
    assert again.json()["created"] is False


async def test_send_same_key_other_upload_is_409(client, db, clock):
    cookie, _account_id, channel_id, _upload_id, _first = await _upload_and_send(client, db, clock, text="x")
    other = (await _upload(client, cookie, channel_id, PDF)).json()["upload_id"]
    r = await _send(client, cookie, channel_id, text="x", files=[other], key="k1")
    assert r.status_code == 409  # П15
    r = await _send(client, cookie, channel_id, text="x", key="k1")
    assert r.status_code == 409  # тот же ключ без файла — тоже другое сообщение
    assert _upload_count(db) == 1


async def test_send_file_to_non_file_source_is_422(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    account_id, channel_id = _setup(db)
    upload_id = (await _upload(client, cookie, channel_id)).json()["upload_id"]
    # Источник беседы сменился на не принимающий файлы (защита в enqueue, не только в ручке загрузки; почта
    # файлы принимает с Ф11-3b).
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.account SET source = 'pyrus' WHERE id = ?", (account_id,))
    )
    r = await _send(client, cookie, channel_id, text="x", files=[upload_id])
    assert r.status_code == 422
    assert _upload_count(db) == 1


async def test_telegram_caption_over_1024_utf16_is_422(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    _account_id, channel_id = _setup(db)
    upload_id = (await _upload(client, cookie, channel_id)).json()["upload_id"]
    # 512 эмодзи = 1024 UTF-16 — граница; ещё одна буква — 1025.
    too_long = "😀" * 512 + "a"
    r = await _send(client, cookie, channel_id, text=too_long, files=[upload_id], key="a")
    assert r.status_code == 422
    assert _upload_count(db) == 1
    r = await _send(client, cookie, channel_id, text="😀" * 512, files=[upload_id], key="b")
    assert r.status_code == 200


async def test_slack_long_caption_is_not_limited_by_telegram_rule(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    _account_id, channel_id = _setup(db, source="slack")
    upload_id = (await _upload(client, cookie, channel_id)).json()["upload_id"]
    r = await _send(client, cookie, channel_id, text="a" * 2000, files=[upload_id])
    assert r.status_code == 200


async def test_send_file_missing_from_store_is_422(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    account_id, channel_id = _setup(db)
    upload_id = (await _upload(client, cookie, channel_id)).json()["upload_id"]
    media.file_path(db.data_dir, account_id, hashlib.sha256(PNG).hexdigest()).unlink()
    r = await _send(client, cookie, channel_id, files=[upload_id])
    assert r.status_code == 422


# --- A5: лиз и байты коннектору ---------------------------------------------------------------------------


async def test_lease_carries_file_and_long_lease(client, iclient, db, clock):
    _cookie, account_id, _channel_id, _upload_id, body = await _upload_and_send(client, db, clock)
    secret = _connector_secret(db, account_id)
    r = await iclient.post("/internal/v1/outbox/lease", headers=_auth(secret), json={"lease_ms": 60_000})
    (item,) = r.json()
    assert item["text"] == ""
    assert "file" not in item  # Ф11-3b A4: поля `file` больше нет
    assert item["files"] == [
        {
            "size": len(PNG),
            "mime": "image/png",
            "name": "photo.png",
            "kind": "photo",
            "sha256": hashlib.sha256(PNG).hexdigest(),
        }
    ]
    row = _outbox(db, body["outbox_id"])
    now_ms = int(clock() * 1000)
    assert row["lease_expires_ts"] == now_ms + outbox_mod.FILE_LEASE_MS  # П3: max(60 000, 420 000)


async def test_lease_without_file_keeps_short_lease_and_file_none(iclient, db, clock):
    account_id, channel_id = _setup(db)
    secret = _connector_secret(db, account_id)
    token_id = db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.api_token (name, kind, hash) VALUES ('t', 'ui', 'x')").lastrowid
        )
    )
    db.write_sync(
        lambda conn: outbox_mod.enqueue(
            conn,
            channel_id=channel_id,
            text="hi",
            origin="ui",
            api_token_id=token_id,
            idempotency_key=None,
            now_ms=1000,
        )
    )
    (item,) = (
        await iclient.post("/internal/v1/outbox/lease", headers=_auth(secret), json={"lease_ms": 60_000})
    ).json()
    assert item["files"] is None
    row = _outbox(db, item["outbox_id"])
    assert row["lease_expires_ts"] == int(clock() * 1000) + 60_000


async def test_stale_lease_branch_carries_file(client, iclient, db, clock):
    _cookie, account_id, _channel_id, _upload_id, _body = await _upload_and_send(client, db, clock)
    secret = _connector_secret(db, account_id)
    await iclient.post("/internal/v1/outbox/lease", headers=_auth(secret), json={})
    clock.advance_ms(outbox_mod.FILE_LEASE_MS + 1_000)
    (item,) = (await iclient.post("/internal/v1/outbox/lease", headers=_auth(secret), json={})).json()
    assert item["mode"] == "reconcile"
    assert item["maybe_sent"] is True
    assert item["files"][0]["name"] == "photo.png"  # П14
    assert item["files"][0]["size"] == len(PNG)


async def test_outbox_file_bytes_only_for_live_lease(client, iclient, db, clock):
    _cookie, account_id, _channel_id, _upload_id, body = await _upload_and_send(client, db, clock)
    secret = _connector_secret(db, account_id)
    (item,) = (await iclient.post("/internal/v1/outbox/lease", headers=_auth(secret), json={})).json()
    oid, gen = item["outbox_id"], item["lease_gen"]

    ok = await iclient.get(f"/internal/v1/outbox/{oid}/file?lease_gen={gen}", headers=_auth(secret))
    assert ok.status_code == 200
    assert ok.content == PNG
    assert ok.headers["content-length"] == str(len(PNG))
    assert ok.headers["content-type"] == "application/octet-stream"

    stale = await iclient.get(f"/internal/v1/outbox/{oid}/file?lease_gen={gen - 1}", headers=_auth(secret))
    assert stale.status_code == 404

    # Чужой аккаунт — 404 (П9), даже с верным поколением.
    other_account = insert_account(db, source="telegram")
    other_secret = _connector_secret(db, other_account)
    foreign = await iclient.get(
        f"/internal/v1/outbox/{oid}/file?lease_gen={gen}", headers=_auth(other_secret)
    )
    assert foreign.status_code == 404

    unauth = await iclient.get(f"/internal/v1/outbox/{oid}/file?lease_gen={gen}")
    assert unauth.status_code == 401

    # Лиз истёк — 404.
    clock.advance_ms(outbox_mod.FILE_LEASE_MS + 1)
    expired = await iclient.get(f"/internal/v1/outbox/{oid}/file?lease_gen={gen}", headers=_auth(secret))
    assert expired.status_code == 404
    assert body["outbox_id"] == oid


async def test_outbox_file_after_report_is_404(client, iclient, db, clock):
    _cookie, account_id, _channel_id, _upload_id, _body = await _upload_and_send(client, db, clock)
    secret = _connector_secret(db, account_id)
    (item,) = (await iclient.post("/internal/v1/outbox/lease", headers=_auth(secret), json={})).json()
    r = await iclient.post(
        "/internal/v1/outbox/report",
        headers=_auth(secret),
        json={
            "outbox_id": item["outbox_id"],
            "lease_gen": item["lease_gen"],
            "outcome": "sent",
            "ext_id": "77",
        },
    )
    assert r.status_code == 200
    r = await iclient.get(
        f"/internal/v1/outbox/{item['outbox_id']}/file?lease_gen={item['lease_gen']}", headers=_auth(secret)
    )
    assert r.status_code == 404


async def test_outbox_file_for_text_row_is_404(iclient, db, clock):
    account_id, channel_id = _setup(db)
    secret = _connector_secret(db, account_id)
    token_id = db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.api_token (name, kind, hash) VALUES ('t', 'ui', 'x')").lastrowid
        )
    )
    db.write_sync(
        lambda conn: outbox_mod.enqueue(
            conn,
            channel_id=channel_id,
            text="hi",
            origin="ui",
            api_token_id=token_id,
            idempotency_key=None,
            now_ms=1000,
        )
    )
    (item,) = (await iclient.post("/internal/v1/outbox/lease", headers=_auth(secret), json={})).json()
    r = await iclient.get(
        f"/internal/v1/outbox/{item['outbox_id']}/file?lease_gen={item['lease_gen']}", headers=_auth(secret)
    )
    assert r.status_code == 404


# --- П4: resolve_unknown(retry) ----------------------------------------------------------------------------


async def test_resolve_unknown_retry_file_without_caption_is_queued(client, db, clock):
    _cookie, account_id, _channel_id, _upload_id, body = await _upload_and_send(client, db, clock, text="")
    oid = body["outbox_id"]
    now_ms = int(clock() * 1000)
    (lease,) = db.write_sync(
        lambda conn: outbox_mod.lease(conn, account_id=account_id, owner="o", now_ms=now_ms)
    )
    db.write_sync(
        lambda conn: outbox_mod.report(
            conn, outbox_id=oid, owner="o", lease_gen=lease.lease_gen, outcome="unknown", now_ms=now_ms
        )
    )
    assert _outbox(db, oid)["status"] == "unknown"
    status = db.write_sync(
        lambda conn: outbox_mod.resolve_unknown(conn, oid, decision="retry", now_ms=now_ms)
    )
    assert status == "queued"


def test_resolve_unknown_retry_text_erased_without_file_still_refused(db):
    account_id, channel_id = _setup(db)
    token_id = db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.api_token (name, kind, hash) VALUES ('t', 'ui', 'x')").lastrowid
        )
    )
    res = db.write_sync(
        lambda conn: outbox_mod.enqueue(
            conn, channel_id=channel_id, text="hi", origin="ui", api_token_id=token_id,
            idempotency_key=None, now_ms=1000,
        )
    )  # fmt: skip
    (lease,) = db.write_sync(
        lambda conn: outbox_mod.lease(conn, account_id=account_id, owner="o", now_ms=1000)
    )
    db.write_sync(
        lambda conn: outbox_mod.report(
            conn,
            outbox_id=res.outbox_id,
            owner="o",
            lease_gen=lease.lease_gen,
            outcome="unknown",
            now_ms=1000,
        )
    )
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.outbox SET text = '' WHERE id = ?", (res.outbox_id,))
    )
    with pytest.raises(outbox_mod.InvalidTransition):
        db.write_sync(
            lambda conn: outbox_mod.resolve_unknown(conn, res.outbox_id, decision="retry", now_ms=1)
        )


# --- П6: эхо ------------------------------------------------------------------------------------------------


def _ingest(db, account_id, *, ext_id, ts, text="", attachments=None, is_me=True):
    event = {
        "type": "upsert",
        "channel": {"ext_id": "c1", "kind": "dm"},
        "ext_id": ext_id,
        "ts": ts,
        "is_me": is_me,
        "kind": "file" if attachments else "text",
        "text": text,
        "attachments": attachments,
        "mentions_me": False,
    }
    result = db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=[event], cursor=None, now_ms=ts)
    )
    assert result.dead == 0
    with db.reader() as conn:
        return conn.execute("SELECT id FROM main.message WHERE ext_id = ?", (ext_id,)).fetchone()[0]


def _visible_own(db, channel_id):
    with db.reader() as conn:
        return conn.execute(
            "SELECT id FROM main.message WHERE channel_id = ? AND is_me = 1 AND deleted = 0", (channel_id,)
        ).fetchall()


async def test_echo_as_separate_row_before_report_leaves_no_orphan_media(client, db, clock):
    _cookie, account_id, channel_id, _upload_id, body = await _upload_and_send(client, db, clock)
    oid, placeholder = body["outbox_id"], body["message_id"]
    now_ms = int(clock() * 1000)
    db.write_sync(lambda conn: media.ensure_start_ts(conn, 0))
    (lease,) = db.write_sync(
        lambda conn: outbox_mod.lease(conn, account_id=account_id, owner="o", now_ms=now_ms)
    )
    # Эхо файла без подписи пришло раньше отчёта — отдельной строкой (по пустому тексту не склеивается).
    real = _ingest(
        db,
        account_id,
        ext_id="1700000000.000100",
        ts=now_ms + 500,
        attachments=[{"type": "photo", "name": "photo.png", "mime": "image/png", "src_id": "F1"}],
    )
    assert real != placeholder
    assert len(_media_rows(db, real)) == 1  # у эха своя строка media idx=0
    db.write_sync(
        lambda conn: outbox_mod.report(
            conn,
            outbox_id=oid,
            owner="o",
            lease_gen=lease.lease_gen,
            outcome="sent",
            ext_id="1700000000.000100",
            now_ms=now_ms + 1000,
        )
    )
    assert _visible_own(db, channel_id) == [(real,)]
    assert _media_rows(db, placeholder) == []
    assert len(_media_rows(db, real)) == 1
    assert _message(db, real)["outbox_id"] == oid


async def test_echo_without_attachment_row_gets_placeholder_media(client, db, clock):
    _cookie, account_id, channel_id, _upload_id, body = await _upload_and_send(client, db, clock)
    oid, placeholder = body["outbox_id"], body["message_id"]
    now_ms = int(clock() * 1000)
    (lease,) = db.write_sync(
        lambda conn: outbox_mod.lease(conn, account_id=account_id, owner="o", now_ms=now_ms)
    )
    real = _ingest(db, account_id, ext_id="555", ts=now_ms + 500, text="")
    db.write_sync(
        lambda conn: outbox_mod.report(
            conn, outbox_id=oid, owner="o", lease_gen=lease.lease_gen, outcome="sent", ext_id="555",
            now_ms=now_ms + 1000,
        )
    )  # fmt: skip
    assert _media_rows(db, placeholder) == []
    (moved,) = _media_rows(db, real)
    assert moved["status"] == "ready"
    assert media.file_path(db.data_dir, account_id, moved["sha256"]).is_file()
    assert _visible_own(db, channel_id) == [(real,)]


async def test_own_photo_without_caption_does_not_glue_to_file_placeholder(client, db, clock):
    _cookie, account_id, channel_id, _upload_id, body = await _upload_and_send(client, db, clock, text="")
    now_ms = int(clock() * 1000)
    db.write_sync(lambda conn: outbox_mod.lease(conn, account_id=account_id, owner="o", now_ms=now_ms))
    # Своё фото с телефона без подписи в окне эха — отдельная строка, заготовка не тронута.
    phone = _ingest(
        db, account_id, ext_id="900", ts=now_ms + 100, attachments=[{"type": "photo", "src_id": "P"}]
    )
    assert phone != body["message_id"]
    assert _message(db, body["message_id"])["ext_id"] is None
    assert _message(db, phone)["outbox_id"] is None


async def test_echo_with_caption_glues_and_keeps_ready_media(client, db, clock):
    _cookie, account_id, channel_id, _upload_id, body = await _upload_and_send(
        client, db, clock, text="смотри"
    )
    now_ms = int(clock() * 1000)
    db.write_sync(lambda conn: media.ensure_start_ts(conn, 0))
    db.write_sync(lambda conn: outbox_mod.lease(conn, account_id=account_id, owner="o", now_ms=now_ms))
    merged = _ingest(
        db,
        account_id,
        ext_id="901",
        ts=now_ms + 100,
        text="смотри",
        attachments=[{"type": "photo", "src_id": "TG1", "mime": "image/png"}],
    )
    assert merged == body["message_id"]
    (row,) = _media_rows(db, merged)
    assert row["status"] == "ready"
    assert row["src_id"] == "TG1"


# --- П2: ссылки на файл, очистка, «удалить у себя» ----------------------------------------------------------


async def test_release_file_counts_upload_rows(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    account_id, channel_id = _setup(db)
    upload_id = (await _upload(client, cookie, channel_id)).json()["upload_id"]
    sha = hashlib.sha256(PNG).hexdigest()
    # Скачанное медиа с тем же sha — удалить у себя (как `hiding._scrub`): файл обязан остаться для загрузки.
    msg = _ingest(
        db, account_id, ext_id="1", ts=1000, attachments=[{"type": "photo", "src_id": "x"}], is_me=False
    )
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.media SET status = 'ready', sha256 = ? WHERE message_id = ?", (sha, msg)
        )
    )
    db.write_sync(lambda conn: hiding.hide_message(conn, msg, now_ms=2000))
    assert media.file_path(db.data_dir, account_id, sha).is_file()
    assert (await _send(client, cookie, channel_id, files=[upload_id])).status_code == 200


async def test_cleanup_removes_old_unused_uploads_and_their_files(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    account_id, channel_id = _setup(db)
    old_id = (await _upload(client, cookie, channel_id, PDF)).json()["upload_id"]
    clock.advance_ms(uploads.UPLOAD_TTL_MS - 1000)
    fresh_id = (await _upload(client, cookie, channel_id, PNG)).json()["upload_id"]
    clock.advance_ms(2000)
    now_ms = int(clock() * 1000)
    removed = db.write_sync(lambda conn: uploads.cleanup(conn, now_ms=now_ms))
    assert removed == 1
    with db.reader() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM main.upload")]
    assert ids == [fresh_id]
    assert not media.file_path(db.data_dir, account_id, hashlib.sha256(PDF).hexdigest()).exists()
    assert media.file_path(db.data_dir, account_id, hashlib.sha256(PNG).hexdigest()).is_file()
    assert (await _send(client, cookie, channel_id, files=[old_id], key="old")).status_code == 422


async def test_cleanup_keeps_file_referenced_by_sent_message(client, db, clock):
    cookie, account_id, channel_id, _upload_id, _body = await _upload_and_send(client, db, clock)
    # Та же картинка прикреплена ещё раз и брошена — очистка удаляет строку, файл нужен отправленному.
    await _upload(client, cookie, channel_id, PNG)
    clock.advance_ms(uploads.UPLOAD_TTL_MS + 1)
    now_ms = int(clock() * 1000)
    assert db.write_sync(lambda conn: uploads.cleanup(conn, now_ms=now_ms)) == 1
    assert _upload_count(db) == 0
    assert media.file_path(db.data_dir, account_id, hashlib.sha256(PNG).hexdigest()).is_file()


async def test_cleanup_work_schema(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    account_id, channel_id = _setup(db, source="slack", db_name="work")
    await _upload(client, cookie, channel_id, PNG)
    clock.advance_ms(uploads.UPLOAD_TTL_MS + 1)
    now_ms = int(clock() * 1000)
    assert db.write_sync(lambda conn: uploads.cleanup(conn, now_ms=now_ms)) == 1
    assert _upload_count(db, "work") == 0
    assert not media.file_path(db.data_dir, account_id, hashlib.sha256(PNG).hexdigest()).exists()


async def test_uploads_cleanup_loop_runs_hourly(db, clock):
    calls = []
    original = uploads.cleanup

    def spy(conn, *, now_ms):
        calls.append(now_ms)
        return original(conn, now_ms=now_ms)

    stop = asyncio.Event()
    ticks = 0

    async def fake_sleep(_seconds):
        nonlocal ticks
        ticks += 1
        clock.advance_ms(20 * 60 * 1000)  # тик = 20 минут настенных часов
        if ticks >= 7:
            stop.set()

    uploads.cleanup = spy
    try:
        await runner._uploads_cleanup_loop(db, clock, sleep=fake_sleep, stop_event=stop)
    finally:
        uploads.cleanup = original
    # Тики на 20, 40, …, 140 мин: прогон на первом тике, затем через час и ещё через час.
    assert len(calls) == 3
    assert calls[1] - calls[0] == 60 * 60 * 1000


async def test_hide_sent_file_message_frees_file_and_erases_name(client, db, clock):
    _cookie, account_id, _channel_id, _upload_id, body = await _upload_and_send(
        client, db, clock, text="тайна"
    )
    oid, placeholder = body["outbox_id"], body["message_id"]
    now_ms = int(clock() * 1000)
    (lease,) = db.write_sync(
        lambda conn: outbox_mod.lease(conn, account_id=account_id, owner="o", now_ms=now_ms)
    )
    db.write_sync(
        lambda conn: outbox_mod.report(
            conn, outbox_id=oid, owner="o", lease_gen=lease.lease_gen, outcome="sent", ext_id="42",
            now_ms=now_ms,
        )
    )  # fmt: skip
    db.write_sync(lambda conn: hiding.hide_message(conn, placeholder, now_ms=now_ms + 1))
    row = _outbox(db, oid)
    assert row["text"] == ""
    assert json.loads(row["file_json"]) == [{"kind": "photo", "size": len(PNG)}]
    assert _media_rows(db, placeholder) == []
    assert not media.file_path(db.data_dir, account_id, hashlib.sha256(PNG).hexdigest()).exists()


async def test_hide_queued_file_message_cancels_and_erases_name(client, db, clock):
    _cookie, account_id, _channel_id, _upload_id, body = await _upload_and_send(client, db, clock)
    now_ms = int(clock() * 1000)
    db.write_sync(lambda conn: hiding.hide_message(conn, body["message_id"], now_ms=now_ms))
    row = _outbox(db, body["outbox_id"])
    assert row["status"] == "cancelled"
    assert json.loads(row["file_json"]) == [{"kind": "photo", "size": len(PNG)}]
    assert not media.file_path(db.data_dir, account_id, hashlib.sha256(PNG).hexdigest()).exists()


# --- A1: миграция 0018 на копии предыдущей версии, обе схемы -----------------------------------------------


def test_migration_0018_on_previous_version_both_files(tmp_path, monkeypatch):
    original = db_module._migrations

    def _up_to_17(*, main: bool):
        return {v: files for v, files in original(main=main).items() if v <= 17}

    monkeypatch.setattr(db_module, "_migrations", _up_to_17)
    old = Db(tmp_path)
    old.start()
    try:
        token_id = old.write_sync(
            lambda conn: (
                conn.execute(
                    "INSERT INTO main.api_token (name, kind, hash) VALUES ('t', 'ui', 'x')"
                ).lastrowid
            )
        )
        for schema, source in (("main", "telegram"), ("work", "slack")):
            acc = insert_account(old, source=source, db_name=schema)

            def _fn(conn, schema=schema, acc=acc):
                cid = conn.execute(
                    f"INSERT INTO {schema}.channel (account_id, ext_id, kind, title, first_seen_ts) "  # noqa: S608
                    "VALUES (?, 'c1', 'dm', 'Вася', 0)",
                    (acc,),
                ).lastrowid
                conn.execute(
                    f"INSERT INTO {schema}.outbox (channel_id, account_id, text, origin, api_token_id, "  # noqa: S608
                    "send_token, status, not_before_ts, created_ts) "
                    "VALUES (?, ?, 'старое', 'ui', ?, 'tok', 'sent', 0, 0)",
                    (cid, acc, token_id),
                )

            old.write_sync(_fn)
        with old.reader() as conn:
            for schema in ("main", "work"):
                assert conn.execute(f"PRAGMA {schema}.user_version").fetchone()[0] == 17
                cols = {r[1] for r in conn.execute(f"PRAGMA {schema}.table_info(outbox)")}
                assert "file_json" not in cols
    finally:
        old.close()

    monkeypatch.setattr(db_module, "_migrations", original)
    new = Db(tmp_path)
    new.start()
    try:
        with new.reader() as conn:
            for schema in ("main", "work"):
                assert conn.execute(f"PRAGMA {schema}.user_version").fetchone()[0] >= 18
                cols = {r[1]: r for r in conn.execute(f"PRAGMA {schema}.table_info(upload)")}
                assert set(cols) == {
                    "id",
                    "channel_id",
                    "account_id",
                    "sha256",
                    "size",
                    "mime",
                    "detected",
                    "name",
                    "created_ts",
                    "kind",  # Ф14-4: миграция 0024
                }
                assert cols["id"][5] == 1
                idx = {r[1] for r in conn.execute(f"PRAGMA {schema}.index_list(upload)")}
                assert {"upload_account_sha", "upload_created"} <= idx
                rows = conn.execute(f"SELECT text, file_json FROM {schema}.outbox").fetchall()  # noqa: S608
                assert rows == [("старое", None)]
    finally:
        new.close()


# --- Ф11-3a C: уборка файла отменённой отправки ------------------------------------------------------------


async def _cancel(client, cookie, outbox_id):
    r = await client.post(f"/api/v1/outbox/{outbox_id}/cancel", headers=csrf_headers(cookie))
    assert r.status_code == 200, r.text
    return r.json()


async def test_f11_cancel_before_attempt_drops_media_and_file(client, db, clock):
    cookie, account_id, _channel_id, _upload_id, body = await _upload_and_send(client, db, clock)
    path = media.file_path(db.data_dir, account_id, hashlib.sha256(PNG).hexdigest())
    assert path.is_file() and len(_media_rows(db, body["message_id"])) == 1
    await _cancel(client, cookie, body["outbox_id"])
    assert _outbox(db, body["outbox_id"])["status"] == "cancelled"
    assert _media_rows(db, body["message_id"]) == []
    assert not path.exists()


async def test_f11_cancel_keeps_file_referenced_by_second_draft(client, db, clock):
    cookie, account_id, channel_id, _upload_id, first = await _upload_and_send(client, db, clock)
    second_upload = (await _upload(client, cookie, channel_id, PNG)).json()["upload_id"]
    second = (await _send(client, cookie, channel_id, files=[second_upload], key="k2")).json()
    path = media.file_path(db.data_dir, account_id, hashlib.sha256(PNG).hexdigest())
    await _cancel(client, cookie, first["outbox_id"])
    assert _media_rows(db, first["message_id"]) == []
    assert len(_media_rows(db, second["message_id"])) == 1
    assert path.is_file()


async def test_f11_cancel_after_attempt_unknown_keeps_media_and_file(client, db, clock):
    cookie, account_id, _channel_id, _upload_id, body = await _upload_and_send(client, db, clock)
    oid = body["outbox_id"]
    now_ms = int(clock() * 1000)
    (lease,) = db.write_sync(
        lambda conn: outbox_mod.lease(conn, account_id=account_id, owner="o", now_ms=now_ms)
    )
    db.write_sync(
        lambda conn: outbox_mod.report(
            conn, outbox_id=oid, owner="o", lease_gen=lease.lease_gen, outcome="temporary", now_ms=now_ms
        )
    )
    assert _outbox(db, oid)["status"] == "queued"
    await _cancel(client, cookie, oid)
    assert _outbox(db, oid)["status"] == "unknown"
    assert len(_media_rows(db, body["message_id"])) == 1
    assert media.file_path(db.data_dir, account_id, hashlib.sha256(PNG).hexdigest()).is_file()
