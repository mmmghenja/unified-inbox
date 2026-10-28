"""Ф14-3: файлы от сессий Claude в черновиках — ядро (`docs/architecture.md`,
тесты F 1–9 и поправки П3–П5, П8, П9). Скоуп `attach`, `POST /uploads` от bearer с квотой, пересылка
`media:<id>` (`POST /uploads/from-media`), `files` в `/messages/send` от bearer (всегда `pending_approval`),
`drafts/new` с `hold`, уборка `hold`-заготовок, `approve` с файлами, `approve-batch`, `files` в
`GET /outbox`."""

import asyncio
import hashlib
import json

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import draft_channels, hiding, media, queries, security, uploads
from hub.core.app_internal import create_internal_app
from hub.core.audit import write_audit
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.feed import Notifier
from hub.core.routes import uploads as uploads_route

from .conftest import (
    BASE_URL,
    csrf_headers,
    insert_channel,
    insert_message,
    insert_project,
    login_via_code,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200 + b"png-end"
MIB = 1024 * 1024
ADMIN_SECRET = ADMIN_PREFIX + "session-files-admin-secret-0123456789abcdef"


def _now(clock) -> int:
    return int(clock() * 1000)


def _pdf(tag: str) -> bytes:
    return b"%PDF-1.7\n" + tag.encode() + b"x" * 100


def _account(db, source="telegram", *, db_name="main") -> int:
    config = {"idempotent_send": True, "pace_window": "00:00-24:00"}
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, config_json) VALUES (?, ?, ?)",
                (source, db_name, json.dumps(config)),
            ).lastrowid
        )
    )


def _token(db, *, scopes=("read", "draft", "attach"), name="mcp:all", project_ids=None, kind="interactive"):
    created = db.write_sync(
        lambda conn: security.create_api_token(
            conn, name=name, kind=kind, scopes=list(scopes), project_ids=project_ids, expires_ts=None
        )
    )
    return created.token, created.token_id


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def bearer(app):
    """Отдельный клиент без cookie (у `client` после входа кука — bearer с ней был бы 400)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as c:
        yield c


@pytest.fixture
async def admin_client(db, clock):
    app = create_internal_app(db, clock=clock, notifier=Notifier(), admin_key_hash=hash_secret(ADMIN_SECRET))
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://hub-internal",
        headers={"Authorization": f"Bearer {ADMIN_SECRET}"},
    ) as c:
        yield c


@pytest.fixture
def env(db):
    project = insert_project(db, name="Umbrella")
    other = insert_project(db, name="Initech")
    tg = _account(db)
    channel = insert_channel(db, tg, project_id=project, ext_id="c1")
    token, token_id = _token(db)
    return {
        "project": project,
        "other": other,
        "tg": tg,
        "channel": channel,
        "token": token,
        "token_id": token_id,
    }


async def _up(bearer, token, channel_id, body=None, *, name="a.pdf"):
    return await bearer.post(
        f"/api/v1/uploads?channel_id={channel_id}",
        content=body if body is not None else _pdf(name),
        headers={**_auth(token), "X-Filename": name},
    )


async def _send(bearer, token, channel_id, *, text="", files=None, key="k1"):
    payload = {"channel_id": channel_id, "text": text, "idempotency_key": key}
    if files is not None:
        payload["files"] = files
    return await bearer.post("/api/v1/messages/send", json=payload, headers=_auth(token))


def _audit_upload(db, clock, token_id, size, *, count=1):
    def _fn(conn):
        for _ in range(count):
            write_audit(
                conn,
                now_ms=_now(clock),
                actor="interactive",
                api_token_id=token_id,
                client_name=None,
                request_id="r",
                action="upload",
                result="ok",
                detail={"size": size, "detected": "pdf"},
            )

    db.write_sync(_fn)


def _count(db, sql, params=()):
    with db.reader() as conn:
        return conn.execute(sql, params).fetchone()[0]


def _tmp_files(db, account_id):
    root = media.account_dir(db.data_dir, account_id)
    return [p for p in root.rglob(f"{media.TMP_PREFIX}*")] if root.exists() else []


def _outbox(db, outbox_id):
    schema = "work" if outbox_id >= 2**40 else "main"
    with db.reader() as conn:
        cur = conn.execute(f"SELECT * FROM {schema}.outbox WHERE id = ?", (outbox_id,))  # noqa: S608
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, cur.fetchone(), strict=True))


# --- F1: скоуп `attach` ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scopes", "kind", "name"),
    [
        (("read", "draft", "organize", "orchestrate", "attach"), "interactive", "mcp:x"),
        (("read", "draft", "runs", "attach"), "interactive", "mcp:x"),
        (("read", "draft", "attach"), "background", "agent"),
        (("read", "draft", "attach"), "interactive", "mcp:wake-3"),
        (("read", "draft", "attach"), "interactive", "mcp:orchestrator"),
    ],
)
def test_1_create_token_attach_denied(db, scopes, kind, name):
    with pytest.raises(ValueError, match="attach|orchestrate"):
        _token(db, scopes=scopes, kind=kind, name=name)


def test_1_create_token_attach_allowed_for_interactive(db):
    _token_value, token_id = _token(db, scopes=("read", "draft", "organize", "attach"))
    with db.reader() as conn:
        scopes = conn.execute("SELECT scopes FROM main.api_token WHERE id = ?", (token_id,)).fetchone()[0]
    assert "attach" in scopes.split()


async def test_1_attach_route_on_off_takes_effect_next_request(admin_client, bearer, db, env):
    token, token_id = _token(db, scopes=("read", "draft"), name="mcp:marketer")
    assert (await _up(bearer, token, env["channel"])).status_code == 403

    r = await admin_client.post(f"/internal/v1/admin/tokens/{token_id}/attach", json={"on": True})
    assert r.status_code == 200, r.text
    assert "attach" in r.json()["scopes"]
    assert (await _up(bearer, token, env["channel"])).status_code == 200

    r = await admin_client.post(f"/internal/v1/admin/tokens/{token_id}/attach", json={"on": False})
    assert r.status_code == 200
    assert "attach" not in r.json()["scopes"]
    assert (await _up(bearer, token, env["channel"], name="b.pdf")).status_code == 403
    audits = _count(
        db, "SELECT COUNT(*) FROM main.audit WHERE action = 'token_attach' AND api_token_id = ?", (token_id,)
    )
    assert audits == 2


async def test_1_attach_route_refusals(admin_client, db, client, clock):
    _t, revoked_id = _token(db, scopes=("read", "draft"), name="mcp:old")
    db.write_sync(lambda conn: security.revoke_api_token(conn, revoked_id))
    _t, wake_id = _token(db, scopes=("read", "draft", "runs"), name="mcp:wake-3")
    _t, plain_wake_id = _token(db, scopes=("read", "draft"), name="mcp:wake-4")
    _t, orch_id = _token(db, scopes=("read", "draft", "organize", "orchestrate"), name="mcp:orchestrator")
    _t, bg_id = _token(db, scopes=("read", "draft"), name="agent", kind="background")
    await login_via_code(client, db, clock)
    with db.reader() as conn:
        ui_id = conn.execute("SELECT id FROM main.api_token WHERE kind = 'ui'").fetchone()[0]

    for token_id in (revoked_id, wake_id, plain_wake_id, orch_id, bg_id, ui_id):
        r = await admin_client.post(f"/internal/v1/admin/tokens/{token_id}/attach", json={"on": True})
        assert r.status_code == 409, (token_id, r.text)
    r = await admin_client.post("/internal/v1/admin/tokens/999999/attach", json={"on": True})
    assert r.status_code == 404
    with db.reader() as conn:
        rows = conn.execute("SELECT scopes FROM main.api_token").fetchall()
    assert all("attach" not in (s or "").split() for (s,) in rows)


# --- F2: `POST /uploads` от bearer --------------------------------------------------------------------------


async def test_2_upload_bearer_without_attach_403_with_audit(bearer, db, env):
    token, _ = _token(db, scopes=("read", "draft", "send"), name="mcp:plain")
    r = await _up(bearer, token, env["channel"])
    assert r.status_code == 403
    assert _count(db, "SELECT COUNT(*) FROM main.audit WHERE action = 'ui_only_denied'") == 1
    assert _count(db, "SELECT COUNT(*) FROM main.upload") == 0


async def test_2_upload_bearer_with_attach_ok_and_audited(bearer, db, env):
    r = await _up(bearer, env["token"], env["channel"], name="dogovor.pdf")
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "dogovor.pdf"
    with db.reader() as conn:
        row = conn.execute(
            "SELECT api_token_id, actor, detail_json FROM main.audit WHERE action = 'upload'"
        ).fetchone()
    assert row[0] == env["token_id"]
    assert row[1] == "interactive"
    assert "dogovor" not in row[2]  # в аудит — без имён


async def test_2_upload_invisible_channel_404_and_no_reply_422(bearer, db, env):
    token, _ = _token(db, scopes=("read", "draft", "attach"), name="mcp:initech", project_ids=[env["other"]])
    assert (await _up(bearer, token, env["channel"])).status_code == 404
    tracker = insert_channel(db, env["tg"], project_id=env["project"], ext_id="jira")
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET meta_json = ? WHERE id = ?",
            (json.dumps({"attrs": {"no_reply": True}}), tracker),
        )
    )
    r = await _up(bearer, env["token"], tracker)
    assert r.status_code == 422
    assert "не отправляются" in r.json()["detail"]
    assert _count(db, "SELECT COUNT(*) FROM main.upload") == 0


async def test_2_quota_30_files_then_429(bearer, db, clock, env):
    _audit_upload(db, clock, env["token_id"], 10, count=29)
    assert (await _up(bearer, env["token"], env["channel"], name="30.pdf")).status_code == 200
    r = await _up(bearer, env["token"], env["channel"], name="31.pdf")
    assert r.status_code == 429
    assert "квота файлов сессии" in r.json()["detail"]
    assert "осталось 0 файлов" in r.json()["detail"]
    # скользящие сутки: через 24 ч квота снова есть
    clock.advance_ms(uploads.SESSION_WINDOW_MS + 1000)
    assert (await _up(bearer, env["token"], env["channel"], name="32.pdf")).status_code == 200


async def test_2_quota_100_mib_sum(bearer, db, clock, env):
    body = _pdf("big")
    _audit_upload(db, clock, env["token_id"], uploads.SESSION_MAX_BYTES - len(body) + 1)
    r = await _up(bearer, env["token"], env["channel"], body)
    assert r.status_code == 429
    small = b"%PDF-1.7\n"
    assert (await _up(bearer, env["token"], env["channel"], small, name="s.pdf")).status_code == 200


async def test_2_quota_per_token_and_ui_unlimited(bearer, client, db, clock, env):
    _audit_upload(db, clock, env["token_id"], 10, count=30)
    assert (await _up(bearer, env["token"], env["channel"])).status_code == 429
    other, _ = _token(db, name="mcp:second")
    assert (await _up(bearer, other, env["channel"])).status_code == 200

    cookie, _ = await login_via_code(client, db, clock)
    with db.reader() as conn:
        ui_id = conn.execute("SELECT id FROM main.api_token WHERE kind = 'ui'").fetchone()[0]
    _audit_upload(db, clock, ui_id, 10 * MIB, count=40)
    r = await client.post(
        f"/api/v1/uploads?channel_id={env['channel']}",
        content=PNG,
        headers={**csrf_headers(cookie), "X-Filename": "shot.png"},
    )
    assert r.status_code == 200, r.text


async def test_p4_quota_rechecked_in_writer(bearer, db, clock, env, monkeypatch):
    """П4: ранняя проверка пропустила (подменена), писатель отказал — 429, временный файл удалён."""
    monkeypatch.setattr(uploads_route, "_early_quota", lambda *a, **k: None)
    _audit_upload(db, clock, env["token_id"], 10, count=30)
    r = await _up(bearer, env["token"], env["channel"])
    assert r.status_code == 429
    assert _count(db, "SELECT COUNT(*) FROM main.upload") == 0
    assert _tmp_files(db, env["tg"]) == []
    assert not any(media.account_dir(db.data_dir, env["tg"]).rglob("*.pdf"))


async def test_p4_parallel_uploads_on_boundary_one_429(bearer, db, clock, env):
    _audit_upload(db, clock, env["token_id"], 10, count=29)
    first, second = await asyncio.gather(
        _up(bearer, env["token"], env["channel"], name="x.pdf"),
        _up(bearer, env["token"], env["channel"], name="y.pdf"),
    )
    assert sorted([first.status_code, second.status_code]) == [200, 429]
    assert _count(db, "SELECT COUNT(*) FROM main.upload") == 1
    assert _tmp_files(db, env["tg"]) == []


# --- F3 + П5: пересылка `media:<id>` ------------------------------------------------------------------------


def _media(db, clock, channel_id, body, *, name="scan.pdf", ready=True, ext_id="m1"):
    """Входящее сообщение с одним вложением; `ready` — файл скачан и лежит на складе."""
    schema = "work" if channel_id >= 2**40 else "main"
    message_id = insert_message(db, channel_id, ext_id=ext_id, ts=_now(clock) - 1000, kind="file")
    with db.reader() as conn:
        account_id = conn.execute(
            f"SELECT account_id FROM {schema}.channel WHERE id = ?", (channel_id,)  # noqa: S608
        ).fetchone()[0]
    sha = hashlib.sha256(body).hexdigest()
    if ready:
        path = media.file_path(db.data_dir, account_id, sha)
        media.ensure_dir(path.parent)
        path.write_bytes(body)

    def _fn(conn):
        return conn.execute(
            f"INSERT INTO {schema}.media (message_id, account_id, idx, kind, mime, size, name, status, "  # noqa: S608
            "sha256, stored_size, detected, created_ts, fetched_ts) VALUES (?, ?, 0, 'document', "
            "'application/pdf', ?, ?, ?, ?, ?, 'pdf', 0, 0)",
            (
                message_id,
                account_id,
                len(body),
                name,
                "ready" if ready else "pending",
                sha if ready else None,
                len(body) if ready else None,
            ),
        ).lastrowid

    return message_id, db.write_sync(_fn), account_id, sha


async def _fwd(bearer, token, channel_id, media_id):
    return await bearer.post(
        "/api/v1/uploads/from-media",
        json={"channel_id": channel_id, "media_id": media_id},
        headers=_auth(token),
    )


async def test_3_from_media_same_account_no_copy(bearer, db, clock, env):
    source = insert_channel(db, env["tg"], project_id=env["project"], ext_id="src")
    body = _pdf("same")
    _msg, media_id, _acc, sha = _media(db, clock, source, body, name="акт.pdf")
    r = await _fwd(bearer, env["token"], env["channel"], media_id)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["name"] == "акт.pdf"
    assert out["size"] == len(body)
    assert out["mime"] == "application/pdf"
    with db.reader() as conn:
        row = conn.execute(
            "SELECT channel_id, account_id, sha256 FROM main.upload WHERE id = ?", (out["upload_id"],)
        ).fetchone()
        detail = conn.execute("SELECT detail_json FROM main.audit WHERE action = 'upload'").fetchone()[0]
    assert row == (env["channel"], env["tg"], sha)
    assert json.loads(detail)["from_media"] is True
    files = [p for p in media.account_dir(db.data_dir, env["tg"]).rglob("*") if p.is_file()]
    assert len(files) == 1  # без копии


async def test_3_from_media_other_account_copies_work_to_main(bearer, db, clock, env):
    work_acc = _account(db, db_name="work")
    source = insert_channel(db, work_acc, project_id=env["project"], ext_id="src", db_name="work")
    body = _pdf("cross")
    _msg, media_id, _acc, sha = _media(db, clock, source, body)
    assert media_id >= 2**40
    r = await _fwd(bearer, env["token"], env["channel"], media_id)
    assert r.status_code == 200, r.text
    copied = media.file_path(db.data_dir, env["tg"], sha)
    assert copied.read_bytes() == body
    assert media.file_path(db.data_dir, work_acc, sha).is_file()  # источник на месте
    with db.reader() as conn:
        row = conn.execute("SELECT account_id, sha256 FROM main.upload").fetchone()
    assert row == (env["tg"], sha)
    assert _tmp_files(db, env["tg"]) == []


async def test_3_from_media_invisible_hidden_deleted_404(bearer, db, clock, env):
    foreign = insert_channel(db, env["tg"], project_id=env["other"], ext_id="initech")
    _m, foreign_media, _a, _s = _media(db, clock, foreign, _pdf("initech"), ext_id="f1")
    token, _ = _token(db, name="mcp:iip", project_ids=[env["project"]])
    assert (await _fwd(bearer, token, env["channel"], foreign_media)).status_code == 404

    source = insert_channel(db, env["tg"], project_id=env["project"], ext_id="src")
    hidden_msg, hidden_media, _a, _s = _media(db, clock, source, _pdf("hid"), ext_id="h1")
    db.write_sync(lambda conn: hiding.hide_message(conn, hidden_msg, now_ms=_now(clock)))
    assert (await _fwd(bearer, env["token"], env["channel"], hidden_media)).status_code == 404

    deleted_msg, deleted_media, _a, _s = _media(db, clock, source, _pdf("del"), ext_id="d1")
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.message SET deleted = 1 WHERE id = ?", (deleted_msg,))
    )
    assert (await _fwd(bearer, env["token"], env["channel"], deleted_media)).status_code == 404
    assert (await _fwd(bearer, env["token"], env["channel"], 987_654)).status_code == 404
    assert _count(db, "SELECT COUNT(*) FROM main.upload") == 0


async def test_3_from_media_not_downloaded_422_and_counts_in_quota(bearer, db, clock, env):
    source = insert_channel(db, env["tg"], project_id=env["project"], ext_id="src")
    _m, pending, _a, _s = _media(db, clock, source, _pdf("p"), ready=False, ext_id="p1")
    r = await _fwd(bearer, env["token"], env["channel"], pending)
    assert r.status_code == 422
    assert "не скачано" in r.json()["detail"]

    _m, ready, _a, _s = _media(db, clock, source, _pdf("r"), ext_id="r1")
    _audit_upload(db, clock, env["token_id"], 10, count=29)
    assert (await _fwd(bearer, env["token"], env["channel"], ready)).status_code == 200
    assert (await _up(bearer, env["token"], env["channel"])).status_code == 429  # пересылка вошла в квоту


async def test_3_from_media_without_attach_403(bearer, db, clock, env):
    source = insert_channel(db, env["tg"], project_id=env["project"], ext_id="src")
    _m, media_id, _a, _s = _media(db, clock, source, _pdf("x"))
    token, _ = _token(db, scopes=("read", "draft"), name="mcp:plain")
    assert (await _fwd(bearer, token, env["channel"], media_id)).status_code == 403


@pytest.mark.parametrize("cross", [False, True])
async def test_p5_source_hidden_between_read_and_write_404(bearer, db, clock, env, monkeypatch, cross):
    if cross:
        acc = _account(db, db_name="work")
        source = insert_channel(db, acc, project_id=env["project"], ext_id="src", db_name="work")
    else:
        source = insert_channel(db, env["tg"], project_id=env["project"], ext_id="src")
    message_id, media_id, _a, sha = _media(db, clock, source, _pdf("race"))
    original = uploads_route._prepare

    async def _hide_then_prepare(*args, **kwargs):
        result = await original(*args, **kwargs)
        db.write_sync(lambda conn: hiding.hide_message(conn, message_id, now_ms=_now(clock)))
        return result

    monkeypatch.setattr(uploads_route, "_prepare", _hide_then_prepare)
    r = await _fwd(bearer, env["token"], env["channel"], media_id)
    assert r.status_code == 404
    assert _count(db, "SELECT COUNT(*) FROM main.upload") == 0
    assert _tmp_files(db, env["tg"]) == []
    if cross:
        assert not media.file_path(db.data_dir, env["tg"], sha).exists()


# --- F4: `/messages/send` от bearer с файлами ---------------------------------------------------------------


async def test_4_send_files_without_attach_422(bearer, db, env):
    upload_id = (await _up(bearer, env["token"], env["channel"])).json()["upload_id"]
    token, _ = _token(db, scopes=("read", "draft", "send"), name="mcp:plain")
    r = await _send(bearer, token, env["channel"], text="x", files=[upload_id])
    assert r.status_code == 422
    assert "право прикладывать файлы" in r.json()["detail"]


async def test_4_send_files_with_attach_is_pending_approval(bearer, db, env):
    a = (await _up(bearer, env["token"], env["channel"], name="a.pdf")).json()["upload_id"]
    b = (await _up(bearer, env["token"], env["channel"], name="b.pdf")).json()["upload_id"]
    r = await _send(bearer, env["token"], env["channel"], text="", files=[a, b])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending_approval"
    row = _outbox(db, r.json()["outbox_id"])
    assert row["status"] == "pending_approval"
    assert row["origin"] == "api"
    assert [f["name"] for f in json.loads(row["file_json"])] == ["a.pdf", "b.pdf"]
    with db.reader() as conn:
        names = [
            n
            for (n,) in conn.execute(
                "SELECT name FROM main.media WHERE message_id = ? ORDER BY idx", (r.json()["message_id"],)
            )
        ]
    assert names == ["a.pdf", "b.pdf"]


# --- F5 + П2, П3: `drafts/new` с `hold` ---------------------------------------------------------------------


async def _hold(bearer, token, env, to="@Petr_Ivanov", **kw):
    return await bearer.post(
        "/api/v1/drafts/new",
        json={"account_id": env["tg"], "project_id": env["project"], "to": to, "hold": True, **kw},
        headers=_auth(token),
    )


async def test_5_hold_creates_channel_without_outbox_then_send(bearer, db, env):
    r = await _hold(bearer, env["token"], env)
    assert r.status_code == 200, r.text
    held = r.json()
    assert held["outbox_id"] is None
    assert held["status"] == "held"
    assert held["existing"] is False
    assert held["created"] is True
    channel_id = held["channel_id"]
    assert _count(db, "SELECT COUNT(*) FROM main.outbox WHERE channel_id = ?", (channel_id,)) == 0
    with db.reader() as conn:
        ext_id, meta_json = conn.execute(
            "SELECT ext_id, meta_json FROM main.channel WHERE id = ?", (channel_id,)
        ).fetchone()
        audit = conn.execute(
            "SELECT result, detail_json FROM main.audit WHERE action = 'draft_new'"
        ).fetchone()
    assert draft_channels.is_temp_ext_id(ext_id)
    assert json.loads(meta_json)["new_draft"] == {
        "to": {"kind": "tg_username", "username": "Petr_Ivanov"},
        "origin": "api",
        "hold": True,
    }
    assert audit[0] == "held"
    assert json.loads(audit[1])["hold"] is True
    assert json.loads(audit[1])["text_len"] == 0

    again = (await _hold(bearer, env["token"], env, to="@petr_ivanov")).json()
    assert again["channel_id"] == channel_id
    assert again["created"] is False

    upload_id = (await _up(bearer, env["token"], channel_id)).json()["upload_id"]
    s = await _send(bearer, env["token"], channel_id, text="Договор во вложении", files=[upload_id])
    assert s.status_code == 200, s.text
    assert s.json()["status"] == "pending_approval"
    assert s.json()["paced"] is True
    assert _count(db, "SELECT COUNT(*) FROM main.outbox WHERE channel_id = ?", (channel_id,)) == 1

    busy = await _hold(bearer, env["token"], env)
    assert busy.status_code == 409  # живая строка есть — как `DraftExists`


async def test_5_hold_returns_open_ui_draft(bearer, client, db, clock, env):
    """П3: открытая заготовка без живой строки — она же, чья бы ни была (здесь — UI-заготовка владельца)."""
    cookie, _ = await login_via_code(client, db, clock)
    r = await client.post(
        "/api/v1/channels/new",
        json={"account_id": env["tg"], "project_id": env["project"], "to": "@Petr_Ivanov"},
        headers=csrf_headers(cookie),
    )
    assert r.status_code == 200, r.text
    ui_channel = r.json()["channel"]["id"]
    held = (await _hold(bearer, env["token"], env)).json()
    assert held["channel_id"] == ui_channel
    assert held["created"] is False


async def test_5_without_hold_empty_text_still_422(bearer, db, env):
    r = await bearer.post(
        "/api/v1/drafts/new",
        json={"account_id": env["tg"], "project_id": env["project"], "to": "@Petr_Ivanov"},
        headers=_auth(env["token"]),
    )
    assert r.status_code == 422
    assert _count(db, "SELECT COUNT(*) FROM main.channel") == 1


# --- F6: уборка `hold`-заготовок ----------------------------------------------------------------------------


async def test_6_cleanup_hold_drafts(bearer, db, clock, env):
    empty = (await _hold(bearer, env["token"], env, to="@Empty_Man")).json()["channel_id"]
    with_row = (await _hold(bearer, env["token"], env, to="@Row_Man")).json()["channel_id"]
    upload_id = (await _up(bearer, env["token"], with_row)).json()["upload_id"]
    assert (await _send(bearer, env["token"], with_row, files=[upload_id])).status_code == 200
    clock.advance_ms(draft_channels.IDLE_MS - 60_000)
    young = (await _hold(bearer, env["token"], env, to="@Young_Man")).json()["channel_id"]
    assert db.write_sync(lambda conn: draft_channels.cleanup_idle(conn, now_ms=_now(clock))) == 0

    clock.advance_ms(2 * 60_000)
    assert db.write_sync(lambda conn: draft_channels.cleanup_idle(conn, now_ms=_now(clock))) == 1
    with db.reader() as conn:
        archived = dict(conn.execute("SELECT id, archived FROM main.channel WHERE id IN (?, ?, ?)",
                                     (empty, with_row, young)).fetchall())  # fmt: skip
    assert archived == {empty: 1, with_row: 0, young: 0}

    # строка ушла в конечный статус мимо `settle` (архива нет) и давно: заготовку со строкой уборка не трогает
    db.write_sync(lambda conn: conn.execute("UPDATE main.outbox SET status = 'failed' WHERE channel_id = ?",
                                            (with_row,)))  # fmt: skip
    clock.advance_ms(2 * draft_channels.IDLE_MS)
    assert db.write_sync(lambda conn: draft_channels.cleanup_idle(conn, now_ms=_now(clock))) == 1  # young
    with db.reader() as conn:
        (row_archived,) = conn.execute(
            "SELECT archived FROM main.channel WHERE id = ?", (with_row,)
        ).fetchone()
    assert row_archived == 0


# --- F7: `approve` с файлами --------------------------------------------------------------------------------


async def _file_draft(bearer, env, *, text=""):
    upload_id = (await _up(bearer, env["token"], env["channel"], PNG, name="p.png")).json()["upload_id"]
    r = await _send(bearer, env["token"], env["channel"], text=text, files=[upload_id], key=upload_id)
    assert r.status_code == 200, r.text
    return r.json()["outbox_id"]


async def test_7_approve_files_with_empty_text(bearer, client, db, clock, env):
    outbox_id = await _file_draft(bearer, env)
    cookie, _ = await login_via_code(client, db, clock)
    r = await client.post(f"/api/v1/outbox/{outbox_id}/approve", json={}, headers=csrf_headers(cookie))
    assert r.status_code == 200, r.text
    assert _outbox(db, outbox_id)["status"] == "queued"

    second = await _file_draft(bearer, env, text="подпись")
    r = await client.post(
        f"/api/v1/outbox/{second}/approve", json={"text": "  "}, headers=csrf_headers(cookie)
    )
    assert r.status_code == 200, r.text
    assert _outbox(db, second)["status"] == "queued"


async def test_7_approve_edit_caption_rechecks_telegram_limit(bearer, client, db, clock, env):
    outbox_id = await _file_draft(bearer, env, text="коротко")
    cookie, _ = await login_via_code(client, db, clock)
    r = await client.post(
        f"/api/v1/outbox/{outbox_id}/approve", json={"text": "я" * 1025}, headers=csrf_headers(cookie)
    )
    assert r.status_code == 422
    assert "1024" in r.json()["detail"]
    row = _outbox(db, outbox_id)
    assert row["status"] == "pending_approval"
    assert row["text"] == "коротко"


async def test_7_approve_without_files_empty_text_refused(bearer, client, db, clock, env):
    r = await _send(bearer, env["token"], env["channel"], text="Привет")
    outbox_id = r.json()["outbox_id"]
    cookie, _ = await login_via_code(client, db, clock)
    r = await client.post(
        f"/api/v1/outbox/{outbox_id}/approve", json={"text": " "}, headers=csrf_headers(cookie)
    )
    assert r.status_code == 422
    db.write_sync(lambda conn: conn.execute("UPDATE main.outbox SET text = '' WHERE id = ?", (outbox_id,)))
    r = await client.post(f"/api/v1/outbox/{outbox_id}/approve", json={}, headers=csrf_headers(cookie))
    assert r.status_code == 409  # «текст стёрт по сроку» — только у черновика без файлов
    assert _outbox(db, outbox_id)["status"] == "pending_approval"


# --- F8: `approve-batch` ------------------------------------------------------------------------------------


async def test_8_approve_batch_skips_file_drafts(bearer, client, db, clock, env):
    with_files = await _file_draft(bearer, env, text="файл")
    plain = (await _send(bearer, env["token"], env["channel"], text="Просто текст", key="plain")).json()
    cookie, _ = await login_via_code(client, db, clock)
    r = await client.post(
        "/api/v1/outbox/approve-batch",
        json={"ids": [with_files, plain["outbox_id"]]},
        headers=csrf_headers(cookie),
    )
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert results[0] == {"id": with_files, "status": "error", "error": "файлы — подтвердите из карточки"}
    assert results[1]["status"] == "queued"
    assert _outbox(db, with_files)["status"] == "pending_approval"
    assert _outbox(db, plain["outbox_id"])["status"] == "queued"


# --- F9 + П9: `files` в `GET /outbox` -----------------------------------------------------------------------


async def test_9_outbox_list_files(bearer, db, clock, env):
    a = (await _up(bearer, env["token"], env["channel"], name="z.pdf")).json()["upload_id"]
    b = (await _up(bearer, env["token"], env["channel"], name="a.pdf")).json()["upload_id"]
    with_files = (await _send(bearer, env["token"], env["channel"], files=[a, b], key="f")).json()
    plain = (await _send(bearer, env["token"], env["channel"], text="без файлов", key="p")).json()

    r = await bearer.get("/api/v1/outbox", params={"status": "pending_approval"}, headers=_auth(env["token"]))
    assert r.status_code == 200
    items = {item["id"]: item for item in r.json()}
    files = items[with_files["outbox_id"]]["files"]
    assert [(f["name"], f["kind"], f["mime"]) for f in files] == [
        ("z.pdf", "document", "application/pdf"),
        ("a.pdf", "document", "application/pdf"),
    ]
    assert all(set(f) == {"media_id", "name", "size", "kind", "mime"} for f in files)
    assert files[0]["size"] == len(_pdf("z.pdf"))
    with db.reader() as conn:
        media_ids = [
            m for (m,) in conn.execute(
                "SELECT id FROM main.media WHERE message_id = ? ORDER BY idx", (with_files["message_id"],)
            )
        ]  # fmt: skip
    assert [f["media_id"] for f in files] == media_ids
    assert items[plain["outbox_id"]]["files"] == []
    assert "upload_id" not in json.dumps(r.json())


async def test_p9_hidden_draft_files_empty(bearer, db, clock, env):
    outbox_id = await _file_draft(bearer, env, text="x")
    with db.reader() as conn:
        assert len(queries.outbox_files(conn, "main", [outbox_id])[outbox_id]) == 1
        (message_id,) = conn.execute(
            "SELECT id FROM main.message WHERE outbox_id = ?", (outbox_id,)
        ).fetchone()
    db.write_sync(lambda conn: hiding.hide_message(conn, message_id, now_ms=_now(clock)))
    with db.reader() as conn:
        assert queries.outbox_files(conn, "main", [outbox_id]) == {}
