"""Ф11-1 (docs/architecture.md), тесты ядра 1–17: ожидание новых сообщений беседы.

Логика ожидания гоняется напрямую (`channel_wait.wait`/`poll`) с короткими таймаутами в долях секунды —
ручка принимает таймаут от 1 с, а длительность ожидания меряют монотонные часы event loop (решение 5 спеки:
не часы хаба). Через HTTP — то, что есть только у ручек: скоуп (14), отзыв сессии (15), предел ожиданий (16),
`wait_cursor` (17), 422 на плохой `after`.
"""

import asyncio
import hashlib

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import channel_wait, ingest, outbox, security
from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.feed import Notifier
from hub.core.security import Principal

from .conftest import insert_account, insert_channel, insert_message, insert_project, make_bearer_token

UI = Principal(token_id=0, kind="ui", scopes=frozenset({"read"}), project_ids=None)
ADMIN_SECRET = ADMIN_PREFIX + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHI"
SHORT = 0.2  # таймаут «тишины» в прямых тестах, с
LONG = 5.0  # таймаут, до которого тест не должен доживать (пробуждение раньше)


def _upsert(*, ext_id, text="привет", ts=1000, is_me=0, channel="c1"):
    return {
        "type": "upsert",
        "channel": {"ext_id": channel, "kind": "dm"},
        "ext_id": ext_id,
        "ts": ts,
        "is_me": is_me,
        "kind": "text",
        "text": text,
        "mentions_me": False,
    }


def _apply(db, acc, events, now_ms=10_000):
    db.write_sync(
        lambda conn: ingest.apply_batch(conn, account_id=acc, events=events, cursor=None, now_ms=now_ms)
    )


def _max_seq(db, channel_id) -> int:
    with db.reader() as conn:
        return conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM main.message WHERE channel_id = ?", (channel_id,)
        ).fetchone()[0]


def _seq_of(db, message_id) -> int:
    with db.reader() as conn:
        return conn.execute("SELECT seq FROM main.message WHERE id = ?", (message_id,)).fetchone()[0]


@pytest.fixture
async def notifier(db):
    n = Notifier()
    n.attach(asyncio.get_running_loop())
    db.on_commit(n.notify)
    return n


@pytest.fixture
def chat(db):
    """Беседа `c1` (личка Telegram) в проекте A + вторая беседа `c2` там же."""
    project = insert_project(db, name="A")
    acc = insert_account(db)
    ch = insert_channel(db, acc, project_id=project, ext_id="c1")
    other = insert_channel(db, acc, project_id=project, ext_id="c2")
    return {"project": project, "acc": acc, "ch": ch, "other": other}


def _start(db, notifier, channel_id, *, after, thread=None, skip_mine=True, timeout_s=LONG, limit=100):
    return asyncio.create_task(
        channel_wait.wait(
            db,
            notifier,
            UI,
            channel_id,
            after=after,
            thread=thread,
            skip_mine=skip_mine,
            timeout_s=timeout_s,
            limit=limit,
        )
    )


async def _settle(task) -> None:
    """Дать ожиданию дойти до `notifier.wait` и убедиться, что оно не вернулось раньше времени."""
    await asyncio.sleep(0.05)
    assert not task.done()


# --- 1–3 ------------------------------------------------------------------------------------------------


async def test_1_waking_message_already_there_returns_at_once(db, notifier, chat):
    before = _max_seq(db, chat["ch"])
    mid = insert_message(db, chat["ch"], ext_id="1", ts=1000, text="уже тут")
    loop = asyncio.get_running_loop()
    started = loop.time()
    page = await _start(db, notifier, chat["ch"], after=before)
    assert loop.time() - started < 1
    assert page.woke and not page.more and not page.from_now
    assert [m["id"] for m in page.messages] == [mid]
    assert page.messages[0]["wait_class"] == "new"
    assert page.cursor == _seq_of(db, mid)


async def test_2_incoming_via_ingest_wakes_before_timeout(db, notifier, chat):
    _apply(db, chat["acc"], [_upsert(ext_id="1", ts=1000)])
    after = _max_seq(db, chat["ch"])
    task = _start(db, notifier, chat["ch"], after=after)
    await _settle(task)
    _apply(db, chat["acc"], [_upsert(ext_id="2", ts=2000, text="ответ Миши")])
    page = await asyncio.wait_for(task, 2)
    assert page.woke
    assert [m["text"] for m in page.messages] == ["ответ Миши"]
    assert page.cursor == page.messages[0]["seq"] == _max_seq(db, chat["ch"])


async def test_3_timeout_without_changes(db, notifier, chat):
    insert_message(db, chat["ch"], ext_id="1", ts=1000)
    after = _max_seq(db, chat["ch"])
    loop = asyncio.get_running_loop()
    started = loop.time()
    page = await _start(db, notifier, chat["ch"], after=after, timeout_s=SHORT)
    assert loop.time() - started >= SHORT * 0.9
    assert page.woke is False and page.messages == [] and page.cursor == after


# --- 4–7: что будит, что нет ----------------------------------------------------------------------------


async def test_4_own_message_does_not_wake_unless_skip_mine_off(db, notifier, chat):
    after = _max_seq(db, chat["ch"])
    task = _start(db, notifier, chat["ch"], after=after, timeout_s=SHORT * 2)
    await _settle(task)
    mine = insert_message(db, chat["ch"], ext_id="1", ts=1000, is_me=1, text="это я")
    page = await task
    assert page.woke is False
    assert [(m["id"], m["wait_class"]) for m in page.messages] == [(mine, "mine")]
    assert page.cursor == _seq_of(db, mine)

    # skip_mine=False — своё будит.
    after = page.cursor
    task = _start(db, notifier, chat["ch"], after=after, skip_mine=False)
    await _settle(task)
    mine2 = insert_message(db, chat["ch"], ext_id="2", ts=2000, is_me=1, text="снова я")
    page = await asyncio.wait_for(task, 2)
    assert page.woke and [(m["id"], m["wait_class"]) for m in page.messages] == [(mine2, "new")]


async def test_5_bearer_draft_does_not_wake_approve_shows_new_text(db, notifier, chat, client):
    token = make_bearer_token(db, scopes=("read", "draft"), project_ids=[chat["project"]])
    after = _max_seq(db, chat["ch"])
    task = _start(db, notifier, chat["ch"], after=after, timeout_s=SHORT * 3)
    await _settle(task)
    r = await client.post(
        "/api/v1/messages/send",
        json={"channel_id": chat["ch"], "text": "черновик", "idempotency_key": "k1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    outbox_id = r.json()["outbox_id"]
    assert r.json()["status"] == "pending_approval"
    page = await task
    assert page.woke is False
    [draft] = page.messages
    assert draft["wait_class"] == "mine" and draft["outbox_status"] == "pending_approval"

    after = page.cursor
    db.write_sync(lambda conn: outbox.approve(conn, outbox_id, text="исправил владелец", now_ms=20_000))
    page = await _start(db, notifier, chat["ch"], after=after, timeout_s=SHORT)
    assert page.woke is False
    [draft] = page.messages
    assert draft["wait_class"] == "mine"
    assert draft["text"] == "исправил владелец" and draft["outbox_status"] == "queued"
    assert page.cursor > after


async def test_6_edit_delete_reaction_do_not_wake_and_come_as_changed(db, notifier, chat):
    _apply(
        db,
        chat["acc"],
        [_upsert(ext_id="1", ts=1000), _upsert(ext_id="2", ts=2000), _upsert(ext_id="3", ts=3000)],
    )
    after = _max_seq(db, chat["ch"])
    task = _start(db, notifier, chat["ch"], after=after, timeout_s=SHORT * 3)
    await _settle(task)
    _apply(db, chat["acc"], [_upsert(ext_id="1", ts=1000, text="правка")])
    _apply(db, chat["acc"], [{"type": "delete", "ext_ids": ["2"], "channel": {"ext_id": "c1", "kind": "dm"}}])
    _apply(
        db,
        chat["acc"],
        [
            {
                "type": "reaction",
                "channel": {"ext_id": "c1", "kind": "dm"},
                "ext_id": "3",
                "reactions": [{"emoji": "👍", "count": 1}],
            }
        ],
    )
    page = await task
    assert page.woke is False
    by_ext = {m["ext_id"]: m for m in page.messages}
    assert set(by_ext) == {"1", "2", "3"}
    assert all(m["wait_class"] == "changed" for m in page.messages)
    assert by_ext["1"]["text"] == "правка" and by_ext["2"]["deleted"] and by_ext["3"]["reactions_json"]
    assert page.cursor == _max_seq(db, chat["ch"])


async def test_7_late_inserted_old_message_wakes(db, notifier, chat):
    """Догон после сна: сообщение со временем РАНЬШЕ последнего показанного, но вставленное позже."""
    _apply(db, chat["acc"], [_upsert(ext_id="20", ts=20_000, text="свежее")])
    after = _max_seq(db, chat["ch"])
    task = _start(db, notifier, chat["ch"], after=after)
    await _settle(task)
    _apply(db, chat["acc"], [_upsert(ext_id="10", ts=10_000, text="старое, пришло поздно")])
    page = await asyncio.wait_for(task, 2)
    assert page.woke
    assert [(m["text"], m["wait_class"]) for m in page.messages] == [("старое, пришло поздно", "new")]


# --- 8–9: треды -----------------------------------------------------------------------------------------


async def test_8_thread_filter(db, notifier, chat):
    parent = insert_message(db, chat["ch"], ext_id="100.1", ts=1000, ext_sort=1)
    after = _max_seq(db, chat["ch"])
    task = _start(db, notifier, chat["ch"], after=after, thread="100.1", timeout_s=SHORT * 3)
    await _settle(task)
    insert_message(db, chat["ch"], ext_id="200.1", ts=2000, ext_sort=2, text="верхний уровень")
    insert_message(db, chat["ch"], ext_id="300.1", ts=3000, ext_sort=3, thread_ext_id="999.1", text="тред Y")
    await asyncio.sleep(0.05)
    assert not task.done()
    reply = insert_message(
        db, chat["ch"], ext_id="400.1", ts=4000, ext_sort=4, thread_ext_id="100.1", text="в X"
    )
    page = await asyncio.wait_for(task, 2)
    assert page.woke
    assert [m["id"] for m in page.messages] == [reply]
    assert page.cursor == _seq_of(db, reply)
    assert parent not in [m["id"] for m in page.messages]


async def test_9_without_thread_reply_in_thread_wakes(db, notifier, chat):
    insert_message(db, chat["ch"], ext_id="100.1", ts=1000, ext_sort=1)
    after = _max_seq(db, chat["ch"])
    task = _start(db, notifier, chat["ch"], after=after)
    await _settle(task)
    reply = insert_message(
        db, chat["ch"], ext_id="400.1", ts=4000, ext_sort=4, thread_ext_id="100.1", text="в треде"
    )
    page = await asyncio.wait_for(task, 2)
    assert page.woke
    [m] = page.messages
    assert m["id"] == reply and m["thread_ext_id"] == "100.1" and m["wait_class"] == "new"


# --- 10–13 ----------------------------------------------------------------------------------------------


async def test_10_write_in_other_channel_wakes_notifier_but_not_the_wait(db, notifier, chat):
    after = _max_seq(db, chat["ch"])
    gen = notifier.gen
    loop = asyncio.get_running_loop()
    started = loop.time()
    task = _start(db, notifier, chat["ch"], after=after, timeout_s=SHORT * 2)
    await _settle(task)
    insert_message(db, chat["other"], ext_id="1", ts=1000, text="в другой беседе")
    await asyncio.sleep(0.02)
    assert notifier.gen > gen  # пробуждение было
    assert not task.done()
    page = await task
    assert loop.time() - started >= SHORT * 2 * 0.9
    assert page.woke is False and page.messages == [] and page.cursor == after


async def test_11_event_does_not_wake_hidden_not_shown_but_moves_cursor(db, notifier, chat):
    after = _max_seq(db, chat["ch"])
    task = _start(db, notifier, chat["ch"], after=after, timeout_s=SHORT * 2)
    await _settle(task)
    event = insert_message(db, chat["ch"], ext_id="1", ts=1000, kind="event", text="Миша вошёл в чат")
    hidden = insert_message(db, chat["ch"], ext_id="2", ts=2000, text="удалю у себя")
    db.write_sync(lambda conn: conn.execute("UPDATE main.message SET hidden = 1 WHERE id = ?", (hidden,)))
    page = await task
    assert page.woke is False
    assert [(m["id"], m["wait_class"]) for m in page.messages] == [(event, "event")]
    assert page.cursor == _seq_of(db, hidden)


def test_12_more_than_limit_pages_without_loss_or_dups(db, chat):
    ids = [insert_message(db, chat["ch"], ext_id=str(i), ts=1000 * i) for i in range(1, 6)]
    with db.reader() as conn:
        first = channel_wait.poll(conn, UI, chat["ch"], after=0, thread=None, skip_mine=True, limit=3)
    assert first.more and first.woke
    assert [m["id"] for m in first.messages] == ids[:3]
    assert first.cursor == _seq_of(db, ids[2])
    with db.reader() as conn:
        second = channel_wait.poll(
            conn, UI, chat["ch"], after=first.cursor, thread=None, skip_mine=True, limit=3
        )
    assert not second.more
    assert [m["id"] for m in second.messages] == ids[3:]
    assert second.cursor == _seq_of(db, ids[4])


async def test_13_no_after_means_from_now(db, notifier, chat):
    insert_message(db, chat["ch"], ext_id="1", ts=1000, text="старое")
    task = _start(db, notifier, chat["ch"], after=None)
    await _settle(task)
    new = insert_message(db, chat["ch"], ext_id="2", ts=2000, text="новое")
    page = await asyncio.wait_for(task, 2)
    assert page.woke and page.from_now
    assert [m["id"] for m in page.messages] == [new]
    assert page.after == _seq_of(db, new) - 1

    quiet = await _start(db, notifier, chat["ch"], after=None, timeout_s=SHORT)
    assert (
        quiet.from_now
        and not quiet.woke
        and quiet.messages == []
        and quiet.cursor == _max_seq(db, chat["ch"])
    )


# --- 14–17: ручки ---------------------------------------------------------------------------------------


def _bearer(token) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _token_id(db, token) -> int:
    with db.reader() as conn:
        return conn.execute(
            "SELECT id FROM main.api_token WHERE hash = ?", (hashlib.sha256(token.encode()).hexdigest(),)
        ).fetchone()[0]


async def test_14_scope_404_and_admin_sees_any_project(db, clock, client, chat):
    project_b = insert_project(db, name="B")
    ch_b = insert_channel(db, chat["acc"], project_id=project_b, ext_id="cb")
    mid = insert_message(db, ch_b, ext_id="1", ts=1000, text="в проекте B")
    insert_message(db, chat["ch"], ext_id="1", ts=1000, text="в проекте A")
    token = make_bearer_token(db, scopes=("read",), project_ids=[chat["project"]])

    r = await client.get(
        f"/api/v1/channels/{ch_b}/wait", params={"after": 0, "timeout_s": 1}, headers=_bearer(token)
    )
    assert r.status_code == 404
    r = await client.get(
        "/api/v1/channels/999999/wait", params={"after": 0, "timeout_s": 1}, headers=_bearer(token)
    )
    assert r.status_code == 404
    r = await client.get(
        f"/api/v1/channels/{chat['ch']}/wait", params={"after": 0, "timeout_s": 1}, headers=_bearer(token)
    )
    assert r.status_code == 200
    body = r.json()
    assert body["woke"] and body["channel_id"] == chat["ch"] and body["after"] == 0 and not body["from_now"]
    assert set(body) == {"channel_id", "after", "cursor", "woke", "more", "from_now", "waited_ms", "messages"}
    for bad in ("-1", "abc"):
        r = await client.get(
            f"/api/v1/channels/{chat['ch']}/wait", params={"after": bad}, headers=_bearer(token)
        )
        assert r.status_code == 422
    for bad in ("0", "121"):
        r = await client.get(
            f"/api/v1/channels/{chat['ch']}/wait", params={"timeout_s": bad}, headers=_bearer(token)
        )
        assert r.status_code == 422

    notifier = Notifier()
    db.on_commit(notifier.notify)
    internal = create_internal_app(
        db, clock=clock, notifier=notifier, admin_key_hash=hash_secret(ADMIN_SECRET)
    )
    async with AsyncClient(transport=ASGITransport(app=internal), base_url="http://hub-internal") as admin:
        r = await admin.get(
            f"/internal/v1/admin/channels/{ch_b}/wait",
            params={"after": 0, "timeout_s": 1},
            headers={"Authorization": f"Bearer {ADMIN_SECRET}"},
        )
        assert r.status_code == 200, r.text
        assert [m["id"] for m in r.json()["messages"]] == [mid]
        r = await admin.get(
            "/internal/v1/admin/channels/999999/wait",
            params={"after": 0},
            headers={"Authorization": f"Bearer {ADMIN_SECRET}"},
        )
        assert r.status_code == 404
        r = await admin.get(f"/internal/v1/admin/channels/{ch_b}/wait", params={"after": 0})
        assert r.status_code == 401


async def test_15_revoked_during_wait_401(db, client, chat):
    token = make_bearer_token(db, scopes=("read",), project_ids=[chat["project"]])
    token_id = _token_id(db, token)
    task = asyncio.create_task(
        client.get(f"/api/v1/channels/{chat['ch']}/wait", params={"timeout_s": 5}, headers=_bearer(token))
    )
    await _settle(task)
    db.write_sync(lambda conn: security.revoke_api_token(conn, token_id))
    r = await asyncio.wait_for(task, 3)
    assert r.status_code == 401


async def test_16_ninth_concurrent_wait_429(db, client, chat):
    token = make_bearer_token(db, scopes=("read",), project_ids=[chat["project"]])
    url = f"/api/v1/channels/{chat['ch']}/wait"
    waits = [
        asyncio.create_task(client.get(url, params={"timeout_s": 5}, headers=_bearer(token)))
        for _ in range(8)
    ]
    await asyncio.sleep(0.1)
    assert not any(t.done() for t in waits)
    r = await client.get(url, params={"timeout_s": 1}, headers=_bearer(token))
    assert r.status_code == 429
    # другой токен — свой счётчик
    other = make_bearer_token(db, scopes=("read",), project_ids=[chat["project"]], name="other")
    insert_message(db, chat["ch"], ext_id="1", ts=1000, text="будит всех")
    r = await client.get(url, params={"after": 0, "timeout_s": 1}, headers=_bearer(other))
    assert r.status_code == 200
    results = await asyncio.wait_for(asyncio.gather(*waits), 3)
    assert [x.status_code for x in results] == [200] * 8 and all(x.json()["woke"] for x in results)
    r = await client.get(url, params={"after": 0, "timeout_s": 1}, headers=_bearer(token))
    assert r.status_code == 200


async def test_17_wait_cursor_in_reads(db, client, chat):
    token = make_bearer_token(db, scopes=("read",), project_ids=[chat["project"]])
    insert_message(db, chat["ch"], ext_id="100.1", ts=1000, ext_sort=1)
    reply = insert_message(db, chat["ch"], ext_id="200.1", ts=2000, ext_sort=2, thread_ext_id="100.1")
    top = insert_message(db, chat["ch"], ext_id="300.1", ts=3000, ext_sort=3)
    hidden = insert_message(db, chat["ch"], ext_id="400.1", ts=4000, ext_sort=4)
    db.write_sync(lambda conn: conn.execute("UPDATE main.message SET hidden = 1 WHERE id = ?", (hidden,)))
    insert_message(db, chat["other"], ext_id="1", ts=5000)  # чужая беседа курсор не двигает

    r = await client.get(f"/api/v1/channels/{chat['ch']}/messages", headers=_bearer(token))
    assert r.status_code == 200
    assert r.json()["wait_cursor"] == _seq_of(db, hidden)
    r = await client.get(
        f"/api/v1/channels/{chat['ch']}/messages", params={"before": "3000,3,999999"}, headers=_bearer(token)
    )
    assert r.status_code == 200 and "wait_cursor" not in r.json()

    r = await client.get(f"/api/v1/channels/{chat['ch']}/threads/100.1/messages", headers=_bearer(token))
    assert r.status_code == 200
    assert r.json()["wait_cursor"] == _seq_of(db, reply) < _seq_of(db, top)
    r = await client.get(
        f"/api/v1/channels/{chat['ch']}/threads/100.1/messages",
        params={"before": "3000,3,999999"},
        headers=_bearer(token),
    )
    assert r.status_code == 200 and "wait_cursor" not in r.json()
