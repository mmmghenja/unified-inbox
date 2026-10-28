"""API тредов Slack (спека `docs/architecture.md`). Все каналы — work-аккаунт."""

from hub.core.db import next_seq

from .conftest import (
    csrf_headers,
    insert_account,
    insert_channel,
    insert_message,
    insert_thread,
    login_via_code,
)

DAY_MS = 24 * 60 * 60 * 1000


def _bump_channel_seq(db, channel_id):
    """Каналу нужен `seq > 0`, чтобы попасть в `/sync` наравне с сообщениями (модель канала растит seq сама
    при любом изменении; в этих тестах канал не создаётся через ingest)."""
    schema = "work" if channel_id >= 2**40 else "main"

    def _fn(conn):
        seq = next_seq(conn)
        conn.execute(f"UPDATE {schema}.channel SET seq = ? WHERE id = ?", (seq, channel_id))

    db.write_sync(_fn)


# 1. GET /channels/{id}/messages: только top_level, thread у родителя, thread_broadcast/purged у всех.
async def test_list_messages_only_top_level_with_thread_summary(client, db, clock):
    acc = insert_account(db, source="slack", db_name="work")
    ch = insert_channel(db, acc, ext_id="c1", db_name="work")
    insert_message(db, ch, ext_id="P1", ts=1000)
    insert_message(db, ch, ext_id="R1", ts=1001, thread_ext_id="P1")
    insert_message(db, ch, ext_id="R2", ts=1002, thread_ext_id="P1")
    insert_message(db, ch, ext_id="B1", ts=1003, thread_ext_id="P1", thread_broadcast=1)
    insert_thread(db, ch, ext_id="P1", last_reply_ts=1003, subscribed=1, first_seen_ts=500)

    await login_via_code(client, db, clock)
    r = await client.get(f"/api/v1/channels/{ch}/messages")
    assert r.status_code == 200
    body = r.json()["messages"]
    ext_ids = [m["ext_id"] for m in body]
    assert "R1" not in ext_ids
    assert "R2" not in ext_ids
    assert "P1" in ext_ids
    assert "B1" in ext_ids  # broadcast — top_level, в ленте канала тоже

    parent = next(m for m in body if m["ext_id"] == "P1")
    assert parent["thread"] is not None
    assert parent["thread"]["reply_count"] == 3  # R1, R2, B1 — все реальные ответы
    assert parent["thread"]["subscribed"] is True
    assert parent["thread_broadcast"] is False
    assert parent["purged"] is False

    broadcast = next(m for m in body if m["ext_id"] == "B1")
    assert broadcast["thread"] is None  # сводка — только у родителя
    assert broadcast["thread_broadcast"] is True


# 2. GET /channels/{id}/threads/{ext}/messages: заготовка и broadcast видны, родитель отдельным полем.
async def test_thread_messages_includes_placeholder_and_broadcast_with_parent(client, db, clock):
    acc = insert_account(db, source="slack", db_name="work")
    ch = insert_channel(db, acc, ext_id="c1", db_name="work")
    insert_message(db, ch, ext_id="P1", ts=1000)
    insert_message(db, ch, ext_id="R1", ts=1001, thread_ext_id="P1")
    insert_message(db, ch, ext_id="B1", ts=1002, thread_ext_id="P1", thread_broadcast=1)
    insert_message(db, ch, ext_id=None, ts=1003, thread_ext_id="P1", text="черновик")  # заготовка outbox

    await login_via_code(client, db, clock)
    r = await client.get(f"/api/v1/channels/{ch}/threads/P1/messages")
    assert r.status_code == 200
    body = r.json()
    assert body["parent"]["ext_id"] == "P1"
    ext_ids = [m["ext_id"] for m in body["messages"]]
    assert ext_ids == ["R1", "B1", None]  # порядок показа, по ts


# 2 (продолжение). Пагинация ленты треда при одинаковом ts — тем же курсором, что лента канала.
async def test_thread_messages_pagination_with_equal_ts(client, db, clock):
    acc = insert_account(db, source="slack", db_name="work")
    ch = insert_channel(db, acc, ext_id="c1", db_name="work")
    insert_message(db, ch, ext_id="P1", ts=1000)
    for ext_id in ("98", "99", "100", "101", "102", "103"):
        insert_message(db, ch, ext_id=ext_id, ts=5000, thread_ext_id="P1")

    await login_via_code(client, db, clock)

    pages: list[list[str | None]] = []
    before = None
    for _page in range(3):
        params: dict = {"limit": 2}
        if before is not None:
            params["before"] = before
        r = await client.get(f"/api/v1/channels/{ch}/threads/P1/messages", params=params)
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["messages"]) == 2
        assert body["parent"]["ext_id"] == "P1"  # родитель на каждой странице, вне списка
        pages.append([m["ext_id"] for m in body["messages"]])
        before = body["next_before"]

    # страницы — от новых к старым, внутри страницы порядок показа (старые сверху), как у ленты канала.
    assert pages == [["102", "103"], ["100", "101"], ["98", "99"]]
    assert before is None


# 3. Неизвестный тред при видимом канале — пустая страница, не 404; parent = null.
async def test_unknown_thread_returns_empty_page_not_404(client, db, clock):
    acc = insert_account(db, source="slack", db_name="work")
    ch = insert_channel(db, acc, ext_id="c1", db_name="work")
    insert_message(db, ch, ext_id="M1", ts=1000)

    await login_via_code(client, db, clock)
    r = await client.get(f"/api/v1/channels/{ch}/threads/UNKNOWN/messages")
    assert r.status_code == 200
    body = r.json()
    assert body["messages"] == []
    assert body["parent"] is None


# 4. Ответ без родителя — тред есть в GET .../threads?unread=1, parent = null.
async def test_reply_without_parent_listed_with_null_parent(client, db, clock):
    acc = insert_account(db, source="slack", db_name="work")
    ch = insert_channel(db, acc, ext_id="c1", db_name="work")
    insert_message(db, ch, ext_id="R1", ts=1001, thread_ext_id="ORPHAN")
    insert_thread(db, ch, ext_id="ORPHAN", last_reply_ts=1001, subscribed=1, first_seen_ts=1000)

    await login_via_code(client, db, clock)
    r = await client.get(f"/api/v1/channels/{ch}/threads", params={"unread": 1})
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["thread_ext_id"] == "ORPHAN"
    assert body[0]["parent"] is None
    assert body[0]["thread"]["unread"] == 1


# 5. Тредовая read: 200 меняет маркер, повтор — unchanged; неверный тред — 422; bearer — 403.
async def test_thread_read_route(client, db, clock):
    acc = insert_account(db, source="slack", db_name="work")
    ch = insert_channel(db, acc, ext_id="c1", db_name="work")
    insert_message(db, ch, ext_id="P1", ts=1000)
    r1_id = insert_message(db, ch, ext_id="R1", ts=1001, thread_ext_id="P1")
    other_id = insert_message(db, ch, ext_id="OTHER_TOP", ts=999)
    insert_thread(db, ch, ext_id="P1", last_reply_ts=1001, subscribed=1, first_seen_ts=500)

    cookie, csrf = await login_via_code(client, db, clock)
    r = await client.post(
        f"/api/v1/channels/{ch}/threads/P1/read",
        json={"up_to_message_id": r1_id},
        headers=csrf_headers(cookie),
    )
    assert r.status_code == 200, r.text
    assert r.json()["changed"] is True

    # сообщение из ДРУГОГО треда (не родитель, не ответ этого треда) — 422
    r_wrong = await client.post(
        f"/api/v1/channels/{ch}/threads/P1/read",
        json={"up_to_message_id": other_id},
        headers=csrf_headers(cookie),
    )
    assert r_wrong.status_code == 422


# 6. Канальная read с id не-broadcast ответа — 422; с broadcast — ок.
async def test_channel_read_rejects_non_broadcast_reply(client, db, clock):
    acc = insert_account(db, source="slack", db_name="work")
    ch = insert_channel(db, acc, ext_id="c1", db_name="work")
    insert_message(db, ch, ext_id="P1", ts=1000)
    reply_id = insert_message(db, ch, ext_id="R1", ts=1001, thread_ext_id="P1")
    broadcast_id = insert_message(db, ch, ext_id="B1", ts=1002, thread_ext_id="P1", thread_broadcast=1)

    cookie, csrf = await login_via_code(client, db, clock)
    r = await client.post(
        f"/api/v1/channels/{ch}/read", json={"up_to_message_id": reply_id}, headers=csrf_headers(cookie)
    )
    assert r.status_code == 422

    r_ok = await client.post(
        f"/api/v1/channels/{ch}/read", json={"up_to_message_id": broadcast_id}, headers=csrf_headers(cookie)
    )
    assert r_ok.status_code == 200


# 6 (продолжение). Тредовая read снимает только тредовое, канальная — только верхний уровень.
async def test_channel_read_and_thread_read_are_isolated(client, db, clock):
    project_id = db.write_sync(
        lambda conn: conn.execute("INSERT INTO main.project (name) VALUES ('P')").lastrowid
    )
    acc = insert_account(db, source="slack", db_name="work")
    ch = insert_channel(db, acc, ext_id="c1", db_name="work", project_id=project_id)
    other_top_id = insert_message(db, ch, ext_id="OTHER_TOP", ts=900)
    insert_message(db, ch, ext_id="P1", ts=1000, is_me=True)
    reply_id = insert_message(db, ch, ext_id="R1", ts=1001, thread_ext_id="P1")
    insert_thread(db, ch, ext_id="P1", last_reply_ts=1001, subscribed=1, first_seen_ts=500)

    cookie, csrf = await login_via_code(client, db, clock)

    r_channels_before = await client.get(f"/api/v1/projects/{project_id}/channels")
    before = next(c for c in r_channels_before.json() if c["id"] == ch)
    assert before["unread_top"] == 1  # OTHER_TOP
    assert before["unread_threads"] == 1  # R1

    # тредовая read снимает только R1 (unread_threads), не трогает unread_top
    r_thread_read = await client.post(
        f"/api/v1/channels/{ch}/threads/P1/read",
        json={"up_to_message_id": reply_id},
        headers=csrf_headers(cookie),
    )
    assert r_thread_read.status_code == 200, r_thread_read.text
    r_channels_mid = await client.get(f"/api/v1/projects/{project_id}/channels")
    mid = next(c for c in r_channels_mid.json() if c["id"] == ch)
    assert mid["unread_top"] == 1
    assert mid["unread_threads"] == 0

    # канальная read снимает только OTHER_TOP (unread_top)
    r_channel_read = await client.post(
        f"/api/v1/channels/{ch}/read",
        json={"up_to_message_id": other_top_id},
        headers=csrf_headers(cookie),
    )
    assert r_channel_read.status_code == 200, r_channel_read.text
    r_channels_after = await client.get(f"/api/v1/projects/{project_id}/channels")
    after = next(c for c in r_channels_after.json() if c["id"] == ch)
    assert after["unread_top"] == 0
    assert after["unread_threads"] == 0


# 7. Непрочитанное: чужой ответ в подписанном треде -> unread_threads += 1; в неподписанном -> 0; broadcast
#    считается один раз; согласованность между /projects, /channels, /inbox, /sync.
async def test_unread_consistency_across_endpoints(client, db, clock):
    now_ms = int(clock() * 1000)
    project_id = db.write_sync(
        lambda conn: conn.execute("INSERT INTO main.project (name) VALUES ('P')").lastrowid
    )
    acc = insert_account(db, source="slack", db_name="work")
    ch = insert_channel(db, acc, ext_id="c1", db_name="work", project_id=project_id)
    insert_message(db, ch, ext_id="P1", ts=now_ms - 1000)
    # чужой ответ в НЕподписанном треде — не считается вовсе
    insert_message(db, ch, ext_id="R_UNSUB", ts=now_ms - 900, thread_ext_id="P1")
    insert_thread(db, ch, ext_id="P1", last_reply_ts=now_ms - 900, subscribed=0, first_seen_ts=now_ms - 2000)

    ch2 = insert_channel(db, acc, ext_id="c2", db_name="work", project_id=project_id)
    insert_message(db, ch2, ext_id="P2", ts=now_ms - 1000)
    insert_message(db, ch2, ext_id="R_SUB", ts=now_ms - 900, thread_ext_id="P2")
    insert_message(db, ch2, ext_id="B_SUB", ts=now_ms - 800, thread_ext_id="P2", thread_broadcast=1)
    insert_thread(db, ch2, ext_id="P2", last_reply_ts=now_ms - 800, subscribed=1, first_seen_ts=now_ms - 2000)
    _bump_channel_seq(db, ch)
    _bump_channel_seq(db, ch2)

    cookie, csrf = await login_via_code(client, db, clock)

    r_channels = await client.get(f"/api/v1/projects/{project_id}/channels")
    assert r_channels.status_code == 200
    by_id = {c["id"]: c for c in r_channels.json()}
    # ch: родитель (1, не мой, top_level) непрочитан + 0 из неподписанного треда
    assert by_id[ch]["unread_top"] == 1
    assert by_id[ch]["unread_threads"] == 0
    assert by_id[ch]["unread"] == 1
    # ch2: родитель (1) + broadcast (уже top_level, в unread_top) + R_SUB (в unread_threads, не broadcast)
    assert by_id[ch2]["unread_top"] == 2  # P2 + B_SUB (broadcast — top_level)
    assert by_id[ch2]["unread_threads"] == 1  # R_SUB — подписанный тред, не broadcast
    assert by_id[ch2]["unread"] == 3

    r_projects = await client.get("/api/v1/projects")
    assert r_projects.status_code == 200
    proj = next(p for p in r_projects.json() if p["id"] == project_id)
    assert proj["unread"] == by_id[ch]["unread"] + by_id[ch2]["unread"]

    r_inbox = await client.get("/api/v1/inbox")
    assert r_inbox.status_code == 200
    inbox_by_id = {c["id"]: c for c in r_inbox.json()}
    assert inbox_by_id[ch]["unread"] == by_id[ch]["unread"]
    assert inbox_by_id[ch2]["unread"] == by_id[ch2]["unread"]

    r_sync = await client.get("/api/v1/sync", params={"since": 0})
    assert r_sync.status_code == 200
    sync_channels = {c["id"]: c for c in r_sync.json()["channels"]}
    assert sync_channels[ch]["unread"] == by_id[ch]["unread"]
    assert sync_channels[ch2]["unread"] == by_id[ch2]["unread"]


# 8. Scope: чужой проект не видит ленту треда и список тредов (404); bearer на тредовую read — 403.
async def test_scope_hides_thread_endpoints_from_other_project(client, db, clock):
    from .conftest import make_bearer_token

    p1 = db.write_sync(lambda conn: conn.execute("INSERT INTO main.project (name) VALUES ('P1')").lastrowid)
    p2 = db.write_sync(lambda conn: conn.execute("INSERT INTO main.project (name) VALUES ('P2')").lastrowid)
    acc = insert_account(db, source="slack", db_name="work")
    ch = insert_channel(db, acc, ext_id="c1", db_name="work", project_id=p1)
    insert_message(db, ch, ext_id="P1", ts=1000)
    r1_id = insert_message(db, ch, ext_id="R1", ts=1001, thread_ext_id="P1")
    insert_thread(db, ch, ext_id="P1", last_reply_ts=1001, subscribed=1, first_seen_ts=500)

    other_token = make_bearer_token(db, kind="interactive", scopes=("read", "send"), project_ids=[p2])
    headers = {"Authorization": f"Bearer {other_token}"}

    r = await client.get(f"/api/v1/channels/{ch}/threads/P1/messages", headers=headers)
    assert r.status_code == 404

    r2 = await client.get(f"/api/v1/channels/{ch}/threads", headers=headers)
    assert r2.status_code == 404

    # тот же токен, но на СВОЙ проект — bearer на тредовую read всё равно 403 (только UI-сессия)
    own_token = make_bearer_token(db, kind="interactive", scopes=("read", "send"), project_ids=[p1])
    r3 = await client.post(
        f"/api/v1/channels/{ch}/threads/P1/read",
        json={"up_to_message_id": r1_id},
        headers={"Authorization": f"Bearer {own_token}", "Origin": "http://127.0.0.1:8787"},
    )
    assert r3.status_code == 403


# 9. search несёт thread_ext_id/thread_broadcast/purged.
async def test_search_carries_thread_fields(client, db, clock):
    acc = insert_account(db, source="slack", db_name="work")
    ch = insert_channel(db, acc, ext_id="c1", db_name="work")
    insert_message(db, ch, ext_id="R1", ts=1000, thread_ext_id="P1", text="уникальныйтекст")

    await login_via_code(client, db, clock)
    r = await client.get("/api/v1/search", params={"q": "уникальныйтекст"})
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["thread_ext_id"] == "P1"
    assert body[0]["thread_broadcast"] is False
    assert body[0]["purged"] is False
