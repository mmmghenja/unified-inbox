"""Ф14-5 П20: подписка на проект в режиме «каждое сообщение» (`mode = 'messages'`) и миграция 0025. Ожидание —
прежнее `POST /subs/{id}/wait` (команда сессии та же), сигнал — только числа (`kind: messages`)."""

import json

import pytest

from hub.core import db as db_module
from hub.core import security, subs
from hub.core.db import Db
from hub.core.ingest import apply_batch

from .conftest import insert_account, insert_channel, insert_project

ORIGIN = "http://127.0.0.1:8787"
PID, START, SID = 4242, 777_000, "sess-aaaaaaaa-1111"


@pytest.fixture
def procs(monkeypatch):
    table = {PID: START}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


def _h(token: str) -> dict:
    # Ф14-6b п.7: клиент ожидания нового протокола (`hub sub wait` шлёт этот заголовок).
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN, "X-Hub-Sub-Wait": "2"}


def _token(db, *, name="mcp:all", kind="interactive", scopes=("read", "draft")):
    return db.write_sync(
        lambda conn: security.create_api_token(
            conn, name=name, kind=kind, scopes=list(scopes), project_ids=None, expires_ts=None
        )
    )


@pytest.fixture
def world(db, tmp_path):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    pid = insert_project(db, name="Umbrella", workdir=str(workdir))
    other = insert_project(db, name="Другой")
    db.write_sync(lambda conn: subs.set_project_subs(conn, pid, True, now_ms=0))
    acc = insert_account(db, source="tg")
    cid = insert_channel(db, acc, project_id=pid, ext_id="c1")
    cid2 = insert_channel(db, acc, project_id=pid, ext_id="c2")
    muted = insert_channel(db, acc, project_id=pid, ext_id="c3", muted=1)
    foreign = insert_channel(db, acc, project_id=other, ext_id="c4")
    tok = _token(db)
    triage = _token(db, name="agent:triage", kind="background", scopes=("read", "triage"))
    return {
        "pid": pid,
        "other": other,
        "workdir": str(workdir),
        "acc": acc,
        "cid": cid,
        "cid2": cid2,
        "muted": muted,
        "foreign": foreign,
        "tok": tok.token,
        "triage": triage.token,
    }


def _session(w, *, sid=SID, full=True):
    body = {"pid": PID, "proc_start": START, "session_id": sid}
    if full:
        body.update({"cwd": w["workdir"], "kind": "interactive", "name": None, "name_source": None})
    return body


async def _subscribe(client, w, mode=None):
    body = {"session": _session(w)}
    if mode is not None:
        body["mode"] = mode
    r = await client.post("/api/v1/subs", json=body, headers=_h(w["tok"]))
    assert r.status_code == 200, r.text
    return r.json()


async def _wait(client, w, sub_id, *, timeout_s=1):
    return await client.post(
        f"/api/v1/subs/{sub_id}/wait",
        json={"session": _session(w, full=False), "waiter": "0123456789abcdef", "timeout_s": timeout_s},
        headers=_h(w["tok"]),
    )


def _event(channel_ext, ext_id, ts, *, is_me=False):
    return {
        "type": "upsert",
        "channel": {"ext_id": channel_ext, "kind": "dm"},
        "ext_id": ext_id,
        "ts": ts,
        "is_me": is_me,
        "kind": "text",
        "text": "привет",
        "mentions_me": False,
    }


def _ingest(db, w, events, clock):
    now = int(clock() * 1000)
    return db.write_sync(
        lambda conn: apply_batch(conn, account_id=w["acc"], events=events, cursor=None, now_ms=now)
    )


def _msg_id(db, channel_id, ext_id):
    with db.reader() as conn:
        return conn.execute(
            "SELECT id FROM main.message WHERE channel_id = ? AND ext_id = ?", (channel_id, ext_id)
        ).fetchone()[0]


def _msg_signals(db):
    with db.reader() as conn:
        return conn.execute(
            "SELECT project_sub_id, channel_id, msg_count, status, reason FROM main.session_chan_signal "
            "ORDER BY id"
        ).fetchall()


def _card_signals(db):
    with db.reader() as conn:
        return conn.execute("SELECT sub_id, item_id, status FROM main.session_signal ORDER BY id").fetchall()


async def _card(client, w, channel_id, message_ids):
    r = await client.post(
        "/api/v1/agent/items",
        json={
            "channel_id": channel_id,
            "message_ids": message_ids,
            "label": "reply",
            "summary": "сводка",
            "action": None,
            "provider": "test",
            "model": "m",
        },
        headers=_h(w["triage"]),
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def test_every_message_signal_without_card(client, app, db, world, procs, clock):
    body = await _subscribe(client, world, "messages")
    sub = body["sub"]
    assert body["created"] is True and sub["mode"] == "messages"
    now = int(clock() * 1000)
    _ingest(
        db, world, [_event("c1", "1", now), _event("c1", "2", now + 1), _event("c1", "3", now + 2)], clock
    )
    # прочитано владельцем и беседа открыта у него — сигнал всё равно есть (П3)
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET last_read_ts = ?, last_read_sort = 999, last_read_ext = '3' "
            "WHERE id = ?",
            (now + 5, world["cid"]),
        )
    )
    app.state.presence.set(
        token_id=1, client_id="ui", channel_id=world["cid"], thread_ext_id=None, now_ms=now
    )
    r = await _wait(client, world, sub["id"])
    assert r.status_code == 200, r.text
    assert r.json() == {
        "sub_id": sub["id"],
        "project_id": world["pid"],
        "signals": [
            {
                "kind": "messages",
                "channel_id": world["cid"],
                "account_id": world["acc"],
                "msg_count": 3,
                "message_id": _msg_id(db, world["cid"], "3"),
                # Ф20-1 D: категория (беседа — личка) и родитель треда (нет).
                "cat": "dm",
                "thread_ext": None,
            }
        ],
    }
    detail = None
    with db.reader() as conn:
        detail = conn.execute("SELECT detail_json FROM main.audit WHERE action = 'sub_signal'").fetchone()[0]
    assert json.loads(detail)["kind"] == "messages"


async def test_no_signal_for_own_muted_foreign_and_chan_sub(client, db, world, procs, clock):
    await _subscribe(client, world, "messages")
    now = int(clock() * 1000)
    # Ф26-1: подписка на беседу глушит подписку на проект только той же сессии
    r = await client.post(
        "/api/v1/chan-subs",
        json={
            "channel_id": world["cid2"],
            "session": _session(world),
        },
        headers=_h(world["tok"]),
    )
    assert r.status_code == 200, r.text
    _ingest(
        db,
        world,
        [
            _event("c1", "1", now, is_me=True),
            _event("c3", "1", now),
            _event("c4", "1", now),
            _event("c2", "1", now),
        ],
        clock,
    )
    rows = _msg_signals(db)
    # только подписке на беседу c2 (строка с chan_sub_id: project_sub_id пуст)
    assert [(r[0], r[1]) for r in rows] == [(None, world["cid2"])]


async def test_answered_skipped_and_cards_not_signalled_in_messages_mode(client, db, world, procs, clock):
    sub_id = (await _subscribe(client, world, "messages"))["sub"]["id"]
    now = int(clock() * 1000)
    _ingest(db, world, [_event("c1", "1", now), _event("c1", "2", now + 1, is_me=True)], clock)
    await _card(client, world, world["cid"], [_msg_id(db, world["cid"], "1")])
    assert _card_signals(db) == []
    r = await _wait(client, world, sub_id)
    assert r.json()["signals"] == []
    assert _msg_signals(db) == [(sub_id, world["cid"], 1, "skipped", "answered")]


async def test_mode_switch_in_place_and_cards_mode_as_13_1(client, db, world, procs, clock):
    first = await _subscribe(client, world)
    sub_id = first["sub"]["id"]
    assert first["sub"]["mode"] == "cards"
    now = int(clock() * 1000)
    _ingest(db, world, [_event("c1", "1", now)], clock)
    assert _msg_signals(db) == []  # режим `cards` — сигналов сообщений нет
    item = await _card(client, world, world["cid"], [_msg_id(db, world["cid"], "1")])
    assert _card_signals(db) == [(sub_id, item, "pending")]
    switched = await _subscribe(client, world, "messages")
    assert (
        switched["created"] is False
        and switched["sub"]["id"] == sub_id
        and switched["sub"]["mode"] == "messages"
    )
    assert _card_signals(db) == [(sub_id, item, "ended")]
    _ingest(db, world, [_event("c1", "2", now + 1)], clock)
    assert _msg_signals(db) == [(sub_id, world["cid"], 1, "pending", None)]
    # без `mode` — режим своей живой не меняется
    assert (await _subscribe(client, world))["sub"]["mode"] == "messages"
    back = await _subscribe(client, world, "cards")
    assert back["sub"]["id"] == sub_id and back["sub"]["mode"] == "cards"
    assert _msg_signals(db) == [(sub_id, world["cid"], 1, "ended", None)]
    with db.reader() as conn:
        modes = [
            json.loads(d)
            for (d,) in conn.execute(
                "SELECT detail_json FROM main.audit WHERE action = 'sub_mode' ORDER BY id"
            )
        ]
    assert modes == [{"sub_id": sub_id, "mode": "messages"}, {"sub_id": sub_id, "mode": "cards"}]
    listed = (await client.get("/api/v1/subs", headers=_h(world["tok"]))).json()["items"]
    assert [row["mode"] for row in listed] == ["cards"]


@pytest.mark.parametrize(("mode", "second_after_6s"), [("messages", True), ("cards", False)])
async def test_debounce_messages_5s_cards_60s(client, db, world, procs, clock, mode, second_after_6s):
    """Стенд Ф14-5: режим `messages` — дебаунс 5 с (как у бесед), `cards` — 60 с (13-1). Две выдачи с
    интервалом 6 с: в `messages` обе сразу, в `cards` вторая — только через 60 с после первой."""
    sub_id = (await _subscribe(client, world, mode))["sub"]["id"]

    async def _new_message(ext_id):
        now = int(clock() * 1000)
        _ingest(db, world, [_event("c1", ext_id, now)], clock)
        if mode == "cards":
            await _card(client, world, world["cid"], [_msg_id(db, world["cid"], ext_id)])

    await _new_message("1")
    assert len((await _wait(client, world, sub_id)).json()["signals"]) == 1
    clock.advance_ms(6_000)
    await _new_message("2")
    second = (await _wait(client, world, sub_id)).json()["signals"]
    assert (len(second) == 1) is second_after_6s
    if not second_after_6s:
        clock.advance_ms(54_000)
        assert len((await _wait(client, world, sub_id)).json()["signals"]) == 1


async def test_bad_mode_422(client, db, world, procs):
    r = await client.post(
        "/api/v1/subs", json={"session": _session(world), "mode": "all"}, headers=_h(world["tok"])
    )
    assert r.status_code == 422


# --- миграция 0025 ---------------------------------------------------------------------------------------


def test_migration_0025_old_subs_are_cards(tmp_path, monkeypatch):
    original = db_module._migrations
    monkeypatch.setattr(
        db_module, "_migrations", lambda *, main: {v: f for v, f in original(main=main).items() if v < 25}
    )
    old = Db(tmp_path)
    old.start()
    try:
        old.write_sync(
            lambda conn: conn.execute(
                "INSERT INTO main.session_sub (project_id, api_token_id, session_pid, session_proc_start, "
                "session_id, session_cwd, created_ts) VALUES (1, 1, 1, 1, 's', '/', 0)"
            )
        )
        with old.reader() as conn:
            assert conn.execute("PRAGMA main.user_version").fetchone()[0] == 24
    finally:
        old.close()
    # Версия 25 ровно (Ф14-6 добавил 0026_shared — она этой проверке не нужна).
    monkeypatch.setattr(
        db_module, "_migrations", lambda *, main: {v: f for v, f in original(main=main).items() if v <= 25}
    )
    new = Db(tmp_path)
    new.start()
    try:
        with new.reader() as conn:
            assert conn.execute("PRAGMA main.user_version").fetchone()[0] == 25
            assert conn.execute("PRAGMA work.user_version").fetchone()[0] == 24
            assert conn.execute("SELECT mode FROM main.session_sub").fetchall() == [("cards",)]
            for table in ("session_chan_sub", "session_chan_signal"):
                for schema, expected in (("main", 1), ("work", 0)):
                    count = conn.execute(
                        f"SELECT COUNT(*) FROM {schema}.sqlite_master WHERE type='table' AND name = ?",
                        (table,),
                    ).fetchone()[0]
                    assert count == expected

        def _signal(conn, chan, project, status="pending", channel=1):
            conn.execute(
                "INSERT INTO main.session_chan_signal (chan_sub_id, project_sub_id, channel_id, "
                "first_message_id, last_message_id, msg_count, status, created_ts, updated_ts) "
                "VALUES (?, ?, ?, 1, 1, 1, ?, 0, 0)",
                (chan, project, channel, status),
            )

        with pytest.raises(Exception, match="CHECK"):
            new.write_sync(lambda conn: _signal(conn, None, None))
        with pytest.raises(Exception, match="CHECK"):
            new.write_sync(lambda conn: _signal(conn, 1, 1))
        new.write_sync(lambda conn: _signal(conn, 1, None))
        with pytest.raises(Exception, match="UNIQUE"):
            new.write_sync(lambda conn: _signal(conn, 1, None))
        new.write_sync(lambda conn: _signal(conn, None, 1))
        new.write_sync(lambda conn: _signal(conn, None, 1, channel=2))
        with pytest.raises(Exception, match="UNIQUE"):
            new.write_sync(lambda conn: _signal(conn, None, 1))
        with pytest.raises(Exception, match="CHECK"):
            new.write_sync(lambda conn: conn.execute("UPDATE main.session_sub SET mode = 'all'"))

        def _chan_sub(conn, ended):
            conn.execute(
                "INSERT INTO main.session_chan_sub (channel_id, api_token_id, session_pid, "
                "session_proc_start, session_id, session_cwd, created_ts, ended_ts) "
                "VALUES (7, 1, 1, 1, 's', '/', 0, ?)",
                (ended,),
            )

        new.write_sync(lambda conn: _chan_sub(conn, 5))
        new.write_sync(lambda conn: _chan_sub(conn, None))
        with pytest.raises(Exception, match="UNIQUE"):
            new.write_sync(lambda conn: _chan_sub(conn, None))
    finally:
        new.close()
