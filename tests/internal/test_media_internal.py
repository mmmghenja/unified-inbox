"""Ф8-5 (docs/architecture.md): очередь медиа на внутреннем сокете — T2 (лиз своего аккаунта, `PUT`
с правильным/устаревшим `lease_gen`, потолок, дедуп), T3 через HTTP, поправки П3 (форма задания), П4 (длина,
обрыв, повтор, истёкший лиз), П5 (временный файл при 409), П12 (507), контракт 5b."""

import hashlib
import json
import stat

import pytest

from hub.core import media

from .conftest import connector_headers, make_connector_account

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60 + b"jpeg-body"
HTML = b"<!doctype html><script>alert(1)</script>"


def _start(db, ts=0):
    db.write_sync(lambda conn: media.ensure_start_ts(conn, ts))


async def _post_photo(client, secret, ext_id, *, ts=1_700_000_000_000, att=None, channel_ext="c1"):
    event = {
        "type": "upsert",
        "channel": {"ext_id": channel_ext, "kind": "dm"},
        "ext_id": ext_id,
        "ts": ts,
        "is_me": False,
        "kind": "file",
        "attachments": att if att is not None else [{"type": "photo", "src_id": f"p{ext_id}"}],
        "mentions_me": False,
    }
    r = await client.post("/internal/v1/events", headers=connector_headers(secret), json={"events": [event]})
    assert r.status_code == 200 and r.json()["dead"] == 0, r.text


async def _lease(client, secret, limit=1):
    r = await client.post(
        "/internal/v1/media/lease", headers=connector_headers(secret), json={"limit": limit}
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _put(client, secret, media_id, lease_gen, body, headers=None):
    h = {**connector_headers(secret), "Content-Type": "application/octet-stream", **(headers or {})}
    return await client.put(
        f"/internal/v1/media/{media_id}/content?lease_gen={lease_gen}", headers=h, content=body
    )


def _row(db, media_id):
    schema = "work" if media_id >= 2**40 else "main"
    with db.reader() as conn:
        cur = conn.execute(f"SELECT * FROM {schema}.media WHERE id = ?", (media_id,))
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, cur.fetchone(), strict=True))


def _msg_seq(db, media_id):
    with db.reader() as conn:
        return conn.execute(
            "SELECT m.seq FROM main.message m JOIN main.media md ON md.message_id = m.id WHERE md.id = ?",
            (media_id,),
        ).fetchone()[0]


def _tmp_files(data_dir):
    root = media.media_root(data_dir)
    if not root.exists():
        return []
    return [p for p in root.rglob("*") if p.name.startswith(".tmp") and p.is_file()]


# --- П3: форма задания, T2: только свой аккаунт ------------------------------------------------------------


async def test_lease_shape_and_own_account_only(internal_client, db):
    acc_a, secret_a = make_connector_account(db, source="telegram")
    _acc_b, secret_b = make_connector_account(db, source="telegram")
    _start(db)
    await _post_photo(internal_client, secret_a, "10")
    await _post_photo(internal_client, secret_b, "20")

    (job,) = await _lease(internal_client, secret_a, limit=5)
    assert set(job) == {
        "media_id",
        "lease_gen",
        "max_bytes",
        "message",
        "idx",
        "kind",
        "mime",
        "size",
        "name",
        "src_id",
        "ref",
    }
    assert job["message"] == {"channel_ext_id": "c1", "ext_id": "10", "thread_ext_id": None}
    assert job["max_bytes"] == media.AUTO_MAX_BYTES
    assert job["src_id"] == "p10" and job["ref"] is None and job["kind"] == "photo"
    assert _row(db, job["media_id"])["account_id"] == acc_a

    (job_b,) = await _lease(internal_client, secret_b)
    # чужое задание: ни PUT, ни report
    r = await _put(internal_client, secret_a, job_b["media_id"], job_b["lease_gen"], JPEG)
    assert r.status_code == 403
    r = await internal_client.post(
        f"/internal/v1/media/{job_b['media_id']}/report",
        headers=connector_headers(secret_a),
        json={"lease_gen": job_b["lease_gen"], "outcome": "gone"},
    )
    assert r.status_code == 403
    assert _row(db, job_b["media_id"])["status"] == "fetching"


async def test_lease_rejects_account_id_in_body(internal_client, db):
    _acc, secret = make_connector_account(db, source="telegram")
    r = await internal_client.post(
        "/internal/v1/media/lease", headers=connector_headers(secret), json={"limit": 1, "account_id": 9}
    )
    assert r.status_code == 422


# --- T2: PUT --------------------------------------------------------------------------------------------


async def test_put_ok_file_0600_ready_seq_grows(internal_client, db):
    account_id, secret = make_connector_account(db, source="telegram")
    _start(db)
    await _post_photo(internal_client, secret, "1")
    (job,) = await _lease(internal_client, secret)
    seq_before = _msg_seq(db, job["media_id"])

    r = await _put(internal_client, secret, job["media_id"], job["lease_gen"], JPEG)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "size": len(JPEG), "detected": "jpeg"}

    sha = hashlib.sha256(JPEG).hexdigest()
    path = media.file_path(db.data_dir, account_id, sha)
    assert path.read_bytes() == JPEG
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for d in (path.parent, path.parent.parent, media.media_root(db.data_dir)):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
    row = _row(db, job["media_id"])
    assert (row["status"], row["sha256"], row["stored_size"], row["detected"]) == (
        "ready",
        sha,
        len(JPEG),
        "jpeg",
    )
    assert row["fetched_ts"] is not None and row["lease_expires_ts"] is None
    assert _msg_seq(db, job["media_id"]) > seq_before
    assert _tmp_files(db.data_dir) == []


async def test_put_stale_lease_gen_409_no_file(internal_client, db):
    account_id, secret = make_connector_account(db, source="telegram")
    _start(db)
    await _post_photo(internal_client, secret, "1")
    (job,) = await _lease(internal_client, secret)
    r = await _put(internal_client, secret, job["media_id"], job["lease_gen"] + 1, JPEG)
    assert r.status_code == 409
    assert not media.file_path(db.data_dir, account_id, hashlib.sha256(JPEG).hexdigest()).exists()
    assert _tmp_files(db.data_dir) == []
    assert _row(db, job["media_id"])["status"] == "fetching"


async def test_put_over_cap_deferred_or_too_big_no_tmp(internal_client, db, monkeypatch):
    _account, secret = make_connector_account(db, source="telegram")
    _start(db)
    monkeypatch.setattr(media, "AUTO_MAX_BYTES", 16)
    monkeypatch.setattr(media, "CLICK_MAX_BYTES", 32)
    await _post_photo(internal_client, secret, "1")
    (job,) = await _lease(internal_client, secret)
    assert job["max_bytes"] == 16
    r = await _put(internal_client, secret, job["media_id"], job["lease_gen"], b"x" * 20)
    assert r.status_code == 413
    row = _row(db, job["media_id"])
    assert (row["status"], row["size"]) == ("deferred", 20)  # П6: без priority — по клику
    assert _tmp_files(db.data_dir) == []

    # по клику — priority и потолок 300 МиБ (здесь 32 байта): больше -> too_big
    with db.reader() as conn:
        message_id = conn.execute(
            "SELECT message_id FROM main.media WHERE id = ?", (job["media_id"],)
        ).fetchone()[0]
    db.write_sync(lambda conn: media.request_fetch(conn, message_id=message_id, idx=0, now_ms=1))
    (job2,) = await _lease(internal_client, secret)
    assert job2["max_bytes"] == 32
    r = await _put(internal_client, secret, job2["media_id"], job2["lease_gen"], b"x" * 40)
    assert r.status_code == 413
    assert _row(db, job2["media_id"])["status"] == "too_big"
    assert _tmp_files(db.data_dir) == []


async def test_put_dedup_one_file_two_rows(internal_client, db):
    account_id, secret = make_connector_account(db, source="telegram")
    _start(db)
    await _post_photo(internal_client, secret, "1")
    await _post_photo(internal_client, secret, "2")
    jobs = await _lease(internal_client, secret, limit=5)
    assert len(jobs) == 2
    for job in jobs:
        r = await _put(internal_client, secret, job["media_id"], job["lease_gen"], JPEG)
        assert r.status_code == 200
    sha = hashlib.sha256(JPEG).hexdigest()
    files = [p for p in media.account_dir(db.data_dir, account_id).rglob("*") if p.is_file()]
    assert files == [media.file_path(db.data_dir, account_id, sha)]
    assert {_row(db, j["media_id"])["sha256"] for j in jobs} == {sha}


# --- П4: длина, обрыв, повтор, истёкший лиз ---------------------------------------------------------------


async def test_put_requires_content_length(internal_client, db):
    _account, secret = make_connector_account(db, source="telegram")
    _start(db)
    await _post_photo(internal_client, secret, "1")
    (job,) = await _lease(internal_client, secret)

    async def _gen():
        yield JPEG

    r = await internal_client.put(
        f"/internal/v1/media/{job['media_id']}/content?lease_gen={job['lease_gen']}",
        headers=connector_headers(secret),
        content=_gen(),
    )
    assert r.status_code == 411
    assert _row(db, job["media_id"])["status"] == "fetching"


async def test_put_length_mismatch_then_retry_same_lease(internal_client, db):
    _account, secret = make_connector_account(db, source="telegram")
    _start(db)
    await _post_photo(internal_client, secret, "1")
    (job,) = await _lease(internal_client, secret)
    r = await _put(
        internal_client, secret, job["media_id"], job["lease_gen"], JPEG, headers={"Content-Length": "5000"}
    )
    assert r.status_code == 400
    assert _row(db, job["media_id"])["status"] == "fetching"
    assert _tmp_files(db.data_dir) == []
    r = await _put(internal_client, secret, job["media_id"], job["lease_gen"], JPEG)
    assert r.status_code == 200
    assert _row(db, job["media_id"])["status"] == "ready"


async def test_put_client_disconnect_keeps_fetching(internal_app, db):
    _account, secret = make_connector_account(db, source="telegram")
    _start(db)
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=internal_app), base_url="http://hub-internal") as c:
        await _post_photo(c, secret, "1")
        (job,) = await _lease(c, secret)

    messages = [
        {"type": "http.request", "body": JPEG[:10], "more_body": True},
        {"type": "http.disconnect"},
    ]

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "PUT",
        "scheme": "http",
        "path": f"/internal/v1/media/{job['media_id']}/content",
        "raw_path": f"/internal/v1/media/{job['media_id']}/content".encode(),
        "query_string": f"lease_gen={job['lease_gen']}".encode(),
        "root_path": "",
        "headers": [
            (b"host", b"hub-internal"),
            (b"authorization", f"Bearer {secret}".encode()),
            (b"content-length", str(len(JPEG)).encode()),
        ],
        "client": ("127.0.0.1", 1),
        "server": ("hub-internal", 80),
    }
    await internal_app(scope, receive, send)
    assert _row(db, job["media_id"])["status"] == "fetching"
    assert _tmp_files(db.data_dir) == []


async def test_put_after_takeover_is_409(internal_client, db, clock):
    _account, secret = make_connector_account(db, source="telegram")
    _start(db)
    await _post_photo(internal_client, secret, "1")
    await _post_photo(internal_client, secret, "2", ts=1_600_000_000_000)
    (job,) = await _lease(internal_client, secret)
    clock.advance_ms(media.LEASE_MS + 1)
    # другой лиз гоняет уборку истёкших: строка уходит в pending, но её никто не перехватил
    (other,) = await _lease(internal_client, secret)
    assert other["media_id"] == job["media_id"]  # свежее первым: снова она же, уже с новым поколением
    r = await _put(internal_client, secret, job["media_id"], job["lease_gen"], JPEG)
    assert r.status_code == 409  # перехвачена — старый PUT не принимается
    r = await _put(internal_client, secret, other["media_id"], other["lease_gen"], JPEG)
    assert r.status_code == 200


async def test_put_after_expiry_sweep_without_takeover(internal_client, db, clock):
    account_id, secret = make_connector_account(db, source="telegram")
    _start(db)
    await _post_photo(internal_client, secret, "1")
    (job,) = await _lease(internal_client, secret)
    clock.advance_ms(media.LEASE_MS + 1)
    # уборка истёкших без выдачи этой же строки: аккаунт limited — lease ничего не отдаёт
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.account SET status = 'limited' WHERE id = ?", (account_id,))
    )
    assert await _lease(internal_client, secret) == []
    assert _row(db, job["media_id"])["status"] == "pending"
    r = await _put(internal_client, secret, job["media_id"], job["lease_gen"], JPEG)
    assert r.status_code == 200
    assert _row(db, job["media_id"])["status"] == "ready"


# --- П12: 507 ----------------------------------------------------------------------------------------------


async def test_put_disk_full_507_temporary(internal_client, db, monkeypatch):
    _account, secret = make_connector_account(db, source="telegram")
    _start(db)
    await _post_photo(internal_client, secret, "1")
    (job,) = await _lease(internal_client, secret)
    monkeypatch.setattr(media, "free_bytes", lambda _d: 1024)
    r = await _put(internal_client, secret, job["media_id"], job["lease_gen"], JPEG)
    assert r.status_code == 507
    row = _row(db, job["media_id"])
    assert (row["status"], row["attempts"]) == ("pending", 1)
    assert row["not_before_ts"] is not None
    assert _tmp_files(db.data_dir) == []


# --- П5: устаревший лиз в потоке-писателе удаляет временный файл ------------------------------------------


async def test_commit_upload_stale_removes_tmp(internal_client, db):
    account_id, secret = make_connector_account(db, source="telegram")
    _start(db)
    await _post_photo(internal_client, secret, "1")
    (job,) = await _lease(internal_client, secret)
    tmp = media.account_dir(db.data_dir, account_id) / ".tmp-x"
    media.ensure_dir(tmp.parent)
    tmp.write_bytes(JPEG)
    with pytest.raises(media.StaleLease):
        db.write_sync(
            lambda conn: media.commit_upload(
                conn,
                media_id=job["media_id"],
                account_id=account_id,
                lease_gen=job["lease_gen"] + 5,
                tmp_path=tmp,
                sha256="e" * 64,
                size=len(JPEG),
                detected="jpeg",
                now_ms=1,
            )
        )
    assert not tmp.exists()


# --- report и wait (T3 по HTTP, контракт 5b) ------------------------------------------------------------


async def test_report_endpoint(internal_client, db):
    _account, secret = make_connector_account(db, source="telegram")
    _start(db)
    for ext in ("1", "2", "3"):
        await _post_photo(internal_client, secret, ext)
    a, b, c = await _lease(internal_client, secret, limit=5)
    url = "/internal/v1/media/{}/report"
    h = connector_headers(secret)
    r = await internal_client.post(
        url.format(a["media_id"]), headers=h, json={"lease_gen": a["lease_gen"], "outcome": "gone"}
    )
    assert r.status_code == 200
    r = await internal_client.post(
        url.format(b["media_id"]),
        headers=h,
        json={"lease_gen": b["lease_gen"], "outcome": "temporary", "error": "flood", "retry_after_s": 30},
    )
    assert r.status_code == 200
    r = await internal_client.post(
        url.format(c["media_id"]),
        headers=h,
        json={"lease_gen": c["lease_gen"], "outcome": "too_big", "size": 99},
    )
    assert r.status_code == 200
    assert _row(db, a["media_id"])["status"] == "gone"
    row_b = _row(db, b["media_id"])
    assert (row_b["status"], row_b["attempts"]) == ("pending", 0)
    assert (_row(db, c["media_id"])["status"], _row(db, c["media_id"])["size"]) == ("deferred", 99)

    r = await internal_client.post(
        url.format(a["media_id"]), headers=h, json={"lease_gen": a["lease_gen"], "outcome": "gone"}
    )
    assert r.status_code == 409  # лиз уже закрыт
    r = await internal_client.post(
        url.format(a["media_id"]), headers=h, json={"lease_gen": 1, "outcome": "boom"}
    )
    assert r.status_code == 422
    r = await internal_client.post(url.format(10**9), headers=h, json={"lease_gen": 1, "outcome": "gone"})
    assert r.status_code == 404


async def test_wait_ready_and_timeout(internal_client, db):
    _account, secret = make_connector_account(db, source="telegram")
    _start(db)
    r = await internal_client.get("/internal/v1/media/wait?timeout_s=0", headers=connector_headers(secret))
    assert r.json() == {"ready": False}
    await _post_photo(internal_client, secret, "1")
    r = await internal_client.get("/internal/v1/media/wait?timeout_s=25", headers=connector_headers(secret))
    assert r.status_code == 200 and r.json() == {"ready": True}


async def test_slack_ref_passed_to_job(internal_client, db):
    _account, secret = make_connector_account(db, source="slack")
    _start(db)
    ref = {"file_id": "F9", "url": "https://files.slack.com/files-pri/T-F9/a.png"}
    await _post_photo(
        internal_client,
        secret,
        "1.5",
        ts=1_700_000_000_000,
        att=[{"type": "photo", "src_id": "F9", "media_ref": ref}],
    )
    (job,) = await _lease(internal_client, secret)
    assert job["ref"] == ref and job["src_id"] == "F9"
    with db.reader() as conn:
        stored = conn.execute("SELECT attachments_json FROM main.message WHERE ext_id = '1.5'").fetchone()[0]
    assert "media_ref" not in json.loads(stored)[0]
