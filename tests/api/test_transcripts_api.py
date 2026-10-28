"""Ф14-6 (docs/architecture.md, 14-6a, разделы C, F и П7, П8, П10): ручки службы расшифровки
(лиз долгим опросом, продление, отчёт), кнопка UI, скоуп `transcribe`, поле `transcript` в `/sync` и
`GET /messages/{id}`."""

import asyncio
import hashlib

import pytest

from hub.core import media, security, transcripts
from hub.core.ingest import apply_batch

from .conftest import csrf_headers, insert_account, insert_channel, insert_project, login_via_code

OGG = b"OggS" + b"\x00" * 60
VOICE = {"type": "voice", "mime": "audio/ogg", "size": 64, "duration_s": 42, "src_id": "v1"}
VIDEO = {"type": "video", "mime": "video/mp4", "size": 64, "duration_s": 5, "src_id": "x1"}


def _token(db, *, scopes=("read", "transcribe"), kind="background", project_ids=None):
    return db.write_sync(
        lambda conn: security.create_api_token(
            conn,
            name=transcripts.TOKEN_NAME,
            kind=kind,
            scopes=list(scopes),
            project_ids=project_ids,
            expires_ts=None,
        )
    ).token


def _forged_token(db, scopes: str) -> str:
    """Токен в обход `create_api_token` (так его выдать нельзя, П7) — чтобы проверить сами ручки."""
    value = "hub_forged_" + scopes.replace(" ", "_")

    def _fn(conn):
        conn.execute(
            "INSERT INTO main.api_token (name, kind, hash, scopes, revoked) "
            "VALUES ('f', 'background', ?, ?, 0)",
            (hashlib.sha256(value.encode()).hexdigest(), scopes),
        )

    db.write_sync(_fn)
    return value


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _setup(db, clock, attachments, *, ext_id="1"):
    account_id = insert_account(db, source="telegram")
    project_id = insert_project(db, name="P")
    insert_channel(db, account_id, project_id=project_id, ext_id="c1")
    now = int(clock() * 1000)
    db.write_sync(lambda conn: media.ensure_start_ts(conn, now - 60_000))
    event = {
        "type": "upsert",
        "channel": {"ext_id": "c1", "kind": "dm"},
        "ext_id": ext_id,
        "ts": now - 1000,
        "is_me": False,
        "kind": "file",
        "attachments": attachments,
        "mentions_me": False,
    }
    db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=[event], cursor=None, now_ms=now)
    )
    with db.reader() as conn:
        message_id = conn.execute("SELECT id FROM main.message WHERE ext_id = ?", (ext_id,)).fetchone()[0]
        media_ids = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM main.media WHERE message_id = ? ORDER BY idx", (message_id,)
            )
        ]
    return account_id, message_id, media_ids


def _ready(db, account_id, media_id, now_ms, *, queue=True):
    sha = hashlib.sha256(OGG).hexdigest()
    path = media.file_path(db.data_dir, account_id, sha)
    media.ensure_dir(path.parent)
    path.write_bytes(OGG)

    def _fn(conn):
        conn.execute(
            "UPDATE main.media SET status = 'ready', sha256 = ?, stored_size = ?, detected = 'ogg', "
            "fetched_ts = ? WHERE id = ?",
            (sha, len(OGG), now_ms, media_id),
        )
        if queue:
            transcripts.on_media_ready(conn, media_id, now_ms=now_ms)

    db.write_sync(_fn)


# --- скоуп (П7) --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "scopes", "project_ids"),
    [
        ("interactive", ("read", "transcribe"), None),
        ("background", ("read", "transcribe"), [1]),
        ("background", ("transcribe",), None),
        ("background", ("read", "transcribe", "triage"), None),
        ("background", ("read", "draft", "transcribe"), None),
    ],
)
def test_transcribe_scope_only_background_all_projects_read(db, kind, scopes, project_ids):
    with pytest.raises(ValueError, match="transcribe"):
        _token(db, kind=kind, scopes=scopes, project_ids=project_ids)


def test_transcribe_scope_allowed_combination(db):
    assert _token(db).startswith(security.BEARER_PREFIX)


async def test_read_without_transcribe_cannot_lease(client, db):
    token = db.write_sync(
        lambda conn: security.create_api_token(
            conn,
            name="agent:triage",
            kind="background",
            scopes=["read", "triage"],
            project_ids=None,
            expires_ts=None,
        )
    ).token
    r = await client.post("/api/v1/transcripts/lease", json={}, headers=_bearer(token))
    assert r.status_code == 403
    r = await client.post(
        "/api/v1/transcripts/1/report", json={"lease_gen": 1, "outcome": "done"}, headers=_bearer(token)
    )
    assert r.status_code == 403


async def test_transcribe_without_read_cannot_get_media(client, db, clock):
    account_id, _mid, (media_id,) = _setup(db, clock, [VOICE])
    _ready(db, account_id, media_id, int(clock() * 1000))
    only = _forged_token(db, "transcribe")
    both = _forged_token(db, "read transcribe")
    assert (await client.get(f"/api/v1/media/{media_id}", headers=_bearer(only))).status_code == 403
    r = await client.get(f"/api/v1/media/{media_id}", headers=_bearer(both))
    assert r.status_code == 200 and r.content == OGG


async def test_ui_session_cannot_lease(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    r = await client.post("/api/v1/transcripts/lease", json={}, headers=csrf_headers(cookie))
    assert r.status_code == 403


# --- лиз, продление, отчёт --------------------------------------------------------------------------------


async def test_lease_report_roundtrip_and_output(client, db, clock):
    account_id, mid, (media_id,) = _setup(db, clock, [VOICE])
    now = int(clock() * 1000)
    _ready(db, account_id, media_id, now)
    token = _token(db)

    r = await client.post("/api/v1/transcripts/lease", json={"wait_s": 0}, headers=_bearer(token))
    assert r.status_code == 200, r.text
    job = r.json()
    assert set(job) == {
        "media_id",
        "message_id",
        "lease_gen",
        "kind",
        "mime",
        "detected",
        "duration_s",
        "size",
        "manual",
        "max_s",
    }
    assert (job["media_id"], job["message_id"], job["manual"], job["max_s"]) == (media_id, mid, False, 600)

    r = await client.post(
        f"/api/v1/transcripts/{media_id}/extend",
        json={"lease_gen": job["lease_gen"], "duration_s": 300},
        headers=_bearer(token),
    )
    assert r.status_code == 200 and r.json()["lease_expires_ts"] == now + 900_000

    r = await client.post(
        f"/api/v1/transcripts/{media_id}/report",
        json={"lease_gen": job["lease_gen"], "outcome": "done", "text": "привет из голосового", "lang": "de"},
        headers=_bearer(token),
    )
    assert r.status_code == 422
    r = await client.post(
        f"/api/v1/transcripts/{media_id}/report",
        json={"lease_gen": job["lease_gen"] + 5, "outcome": "done", "text": "x", "lang": "ru"},
        headers=_bearer(token),
    )
    assert r.status_code == 409
    r = await client.post(
        f"/api/v1/transcripts/{media_id}/report",
        json={"lease_gen": job["lease_gen"], "outcome": "maybe"},
        headers=_bearer(token),
    )
    assert r.status_code == 422
    r = await client.post(
        f"/api/v1/transcripts/{media_id}/report",
        json={
            "lease_gen": job["lease_gen"],
            "outcome": "done",
            "text": "привет из голосового",
            "lang": "ru",
            "lang_prob": 0.98,
            "model": "large-v3-turbo",
            "device": "cuda",
            "elapsed_ms": 900,
        },
        headers=_bearer(token),
    )
    assert r.status_code == 200 and r.json() == {"ok": True, "status": "done"}

    expected = {
        "status": "done",
        "text": "привет из голосового",
        "lang": "ru",
        "problem": None,
        "manual": False,
    }
    cookie, _ = await login_via_code(client, db, clock)
    r = await client.get(f"/api/v1/messages/{mid}")
    assert r.status_code == 200
    assert r.json()["media"][0]["transcript"] == expected
    assert r.json()["transcript_wait_ms"] == 0
    r = await client.get("/api/v1/sync", params={"since": 0})
    (msg,) = [m for m in r.json()["messages"] if m["id"] == mid]
    assert msg["media"][0]["transcript"] == expected
    r = await client.get("/api/v1/search", params={"q": "голосового"})
    assert [m["message_id"] for m in r.json()] == [mid]
    assert r.json()[0]["text"] == "голосовое: привет из голосового"


async def test_lease_204_after_wait(client, db, monkeypatch):
    token = _token(db)
    monkeypatch.setattr("hub.core.routes.transcripts._POLL_S", 0.05)
    r = await client.post("/api/v1/transcripts/lease", json={"wait_s": 0.2}, headers=_bearer(token))
    assert r.status_code == 204


async def test_lease_long_poll_wakes_on_new_job(client, db, clock):
    account_id, _mid, (media_id,) = _setup(db, clock, [VOICE])
    token = _token(db)

    async def _make_ready():
        await asyncio.sleep(0.2)
        now = int(clock() * 1000)
        sha = hashlib.sha256(OGG).hexdigest()
        path = media.file_path(db.data_dir, account_id, sha)
        media.ensure_dir(path.parent)
        path.write_bytes(OGG)

        def _fn(conn):
            conn.execute(
                "UPDATE main.media SET status = 'ready', sha256 = ?, stored_size = 64, detected = 'ogg', "
                "fetched_ts = ? WHERE id = ?",
                (sha, now, media_id),
            )
            transcripts.on_media_ready(conn, media_id, now_ms=now)

        await db.write(_fn)

    task = asyncio.create_task(_make_ready())
    started = asyncio.get_running_loop().time()
    r = await client.post("/api/v1/transcripts/lease", json={"wait_s": 10}, headers=_bearer(token))
    elapsed = asyncio.get_running_loop().time() - started
    await task
    assert r.status_code == 200 and r.json()["media_id"] == media_id
    assert elapsed < 5  # пробуждение коммитом, не опрос раз в 5 с и не конец ожидания


async def test_lease_wait_writes_nothing_while_idle(client, db, monkeypatch):
    """Долгий опрос без заданий не пишет в БД (пустая запись будила бы всех ожидающих — П1)."""
    token = _token(db)
    writes = []
    original = db.write

    async def _counting(fn):
        writes.append(fn)
        return await original(fn)

    monkeypatch.setattr(db, "write", _counting)
    monkeypatch.setattr("hub.core.routes.transcripts._POLL_S", 0.05)
    r = await client.post("/api/v1/transcripts/lease", json={"wait_s": 0.3}, headers=_bearer(token))
    assert r.status_code == 204
    assert writes == []


# --- кнопка UI -------------------------------------------------------------------------------------------


async def test_ui_button(client, db, clock):
    account_id, mid, (voice_id, video_id, audio_id) = _setup(
        db, clock, [VOICE, VIDEO, {"type": "audio", "mime": "audio/mpeg", "size": 64, "src_id": "a1"}]
    )
    now = int(clock() * 1000)
    cookie, _ = await login_via_code(client, db, clock)
    headers = csrf_headers(cookie)

    r = await client.post(f"/api/v1/media/{voice_id}/transcribe", headers=headers)
    assert r.status_code == 409 and r.json()["detail"] == "media_not_ready"
    _ready(db, account_id, video_id, now, queue=False)
    r = await client.post(f"/api/v1/media/{video_id}/transcribe", headers=headers)
    assert r.status_code == 422
    _ready(db, account_id, audio_id, now, queue=False)
    r = await client.post(f"/api/v1/media/{audio_id}/transcribe", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["message_id"] == mid
    assert body["media"][2]["transcript"]["status"] == "queued" and body["media"][2]["transcript"]["manual"]
    db.write_sync(lambda conn: transcripts.lease(conn, now_ms=now))
    r = await client.post(f"/api/v1/media/{audio_id}/transcribe", headers=headers)
    assert r.status_code == 409 and r.json()["detail"] == "transcribe_busy"
    assert (await client.post("/api/v1/media/999999/transcribe", headers=headers)).status_code == 404
    with db.reader() as conn:
        audit = conn.execute("SELECT result FROM main.audit WHERE action = 'media_transcribe'").fetchall()
    assert audit == [("ok",)]


async def test_ui_button_bearer_403(client, db, clock):
    account_id, _mid, (voice_id,) = _setup(db, clock, [VOICE])
    _ready(db, account_id, voice_id, int(clock() * 1000), queue=False)
    for token in (_token(db), _forged_token(db, "read draft")):
        r = await client.post(f"/api/v1/media/{voice_id}/transcribe", headers=_bearer(token))
        assert r.status_code == 403
    with db.reader() as conn:
        denied = conn.execute("SELECT COUNT(*) FROM main.audit WHERE action = 'ui_only_denied'").fetchone()[0]
        assert conn.execute("SELECT COUNT(*) FROM main.transcript").fetchone()[0] == 0
    assert denied == 2
