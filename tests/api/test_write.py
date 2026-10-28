"""Тесты пишущих ручек `/api/v1` (этап 4b). Спецификация: docs/architecture.md,14,18,19.

Стиль — как в test_security.py: httpx.AsyncClient + ASGITransport (фикстуры `app`/`client`/`db`/`clock`),
помощники из .conftest.
"""

import hashlib

from hub.core import outbox

from .conftest import (
    csrf_headers,
    insert_account,
    insert_channel,
    insert_message,
    insert_project,
    login_via_code,
    make_bearer_token,
)


def _enqueue(
    db, channel_id, *, api_token_id, text="черновик", origin="ui", idempotency_key="k1", now_ms=1000
) -> int:
    return db.write_sync(
        lambda conn: outbox.enqueue(
            conn,
            channel_id=channel_id,
            text=text,
            origin=origin,
            api_token_id=api_token_id,
            idempotency_key=idempotency_key,
            now_ms=now_ms,
        )
    ).outbox_id


def _outbox_row(db, outbox_id):
    schema = "work" if outbox_id >= 2**40 else "main"
    with db.reader() as conn:
        return conn.execute(f"SELECT status, text FROM {schema}.outbox WHERE id = ?", (outbox_id,)).fetchone()


def _channel_row(db, channel_id):
    schema = "work" if channel_id >= 2**40 else "main"
    with db.reader() as conn:
        return conn.execute(
            f"SELECT last_read_seq, project_id, muted, archived FROM {schema}.channel WHERE id = ?",
            (channel_id,),
        ).fetchone()


# --- Тест 12 --------------------------------------------------------------------------------------------
# send под cookie -> origin='ui', queued; под bearer в недоверенный канал -> pending_approval; без скоупа
# send -> 403; в канал чужого проекта -> 404; повтор ключа -> тот же outbox_id, 200; конфликт ключа -> 409.


async def test_send_under_cookie_is_queued_with_origin_ui(client, db, clock):
    cookie, _csrf = await login_via_code(client, db, clock)
    acc = insert_account(db)
    ch = insert_channel(db, acc, my_msg_count=5, first_seen_ts=0)

    r = await client.post(
        "/api/v1/messages/send",
        json={"channel_id": ch, "text": "привет", "idempotency_key": "ui-1"},
        headers=csrf_headers(cookie),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["created"] is True
    row = _outbox_row(db, body["outbox_id"])
    assert row[0] == "queued"
    with db.reader() as conn:
        origin_row = conn.execute(
            "SELECT origin FROM main.outbox WHERE id = ?", (body["outbox_id"],)
        ).fetchone()
    assert origin_row[0] == "ui"


async def test_send_under_bearer_into_untrusted_channel_is_pending_approval(client, db, clock):
    token = make_bearer_token(db, kind="interactive", scopes=("read", "send"))
    acc = insert_account(db)
    ch = insert_channel(db, acc, my_msg_count=0)  # недоверенный: my_msg_count=0

    r = await client.post(
        "/api/v1/messages/send",
        json={"channel_id": ch, "text": "привет", "idempotency_key": "api-1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending_approval"
    with db.reader() as conn:
        origin_row = conn.execute(
            "SELECT origin FROM main.outbox WHERE id = ?", (body["outbox_id"],)
        ).fetchone()
    assert origin_row[0] == "api"


async def test_send_without_send_scope_is_403(client, db):
    token = make_bearer_token(db, kind="interactive", scopes=("read",))
    acc = insert_account(db)
    ch = insert_channel(db, acc)

    r = await client.post(
        "/api/v1/messages/send",
        json={"channel_id": ch, "text": "привет", "idempotency_key": "no-scope"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


async def test_send_into_foreign_project_channel_is_404(client, db):
    project_a = insert_project(db, name="A")
    project_b = insert_project(db, name="B")
    acc = insert_account(db)
    ch_b = insert_channel(db, acc, project_id=project_b, my_msg_count=5, first_seen_ts=0)

    token = make_bearer_token(db, kind="interactive", scopes=("read", "send"), project_ids=[project_a])
    r = await client.post(
        "/api/v1/messages/send",
        json={"channel_id": ch_b, "text": "привет", "idempotency_key": "foreign-1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404


async def test_send_idempotency_key_repeat_returns_same_outbox_id(client, db, clock):
    cookie, _csrf = await login_via_code(client, db, clock)
    acc = insert_account(db)
    ch = insert_channel(db, acc, my_msg_count=5, first_seen_ts=0)

    body_json = {"channel_id": ch, "text": "один и тот же текст", "idempotency_key": "repeat-1"}
    r1 = await client.post("/api/v1/messages/send", json=body_json, headers=csrf_headers(cookie))
    assert r1.status_code == 200
    r2 = await client.post("/api/v1/messages/send", json=body_json, headers=csrf_headers(cookie))
    assert r2.status_code == 200
    assert r1.json()["outbox_id"] == r2.json()["outbox_id"]
    assert r2.json()["created"] is False


async def test_send_idempotency_key_conflict_is_409(client, db, clock):
    cookie, _csrf = await login_via_code(client, db, clock)
    acc = insert_account(db)
    ch = insert_channel(db, acc, my_msg_count=5, first_seen_ts=0)

    r1 = await client.post(
        "/api/v1/messages/send",
        json={"channel_id": ch, "text": "текст один", "idempotency_key": "conflict-1"},
        headers=csrf_headers(cookie),
    )
    assert r1.status_code == 200

    r2 = await client.post(
        "/api/v1/messages/send",
        json={"channel_id": ch, "text": "другой текст", "idempotency_key": "conflict-1"},
        headers=csrf_headers(cookie),
    )
    assert r2.status_code == 409


async def test_send_without_idempotency_key_is_422(client, db, clock):
    cookie, _csrf = await login_via_code(client, db, clock)
    acc = insert_account(db)
    ch = insert_channel(db, acc, my_msg_count=5, first_seen_ts=0)

    r = await client.post(
        "/api/v1/messages/send", json={"channel_id": ch, "text": "привет"}, headers=csrf_headers(cookie)
    )
    assert r.status_code == 422


# --- Тест 13 --------------------------------------------------------------------------------------------
# bearer на approve, cancel, resolve-unknown, read, PATCH channel -> 403, состояние не изменилось.


async def test_bearer_forbidden_on_ui_only_routes(client, db):
    # Спека 7a (A2, П9): скоуп `admin` bearer-у больше не выдаётся вовсе — самый широкий bearer теперь
    # «read send draft organize». Он по-прежнему не может ни подтвердить/разобрать/отметить, ни отменить
    # подтверждённое (`queued`), ни тронуть `muted` — UI-only остаются UI-only при любых скоупах.
    token = make_bearer_token(db, kind="interactive", scopes=("read", "send", "draft", "organize"))
    headers = {"Authorization": f"Bearer {token}"}

    acc = insert_account(db)
    ch = insert_channel(db, acc)
    msg_id = insert_message(db, ch, ext_id="m1", ts=1000, is_me=0)
    before_channel = _channel_row(db, ch)

    outbox_id = _enqueue(db, ch, api_token_id=_bearer_token_id(db, token), idempotency_key="ka")
    before_outbox = _outbox_row(db, outbox_id)

    r = await client.post(f"/api/v1/outbox/{outbox_id}/approve", json={}, headers=headers)
    assert r.status_code == 403
    r = await client.post(f"/api/v1/outbox/{outbox_id}/cancel", headers=headers)
    assert r.status_code == 403
    r = await client.post(
        f"/api/v1/outbox/{outbox_id}/resolve-unknown", json={"decision": "sent"}, headers=headers
    )
    assert r.status_code == 403
    r = await client.post(f"/api/v1/channels/{ch}/read", json={"up_to_message_id": msg_id}, headers=headers)
    assert r.status_code == 403
    r = await client.patch(f"/api/v1/channels/{ch}", json={"muted": True}, headers=headers)
    assert r.status_code == 403

    assert _outbox_row(db, outbox_id) == before_outbox
    assert _channel_row(db, ch) == before_channel


def _bearer_token_id(db, token) -> int:
    with db.reader() as conn:
        return conn.execute(
            "SELECT id FROM main.api_token WHERE hash = ?", (hashlib.sha256(token.encode()).hexdigest(),)
        ).fetchone()[0]


# --- Тест 14 --------------------------------------------------------------------------------------------
# approve {text} меняет текст; cancel из sending -> 409.


async def test_approve_with_text_changes_text(client, db, clock):
    cookie, _csrf = await login_via_code(client, db, clock)
    token_id = _cookie_token_id(db, cookie)
    acc = insert_account(db)
    ch = insert_channel(db, acc, my_msg_count=0)  # недоверенный, но origin='ui' -> queued сразу
    outbox_id = _enqueue(db, ch, api_token_id=token_id, origin="api", idempotency_key="appr-1")
    assert _outbox_row(db, outbox_id)[0] == "pending_approval"

    r = await client.post(
        f"/api/v1/outbox/{outbox_id}/approve",
        json={"text": "новый текст"},
        headers=csrf_headers(cookie),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued"
    row = _outbox_row(db, outbox_id)
    assert row[0] == "queued"
    assert row[1] == "новый текст"


def _cookie_token_id(db, cookie) -> int:
    with db.reader() as conn:
        return conn.execute(
            "SELECT id FROM main.api_token WHERE hash = ?", (hashlib.sha256(cookie.encode()).hexdigest(),)
        ).fetchone()[0]


async def test_cancel_from_sending_is_409(client, db, clock):
    cookie, _csrf = await login_via_code(client, db, clock)
    token_id = _cookie_token_id(db, cookie)
    acc = insert_account(db)
    ch = insert_channel(db, acc, my_msg_count=5, first_seen_ts=0)
    outbox_id = _enqueue(db, ch, api_token_id=token_id, idempotency_key="sending-1")

    db.write_sync(lambda conn: outbox.lease(conn, account_id=acc, owner="hub-tg", now_ms=1000))
    assert _outbox_row(db, outbox_id)[0] == "sending"

    r = await client.post(f"/api/v1/outbox/{outbox_id}/cancel", headers=csrf_headers(cookie))
    assert r.status_code == 409
    assert _outbox_row(db, outbox_id)[0] == "sending"


# --- Тест 18 ---------------------------------------------------------------------------------------------
# resolve: возвращает несколько кандидатов с одинаковым названием из разных проектов без «лучшего»; под
# токеном проекта A -- только кандидаты A.


async def test_resolve_returns_all_candidates_without_ranking(client, db, clock):
    cookie, _csrf = await login_via_code(client, db, clock)
    project_a = insert_project(db, name="A")
    project_b = insert_project(db, name="B")
    acc = insert_account(db)
    ch_a = insert_channel(db, acc, project_id=project_a, ext_id="ra", title="Проект Игрек", my_msg_count=3)
    ch_b = insert_channel(db, acc, project_id=project_b, ext_id="rb", title="проект игрек", my_msg_count=1)
    insert_message(db, ch_a, ext_id="ma1", ts=100, text="сообщение A", is_me=0)

    r = await client.post("/api/v1/channels/resolve", json={"query": "игрек"}, headers=csrf_headers(cookie))
    assert r.status_code == 200, r.text
    candidates = r.json()
    ids = [c["id"] for c in candidates]
    assert ids == sorted(ids)
    assert {c["id"] for c in candidates} == {ch_a, ch_b}
    assert {c["project_id"] for c in candidates} == {project_a, project_b}
    by_id = {c["id"]: c for c in candidates}
    assert by_id[ch_a]["recent_messages"][0]["text"] == "сообщение A"
    assert by_id[ch_b]["recent_messages"] == []


async def test_resolve_scoped_token_sees_only_own_project(client, db):
    project_a = insert_project(db, name="A")
    project_b = insert_project(db, name="B")
    acc = insert_account(db)
    ch_a = insert_channel(db, acc, project_id=project_a, ext_id="sa", title="Одинтитул", my_msg_count=1)
    insert_channel(db, acc, project_id=project_b, ext_id="sb", title="Одинтитул", my_msg_count=1)

    token = make_bearer_token(db, kind="interactive", scopes=("read",), project_ids=[project_a])
    r = await client.post(
        "/api/v1/channels/resolve",
        json={"query": "одинтитул"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    candidates = r.json()
    assert {c["id"] for c in candidates} == {ch_a}


# --- Тест 19 ---------------------------------------------------------------------------------------------
# Аудит: у каждого пишущего запроса строка с request_id, совпадающим с X-Request-Id; текста сообщения нет.


async def test_audit_row_matches_x_request_id_and_has_no_text(client, db, clock):
    cookie, _csrf = await login_via_code(client, db, clock)
    token_id = _cookie_token_id(db, cookie)
    acc = insert_account(db)
    ch = insert_channel(db, acc, my_msg_count=5, first_seen_ts=0)
    msg_id = insert_message(db, ch, ext_id="am1", ts=1000, is_me=0)

    secret_text = "секретный текст сообщения хаба 12345"
    r = await client.post(
        "/api/v1/messages/send",
        json={"channel_id": ch, "text": secret_text, "idempotency_key": "audit-1"},
        headers={**csrf_headers(cookie), "X-Hub-Client": "pytest-client"},
    )
    assert r.status_code == 200
    request_id_send = r.headers["X-Request-Id"]
    outbox_id = r.json()["outbox_id"]

    r = await client.post(
        f"/api/v1/channels/{ch}/read", json={"up_to_message_id": msg_id}, headers=csrf_headers(cookie)
    )
    assert r.status_code == 200
    request_id_read = r.headers["X-Request-Id"]

    r = await client.post(f"/api/v1/outbox/{outbox_id}/cancel", headers=csrf_headers(cookie))
    assert r.status_code == 200
    request_id_cancel = r.headers["X-Request-Id"]

    request_ids = {request_id_send, request_id_read, request_id_cancel}
    assert len(request_ids) == 3

    with db.reader() as conn:
        rows = conn.execute(
            "SELECT request_id, action, client_name, api_token_id, detail_json FROM main.audit "
            "WHERE request_id IS NOT NULL ORDER BY id"
        ).fetchall()

    seen = {row[0] for row in rows}
    assert request_ids <= seen

    everything = repr(rows)
    assert secret_text not in everything

    send_row = next(row for row in rows if row[0] == request_id_send)
    assert send_row[1] == "message_send"
    assert send_row[2] == "pytest-client"
    assert send_row[3] == token_id


# PATCH канала: ручная привязка ставит map_source='manual' и поднимает channel.seq; null снимает проект.
async def test_patch_channel_assigns_project_manually(client, db, clock):
    acc = insert_account(db)
    project = insert_project(db, name="P")
    ch = insert_channel(db, acc, ext_id="c1")
    cookie, _ = await login_via_code(client, db, clock)

    def _row():
        with db.reader() as conn:
            return conn.execute(
                "SELECT project_id, map_source, muted, seq FROM main.channel WHERE id = ?", (ch,)
            ).fetchone()

    seq_before = _row()[3]
    r = await client.patch(
        f"/api/v1/channels/{ch}", json={"project_id": project, "muted": True}, headers=csrf_headers(cookie)
    )
    assert r.status_code == 200
    assert _row()[:3] == (project, "manual", 1)
    assert _row()[3] > seq_before

    r = await client.patch(f"/api/v1/channels/{ch}", json={"project_id": None}, headers=csrf_headers(cookie))
    assert r.status_code == 200
    assert _row()[0] is None

    r = await client.patch(f"/api/v1/channels/{ch}", json={"project_id": 999}, headers=csrf_headers(cookie))
    assert r.status_code == 404


# read: только вперёд; сообщение чужого канала не принимается.
async def test_read_moves_only_forward_and_checks_channel(client, db, clock):
    acc = insert_account(db)
    ch = insert_channel(db, acc, ext_id="c1")
    other = insert_channel(db, acc, ext_id="c2")
    m1 = insert_message(db, ch, ext_id="1", ts=1000, text="a")
    m2 = insert_message(db, ch, ext_id="2", ts=2000, text="b")
    foreign = insert_message(db, other, ext_id="9", ts=3000, text="x")
    cookie, _ = await login_via_code(client, db, clock)
    headers = csrf_headers(cookie)

    r = await client.post(f"/api/v1/channels/{ch}/read", json={"up_to_message_id": m2}, headers=headers)
    assert r.json() == {"changed": True}
    r = await client.post(f"/api/v1/channels/{ch}/read", json={"up_to_message_id": m1}, headers=headers)
    assert r.json() == {"changed": False}  # назад не двигается
    r = await client.post(f"/api/v1/channels/{ch}/read", json={"up_to_message_id": foreign}, headers=headers)
    assert r.status_code == 404


# resolve: короткий запрос не превращается в выгрузку всех каналов.
async def test_resolve_rejects_short_query(client, db, clock):
    acc = insert_account(db)
    insert_channel(db, acc, ext_id="c1", title="Проект")
    token = make_bearer_token(db, scopes=("read",))
    for query in ("", " ", "п"):
        r = await client.post(
            "/api/v1/channels/resolve", json={"query": query}, headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 422, query
