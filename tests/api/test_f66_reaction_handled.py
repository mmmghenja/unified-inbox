"""Ф66-4 (docs/architecture.md): реакция на чужое сообщение = «разобрано до него».

- B: реакция владельца из хаба — беседа уходит из `GET /home/react`, карточка закрыта, `read_ack` в очереди;
  снятие, своё сообщение — ничего; новое после X остаётся непрочитанным.
- C: событие реакции из источника с новым `me: true` — то же, без `read_ack`; эхо реакции сессии и чужая
  не в счёт.
- D: реакция сессии — строка `channel_handled` с пометкой при включённом `session_handled`; иначе реакция
  ставится, отметки нет."""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import handled, security, subs
from hub.core import reactions as reactions_mod
from hub.core.db import db_of
from hub.core.ingest import apply_batch

from .conftest import (
    BASE_URL,
    csrf_headers,
    insert_channel,
    insert_message,
    insert_project,
    login_via_code,
)

ORIGIN = "http://127.0.0.1:8787"
PID, START, SID = 4242, 777_000, "sess-aaaaaaaa-1111"
SESSION = {"pid": PID, "proc_start": START, "session_id": SID}
MIN = 60_000
ROLE = "маркетолог"


def _now(clock) -> int:
    return int(clock() * 1000)


@pytest.fixture
def procs(monkeypatch):
    table = {PID: START}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


@pytest.fixture
def world(db, clock):
    project = insert_project(db, name="Umbrella")
    db.write_sync(lambda conn: reactions_mod.set_project_reactions(conn, project, sessions=True))
    db.write_sync(lambda conn: handled.set_project_enabled(conn, project, True))
    acc = db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, config_json) VALUES ('telegram', 'main', '{}')"
            ).lastrowid
        )
    )
    ch = insert_channel(db, acc, project_id=project, ext_id="dm1", kind="dm", title="Партнёр")
    now = _now(clock)
    first = insert_message(db, ch, ext_id="100", ts=now - 10 * MIN, text="первое")
    second = insert_message(db, ch, ext_id="101", ts=now - 5 * MIN, text="второе")
    mine = insert_message(db, ch, ext_id="102", ts=now - 4 * MIN, text="моё", is_me=1)
    card = db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.agent_item (channel_id, label, summary, message_ids_json, last_message_id, "
                "covered_seq, status, provider, model, created_ts, updated_ts) "
                "VALUES (?, 'reply', 'сводка', ?, ?, 1, 'new', 'p', 'm', 1, 1)",
                (ch, json.dumps([second]), second),
            ).lastrowid
        )
    )
    return {"p": project, "acc": acc, "ch": ch, "first": first, "second": second, "mine": mine, "card": card}


@pytest.fixture
async def ui(app, db, clock):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as c:
        cookie, _csrf = await login_via_code(c, db, clock)
        headers = csrf_headers(cookie)

        class _Ui:
            async def get(self, path, **kw):
                return await c.get(path, **kw)

            async def post(self, path, body):
                return await c.post(path, json=body, headers=headers)

        yield _Ui()


def _channel_state(db, w):
    with db.reader() as conn:
        card = conn.execute("SELECT status FROM main.agent_item WHERE id = ?", (w["card"],)).fetchone()[0]
        ack = conn.execute(
            f"SELECT message_id FROM {db_of(w['ch'])}.read_ack WHERE channel_id = ?",  # noqa: S608
            (w["ch"],),
        ).fetchone()
        handled_rows = conn.execute("SELECT note FROM main.channel_handled").fetchall()
    return card, ack, [r[0] for r in handled_rows]


async def _react_ids(ui) -> set[int]:
    body = (await ui.get("/api/v1/home/react")).json()
    items = body["items"] if isinstance(body, dict) else body
    return {i["channel"]["id"] for i in items if i.get("channel")}


async def _unread(ui, ch) -> int:
    return (await ui.get(f"/api/v1/channels/{ch}")).json()["unread"]


# --- B: владелец из хаба ---------------------------------------------------------------------


async def test_b_ui_reaction_marks_handled(ui, db, clock, world):
    w = world
    insert_message(db, w["ch"], ext_id="103", ts=_now(clock) - 3 * MIN, text="потом")
    assert w["ch"] in await _react_ids(ui)
    r = await ui.post(f"/api/v1/messages/{w['second']}/reactions", {"emoji": "👍", "on": True})
    assert r.status_code == 200 and r.json()["status"] == "queued", r.text
    card, ack, handled_rows = _channel_state(db, w)
    assert card == "done" and ack == (w["second"],) and handled_rows == []
    # сообщение 103 пришло позже второго — остаётся непрочитанным
    assert await _unread(ui, w["ch"]) == 1


async def test_b_last_message_leaves_react(ui, db, clock, world):
    w = world
    r = await ui.post(f"/api/v1/messages/{w['second']}/reactions", {"emoji": "👍", "on": True})
    assert r.status_code == 200
    assert w["ch"] not in await _react_ids(ui)
    assert await _unread(ui, w["ch"]) == 0


async def test_b_own_message_and_unreact_do_nothing(ui, db, clock, world):
    w = world
    r = await ui.post(f"/api/v1/messages/{w['mine']}/reactions", {"emoji": "👍", "on": True})
    assert r.status_code == 200
    card, ack, _ = _channel_state(db, w)
    assert card == "new" and ack is None
    assert await _unread(ui, w["ch"]) == 2
    # снятие реакции (операция off) ничего не отмечает
    await ui.post(f"/api/v1/messages/{w['second']}/reactions", {"emoji": "👍", "on": False})
    card, ack, _ = _channel_state(db, w)
    assert card == "new" and ack is None
    assert await _unread(ui, w["ch"]) == 2


# --- C: реакция из источника ---------------------------------------------------------------------


def _event(reactions, ext_id="101"):
    return {
        "type": "reaction",
        "channel": {"ext_id": "dm1", "kind": "dm"},
        "ext_id": ext_id,
        "reactions": reactions,
    }


def _ingest(db, clock, w, *events):
    return db.write_sync(
        lambda conn: apply_batch(
            conn, account_id=w["acc"], events=list(events), cursor=None, now_ms=_now(clock)
        )
    )


async def test_c_source_reaction_marks_handled_without_read_ack(ui, db, clock, world):
    w = world
    _ingest(db, clock, w, _event([{"emoji": "👍", "count": 1, "me": False}]))
    assert _channel_state(db, w)[0] == "new"  # чужая реакция — не наша
    _ingest(db, clock, w, _event([{"emoji": "👍", "count": 2, "me": True}]))
    card, ack, _ = _channel_state(db, w)
    assert card == "done" and ack is None
    assert await _unread(ui, w["ch"]) == 0


async def test_c_session_echo_is_not_owner(ui, db, clock, world):
    w = world
    db.write_sync(
        lambda conn: conn.execute(
            f"INSERT INTO {db_of(w['second'])}.reaction_by_session (message_id, emoji_key, token_id, ts) "  # noqa: S608
            "VALUES (?, ?, 1, 0)",
            (w["second"], reactions_mod._key("👍")),  # noqa: SLF001
        )
    )
    _ingest(db, clock, w, _event([{"emoji": "👍", "count": 1, "me": True}]))
    assert _channel_state(db, w)[0] == "new"
    assert await _unread(ui, w["ch"]) == 2


async def test_c_own_message_does_nothing(db, clock, world):
    w = world
    _ingest(db, clock, w, _event([{"emoji": "👍", "count": 1, "me": True}], ext_id="102"))
    assert _channel_state(db, w)[0] == "new"


# --- D: реакция сессии ---------------------------------------------------------------------


@pytest.fixture
def session(db, world, procs):
    created = db.write_sync(
        lambda conn: security.create_api_token(
            conn,
            name="mcp:all",
            kind="interactive",
            scopes=["read", "draft"],
            project_ids=None,
            expires_ts=None,
        )
    )
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.session_sub (project_id, api_token_id, session_pid, session_proc_start, "
            "session_id, session_cwd, created_ts, role, role_key) VALUES (?, ?, ?, ?, ?, '/tmp', 0, ?, ?)",
            (world["p"], created.token_id, PID, START, SID, ROLE, ROLE),
        )
    )
    return created.token


async def _sreact(client, token, message_id, emoji="👍"):
    return await client.post(
        f"/api/v1/messages/{message_id}/reactions",
        json={"emoji": emoji, "on": True, "session": SESSION},
        headers={"Authorization": f"Bearer {token}", "Origin": ORIGIN},
    )


async def test_d_session_reaction_marks_handled_with_note(client, ui, db, world, session):
    w = world
    r = await _sreact(client, session, w["second"])
    assert r.status_code == 200 and r.json()["status"] == "queued", r.text
    card, ack, notes = _channel_state(db, w)
    assert card == "done" and ack is None and notes == ["реакция 👍"]
    assert await _unread(ui, w["ch"]) == 0


async def test_d_switch_off_reaction_still_set(client, ui, db, world, session):
    w = world
    db.write_sync(lambda conn: handled.set_project_enabled(conn, w["p"], False))
    r = await _sreact(client, session, w["second"])
    assert r.status_code == 200 and r.json()["status"] == "queued"
    card, _ack, notes = _channel_state(db, w)
    assert card == "new" and notes == []
    assert await _unread(ui, w["ch"]) == 2


async def test_d_own_message_reaction_set_without_mark(client, db, world, session):
    w = world
    r = await _sreact(client, session, w["mine"])
    assert r.status_code == 200 and r.json()["status"] == "queued"
    card, _ack, notes = _channel_state(db, w)
    assert card == "new" and notes == []
