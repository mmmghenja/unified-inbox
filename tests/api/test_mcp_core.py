"""Ядро куска 7a (hub-mcp): всё через bearer — черновик, скоупы `draft`/`organize`, раскладка через API,
отмена своего черновика, «Неразобранное» для `organize`, токены в админском сокете. Спецификация:
docs/architecture.md.

Стиль — как test_write.py: публичное приложение через ASGITransport (фикстуры `client`/`db`/`clock`),
внутреннее — своей фикстурой с админ-ключом (как tests/internal/conftest.py).
"""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import mapping, security
from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.feed import Notifier

from .conftest import (
    csrf_headers,
    insert_account,
    insert_channel,
    insert_message,
    insert_project,
    login_via_code,
)

DAY_MS = 24 * 60 * 60 * 1000
ADMIN_SECRET = ADMIN_PREFIX + "mcp-core-admin-secret-0123456789abcdefghijk"
INTERNAL_BASE_URL = "http://hub-internal"


@pytest.fixture
async def admin_client(db, clock):
    app = create_internal_app(db, clock=clock, notifier=Notifier(), admin_key_hash=hash_secret(ADMIN_SECRET))
    headers = {"Authorization": f"Bearer {ADMIN_SECRET}"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=INTERNAL_BASE_URL, headers=headers
    ) as c:
        yield c


def _token(db, *, scopes, kind="interactive", project_ids=None, name="bearer", expires_ts=None):
    """(значение, id) — `create_api_token` отдаёт оба (П6)."""
    created = db.write_sync(
        lambda conn: security.create_api_token(
            conn, name=name, kind=kind, scopes=list(scopes), project_ids=project_ids, expires_ts=expires_ts
        )
    )
    return created.token, created.token_id


def _bearer(token: str, client_name: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    if client_name is not None:
        headers["X-Hub-Client"] = client_name
    return headers


def _trusted_channel(db, clock, acc, **kwargs):
    """Доверенный по признакам раздела 5 плана: мои сообщения есть, старше 14 суток, без переименований."""
    now_ms = int(clock() * 1000)
    return insert_channel(db, acc, my_msg_count=5, first_seen_ts=now_ms - 30 * DAY_MS, **kwargs)


def _outbox_status(db, outbox_id):
    schema = "work" if outbox_id >= 2**40 else "main"
    with db.reader() as conn:
        return conn.execute(f"SELECT status FROM {schema}.outbox WHERE id = ?", (outbox_id,)).fetchone()[0]


def _channel(db, channel_id):
    schema = "work" if channel_id >= 2**40 else "main"
    with db.reader() as conn:
        return conn.execute(
            f"SELECT project_id, map_source, map_reason, muted, archived FROM {schema}.channel WHERE id = ?",
            (channel_id,),
        ).fetchone()


def _audit_rows(db, action):
    with db.reader() as conn:
        return conn.execute(
            "SELECT actor, api_token_id, client_name, result, channel_id, outbox_id, detail_json "
            "FROM main.audit WHERE action = ? ORDER BY id",
            (action,),
        ).fetchall()


async def _send(client, token, channel_id, *, key="k1", text="черновик"):
    return await client.post(
        "/api/v1/messages/send",
        json={"channel_id": channel_id, "text": text, "idempotency_key": key},
        headers=_bearer(token),
    )


# --- Тест 1: bearer в ДОВЕРЕННЫЙ канал -> pending_approval; UI туда же -> queued -------------------------


async def test_7a_1_bearer_into_trusted_channel_is_draft_ui_is_queued(client, db, clock):
    acc = insert_account(db)
    ch = _trusted_channel(db, clock, acc)
    token, _tid = _token(db, scopes=("read", "send"))

    r = await _send(client, token, ch)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending_approval"

    cookie, _csrf = await login_via_code(client, db, clock)
    r = await client.post(
        "/api/v1/messages/send",
        json={"channel_id": ch, "text": "от владельца", "idempotency_key": "ui1"},
        headers=csrf_headers(cookie),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued"


# --- Тест 2: только `draft` -> 200 pending_approval; только `read` -> 403 ---------------------------------


async def test_7a_2_draft_scope_sends_as_draft_read_only_is_forbidden(client, db, clock):
    acc = insert_account(db)
    ch = _trusted_channel(db, clock, acc)
    draft_token, _ = _token(db, scopes=("draft",), name="d")
    read_token, _ = _token(db, scopes=("read",), name="r")

    r = await _send(client, draft_token, ch)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending_approval"

    r = await _send(client, read_token, ch, key="k2")
    assert r.status_code == 403


# --- Тест 3: create_api_token отвергает неизвестный скоуп, admin, background+organize, organize+проекты -----


@pytest.mark.parametrize(
    ("kind", "scopes", "project_ids"),
    [
        ("interactive", ["read", "delete_everything"], None),
        ("interactive", ["read", "admin"], None),
        ("background", ["read", "organize"], None),
        ("background", ["read", "send"], None),
        ("interactive", ["read", "organize"], [1]),
    ],
)
async def test_7a_3_create_api_token_rejects_bad_scopes(db, admin_client, kind, scopes, project_ids):
    with pytest.raises(ValueError):
        _token(db, scopes=scopes, kind=kind, project_ids=project_ids)

    r = await admin_client.post(
        "/internal/v1/admin/tokens",
        json={"name": "x", "kind": kind, "scopes": scopes, "project_ids": project_ids},
    )
    assert r.status_code == 422
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.api_token").fetchone()[0] == 0


async def test_7a_3_background_draft_is_allowed_and_admin_returns_token_id(db, admin_client):
    r = await admin_client.post(
        "/internal/v1/admin/tokens",
        json={"name": "duty", "kind": "background", "scopes": ["read", "draft"], "project_ids": None},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"].startswith("hub_")
    with db.reader() as conn:
        row = conn.execute(
            "SELECT name, scopes FROM main.api_token WHERE id = ?", (body["token_id"],)
        ).fetchone()
    assert row == ("duty", "read draft")


# --- Тест 4: PATCH от bearer с `organize` ------------------------------------------------------------------


async def test_7a_4_organize_moves_channel_with_audit(client, db, clock):
    p1 = insert_project(db, name="P1")
    acc = insert_account(db)
    ch = insert_channel(db, acc, ext_id="c-move")
    token, tid = _token(db, scopes=("read", "organize"))

    r = await client.patch(
        f"/api/v1/channels/{ch}", json={"project_id": p1}, headers=_bearer(token, "mcp:all")
    )
    assert r.status_code == 200, r.text
    assert _channel(db, ch)[:3] == (p1, "manual", "manual")
    rows = _audit_rows(db, "channel_update")
    assert len(rows) == 1
    actor, api_token_id, client_name, result, channel_id, _outbox_id, detail = rows[0]
    assert (actor, api_token_id, client_name, result, channel_id) == ("interactive", tid, "mcp:all", "ok", ch)
    assert json.loads(detail) == {"project_id": p1}

    r = await client.patch(f"/api/v1/channels/{ch}", json={"project_id": None}, headers=_bearer(token))
    assert r.status_code == 200, r.text
    assert _channel(db, ch)[0] is None


async def test_7a_4_organize_to_rules_recomputes(client, db, clock):
    p1 = insert_project(db, name="P1")
    p2 = insert_project(db, name="P2")
    acc = insert_account(db)
    ch = insert_channel(db, acc, ext_id="c-rules")
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.channel SET map_source = 'auto' WHERE id = ?", (ch,))
    )
    now_ms = int(clock() * 1000)
    db.write_sync(
        lambda conn: mapping.add_rule(
            conn,
            account_id=acc,
            project_id=p2,
            match={"channel_ext_id": "c-rules"},
            priority=None,
            now_ms=now_ms,
        )
    )
    assert _channel(db, ch)[0] == p2
    token, _ = _token(db, scopes=("read", "organize"))

    r = await client.patch(f"/api/v1/channels/{ch}", json={"project_id": p1}, headers=_bearer(token))
    assert r.status_code == 200, r.text
    assert _channel(db, ch)[:2] == (p1, "manual")

    r = await client.patch(f"/api/v1/channels/{ch}", json={"map_source": "auto"}, headers=_bearer(token))
    assert r.status_code == 200, r.text
    project_id, map_source, map_reason, _m, _a = _channel(db, ch)
    assert (project_id, map_source) == (p2, "auto")
    assert map_reason.startswith("правило #")


@pytest.mark.parametrize("body", [{"muted": True}, {"archived": True}, {"project_id": None, "muted": None}])
async def test_7a_4_organize_cannot_touch_muted_or_archived(client, db, body):
    p1 = insert_project(db, name="P1")
    acc = insert_account(db)
    ch = insert_channel(db, acc, project_id=p1)
    before = _channel(db, ch)
    token, _ = _token(db, scopes=("read", "organize"))

    r = await client.patch(f"/api/v1/channels/{ch}", json=body, headers=_bearer(token))
    assert r.status_code == 403
    assert _channel(db, ch) == before
    assert _audit_rows(db, "channel_update") == []


async def test_7a_4_bearer_without_organize_is_denied_with_audit(client, db):
    p1 = insert_project(db, name="P1")
    acc = insert_account(db)
    ch = insert_channel(db, acc)
    token, tid = _token(db, scopes=("read", "draft"))

    r = await client.patch(f"/api/v1/channels/{ch}", json={"project_id": p1}, headers=_bearer(token))
    assert r.status_code == 403
    assert _channel(db, ch)[0] is None
    denied = _audit_rows(db, "ui_only_denied")
    assert len(denied) == 1 and denied[0][1] == tid


async def test_7a_4_organize_to_unknown_project_is_404(client, db):
    acc = insert_account(db)
    ch = insert_channel(db, acc)
    token, _ = _token(db, scopes=("read", "organize"))
    r = await client.patch(f"/api/v1/channels/{ch}", json={"project_id": 999}, headers=_bearer(token))
    assert r.status_code == 404
    assert _channel(db, ch)[0] is None


# --- Тест 5 (+П1): отмена своего черновика ------------------------------------------------------------------


async def test_7a_5_cancel_own_pending_draft(client, db, clock):
    acc = insert_account(db)
    ch = _trusted_channel(db, clock, acc)
    token, _ = _token(db, scopes=("read", "draft"))
    outbox_id = (await _send(client, token, ch)).json()["outbox_id"]

    r = await client.post(f"/api/v1/outbox/{outbox_id}/cancel", headers=_bearer(token))
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "cancelled"}
    assert _outbox_status(db, outbox_id) == "cancelled"


async def test_7a_5_cancel_foreign_draft_is_403_with_audit(client, db, clock):
    acc = insert_account(db)
    ch = _trusted_channel(db, clock, acc)
    owner, _ = _token(db, scopes=("read", "draft"), name="owner")
    other, other_id = _token(db, scopes=("read", "draft", "send"), name="other")
    outbox_id = (await _send(client, owner, ch)).json()["outbox_id"]

    r = await client.post(f"/api/v1/outbox/{outbox_id}/cancel", headers=_bearer(other))
    assert r.status_code == 403
    assert _outbox_status(db, outbox_id) == "pending_approval"
    assert _audit_rows(db, "outbox_cancel") == []
    denied = _audit_rows(db, "ui_only_denied")
    assert len(denied) == 1
    assert denied[0][1] == other_id
    assert json.loads(denied[0][6])["path"] == f"/api/v1/outbox/{outbox_id}/cancel"


async def test_7a_5_cancel_own_draft_approved_by_ui_is_403(client, db, clock):
    acc = insert_account(db)
    ch = _trusted_channel(db, clock, acc)
    token, tid = _token(db, scopes=("read", "draft"))
    outbox_id = (await _send(client, token, ch)).json()["outbox_id"]

    cookie, _csrf = await login_via_code(client, db, clock)
    r = await client.post(f"/api/v1/outbox/{outbox_id}/approve", json={}, headers=csrf_headers(cookie))
    assert r.status_code == 200, r.text
    assert _outbox_status(db, outbox_id) == "queued"
    client.cookies.clear()

    r = await client.post(f"/api/v1/outbox/{outbox_id}/cancel", headers=_bearer(token))
    assert r.status_code == 403
    assert _outbox_status(db, outbox_id) == "queued"
    denied = _audit_rows(db, "ui_only_denied")
    assert len(denied) == 1 and denied[0][1] == tid


async def test_7a_5_cancel_invisible_draft_is_404(client, db, clock):
    p1 = insert_project(db, name="P1")
    p2 = insert_project(db, name="P2")
    acc = insert_account(db)
    ch2 = _trusted_channel(db, clock, acc, project_id=p2)
    owner_all, _ = _token(db, scopes=("read", "draft"), name="all")
    scoped, _ = _token(db, scopes=("read", "draft"), project_ids=[p1], name="p1")
    outbox_id = (await _send(client, owner_all, ch2)).json()["outbox_id"]

    r = await client.post(f"/api/v1/outbox/{outbox_id}/cancel", headers=_bearer(scoped))
    assert r.status_code == 404
    assert _outbox_status(db, outbox_id) == "pending_approval"


async def test_7a_5_cancel_by_read_only_bearer_is_403(client, db, clock):
    acc = insert_account(db)
    ch = _trusted_channel(db, clock, acc)
    drafter, _ = _token(db, scopes=("read", "draft"), name="d")
    reader, _ = _token(db, scopes=("read",), name="r")
    outbox_id = (await _send(client, drafter, ch)).json()["outbox_id"]
    r = await client.post(f"/api/v1/outbox/{outbox_id}/cancel", headers=_bearer(reader))
    assert r.status_code == 403
    assert _outbox_status(db, outbox_id) == "pending_approval"


# --- Тест 6 (+П8): псевдо-проекты -1 / -2 ----------------------------------------------------------------


async def _pseudo_request(client, headers, project_id, endpoint):
    if endpoint == "projects":
        r = await client.get("/api/v1/projects", headers=headers)
        assert r.status_code == 200
        ids = [p["id"] for p in r.json()]
        return 200 if project_id in ids else 404, r
    if endpoint == "channels":
        r = await client.get(f"/api/v1/projects/{project_id}/channels", headers=headers)
    elif endpoint == "inbox":
        r = await client.get("/api/v1/inbox", params={"project": project_id}, headers=headers)
    elif endpoint == "search":
        r = await client.get("/api/v1/search", params={"q": "секрет", "project": project_id}, headers=headers)
    else:
        r = await client.post(
            "/api/v1/channels/resolve", json={"query": "Неразоб", "project": project_id}, headers=headers
        )
    return r.status_code, r


@pytest.mark.parametrize("endpoint", ["projects", "channels", "inbox", "search", "resolve"])
async def test_7a_6_unsorted_visible_only_to_organize(client, db, endpoint):
    p1 = insert_project(db, name="P1")
    acc = insert_account(db)
    unsorted_ch = insert_channel(db, acc, ext_id="u1", title="Неразобранный чат", last_msg_ts=5000)
    insert_message(db, unsorted_ch, ext_id="1", ts=5000, text="секрет неразобранного")

    organize, _ = _token(db, scopes=("read", "organize"), name="org")
    all_no_org, _ = _token(db, scopes=("read", "draft"), name="all")
    scoped, _ = _token(db, scopes=("read", "draft"), project_ids=[p1], name="p1")

    status, r = await _pseudo_request(client, _bearer(organize), -1, endpoint)
    assert status == 200, r.text
    body = r.json()
    if endpoint == "projects":
        assert [p["unread"] for p in body if p["id"] == -1] == [1]
    elif endpoint == "search":
        assert [m["channel_id"] for m in body] == [unsorted_ch]
    else:
        assert [c["id"] for c in body] == [unsorted_ch]

    for token in (all_no_org, scoped):
        status, _r = await _pseudo_request(client, _bearer(token), -1, endpoint)
        assert status == 404

    for token in (organize, all_no_org, scoped):
        status, _r = await _pseudo_request(client, _bearer(token), -2, endpoint)
        assert status == 404


async def test_7a_6_ui_still_sees_both_pseudo_projects(client, db, clock):
    cookie, _csrf = await login_via_code(client, db, clock)
    r = await client.get("/api/v1/projects")
    assert [p["id"] for p in r.json()] == [-1, -2]
    assert (await client.get("/api/v1/projects/-2/channels")).status_code == 200
    assert (await client.get("/api/v1/projects/-1/channels")).status_code == 200


async def test_7a_6_last_error_not_given_to_organize(client, db):
    acc = insert_account(db)
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.account SET last_error = 'секретный текст' WHERE id = ?", (acc,)
        )
    )
    token, _ = _token(db, scopes=("read", "organize"))
    r = await client.get("/api/v1/status", headers=_bearer(token))
    assert r.status_code == 200
    assert [a["last_error"] for a in r.json()["accounts"]] == [None]


# --- Тест 7: /admin/tokens — список и отзыв -------------------------------------------------------------


async def test_7a_7_admin_tokens_list_and_revoke(client, db, clock, admin_client):
    await login_via_code(client, db, clock)  # UI-сессия в списке появиться не должна
    client.cookies.clear()
    r = await admin_client.post(
        "/internal/v1/admin/tokens",
        json={"name": "mcp:all", "kind": "interactive", "scopes": ["read", "draft", "organize"]},
    )
    token, token_id = r.json()["token"], r.json()["token_id"]
    _other, _ = _token(db, scopes=("read",), project_ids=[1, 2], name="p")

    r = await admin_client.get("/internal/v1/admin/tokens")
    assert r.status_code == 200
    tokens = r.json()
    assert [t["name"] for t in tokens] == ["mcp:all", "p"]
    assert tokens[0] == {
        "id": token_id,
        "name": "mcp:all",
        "kind": "interactive",
        "scopes": ["read", "draft", "organize"],
        "project_ids": None,
        "expires_ts": None,
        "revoked": False,
    }
    assert tokens[1]["project_ids"] == [1, 2]
    assert "hash" not in json.dumps(tokens)

    assert (await client.get("/api/v1/projects", headers=_bearer(token))).status_code == 200
    r = await admin_client.post(f"/internal/v1/admin/tokens/{token_id}/revoke")
    assert r.status_code == 200
    assert (await client.get("/api/v1/projects", headers=_bearer(token))).status_code == 401
    assert (await admin_client.get("/internal/v1/admin/tokens")).json()[0]["revoked"] is True


async def test_7a_7_admin_revoke_does_not_touch_ui_sessions(client, db, clock, admin_client):
    await login_via_code(client, db, clock)
    with db.reader() as conn:
        ui_id = conn.execute("SELECT id FROM main.api_token WHERE kind = 'ui'").fetchone()[0]
    r = await admin_client.post(f"/internal/v1/admin/tokens/{ui_id}/revoke")
    assert r.status_code == 404
    assert (await admin_client.post("/internal/v1/admin/tokens/999/revoke")).status_code == 404
    assert (await client.get("/api/v1/projects")).status_code == 200


# --- П4: перенос канала агентом не расширяет видимость проектных токенов ----------------------------------


async def _setup_p4(db, clock, *, with_scoped=True):
    p1 = insert_project(db, name="P1")
    p2 = insert_project(db, name="P2")
    acc = insert_account(db)
    ch = insert_channel(db, acc, project_id=p1, ext_id="c-p4")
    organize, _ = _token(db, scopes=("read", "organize"), name="mcp:all")
    if with_scoped:
        _token(db, scopes=("read", "draft"), project_ids=[p2], name="mcp:personal")
    return p1, p2, acc, ch, organize


async def test_7a_p4_move_into_scoped_project_is_409_with_audit(client, db, clock):
    p1, p2, _acc, ch, organize = await _setup_p4(db, clock)

    r = await client.patch(f"/api/v1/channels/{ch}", json={"project_id": p2}, headers=_bearer(organize))
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "mcp:personal" in detail
    assert f"hub channel move {ch} --project {p2}" in detail
    assert _channel(db, ch)[0] == p1
    rows = _audit_rows(db, "channel_update")
    assert len(rows) == 1
    assert rows[0][3] == "denied_widens_scope"
    assert json.loads(rows[0][6])["widens_scope_tokens"] == ["mcp:personal"]


async def test_7a_p4_narrowing_and_unrelated_moves_pass(client, db, clock):
    p1, p2, acc, _ch, organize = await _setup_p4(db, clock)
    ch2 = insert_channel(db, acc, project_id=p2, ext_id="c-in-p2")
    r = await client.patch(f"/api/v1/channels/{ch2}", json={"project_id": p1}, headers=_bearer(organize))
    assert r.status_code == 200, r.text  # сужение: из P2 в P1
    assert _channel(db, ch2)[0] == p1


async def test_7a_p4_move_from_unsorted_into_scoped_is_409(client, db, clock):
    _p1, p2, acc, _ch, organize = await _setup_p4(db, clock)
    unsorted = insert_channel(db, acc, ext_id="c-unsorted")
    r = await client.patch(f"/api/v1/channels/{unsorted}", json={"project_id": p2}, headers=_bearer(organize))
    assert r.status_code == 409
    assert _channel(db, unsorted)[0] is None


async def test_7a_p4_to_rules_that_would_widen_is_409(client, db, clock):
    p1, p2, acc, ch, organize = await _setup_p4(db, clock)
    now_ms = int(clock() * 1000)
    db.write_sync(
        lambda conn: mapping.add_rule(
            conn,
            account_id=acc,
            project_id=p2,
            match={"channel_ext_id": "c-p4"},
            priority=None,
            now_ms=now_ms,
        )
    )
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.channel SET map_source = 'manual' WHERE id = ?", (ch,))
    )
    r = await client.patch(f"/api/v1/channels/{ch}", json={"map_source": "auto"}, headers=_bearer(organize))
    assert r.status_code == 409
    assert "--rules" in r.json()["detail"]
    assert _channel(db, ch)[:2] == (p1, "manual")


async def test_7a_p4_without_scoped_tokens_everything_passes(client, db, clock):
    _p1, p2, acc, ch, organize = await _setup_p4(db, clock, with_scoped=False)
    unsorted = insert_channel(db, acc, ext_id="c-unsorted")
    r = await client.patch(f"/api/v1/channels/{ch}", json={"project_id": p2}, headers=_bearer(organize))
    assert r.status_code == 200, r.text
    r = await client.patch(f"/api/v1/channels/{unsorted}", json={"project_id": p2}, headers=_bearer(organize))
    assert r.status_code == 200, r.text


async def test_7a_p4_revoked_or_expired_scoped_tokens_do_not_block(client, db, clock):
    _p1, p2, _acc, ch, organize = await _setup_p4(db, clock, with_scoped=False)
    now_ms = int(clock() * 1000)
    _t, revoked_id = _token(db, scopes=("read",), project_ids=[p2], name="old")
    db.write_sync(lambda conn: security.revoke_api_token(conn, revoked_id))
    _token(db, scopes=("read",), project_ids=[p2], name="expired", expires_ts=now_ms - 1)
    r = await client.patch(f"/api/v1/channels/{ch}", json={"project_id": p2}, headers=_bearer(organize))
    assert r.status_code == 200, r.text


async def test_7a_p4_ui_and_admin_cli_are_not_limited(client, db, clock, admin_client):
    p1, p2, _acc, ch, _organize = await _setup_p4(db, clock)
    r = await admin_client.patch(f"/internal/v1/admin/channels/{ch}", json={"project_id": p2})
    assert r.status_code == 200, r.text
    assert r.json()["project_id"] == p2
    rows = _audit_rows(db, "channel_update")
    assert rows[-1][0] == "admin" and rows[-1][3] == "ok"

    cookie, _csrf = await login_via_code(client, db, clock)
    r = await client.patch(f"/api/v1/channels/{ch}", json={"project_id": p1}, headers=csrf_headers(cookie))
    assert r.status_code == 200
    r = await client.patch(f"/api/v1/channels/{ch}", json={"project_id": p2}, headers=csrf_headers(cookie))
    assert r.status_code == 200
    assert _channel(db, ch)[0] == p2


async def test_7a_p4_admin_channel_move_unsorted_and_rules(db, clock, admin_client):
    p1 = insert_project(db, name="P1")
    acc = insert_account(db)
    ch = insert_channel(db, acc, project_id=p1, ext_id="c-a")
    r = await admin_client.patch(f"/internal/v1/admin/channels/{ch}", json={"project_id": None})
    assert r.status_code == 200 and r.json()["project_id"] is None
    r = await admin_client.patch(f"/internal/v1/admin/channels/{ch}", json={"map_source": "auto"})
    assert r.status_code == 200 and r.json()["map_reason"] == "нет правил"
    assert (
        await admin_client.patch("/internal/v1/admin/channels/999", json={"project_id": None})
    ).status_code == 404
    assert (await admin_client.get(f"/internal/v1/admin/channels/{ch}")).json()["id"] == ch


# --- П2: GET /channels/{id}, отправитель в /search -----------------------------------------------------


async def test_7a_p2_get_channel_fields_and_visibility(client, db):
    p1 = insert_project(db, name="P1")
    p2 = insert_project(db, name="P2")
    acc = insert_account(db, source="slack")
    ch = insert_channel(
        db,
        acc,
        project_id=p1,
        ext_id="C1",
        kind="channel",
        title="general",
        my_msg_count=3,
        first_seen_ts=77,
        map_reason="manual",
    )
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET title_history_json = ? WHERE id = ?",
            (json.dumps([{"title": "old", "ts": 5}]), ch),
        )
    )
    insert_message(db, ch, ext_id="1", ts=10, text="привет")
    token, _ = _token(db, scopes=("read",))
    scoped, _ = _token(db, scopes=("read",), project_ids=[p2], name="p2")

    r = await client.get(f"/api/v1/channels/{ch}", headers=_bearer(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == ch and body["source"] == "slack" and body["account_id"] == acc
    assert body["title"] == "general" and body["kind"] == "channel" and body["project_id"] == p1
    assert body["my_msg_count"] == 3 and body["first_seen_ts"] == 77 and body["map_reason"] == "manual"
    assert body["title_history"] == [{"title": "old", "ts": 5}]
    assert body["unread"] == 1

    assert (await client.get(f"/api/v1/channels/{ch}", headers=_bearer(scoped))).status_code == 404
    assert (await client.get("/api/v1/channels/999", headers=_bearer(token))).status_code == 404


async def test_7a_p2_search_returns_sender(client, db):
    acc = insert_account(db)
    ch = insert_channel(db, acc)
    contact_id = db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.contact (account_id, ext_id, name, handle) VALUES (?, 'u1', ?, 'petya')",
                (acc, "Петя"),
            ).lastrowid
        )
    )
    insert_message(db, ch, ext_id="1", ts=10, text="релиз в пятницу", sender_contact_id=contact_id)
    insert_message(db, ch, ext_id="2", ts=20, text="релиз без автора")
    token, _ = _token(db, scopes=("read",))
    r = await client.get("/api/v1/search", params={"q": "релиз"}, headers=_bearer(token))
    assert r.status_code == 200
    by_text = {m["text"]: m["sender"] for m in r.json()}
    assert by_text == {"релиз в пятницу": {"name": "Петя", "handle": "petya"}, "релиз без автора": None}


# --- П9: `own` в /outbox и /outbox/{id} --------------------------------------------------------------------


async def test_7a_p9_outbox_own_flag(client, db, clock):
    acc = insert_account(db)
    ch = _trusted_channel(db, clock, acc)
    mine, _ = _token(db, scopes=("read", "draft"), name="mine")
    theirs, _ = _token(db, scopes=("read", "draft"), name="theirs")
    my_id = (await _send(client, mine, ch, key="a")).json()["outbox_id"]
    their_id = (await _send(client, theirs, ch, key="b", text="другой")).json()["outbox_id"]

    r = await client.get("/api/v1/outbox", params={"status": "pending_approval"}, headers=_bearer(mine))
    assert {o["id"]: o["own"] for o in r.json()} == {my_id: True, their_id: False}
    assert (await client.get(f"/api/v1/outbox/{my_id}", headers=_bearer(mine))).json()["own"] is True
    assert (await client.get(f"/api/v1/outbox/{their_id}", headers=_bearer(mine))).json()["own"] is False
