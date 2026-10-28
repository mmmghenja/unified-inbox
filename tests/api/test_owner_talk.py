"""Ф58-1 (docs/architecture.md.1–A.4): разговор владельца со своим агентом вне карточек.

Права по таблице A.2 (кука / агент `human` / бот / прочий bearer), запись и лента, `before`, `open`/`handled`
(повтор, реплика агента -> 409), `read` -> `talk_unread`, `seq` и пробуждение `/owner_channel/changes` новой репликой
обеих сторон (в том числе когда числа не изменились), 422, аудит без текста, миграция с версии 0088."""

import asyncio
import time

import pytest

from hub.core import db as db_module
from hub.core import owner_talk
from hub.core.db import Db

from . import test_hub_sessions as base
from .test_f56_queue import _agent, _bot, _queue
from .test_hub_sessions import _audits, _h, _token

procs = base.procs
ui = base.ui

API = "/api/v1"
SECRET = "ТЕКСТ-РЕПЛИКИ-НЕ-В-АУДИТ"


async def _say(client, body, *, token=None, ui=None, expect=200) -> dict:
    if ui is not None:
        r = await ui.post(f"{API}/owner_channel/talk", body)
    else:
        r = await client.post(f"{API}/owner_channel/talk", json=body, headers=_h(token))
    assert r.status_code == expect, r.text
    return r.json()


async def _feed(client, *, token=None, ui=None, query="") -> dict:
    path = f"{API}/owner_channel/talk{query}"
    r = await (ui.get(path) if ui is not None else client.get(path, headers=_h(token)))
    assert r.status_code == 200, r.text
    return r.json()


async def _open(client, token) -> list[dict]:
    r = await client.get(f"{API}/owner_channel/talk/open", headers=_h(token))
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _seq(db) -> int:
    with db.reader() as conn:
        return conn.execute("SELECT value FROM main.seq_counter WHERE id = 1").fetchone()[0]


async def test_rights_by_table(client, ui, db):
    agent, _ = _agent(db)
    bot, _ = _bot(db)
    other, _ = _token(db)  # bearer сессии (read, draft, attach)
    mine = await _say(client, {"text": "привет"}, ui=ui)
    theirs = await _say(client, {"text": "здравствуй"}, token=agent)
    assert mine["author"] == "owner_channel" and theirs["author"] == "agent"
    assert [m["id"] for m in (await _feed(client, ui=ui))["items"]] == [mine["id"], theirs["id"]]
    assert [m["id"] for m in (await _feed(client, token=agent))["items"]] == [mine["id"], theirs["id"]]

    for token in (bot, other):
        for method, path, body in (
            ("get", "/owner_channel/talk", None),
            ("post", "/owner_channel/talk", {"text": "x"}),
            ("get", "/owner_channel/talk/open", None),
            ("post", f"/owner_channel/talk/{mine['id']}/handled", {}),
            ("post", "/owner_channel/talk/read", {"upto": theirs["id"]}),
        ):
            kw = {} if body is None else {"json": body}
            r = await client.request(method, f"{API}{path}", headers=_h(token), **kw)
            assert r.status_code == 403, (path, r.status_code)
    # Кука: `open` и `handled` — нет; агент: `read` — нет.
    assert (await ui.get(f"{API}/owner_channel/talk/open")).status_code == 403
    assert (await ui.post(f"{API}/owner_channel/talk/{mine['id']}/handled")).status_code == 403
    r = await client.post(f"{API}/owner_channel/talk/read", json={"upto": theirs["id"]}, headers=_h(agent))
    assert r.status_code == 403
    assert (await client.get(f"{API}/owner_channel/talk", headers={})).status_code in (401, 403)
    # Ничего из отказанного не записалось.
    with db.reader() as conn:
        rows = conn.execute("SELECT id, handled_ts, read_ts FROM main.owner_talk ORDER BY id").fetchall()
    assert rows == [(mine["id"], None, None), (theirs["id"], None, None)]


async def test_write_shape_feed_and_before(client, ui, db, clock):
    agent, _ = _agent(db)
    now = int(clock() * 1000)
    first = await _say(client, {"text": "  по карточке  ", "ref": "req:5"}, ui=ui)
    assert first == {
        "id": first["id"],
        "ts": now,
        "author": "owner_channel",
        "text": "по карточке",
        "ref": "req:5",
        "reply_to": None,
        "handled_ts": None,
        "read_ts": None,
        # Ф61-1: вопрос агента и откуда реплика владельца.
        "ask": False,
        "ask_until_ts": None,
        "closed_ts": None,
        "closed_outcome": None,
        "via": None,
        # Ф66-2: ветка (карточки `req:5` нет — «Общее»).
        "session_id": None,
        # Строка хода агента (23.10).
        "progress": None,
        "progress_ts": None,
        "files": [],
    }
    reply = await _say(client, {"text": "понял", "reply_to": first["id"]}, token=agent)
    assert reply["reply_to"] == first["id"] and reply["ref"] is None
    ids = [first["id"], reply["id"]]
    for n in range(3):
        ids.append((await _say(client, {"text": f"ещё {n}"}, ui=ui))["id"])

    page = await _feed(client, ui=ui, query="?limit=2")
    assert [m["id"] for m in page["items"]] == ids[-2:] and page["has_more"] is True
    page = await _feed(client, ui=ui, query=f"?limit=2&before={ids[-2]}")
    assert [m["id"] for m in page["items"]] == ids[1:3] and page["has_more"] is True
    page = await _feed(client, ui=ui, query=f"?limit=2&before={ids[1]}")
    assert [m["id"] for m in page["items"]] == ids[:1] and page["has_more"] is False
    assert (await ui.get(f"{API}/owner_channel/talk?limit=201")).status_code == 422
    assert (await ui.get(f"{API}/owner_channel/talk?before=0")).status_code == 422


async def test_validation_422(client, ui, db):
    agent, _ = _agent(db)
    for body in (
        {"text": ""},
        {"text": "   \n "},
        {"text": "я" * (owner_talk.TEXT_MAX + 1)},
        {"text": "x", "reply_to": 999_999},
        {"text": "x", "ref": "r" * (owner_talk.REF_MAX + 1)},
        {"text": "x", "ref": "  "},
        {"text": "x", "extra": 1},
        {},
    ):
        await _say(client, body, ui=ui, expect=422)
        await _say(client, body, token=agent, expect=422)
    assert (await _say(client, {"text": "я" * owner_talk.TEXT_MAX}, ui=ui))["author"] == "owner_channel"
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.owner_talk").fetchone()[0] == 1


async def test_open_handled_and_counts(client, ui, db, clock):
    agent, _ = _agent(db)
    one = await _say(client, {"text": "раз"}, ui=ui)
    two = await _say(client, {"text": "два"}, ui=ui)
    own = await _say(client, {"text": "моё"}, token=agent)
    assert [m["id"] for m in await _open(client, agent)] == [one["id"], two["id"]]
    agent_block = (await _queue(client, ui=ui))["agent"]
    assert (agent_block["talk_open"], agent_block["talk_unread"]) == (2, 1)
    assert agent_block["talk_last_id"] == own["id"]

    clock.advance_ms(1_000)
    seq = _seq(db)
    path = f"{API}/owner_channel/talk/{one['id']}/handled"
    r = await client.post(path, json={}, headers=_h(agent))
    assert r.status_code == 200 and r.json()["handled_ts"] == int(clock() * 1000)
    assert _seq(db) > seq
    clock.advance_ms(1_000)
    seq = _seq(db)
    again = await client.post(path, json={}, headers=_h(agent))
    assert again.status_code == 200 and again.json() == r.json()  # повтор — без изменений
    assert _seq(db) == seq
    r = await client.post(f"{API}/owner_channel/talk/{own['id']}/handled", json={}, headers=_h(agent))
    assert r.status_code == 409
    r = await client.post(f"{API}/owner_channel/talk/999999/handled", json={}, headers=_h(agent))
    assert r.status_code == 404
    assert [m["id"] for m in await _open(client, agent)] == [two["id"]]
    assert (await _queue(client, agent))["agent"]["talk_open"] == 1


async def test_read_marks_agent_replies_up_to(client, ui, db, clock):
    agent, _ = _agent(db)
    a1 = await _say(client, {"text": "а1"}, token=agent)
    z = await _say(client, {"text": "ж"}, ui=ui)
    a2 = await _say(client, {"text": "а2"}, token=agent)
    assert (await _queue(client, ui=ui))["agent"]["talk_unread"] == 2

    seq = _seq(db)
    r = await ui.post(f"{API}/owner_channel/talk/read", {"upto": z["id"]})
    assert r.status_code == 200 and r.json() == {"read": 1}
    assert _seq(db) > seq
    assert (await _queue(client, ui=ui))["agent"]["talk_unread"] == 1
    seq = _seq(db)
    r = await ui.post(f"{API}/owner_channel/talk/read", {"upto": z["id"]})
    assert r.json() == {"read": 0} and _seq(db) == seq  # нечего отмечать — `seq` стоит
    r = await ui.post(f"{API}/owner_channel/talk/read", {"upto": a2["id"]})
    assert r.json() == {"read": 1}
    rows = {m["id"]: m for m in (await _feed(client, ui=ui))["items"]}
    assert rows[a1["id"]]["read_ts"] == int(clock() * 1000) and rows[z["id"]]["read_ts"] is None
    assert (await _queue(client, ui=ui))["agent"]["talk_unread"] == 0
    assert (await ui.post(f"{API}/owner_channel/talk/read", {})).status_code == 422


async def test_audit_without_text(client, ui, db):
    agent, agent_id = _agent(db)
    mine = await _say(client, {"text": SECRET}, ui=ui)
    theirs = await _say(client, {"text": SECRET + "!"}, token=agent)
    got = _audits(db, "owner_talk")
    assert got == [
        {"id": mine["id"], "author": "owner_channel", "len": len(SECRET)},
        {"id": theirs["id"], "author": "agent", "len": len(SECRET) + 1},
    ]
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT actor, api_token_id, detail_json FROM main.audit WHERE action = 'owner_talk' ORDER BY id"
        ).fetchall()
    assert rows[1][0] == "background" and rows[1][1] == agent_id
    assert all(SECRET not in row[2] for row in rows)


async def _wakes(poll, write) -> tuple[dict, float]:
    """Длинный опрос `poll` и запись `write` через 0.2 с: (снимок, сколько ждал опрос)."""

    async def later():
        await asyncio.sleep(0.2)
        await write()

    began = time.monotonic()
    r, _ = await asyncio.gather(poll(), later())
    took = time.monotonic() - began
    assert r.status_code == 200, r.text
    return r.json(), took


async def test_changes_wake_on_reply_of_either_side(client, ui, db):
    agent, _ = _agent(db)
    # Агент ждёт — пишет владелец.
    since = (await _queue(client, agent))["seq"]
    snap, took = await _wakes(
        lambda: client.get(f"{API}/owner_channel/changes?since={since}&wait=3", headers=_h(agent)),
        lambda: _say(client, {"text": "вопрос"}, ui=ui),
    )
    assert snap["agent"]["talk_open"] == 1 and took < 1.5, took
    # Владелец ждёт — пишет агент.
    since = (await _queue(client, ui=ui))["seq"]
    snap, took = await _wakes(
        lambda: ui.get(f"{API}/owner_channel/changes?since={since}&wait=3"),
        lambda: _say(client, {"text": "ответ"}, token=agent),
    )
    assert snap["agent"]["talk_unread"] == 1 and took < 1.5, took


async def test_changes_wake_on_new_reply_with_same_counts(client, ui, db, clock):
    """Отпечаток учитывает id последней реплики: реплика агента, прочитанная владельцем той же записью, чисел не
    меняет — опрос всё равно просыпается."""
    agent, _ = _agent(db)
    since = (await _queue(client, ui=ui))["seq"]
    now = int(clock() * 1000)
    audit = {"actor": "background", "api_token_id": None, "client_name": None, "request_id": "t"}

    async def write():
        def fn(conn):
            row = owner_talk.post(conn, author="agent", text="тихо", now_ms=now, audit=audit)
            owner_talk.mark_read(conn, row["id"], now_ms=now)

        await db.write(fn)

    snap, took = await _wakes(lambda: ui.get(f"{API}/owner_channel/changes?since={since}&wait=3"), write)
    assert (snap["agent"]["talk_unread"], snap["agent"]["talk_open"]) == (0, 0)
    assert snap["agent"]["talk_last_id"] > 0 and took < 1.5, took


# --- миграция ---------------------------------------------------------------------------------------------


@pytest.fixture
def stepped(monkeypatch):
    """`upto(N)` — миграции только до версии N включительно."""
    original = db_module._migrations

    def upto(version: int) -> None:
        monkeypatch.setattr(
            db_module,
            "_migrations",
            lambda *, main: {v: f for v, f in original(main=main).items() if v <= version},
        )

    return upto


def test_migration_0089_from_0088(tmp_path, stepped):
    stepped(88)
    old = Db(tmp_path)
    old.start()
    try:
        with old.reader() as conn:
            assert conn.execute("PRAGMA main.user_version").fetchone()[0] == 88
            tables = {r[0] for r in conn.execute("SELECT name FROM main.sqlite_master WHERE type = 'table'")}
        assert "owner_talk" not in tables
    finally:
        old.close()

    stepped(89)
    for _ in range(2):  # повторный запуск — без ошибок
        new = Db(tmp_path)
        new.start()
        try:
            with new.reader() as conn:
                assert conn.execute("PRAGMA main.user_version").fetchone()[0] == 89
                cols = [r[1] for r in conn.execute("PRAGMA main.table_info(owner_talk)")]
                indexes = {r[1] for r in conn.execute("PRAGMA main.index_list(owner_talk)")}
                work_tables = {
                    r[0] for r in conn.execute("SELECT name FROM work.sqlite_master WHERE type = 'table'")
                }
            assert cols == ["id", "ts", "author", "text", "ref", "reply_to", "handled_ts", "read_ts"]
            assert "idx_owner_talk_open" in indexes
            assert "owner_talk" not in work_tables  # только основная база
            with pytest.raises(Exception, match="CHECK"):
                new.write_sync(
                    lambda conn: conn.execute(
                        "INSERT INTO main.owner_talk (ts, author, text) VALUES (1, 'bot', 'x')"
                    )
                )
        finally:
            new.close()
