"""Ф22-1 (docs/architecture.md), ядро: B1 `GET /media/{id}/meta` и B2
`POST /media/{id}/fetch` для bearer-токена сессии (тесты D1, D2 и поправки П1, П3). Предикат проекта и
скрытых сообщений — как у `GET /media/{id}`; чужое, скрытое и несуществующее — одинаково 404; сессия не
обходит отсрочку источника (`failed` с будущим `not_before_ts` не трогается, `attempts` не сбрасываются)."""

import hashlib
import json

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

MIB = 1024 * 1024
BODY = b"%PDF-1.4\n" + b"x" * 200
META_KEYS = {
    "id",
    "status",
    "size",
    "stored_size",
    "detected",
    "name",
    "project_id",
    "channel_id",
    "message_id",
    "not_before_ts",
}


def _setup(db, *, project=True):
    account_id = insert_account(db, source="telegram")
    project_id = insert_project(db, name="P") if project else None
    channel_id = insert_channel(db, account_id, project_id=project_id, ext_id="c1")
    return account_id, project_id, channel_id


def _ingest(db, account_id, ext_id, attachments, *, ts=10_000):
    event = {
        "type": "upsert",
        "channel": {"ext_id": "c1", "kind": "dm"},
        "ext_id": ext_id,
        "ts": ts,
        "is_me": False,
        "kind": "file",
        "attachments": attachments,
        "mentions_me": False,
    }
    result = db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=[event], cursor=None, now_ms=ts)
    )
    assert result.dead == 0
    with db.reader() as conn:
        return conn.execute("SELECT id FROM main.message WHERE ext_id = ?", (ext_id,)).fetchone()[0]


def _media_id(db, message_id, idx=0):
    with db.reader() as conn:
        return conn.execute(
            "SELECT id FROM main.media WHERE message_id = ? AND idx = ?", (message_id, idx)
        ).fetchone()[0]


def _set(db, media_id, **values):
    cols = ", ".join(f"{k} = ?" for k in values)
    db.write_sync(
        lambda conn: conn.execute(
            f"UPDATE main.media SET {cols} WHERE id = ?",  # noqa: S608 — имена колонок из теста
            (*values.values(), media_id),
        )
    )


def _make_ready(db, account_id, media_id, body):
    sha = hashlib.sha256(body).hexdigest()
    path = media.file_path(db.data_dir, account_id, sha)
    media.ensure_dir(path.parent)
    path.write_bytes(body)
    _set(db, media_id, status="ready", sha256=sha, stored_size=len(body), detected=media.detect(body[:64]))


def _row(db, media_id):
    with db.reader() as conn:
        cur = conn.execute("SELECT * FROM main.media WHERE id = ?", (media_id,))
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, cur.fetchone(), strict=True))


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _audit(db):
    with db.reader() as conn:
        return conn.execute(
            "SELECT actor, api_token_id, client_name, result, channel_id, detail_json FROM main.audit "
            "WHERE action = 'media_fetch' ORDER BY id"
        ).fetchall()


def _token_id(db, name):
    with db.reader() as conn:
        return conn.execute("SELECT id FROM main.api_token WHERE name = ?", (name,)).fetchone()[0]


# --- D1: meta -----------------------------------------------------------------------------------------------


async def test_d1_meta_own_fields_and_not_ready_status(client, db):
    account_id, project_id, channel_id = _setup(db)
    db.write_sync(lambda conn: media.ensure_start_ts(conn, 0))
    mid = _ingest(
        db,
        account_id,
        "1",
        [
            {"type": "document", "name": "../../Отчёт.docx", "size": 30 * MIB},
            {"type": "document", "name": "b.pdf", "size": len(BODY)},
        ],
    )
    deferred_id = _media_id(db, mid, 0)
    ready_id = _media_id(db, mid, 1)
    _make_ready(db, account_id, ready_id, BODY)
    token = make_bearer_token(db, scopes=("read",), project_ids=[project_id])

    r = await client.get(f"/api/v1/media/{ready_id}/meta", headers=_bearer(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == META_KEYS
    assert body == {
        "id": ready_id,
        "status": "ready",
        "size": len(BODY),
        "stored_size": len(BODY),
        "detected": "pdf",
        "name": "b.pdf",
        "project_id": project_id,
        "channel_id": channel_id,
        "message_id": mid,
        "not_before_ts": None,
    }

    r = await client.get(f"/api/v1/media/{deferred_id}/meta", headers=_bearer(token))
    assert r.status_code == 200
    body = r.json()
    # не `ready` — статус, имя как в БД (сырое, MCP чистит сам), фактического размера ещё нет
    assert (body["status"], body["name"], body["stored_size"], body["size"]) == (
        "deferred",
        "../../Отчёт.docx",
        None,
        30 * MIB,
    )


async def test_d1_meta_foreign_hidden_missing_same_404(client, db):
    account_id, project_id, _c = _setup(db)
    other = insert_project(db, name="Other")
    mid = _ingest(db, account_id, "1", [{"type": "document", "name": "a.pdf", "size": 10}])
    hidden_mid = _ingest(db, account_id, "2", [{"type": "document", "name": "h.pdf", "size": 10}], ts=11_000)
    db.write_sync(lambda conn: media.request_fetch(conn, message_id=mid, idx=0, now_ms=1))
    db.write_sync(lambda conn: media.request_fetch(conn, message_id=hidden_mid, idx=0, now_ms=1))
    media_id = _media_id(db, mid)
    hidden_id = _media_id(db, hidden_mid)
    db.write_sync(lambda conn: conn.execute("UPDATE main.message SET hidden = 1 WHERE id = ?", (hidden_mid,)))
    own = make_bearer_token(db, scopes=("read",), project_ids=[project_id], name="own")
    foreign = make_bearer_token(db, scopes=("read",), project_ids=[other], name="foreign")

    assert (await client.get(f"/api/v1/media/{media_id}/meta", headers=_bearer(own))).status_code == 200
    responses = [
        await client.get(f"/api/v1/media/{media_id}/meta", headers=_bearer(foreign)),
        await client.get(f"/api/v1/media/{hidden_id}/meta", headers=_bearer(own)),
        await client.get("/api/v1/media/999999/meta", headers=_bearer(own)),
    ]
    assert [r.status_code for r in responses] == [404, 404, 404]
    assert len({r.text for r in responses}) == 1
    assert (await client.get(f"/api/v1/media/{media_id}/meta")).status_code == 401


async def test_d1_meta_unsorted_only_for_all_projects_token(client, db):
    account_id, _p, _c = _setup(db, project=False)
    mid = _ingest(db, account_id, "1", [{"type": "document", "name": "a.pdf", "size": 10}])
    db.write_sync(lambda conn: media.request_fetch(conn, message_id=mid, idx=0, now_ms=1))
    media_id = _media_id(db, mid)
    project_id = insert_project(db, name="P")
    scoped = make_bearer_token(db, scopes=("read",), project_ids=[project_id], name="scoped")
    everything = make_bearer_token(db, scopes=("read",), project_ids=None, name="all")
    assert (await client.get(f"/api/v1/media/{media_id}/meta", headers=_bearer(scoped))).status_code == 404
    r = await client.get(f"/api/v1/media/{media_id}/meta", headers=_bearer(everything))
    assert r.status_code == 200 and r.json()["project_id"] is None


# --- D2: fetch ----------------------------------------------------------------------------------------------


async def test_d2_fetch_deferred_to_pending_with_audit(client, db):
    account_id, project_id, channel_id = _setup(db)
    db.write_sync(lambda conn: media.ensure_start_ts(conn, 0))
    mid = _ingest(db, account_id, "1", [{"type": "document", "name": "секрет Алисы.xlsx", "size": 30 * MIB}])
    media_id = _media_id(db, mid)
    assert _row(db, media_id)["status"] == "deferred"
    token = make_bearer_token(db, scopes=("read",), project_ids=[project_id], name="mcp:sess")

    r = await client.post(
        f"/api/v1/media/{media_id}/fetch", headers={**_bearer(token), "X-Hub-Client": "mcp:p3"}
    )
    assert r.status_code == 200, r.text
    assert set(r.json()) == META_KEYS and r.json()["status"] == "pending"
    row = _row(db, media_id)
    assert (row["status"], row["priority"]) == ("pending", 1)
    ((actor, token_id, client_name, result, audit_channel, detail),) = _audit(db)
    assert (actor, token_id, client_name, result, audit_channel) == (
        "interactive",
        _token_id(db, "mcp:sess"),
        "mcp:p3",
        "ok",
        channel_id,
    )
    assert json.loads(detail) == {"media_id": media_id, "status": "deferred"}
    assert "Алисы" not in detail  # без имени файла

    # повтор по уже `pending` — строка как есть, аудит «без изменений»
    r = await client.post(f"/api/v1/media/{media_id}/fetch", headers=_bearer(token))
    assert r.status_code == 200 and r.json()["status"] == "pending"
    assert [a[3] for a in _audit(db)] == ["ok", "unchanged"]


async def test_d2_fetch_foreign_and_hidden_404_no_change(client, db):
    account_id, project_id, _c = _setup(db)
    other = insert_project(db, name="Other")
    db.write_sync(lambda conn: media.ensure_start_ts(conn, 0))
    mid = _ingest(db, account_id, "1", [{"type": "document", "name": "a.pdf", "size": 30 * MIB}])
    media_id = _media_id(db, mid)
    foreign = make_bearer_token(db, scopes=("read",), project_ids=[other], name="foreign")
    r = await client.post(f"/api/v1/media/{media_id}/fetch", headers=_bearer(foreign))
    assert r.status_code == 404
    assert _row(db, media_id)["status"] == "deferred"
    assert _audit(db) == []
    own = make_bearer_token(db, scopes=("read",), project_ids=[project_id], name="own")
    db.write_sync(lambda conn: conn.execute("UPDATE main.message SET hidden = 1 WHERE id = ?", (mid,)))
    assert (await client.post(f"/api/v1/media/{media_id}/fetch", headers=_bearer(own))).status_code == 404
    assert _row(db, media_id)["status"] == "deferred"
    assert (await client.post("/api/v1/media/999999/fetch", headers=_bearer(own))).status_code == 404


async def test_d2_fetch_too_big_by_size_or_stored_size(client, db):
    account_id, project_id, _c = _setup(db)
    db.write_sync(lambda conn: media.ensure_start_ts(conn, 0))
    mid = _ingest(
        db,
        account_id,
        "1",
        [{"type": "video", "size": 301 * MIB}, {"type": "document", "name": "d.zip"}],
    )
    big_id = _media_id(db, mid, 0)
    unknown_id = _media_id(db, mid, 1)
    _set(db, unknown_id, status="failed", stored_size=400 * MIB)  # размер от источника неизвестен (Slack)
    token = make_bearer_token(db, scopes=("read",), project_ids=[project_id])
    r = await client.post(f"/api/v1/media/{big_id}/fetch", headers=_bearer(token))
    assert r.json()["status"] == "too_big"
    r = await client.post(f"/api/v1/media/{unknown_id}/fetch", headers=_bearer(token))
    assert r.json()["status"] == "too_big"


async def test_p3_failed_respects_not_before_and_keeps_attempts(client, db, clock):
    account_id, project_id, _c = _setup(db)
    mid = _ingest(
        db,
        account_id,
        "1",
        [
            {"type": "document", "name": "a.pdf", "size": 10},
            {"type": "document", "name": "b.pdf", "size": 10},
        ],
    )
    db.write_sync(lambda conn: media.request_fetch(conn, message_id=mid, idx=0, now_ms=1))
    db.write_sync(lambda conn: media.request_fetch(conn, message_id=mid, idx=1, now_ms=1))
    later_id = _media_id(db, mid, 0)
    now_id = _media_id(db, mid, 1)
    now_ms = int(clock() * 1000)
    _set(db, later_id, status="failed", attempts=5, priority=0, not_before_ts=now_ms + 600_000)
    _set(db, now_id, status="failed", attempts=5, priority=0, not_before_ts=now_ms - 1)
    token = make_bearer_token(db, scopes=("read",), project_ids=[project_id])

    r = await client.post(f"/api/v1/media/{later_id}/fetch", headers=_bearer(token))
    assert r.status_code == 200
    assert (r.json()["status"], r.json()["not_before_ts"]) == ("failed", now_ms + 600_000)
    row = _row(db, later_id)
    assert (row["status"], row["attempts"], row["not_before_ts"]) == ("failed", 5, now_ms + 600_000)

    r = await client.post(f"/api/v1/media/{now_id}/fetch", headers=_bearer(token))
    assert r.json()["status"] == "pending"
    row = _row(db, now_id)
    # П3: отсрочка прошла — в очередь с приоритетом, но попытки НЕ сброшены (клик владельца сбросил бы)
    assert (row["status"], row["attempts"], row["priority"], row["not_before_ts"]) == ("pending", 5, 1, None)
    assert [a[3] for a in _audit(db)] == ["unchanged", "ok"]


async def test_d2_ui_click_handle_unchanged_and_still_ui_only(client, db, clock):
    account_id, project_id, _c = _setup(db)
    mid = _ingest(db, account_id, "1", [{"type": "document", "name": "a.pdf", "size": 10}])
    db.write_sync(lambda conn: media.request_fetch(conn, message_id=mid, idx=0, now_ms=1))
    media_id = _media_id(db, mid)
    _set(db, media_id, status="failed", attempts=5, not_before_ts=int(clock() * 1000) + 600_000)
    token = make_bearer_token(db, scopes=("read", "draft"), project_ids=[project_id])
    r = await client.post(f"/api/v1/messages/{mid}/media/0/fetch", headers=_bearer(token))
    assert r.status_code == 403
    cookie, _ = await login_via_code(client, db, clock)
    r = await client.post(f"/api/v1/messages/{mid}/media/0/fetch", headers=csrf_headers(cookie))
    assert r.status_code == 200
    row = _row(db, media_id)
    # клик владельца по-прежнему снимает отсрочку и сбрасывает попытки
    assert (row["status"], row["attempts"], row["not_before_ts"]) == ("pending", 0, None)
