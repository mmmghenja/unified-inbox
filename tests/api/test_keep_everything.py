"""Ф8-4 «Ничего не удалять» (docs/architecture.md): ретенция снята (A, П9), удаление у
себя в хабе — сообщение и беседа (B, П1–П6), история правок (C, П7, П12), миграция 0012 (T7), медиа скрытого
(уточнение архитектора: строки и файлы уходят, `GET`/`fetch` — 404)."""

import asyncio
import hashlib
import json

import pytest

from hub.core import agent_items, hiding, media, retention, security
from hub.core import db as db_module
from hub.core import outbox as outbox_mod
from hub.core import runner as runner_mod
from hub.core.db import Db
from hub.core.ingest import apply_batch

from .conftest import (
    ORIGIN,
    csrf_headers,
    insert_account,
    insert_project,
    login_via_code,
    make_bearer_token,
)

DAY = 24 * 60 * 60 * 1000


# --- помощники ------------------------------------------------------------------------------------------


def _ev(ext_id, ts, *, text="привет", chat="c1", kind="dm", etype="upsert", **extra):
    event = {
        "type": etype,
        "channel": {"ext_id": chat, "kind": kind, "title": "Беседа"},
        "ext_id": ext_id,
        "ts": ts,
        "sender": {"ext_id": "7", "name": "Пётр"},
        "is_me": False,
        "kind": "text",
        "text": text,
        "mentions_me": False,
    }
    event.update(extra)
    return event


def _ingest(db, account_id, events, now_ms):
    result = db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=events, cursor=None, now_ms=now_ms)
    )
    assert result.dead == 0, result
    return result


def _mid(db, ext_id, *, chat="c1", schema="main"):
    with db.reader() as conn:
        return conn.execute(
            f"SELECT m.id FROM {schema}.message m JOIN {schema}.channel c ON c.id = m.channel_id "  # noqa: S608
            "WHERE c.ext_id = ? AND m.ext_id = ?",
            (chat, ext_id),
        ).fetchone()[0]


def _cid(db, chat="c1", schema="main"):
    with db.reader() as conn:
        return conn.execute(
            f"SELECT id FROM {schema}.channel WHERE ext_id = ?",  # noqa: S608
            (chat,),
        ).fetchone()[0]


def _row(db, message_id, schema="main"):
    with db.reader() as conn:
        cur = conn.execute(f"SELECT * FROM {schema}.message WHERE id = ?", (message_id,))  # noqa: S608
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, cur.fetchone(), strict=True))


def _outbox(db, outbox_id, schema="main"):
    with db.reader() as conn:
        return conn.execute(
            f"SELECT status, text FROM {schema}.outbox WHERE id = ?",  # noqa: S608
            (outbox_id,),
        ).fetchone()


def _max_seq(db):
    with db.reader() as conn:
        return conn.execute("SELECT value FROM main.seq_counter").fetchone()[0]


def _setup(db, *, source="telegram", project=True):
    account_id = insert_account(db, source=source)
    project_id = insert_project(db, name="P") if project else None
    return account_id, project_id


def _to_project(db, channel_id, project_id):
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET project_id = ?, map_source = 'manual' WHERE id = ?",
            (project_id, channel_id),
        )
    )


def _hide_message(db, message_id, now_ms=1):
    return db.write_sync(lambda conn: hiding.hide_message(conn, message_id, now_ms=now_ms))


def _hide_channel(db, channel_id, now_ms=1):
    return db.write_sync(lambda conn: hiding.hide_channel(conn, channel_id, now_ms=now_ms))


def _enqueue(db, channel_id, *, text="моё", origin="ui", now_ms):
    token_id = db.write_sync(
        lambda conn: security.create_api_token(
            conn, name="t", kind="interactive", scopes=["draft"], project_ids=None, expires_ts=None
        )
    ).token_id
    return db.write_sync(
        lambda conn: outbox_mod.enqueue(
            conn,
            channel_id=channel_id,
            text=text,
            origin=origin,
            api_token_id=token_id,
            idempotency_key=None,
            now_ms=now_ms,
        )
    )


def _lease(db, account_id, now_ms):
    return db.write_sync(lambda conn: outbox_mod.lease(conn, account_id=account_id, owner="w", now_ms=now_ms))


def _report(db, lease, outcome, now_ms, **kw):
    db.write_sync(
        lambda conn: outbox_mod.report(
            conn,
            outbox_id=lease.outbox_id,
            owner="w",
            lease_gen=lease.lease_gen,
            outcome=outcome,
            now_ms=now_ms,
            **kw,
        )
    )


def _feed_ids(page):
    return [m["id"] for m in page["messages"]]


# --- T1: ретенция выключена ------------------------------------------------------------------------------


def test_t1_retention_disabled_old_work_message_keeps_text(db):
    assert retention.RETENTION_ENABLED is False
    account_id = insert_account(db, source="slack", db_name="work")
    now = 400 * DAY
    # Опорное время источников — свежее: раньше такое сообщение ложилось бы сразу очищенным.
    _ingest(db, account_id, [_ev("new", now - 1000, chat="w1", kind="channel")], now)
    _ingest(db, account_id, [_ev("old", now - 100 * DAY, chat="w1", kind="channel", text="старое")], now)
    mid = _mid(db, "old", chat="w1", schema="work")
    assert _row(db, mid, "work")["text"] == "старое"
    assert _row(db, mid, "work")["purged"] == 0
    # повторный приём и периодический прогон (если бы его кто-то позвал по расписанию) текста не трогают
    _ingest(db, account_id, [_ev("old", now - 100 * DAY, chat="w1", kind="channel", text="старое")], now + 1)
    assert _row(db, mid, "work")["text"] == "старое"


async def test_t1_runner_does_not_start_retention_loop(tmp_path, monkeypatch):
    """Периодический прогон не запускается; с включённой константой — запускается (зубы теста)."""
    calls: list[int] = []

    async def spy_loop(*_args, **_kwargs):
        calls.append(1)

    class _FakeServer:
        def __init__(self, config):
            self.config = config
            self.should_exit = False

        async def serve(self, sockets=None):
            await asyncio.sleep(0)

    monkeypatch.setattr(runner_mod, "_retention_loop", spy_loop)
    monkeypatch.setattr(runner_mod, "_Server", _FakeServer)

    async def _run(name):
        data = tmp_path / name / "data"
        cfg = tmp_path / name / "cfg"
        data.mkdir(parents=True)
        cfg.mkdir(parents=True)
        await runner_mod._serve_forever(data, cfg, host="127.0.0.1", port=0, clock=lambda: 1.0, ui_dir=None)

    await _run("off")
    assert calls == []
    monkeypatch.setattr(retention, "RETENTION_ENABLED", True)
    await _run("on")
    assert calls == [1]


# --- T2: удалить сообщение ------------------------------------------------------------------------------


async def test_t2_delete_message_hidden_everywhere(client, db, clock):
    account_id, pid = _setup(db)
    now = int(clock() * 1000)
    _ingest(
        db,
        account_id,
        [
            _ev("1", now - 3000, text="обычное"),
            _ev(
                "2",
                now - 2000,
                text="секретслово тайна",
                entities=[{"type": "bold", "offset": 0, "length": 3}],
                attachments=[{"type": "document", "name": "тайна.pdf", "size": 10}],
                forward={"from_name": "Кто-то"},
                refs=["DEV-1", "https://x.example/1"],
            ),
        ],
        now,
    )
    _ingest(
        db,
        account_id,
        [{"type": "reaction", "channel": {"ext_id": "c1", "kind": "dm"}, "ext_id": "2", "reactions": [1]}],
        now,
    )
    cid = _cid(db)
    _to_project(db, cid, pid)
    mid = _mid(db, "2")
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.media WHERE message_id = ?", (mid,)).fetchone()[0] == 1
    cookie, _csrf = await login_via_code(client, db, clock)

    (chan,) = (await client.get(f"/api/v1/projects/{pid}/channels")).json()
    assert chan["unread"] == 2
    assert [
        r["message_id"] for r in (await client.get("/api/v1/search", params={"q": "секретслово"})).json()
    ] == [mid]
    seq_before = _max_seq(db)

    r = await client.delete(f"/api/v1/messages/{mid}", headers=csrf_headers(cookie))
    assert r.status_code == 204

    row = _row(db, mid)
    assert row["hidden"] == 1 and row["purged"] == 1
    assert row["text"] is None and row["entities_json"] is None and row["forward_json"] is None
    assert row["reactions_json"] is None
    assert json.loads(row["attachments_json"]) == [{"type": "document"}]
    assert json.loads(row["refs_json"]) == ["DEV-1"]
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.media WHERE message_id = ?", (mid,)).fetchone()[0] == 0

    assert (await client.get("/api/v1/search", params={"q": "секретслово"})).json() == []
    page = (await client.get(f"/api/v1/channels/{cid}/messages")).json()
    assert mid not in _feed_ids(page) and _feed_ids(page) == [_mid(db, "1")]
    (chan,) = (await client.get(f"/api/v1/projects/{pid}/channels")).json()
    assert chan["unread"] == 1
    (inbox,) = (await client.get("/api/v1/inbox")).json()
    assert inbox["unread"] == 1
    projects = {p["id"]: p for p in (await client.get("/api/v1/projects")).json()}
    assert projects[pid]["unread"] == 1

    sync = (await client.get("/api/v1/sync", params={"since": seq_before})).json()
    changes = [m for m in sync["messages"] if m["id"] == mid]
    assert len(changes) == 1
    assert changes[0] == {
        "id": mid,
        "seq": changes[0]["seq"],
        "channel_id": cid,
        "project_id": pid,
        "thread_ext_id": None,
        "hidden": True,
    }
    (chan_change,) = [c for c in sync["channels"] if c["id"] == cid]
    assert chan_change["unread"] == 1 and chan_change["hidden"] is False

    # агенты (bearer, как MCP и разборщик) — тоже не видят
    token = make_bearer_token(db, scopes=("read",))
    bearer = {"Authorization": f"Bearer {token}"}
    client.cookies.clear()
    page = (await client.get(f"/api/v1/channels/{cid}/messages", headers=bearer)).json()
    assert mid not in _feed_ids(page)
    assert (await client.get("/api/v1/search", params={"q": "секретслово"}, headers=bearer)).json() == []

    with db.reader() as conn:
        audit = conn.execute(
            "SELECT channel_id, detail_json FROM main.audit WHERE action = 'message_hidden'"
        ).fetchall()
    assert len(audit) == 1
    assert audit[0][0] == cid and json.loads(audit[0][1])["message_id"] == mid
    assert "секрет" not in audit[0][1]


async def test_p12_repeat_delete_is_204_without_changes(client, db, clock):
    account_id, _pid = _setup(db)
    now = int(clock() * 1000)
    _ingest(db, account_id, [_ev("1", now)], now)
    mid = _mid(db, "1")
    cookie, _csrf = await login_via_code(client, db, clock)
    assert (await client.delete(f"/api/v1/messages/{mid}", headers=csrf_headers(cookie))).status_code == 204
    seq = _row(db, mid)["seq"]
    assert (await client.delete(f"/api/v1/messages/{mid}", headers=csrf_headers(cookie))).status_code == 204
    assert _row(db, mid)["seq"] == seq
    assert (await client.delete("/api/v1/messages/999999", headers=csrf_headers(cookie))).status_code == 404
    with db.reader() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM main.audit WHERE action = 'message_hidden'").fetchone()[0] == 1
        )


# --- T3 + П5: повторный приём удалённого у себя -------------------------------------------------------------


def test_t3_reingest_never_returns_content_or_unhides(db):
    account_id, _pid = _setup(db)
    _ingest(db, account_id, [_ev("1", 1000, text="тайна", attachments=[{"type": "photo"}])], 1000)
    mid = _mid(db, "1")
    _hide_message(db, mid)

    # сверка / окно — upsert с текстом
    _ingest(db, account_id, [_ev("1", 1000, text="тайна", attachments=[{"type": "photo"}])], 2000)
    row = _row(db, mid)
    assert row["text"] is None and row["hidden"] == 1 and row["purged"] == 1
    # правка у источника
    _ingest(db, account_id, [_ev("1", 1000, text="тайна-2", etype="edit", edited_ts=3000)], 3000)
    row = _row(db, mid)
    assert row["text"] is None and row["hidden"] == 1
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.message_edit").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM main.media WHERE message_id = ?", (mid,)).fetchone()[0] == 0
    # реакция — no-op без нового seq (П5)
    seq = _row(db, mid)["seq"]
    reaction = {
        "type": "reaction",
        "channel": {"ext_id": "c1", "kind": "dm"},
        "ext_id": "1",
        "reactions": [1],
    }
    assert _ingest(db, account_id, [reaction], 4000).noop == 1
    assert _row(db, mid)["reactions_json"] is None and _row(db, mid)["seq"] == seq
    # удаление отправителем — скрытое не трогает
    delete = {"type": "delete", "channel": {"ext_id": "c1", "kind": "dm"}, "ext_ids": ["1"]}
    assert _ingest(db, account_id, [delete], 5000).noop == 1
    row = _row(db, mid)
    assert row["hidden"] == 1 and row["seq"] == seq


# --- T4: только UI-сессия с CSRF ---------------------------------------------------------------------------


async def test_t4_bearer_and_missing_csrf_denied(client, db, clock):
    account_id, _pid = _setup(db)
    now = int(clock() * 1000)
    _ingest(db, account_id, [_ev("1", now)], now)
    mid, cid = _mid(db, "1"), _cid(db)
    token = make_bearer_token(db, scopes=("read", "send", "draft", "organize", "triage"))
    bearer = {"Authorization": f"Bearer {token}"}
    assert (await client.delete(f"/api/v1/messages/{mid}", headers=bearer)).status_code == 403
    assert (await client.delete(f"/api/v1/channels/{cid}", headers=bearer)).status_code == 403
    with db.reader() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM main.audit WHERE action = 'ui_only_denied'").fetchone()[0] == 2
        )

    cookie, _csrf = await login_via_code(client, db, clock)
    del cookie
    no_csrf = {"Origin": ORIGIN}
    assert (await client.delete(f"/api/v1/messages/{mid}", headers=no_csrf)).status_code == 403
    assert (await client.delete(f"/api/v1/channels/{cid}", headers=no_csrf)).status_code == 403
    assert _row(db, mid)["hidden"] == 0
    with db.reader() as conn:
        assert (
            conn.execute("SELECT hidden_before_ts FROM main.channel WHERE id = ?", (cid,)).fetchone()[0]
            is None
        )


# --- T5 + П2: удалить беседу --------------------------------------------------------------------------------


async def test_t5_delete_channel_hidden_until_newer_message(client, db, clock):
    account_id, pid = _setup(db)
    now = int(clock() * 1000)
    _ingest(db, account_id, [_ev("1", now - 5000, text="раз"), _ev("2", now - 4000, text="два")], now - 3000)
    cid = _cid(db)
    _to_project(db, cid, pid)
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.channel SET attention = 'react' WHERE id = ?", (cid,))
    )
    # П2: последнее в беседе — моё сообщение из хаба (Telegram: `ts` заготовки — часы хаба)
    enq = _enqueue(db, cid, now_ms=now)
    (lease,) = _lease(db, account_id, now)
    _report(db, lease, "sent", now, ext_id="3")
    cookie, _csrf = await login_via_code(client, db, clock)
    seq_before = _max_seq(db)

    r = await client.delete(f"/api/v1/channels/{cid}", headers=csrf_headers(cookie))
    assert r.status_code == 204
    with db.reader() as conn:
        boundary = conn.execute(
            "SELECT hidden_before_ts, hidden_before_sort FROM main.channel WHERE id = ?", (cid,)
        ).fetchone()
        hidden = conn.execute("SELECT COUNT(*) FROM main.message WHERE channel_id = ? AND hidden = 1", (cid,))
        assert hidden.fetchone()[0] == 3
        assert (
            conn.execute("SELECT COUNT(*) FROM main.audit WHERE action = 'channel_hidden'").fetchone()[0] == 1
        )
    assert boundary == (now, 3)
    assert _outbox(db, enq.outbox_id) == ("sent", "")

    assert (await client.get(f"/api/v1/projects/{pid}/channels")).json() == []
    assert (await client.get("/api/v1/projects/-2/channels")).json() == []
    assert (await client.get("/api/v1/inbox")).json() == []
    projects = {p["id"]: p for p in (await client.get("/api/v1/projects")).json()}
    assert projects[pid]["unread"] == 0 and projects[pid]["unread_react"] == 0
    sync = (await client.get("/api/v1/sync", params={"since": seq_before})).json()
    (chan_change,) = [c for c in sync["channels"] if c["id"] == cid]
    assert chan_change["hidden"] is True
    assert all(m.get("hidden") for m in sync["messages"] if m["channel_id"] == cid)
    assert (await client.get(f"/api/v1/channels/{cid}")).json()["hidden"] is True

    # сверка с тем же старым сообщением и старое, впервые пришедшее из истории, — беседу не возвращают
    _ingest(db, account_id, [_ev("2", now - 4000, text="два"), _ev("0", now - 9000, text="нуль")], now + 10)
    assert (await client.get(f"/api/v1/projects/{pid}/channels")).json() == []
    old = _row(db, _mid(db, "0"))
    assert old["hidden"] == 1 and old["purged"] == 1 and old["text"] is None
    assert _row(db, _mid(db, "2"))["text"] is None

    # новое в ту же секунду, что граница, но с большим `ext_sort` — видно; беседа вернулась только с ним
    _ingest(db, account_id, [_ev("4", now, text="новое")], now + 20)
    (chan,) = (await client.get(f"/api/v1/projects/{pid}/channels")).json()
    assert chan["id"] == cid and chan["unread"] == 1
    page = (await client.get(f"/api/v1/channels/{cid}/messages")).json()
    assert [m["text"] for m in page["messages"]] == ["новое"]
    with db.reader() as conn:
        assert conn.execute(
            "SELECT project_id, map_source, attention FROM main.channel WHERE id = ?", (cid,)
        ).fetchone() == (pid, "manual", "react")
    assert (await client.get(f"/api/v1/channels/{cid}")).json()["hidden"] is False


def test_p2_same_position_as_boundary_stays_hidden(db):
    """Граница — позиция `(ts, ext_sort)`: не новее границы — скрыто, даже в ту же секунду."""
    account_id, _pid = _setup(db)
    _ingest(db, account_id, [_ev("5", 1000)], 1000)
    cid = _cid(db)
    _hide_channel(db, cid)
    _ingest(db, account_id, [_ev("4", 1000)], 2000)
    assert _row(db, _mid(db, "4"))["hidden"] == 1
    _ingest(db, account_id, [_ev("6", 1000)], 2000)
    assert _row(db, _mid(db, "6"))["hidden"] == 0
    # ровно на границе (нечисловые ext_id: `ext_sort` = 0 у всех) — тоже скрыто: «позиция <= границы»
    _ingest(db, account_id, [_ev("a", 1000, chat="m")], 1000)
    _hide_channel(db, _cid(db, "m"))
    _ingest(db, account_id, [_ev("b", 1000, chat="m")], 2000)
    assert _row(db, _mid(db, "b", chat="m"))["hidden"] == 1


async def test_p2_agent_draft_brings_channel_back(client, db, clock):
    account_id, pid = _setup(db)
    now = int(clock() * 1000)
    _ingest(db, account_id, [_ev("1", now - 1000)], now)
    cid = _cid(db)
    _to_project(db, cid, pid)
    _hide_channel(db, cid, now)
    await login_via_code(client, db, clock)
    assert (await client.get(f"/api/v1/projects/{pid}/channels")).json() == []
    _enqueue(db, cid, origin="api", now_ms=now + 5)
    (chan,) = (await client.get(f"/api/v1/projects/{pid}/channels")).json()
    assert chan["id"] == cid


def test_p2_empty_channel_boundary_is_now(db):
    account_id, _pid = _setup(db)
    _ingest(db, account_id, [_ev("1", 1000)], 1000)
    cid = _cid(db)
    db.write_sync(lambda conn: conn.execute("DELETE FROM main.message WHERE channel_id = ?", (cid,)))
    _hide_channel(db, cid, now_ms=50_000)
    with db.reader() as conn:
        assert conn.execute(
            "SELECT hidden_before_ts, hidden_before_sort FROM main.channel WHERE id = ?", (cid,)
        ).fetchone() == (50_000, 0)


async def test_p3_resolve_skips_hidden(client, db, clock):
    account_id, pid = _setup(db)
    now = int(clock() * 1000)
    _ingest(db, account_id, [_ev("1", now, text="тайна")], now)
    cid = _cid(db)
    _to_project(db, cid, pid)
    token = make_bearer_token(db, scopes=("read",))
    bearer = {"Authorization": f"Bearer {token}"}
    res = (await client.post("/api/v1/channels/resolve", json={"query": "Беседа"}, headers=bearer)).json()
    assert [c["id"] for c in res] == [cid]
    assert res[0]["recent_messages"][0]["text"] == "тайна"
    _hide_message(db, _mid(db, "1"))
    res = (await client.post("/api/v1/channels/resolve", json={"query": "Беседа"}, headers=bearer)).json()
    assert res[0]["recent_messages"] == []
    _hide_channel(db, cid)
    assert (
        await client.post("/api/v1/channels/resolve", json={"query": "Беседа"}, headers=bearer)
    ).json() == []


# --- П1: своё исходящее -----------------------------------------------------------------------------------


async def test_p1_sending_or_unknown_is_409_nothing_changes(client, db, clock):
    account_id, _pid = _setup(db)
    now = int(clock() * 1000)
    _ingest(db, account_id, [_ev("1", now - 1000)], now)
    cid = _cid(db)
    enq = _enqueue(db, cid, now_ms=now)
    (lease,) = _lease(db, account_id, now)
    cookie, _csrf = await login_via_code(client, db, clock)
    r = await client.delete(f"/api/v1/messages/{enq.message_id}", headers=csrf_headers(cookie))
    assert r.status_code == 409
    r = await client.delete(f"/api/v1/channels/{cid}", headers=csrf_headers(cookie))
    assert r.status_code == 409
    assert _row(db, enq.message_id)["hidden"] == 0 and _row(db, enq.message_id)["text"] == "моё"
    assert _row(db, _mid(db, "1"))["hidden"] == 0
    _report(db, lease, "unknown", now)
    r = await client.delete(f"/api/v1/messages/{enq.message_id}", headers=csrf_headers(cookie))
    assert r.status_code == 409
    assert _outbox(db, enq.outbox_id) == ("unknown", "моё")
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.audit WHERE action LIKE '%_hidden'").fetchone()[0] == 0


def test_p1_draft_and_queued_cancelled_then_hidden(db):
    account_id, _pid = _setup(db)
    _ingest(db, account_id, [_ev("1", 1000)], 1000)
    cid = _cid(db)
    draft = _enqueue(db, cid, origin="api", text="черновик", now_ms=2000)
    queued = _enqueue(db, cid, origin="ui", text="в очереди", now_ms=2001)
    _hide_message(db, draft.message_id, now_ms=3000)
    _hide_message(db, queued.message_id, now_ms=3000)
    assert _outbox(db, draft.outbox_id) == ("cancelled", "")
    assert _outbox(db, queued.outbox_id) == ("cancelled", "")
    for message_id in (draft.message_id, queued.message_id):
        row = _row(db, message_id)
        assert row["hidden"] == 1 and row["text"] is None
    assert _lease(db, account_id, 4000) == []


def test_p1_echo_by_send_token_to_hidden_placeholder_stays_clean(db):
    """Отчёт коннектора `permanent`, а сообщение всё-таки ушло: заготовка `failed` скрыта, эхо по `send_token`
    к ней — содержимое очищено, `hidden` остался, текст очереди стёрт в `sent`."""
    account_id, _pid = _setup(db)
    _ingest(db, account_id, [_ev("1", 1000)], 1000)
    cid = _cid(db)
    enq = _enqueue(db, cid, text="тайное моё", now_ms=2000)
    (lease,) = _lease(db, account_id, 2000)
    _report(db, lease, "permanent", 2001, error="x")
    _hide_message(db, enq.message_id, now_ms=2500)
    assert _outbox(db, enq.outbox_id) == ("failed", "")
    echo = _ev("9", 2002, text="тайное моё", is_me=True, sender={"ext_id": "1", "is_me": True})
    echo["send_token"] = lease.send_token
    _ingest(db, account_id, [echo], 3000)
    row = _row(db, enq.message_id)
    assert row["ext_id"] == "9" and row["hidden"] == 1 and row["purged"] == 1 and row["text"] is None
    assert _outbox(db, enq.outbox_id) == ("sent", "")


# --- ревью Ф8-4: выданная коннектору `queued` — 409, текст не утекает и не отправляется -----------------


async def test_review_attempted_queued_is_409_text_not_leaked_nor_resent(client, db, clock):
    account_id, _pid = _setup(db)
    now = int(clock() * 1000)
    _ingest(db, account_id, [_ev("1", now - 1000)], now)
    cid = _cid(db)
    enq = _enqueue(db, cid, text="удалённый секрет", now_ms=now)
    (lease,) = _lease(db, account_id, now)
    _report(db, lease, "temporary", now + 1)  # снова `queued`, но коннектор её уже брал
    cookie, _csrf = await login_via_code(client, db, clock)
    r = await client.delete(f"/api/v1/messages/{enq.message_id}", headers=csrf_headers(cookie))
    assert r.status_code == 409
    r = await client.delete(f"/api/v1/channels/{cid}", headers=csrf_headers(cookie))
    assert r.status_code == 409
    # ничего не изменилось: строка очереди в `queued` с текстом, заготовка видна, беседа не удалена
    assert _outbox(db, enq.outbox_id) == ("queued", "удалённый секрет")
    assert _row(db, enq.message_id)["hidden"] == 0
    with db.reader() as conn:
        assert (
            conn.execute("SELECT hidden_before_ts FROM main.channel WHERE id = ?", (cid,)).fetchone()[0]
            is None
        )
    # ни одна строка `unknown` с этим текстом не появилась (её читал бы любой bearer `read`)
    token = make_bearer_token(db, scopes=("read",))
    client.cookies.clear()
    unknown = await client.get(
        "/api/v1/outbox", params={"status": "unknown"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert unknown.json() == []


def test_review_unattempted_queued_and_draft_still_cancelled(db):
    account_id, _pid = _setup(db)
    _ingest(db, account_id, [_ev("1", 1000)], 1000)
    cid = _cid(db)
    queued = _enqueue(db, cid, text="в очереди", now_ms=2000)
    _hide_channel(db, cid, now_ms=3000)
    assert _outbox(db, queued.outbox_id) == ("cancelled", "")
    assert _lease(db, account_id, 4000) == []


# --- ревью Ф8-4 (блокер): граница удалённой беседы — только по сообщениям источника ----------------------


def test_review_boundary_ignores_hub_clock_placeholders(db):
    account_id, _pid = _setup(db)
    _ingest(db, account_id, [_ev("10", 1000, text="старое")], 1000)
    cid = _cid(db)
    _enqueue(db, cid, origin="api", text="черновик агента", now_ms=5000)  # `ts` — часы хаба
    failed = _enqueue(db, cid, text="не ушло", now_ms=5500)
    (lease,) = _lease(db, account_id, 5500)
    _report(db, lease, "permanent", 5501, error="x")
    del failed
    _hide_channel(db, cid, now_ms=6000)
    with db.reader() as conn:
        assert conn.execute(
            "SELECT hidden_before_ts, hidden_before_sort FROM main.channel WHERE id = ?", (cid,)
        ).fetchone() == (1000, 10)
    # сообщение, отправленное до удаления (3000), но доехавшее позже, — видно и с текстом
    _ingest(db, account_id, [_ev("11", 3000, text="новое, владелец его не видел")], 7000)
    row = _row(db, _mid(db, "11"))
    assert (row["text"], row["hidden"]) == ("новое, владелец его не видел", 0)
    # а не новее последнего сообщения источника — скрыто
    _ingest(db, account_id, [_ev("9", 900, text="старое из истории")], 7000)
    assert _row(db, _mid(db, "9"))["hidden"] == 1


def test_review_boundary_without_source_messages_is_now(db):
    account_id, _pid = _setup(db)
    _ingest(db, account_id, [_ev("1", 1000)], 1000)
    cid = _cid(db)
    db.write_sync(lambda conn: conn.execute("DELETE FROM main.message WHERE channel_id = ?", (cid,)))
    _enqueue(db, cid, origin="api", text="черновик", now_ms=90_000)
    _hide_channel(db, cid, now_ms=50_000)
    with db.reader() as conn:
        assert conn.execute(
            "SELECT hidden_before_ts, hidden_before_sort FROM main.channel WHERE id = ?", (cid,)
        ).fetchone() == (50_000, 0)


# --- ревью Ф8-4: эхо без `send_token` не воскрешает скрытое своё сообщение --------------------------------


@pytest.mark.parametrize("decision", ["sent", "not_sent"])
def test_review_echo_by_text_merges_into_hidden_placeholder(db, decision):
    account_id, _pid = _setup(db)
    _ingest(db, account_id, [_ev("1", 1000, text="привет")], 1000)
    cid = _cid(db)
    enq = _enqueue(db, cid, text="секретный текст", now_ms=2000)
    (lease,) = _lease(db, account_id, 2000)
    _report(db, lease, "unknown", 2001)
    db.write_sync(
        lambda conn: outbox_mod.resolve_unknown(conn, enq.outbox_id, decision=decision, now_ms=2500)
    )
    _hide_message(db, enq.message_id, now_ms=2600)
    with db.reader() as conn:
        assert conn.execute("SELECT text FROM main.outbox WHERE id = ?", (enq.outbox_id,)).fetchone() == ("",)
    echo = _ev("9", 2010, text="секретный текст", is_me=True, sender={"ext_id": "1", "is_me": True})
    _ingest(db, account_id, [echo], 3000)
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT id, text, hidden, purged FROM main.message WHERE channel_id = ? AND is_me = 1", (cid,)
        ).fetchall()
    assert rows == [(enq.message_id, None, 1, 1)]
    assert _row(db, enq.message_id)["ext_id"] == "9"
    assert _outbox(db, enq.outbox_id) == ("sent", "")


def test_review_echo_other_text_not_merged_into_hidden(db):
    """Хеш — того же текста, что раньше сравнивался напрямую: другое своё сообщение со скрытой не
    склеивается."""
    account_id, _pid = _setup(db)
    _ingest(db, account_id, [_ev("1", 1000)], 1000)
    cid = _cid(db)
    enq = _enqueue(db, cid, text="секретный текст", now_ms=2000)
    (lease,) = _lease(db, account_id, 2000)
    _report(db, lease, "permanent", 2001, error="x")
    _hide_message(db, enq.message_id, now_ms=2600)
    echo = _ev("9", 2010, text="другой текст", is_me=True, sender={"ext_id": "1", "is_me": True})
    _ingest(db, account_id, [echo], 3000)
    other = _row(db, _mid(db, "9"))
    assert other["id"] != enq.message_id and other["text"] == "другой текст" and other["hidden"] == 0


def test_review_old_outbox_row_gets_hash_before_text_is_erased(db):
    """Строка очереди старше миграции 0012 (без хеша): хеш считается при скрытии, до стирания текста."""
    account_id, _pid = _setup(db)
    _ingest(db, account_id, [_ev("1", 1000)], 1000)
    cid = _cid(db)
    enq = _enqueue(db, cid, text="старый текст", now_ms=2000)
    (lease,) = _lease(db, account_id, 2000)
    _report(db, lease, "permanent", 2001, error="x")
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.outbox SET text_sha256 = NULL WHERE id = ?", (enq.outbox_id,))
    )
    _hide_message(db, enq.message_id, now_ms=2600)
    echo = _ev("9", 2010, text="старый текст", is_me=True, sender={"ext_id": "1", "is_me": True})
    _ingest(db, account_id, [echo], 3000)
    with db.reader() as conn:
        visible = conn.execute(
            "SELECT COUNT(*) FROM main.message WHERE channel_id = ? AND is_me = 1 AND hidden = 0", (cid,)
        ).fetchone()[0]
    assert visible == 0


# --- П4: карточки агента ------------------------------------------------------------------------------------


async def test_p4_cards_dismissed_and_not_listed(client, db, clock):
    account_id, pid = _setup(db)
    now = int(clock() * 1000)
    _ingest(db, account_id, [_ev("1", now - 2000), _ev("2", now - 1000)], now)
    cid = _cid(db)
    _to_project(db, cid, pid)
    m1, m2 = _mid(db, "1"), _mid(db, "2")

    def _card(ids):
        return db.write_sync(
            lambda conn: agent_items.upsert(
                conn,
                channel_id=cid,
                message_ids=ids,
                label="reply",
                summary="пересказ",
                action=None,
                provider="p",
                model="m",
                api_token_id=None,
                now_ms=now,
            )
        ).item_id

    item = _card([m1, m2])
    _hide_message(db, m1)
    with db.reader() as conn:
        assert conn.execute("SELECT status FROM main.agent_item WHERE id = ?", (item,)).fetchone()[0] == "new"
    _hide_message(db, m2)
    with db.reader() as conn:
        assert (
            conn.execute("SELECT status FROM main.agent_item WHERE id = ?", (item,)).fetchone()[0]
            == "dismissed"
        )
    await login_via_code(client, db, clock)
    # hub-wake берёт карточки `status=new` — скрытой среди них нет; и в `any` её тоже нет
    assert (await client.get("/api/v1/agent/items", params={"status": "new"})).json() == []
    assert (await client.get("/api/v1/agent/items", params={"status": "any"})).json() == []


def test_p4_channel_hide_dismisses_all_open_cards(db):
    account_id, _pid = _setup(db)
    _ingest(db, account_id, [_ev("1", 1000), _ev("2", 2000)], 3000)
    cid = _cid(db)
    item = db.write_sync(
        lambda conn: agent_items.upsert(
            conn,
            channel_id=cid,
            message_ids=[_mid(db, "1")],
            label="fyi",
            summary="s",
            action=None,
            provider="p",
            model="m",
            api_token_id=None,
            now_ms=3000,
        )
    ).item_id
    _hide_channel(db, cid)
    with db.reader() as conn:
        assert (
            conn.execute("SELECT status FROM main.agent_item WHERE id = ?", (item,)).fetchone()[0]
            == "dismissed"
        )


# --- П6: треды ------------------------------------------------------------------------------------------


def _thread_events(ts0):
    return [
        _ev("10", ts0, text="родитель", chat="g", kind="group"),
        _ev("11", ts0 + 1, text="ответ раз", chat="g", kind="group", thread_ext_id="10"),
        _ev("12", ts0 + 2, text="ответ два", chat="g", kind="group", thread_ext_id="10"),
    ]


async def test_p6_hide_reply_touches_thread_summary(client, db, clock):
    account_id, _pid = _setup(db, source="slack")
    now = int(clock() * 1000)
    _ingest(db, account_id, _thread_events(now - 100), now)
    cid = _cid(db, "g")
    cookie, _csrf = await login_via_code(client, db, clock)
    (parent,) = (await client.get(f"/api/v1/channels/{cid}/messages")).json()["messages"]
    assert parent["thread"]["reply_count"] == 2
    reply = _mid(db, "11", chat="g")
    assert (await client.delete(f"/api/v1/messages/{reply}", headers=csrf_headers(cookie))).status_code == 204
    (parent,) = (await client.get(f"/api/v1/channels/{cid}/messages")).json()["messages"]
    assert parent["thread"]["reply_count"] == 1
    page = (await client.get(f"/api/v1/channels/{cid}/threads/10/messages")).json()
    assert [m["text"] for m in page["messages"]] == ["ответ два"]
    with db.reader() as conn:
        assert conn.execute("SELECT reply_count FROM main.thread WHERE ext_id = '10'").fetchone()[0] == 1


async def test_p6_hide_parent_hides_replies_new_replies_visible(client, db, clock):
    account_id, _pid = _setup(db, source="slack")
    now = int(clock() * 1000)
    _ingest(db, account_id, _thread_events(now - 100), now)
    cid = _cid(db, "g")
    cookie, _csrf = await login_via_code(client, db, clock)
    parent_id = _mid(db, "10", chat="g")
    assert (
        await client.delete(f"/api/v1/messages/{parent_id}", headers=csrf_headers(cookie))
    ).status_code == 204
    for ext in ("10", "11", "12"):
        assert _row(db, _mid(db, ext, chat="g"))["hidden"] == 1
    assert (await client.get(f"/api/v1/channels/{cid}/messages")).json()["messages"] == []
    page = (await client.get(f"/api/v1/channels/{cid}/threads/10/messages")).json()
    assert page["messages"] == []
    assert (await client.get(f"/api/v1/channels/{cid}/threads")).json() == []

    _ingest(
        db, account_id, [_ev("13", now, text="новый ответ", chat="g", kind="group", thread_ext_id="10")], now
    )
    page = (await client.get(f"/api/v1/channels/{cid}/threads/10/messages")).json()
    assert [m["text"] for m in page["messages"]] == ["новый ответ"]
    parent = page["parent"]
    assert parent["hidden"] is True and parent["text"] is None and parent["sender"] is None
    assert parent["attachments_json"] is None and parent["media"] == []
    (thread,) = (await client.get(f"/api/v1/channels/{cid}/threads")).json()
    assert thread["thread"]["reply_count"] == 1 and thread["parent"]["hidden"] is True


# --- T6 + П7 + П12: история правок --------------------------------------------------------------------------


async def test_t6_edit_history(client, db, clock):
    account_id, pid = _setup(db)
    now = int(clock() * 1000)
    _ingest(
        db,
        account_id,
        [_ev("1", now - 100, text="v1", entities=[{"type": "bold", "offset": 0, "length": 1}])],
        now,
    )
    cid = _cid(db)
    _to_project(db, cid, pid)
    mid = _mid(db, "1")
    _ingest(db, account_id, [_ev("1", now - 100, text="v2", etype="edit", edited_ts=now + 10)], now + 10)
    _ingest(db, account_id, [_ev("1", now - 100, text="v3", etype="edit", edited_ts=now + 20)], now + 20)
    # тот же текст с новой правкой, устаревшая правка, реакция, сводка треда, смена текста без правки —
    # не версии
    _ingest(db, account_id, [_ev("1", now - 100, text="v3", etype="edit", edited_ts=now + 30)], now + 30)
    _ingest(db, account_id, [_ev("1", now - 100, text="старое", etype="edit", edited_ts=now + 15)], now + 31)
    _ingest(
        db,
        account_id,
        [{"type": "reaction", "channel": {"ext_id": "c1", "kind": "dm"}, "ext_id": "1", "reactions": [1]}],
        now + 32,
    )
    _ingest(
        db,
        account_id,
        [_ev("1", now - 100, text="v3", edited_ts=now + 30, thread={"reply_count": 3})],
        now + 33,
    )
    _ingest(db, account_id, [_ev("1", now - 100, text="v3 справочник", edited_ts=now + 30)], now + 34)

    await login_via_code(client, db, clock)
    r = await client.get(f"/api/v1/messages/{mid}/edits")
    assert r.status_code == 200
    edits = r.json()["edits"]
    assert [e["text"] for e in edits] == ["v1", "v2"]
    assert [e["replaced_ts"] for e in edits] == [now + 10, now + 20]
    assert json.loads(edits[0]["entities_json"]) == [{"type": "bold", "offset": 0, "length": 1}]
    (msg,) = (await client.get(f"/api/v1/channels/{cid}/messages")).json()["messages"]
    assert msg["edit_count"] == 2 and msg["text"] == "v3 справочник"

    token = make_bearer_token(db, scopes=("read",), project_ids=[pid])
    other = make_bearer_token(db, scopes=("read",), project_ids=[])
    client.cookies.clear()
    ok = await client.get(f"/api/v1/messages/{mid}/edits", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200 and len(ok.json()["edits"]) == 2
    foreign = await client.get(f"/api/v1/messages/{mid}/edits", headers={"Authorization": f"Bearer {other}"})
    assert foreign.status_code == 404

    _hide_message(db, mid)
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.message_edit").fetchone()[0] == 0
    gone = await client.get(f"/api/v1/messages/{mid}/edits", headers={"Authorization": f"Bearer {token}"})
    assert gone.status_code == 404


def test_t6_edit_of_message_without_text_or_edited_ts_gives_no_version(db):
    account_id, _pid = _setup(db)
    _ingest(db, account_id, [_ev("1", 100, text=None)], 100)
    _ingest(db, account_id, [_ev("1", 100, text="подпись", etype="edit", edited_ts=200)], 200)
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.message_edit").fetchone()[0] == 0


# --- T7: миграция 0012 на БД после 0011 ---------------------------------------------------------------------


def test_t7_migration_0012_after_0011(tmp_path, monkeypatch):
    original = db_module._migrations

    def upto_11(*, main):
        return {v: files for v, files in original(main=main).items() if v <= 11}

    monkeypatch.setattr(db_module, "_migrations", upto_11)
    old = Db(tmp_path)
    old.start()
    try:
        for schema in ("main", "work"):
            account_id = insert_account(old, source="telegram", db_name=schema)

            def _fn(conn, schema=schema, account_id=account_id):
                cid = conn.execute(
                    f"INSERT INTO {schema}.channel (account_id, ext_id, kind, title) "  # noqa: S608
                    "VALUES (?, 'c1', 'dm', 'В')",
                    (account_id,),
                ).lastrowid
                conn.execute(
                    f"INSERT INTO {schema}.message (seq, first_seq, account_id, "  # noqa: S608
                    "channel_id, ext_id, ts, is_me, "
                    "kind, text) VALUES (?, ?, ?, ?, '1', 1000, 0, 'text', 'текст')",
                    (1 if schema == "main" else 2, 1 if schema == "main" else 2, account_id, cid),
                )

            old.write_sync(_fn)
        with old.reader() as conn:
            assert conn.execute("PRAGMA work.user_version").fetchone()[0] == 11
    finally:
        old.close()

    monkeypatch.setattr(db_module, "_migrations", original)
    new = Db(tmp_path)
    new.start()
    try:
        with new.reader() as conn:
            for schema in ("main", "work"):
                assert conn.execute(f"PRAGMA {schema}.user_version").fetchone()[0] >= 12
                assert conn.execute(
                    f"SELECT text, hidden FROM {schema}.message"  # noqa: S608
                ).fetchall() == [("текст", 0)]
                assert conn.execute(
                    f"SELECT hidden_before_ts, hidden_before_sort FROM {schema}.channel"  # noqa: S608
                ).fetchall() == [(None, None)]
                assert conn.execute(f"SELECT COUNT(*) FROM {schema}.message_edit").fetchone()[0] == 0  # noqa: S608
                outbox_cols = [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info(outbox)")]
                assert "text_sha256" in outbox_cols
    finally:
        new.close()
    assert "message_edit" not in db_module._SHARED_TABLES_WITH_OWN_ID


# --- медиа скрытого (уточнение архитектора, T6 спеки медиа) -------------------------------------------------

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 100 + b"end"


def _media_rows(db, message_id):
    with db.reader() as conn:
        return conn.execute("SELECT id FROM main.media WHERE message_id = ?", (message_id,)).fetchall()


def _make_ready(db, account_id, media_id, body):
    sha = hashlib.sha256(body).hexdigest()
    path = media.file_path(db.data_dir, account_id, sha)
    media.ensure_dir(path.parent)
    path.write_bytes(body)
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.media SET status = 'ready', sha256 = ?, stored_size = ?, detected = 'jpeg' "
            "WHERE id = ?",
            (sha, len(body), media_id),
        )
    )
    return path


async def test_media_hide_removes_rows_and_unreferenced_file(client, db, clock):
    account_id, pid = _setup(db)
    now = int(clock() * 1000)
    db.write_sync(lambda conn: media.ensure_start_ts(conn, 0))
    photo = [{"type": "photo"}]
    _ingest(
        db,
        account_id,
        [
            _ev("1", now - 3, attachments=photo),
            _ev("2", now - 2, attachments=photo),
            _ev("3", now - 1, attachments=photo),
        ],
        now,
    )
    cid = _cid(db)
    _to_project(db, cid, pid)
    m1, m2, m3 = _mid(db, "1"), _mid(db, "2"), _mid(db, "3")
    ((media1,),), ((media2,),), ((media3,),) = _media_rows(db, m1), _media_rows(db, m2), _media_rows(db, m3)
    shared = _make_ready(db, account_id, media1, JPEG)
    _make_ready(db, account_id, media2, JPEG)  # тот же sha — дедуп в пределах аккаунта
    own = _make_ready(db, account_id, media3, JPEG + b"x")
    cookie, _csrf = await login_via_code(client, db, clock)
    assert (await client.get(f"/api/v1/media/{media1}")).status_code == 200

    assert (await client.delete(f"/api/v1/messages/{m1}", headers=csrf_headers(cookie))).status_code == 204
    assert _media_rows(db, m1) == []
    assert shared.exists()  # на sha ещё ссылается сообщение 2
    assert (await client.get(f"/api/v1/media/{media1}")).status_code == 404
    assert (await client.get(f"/api/v1/media/{media2}")).status_code == 200

    assert (await client.delete(f"/api/v1/messages/{m2}", headers=csrf_headers(cookie))).status_code == 204
    assert not shared.exists()
    assert (await client.delete(f"/api/v1/channels/{cid}", headers=csrf_headers(cookie))).status_code == 204
    assert not own.exists() and _media_rows(db, m3) == []

    fetch = await client.post(f"/api/v1/messages/{m1}/media/0/fetch", headers=csrf_headers(cookie))
    assert fetch.status_code == 404
    assert _media_rows(db, m1) == []


async def test_media_get_of_hidden_closed_explicitly(client, db, clock):
    """`media_file` смотрит `m.hidden` сам, не только на отсутствие строк (ревью Ф8-5)."""
    account_id, _pid = _setup(db)
    now = int(clock() * 1000)
    db.write_sync(lambda conn: media.ensure_start_ts(conn, 0))
    _ingest(db, account_id, [_ev("1", now, attachments=[{"type": "photo"}])], now)
    mid = _mid(db, "1")
    ((media_id,),) = _media_rows(db, mid)
    _make_ready(db, account_id, media_id, JPEG)
    await login_via_code(client, db, clock)
    assert (await client.get(f"/api/v1/media/{media_id}")).status_code == 200
    db.write_sync(lambda conn: conn.execute("UPDATE main.message SET hidden = 1 WHERE id = ?", (mid,)))
    assert (await client.get(f"/api/v1/media/{media_id}")).status_code == 404


@pytest.mark.parametrize("schema", ["main", "work"])
def test_hide_in_both_files(db, schema):
    account_id = insert_account(db, source="slack", db_name=schema)
    _ingest(db, account_id, [_ev("1", 1000, text="тайна", chat="w", kind="channel")], 1000)
    mid = _mid(db, "1", chat="w", schema=schema)
    _hide_message(db, mid)
    row = _row(db, mid, schema)
    assert row["hidden"] == 1 and row["text"] is None


def test_review_text_hash_set_on_enqueue_and_approve(db):
    account_id, _pid = _setup(db)
    _ingest(db, account_id, [_ev("1", 1000)], 1000)
    draft = _enqueue(db, _cid(db), origin="api", text="черновик", now_ms=2000)

    def _hash():
        with db.reader() as conn:
            return conn.execute(
                "SELECT text_sha256 FROM main.outbox WHERE id = ?", (draft.outbox_id,)
            ).fetchone()[0]

    assert _hash() == hashlib.sha256("черновик".encode()).hexdigest()
    db.write_sync(lambda conn: outbox_mod.approve(conn, draft.outbox_id, text="правлено", now_ms=3000))
    assert _hash() == hashlib.sha256("правлено".encode()).hexdigest()
