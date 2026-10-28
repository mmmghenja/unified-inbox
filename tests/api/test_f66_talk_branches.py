"""Ф66-2 (docs/architecture.md и D): разговор владельца с агентом по веткам сессий.

- ветка новой реплики по порядку A.2: явный `session_id` -> ветка `reply_to` -> сессия карточки `ref` ->
  «Общее»; несуществующая сессия и сессия вне скоупа токена — 422; закрытая — можно;
- лента ветки (`?session_id=<id>|general`), без параметра — вся лента; `talk/open` несёт `session_id`;
- `GET /owner_channel/talk/branches`: «Общее» всегда, заголовок «<проект> · <роль>», непрочитанное сверху;
  `POST /owner_channel/talk/read` по ветке;
- `GET /owner_channel/sessions/{id}/context` — то же, что по `ref` (Ф68-1); кука и агент — да, прочие 403;
- пересказ ветки: читает кука и агент, пишет только агент; `upto_id` не назад и не дальше последней реплики;
- ответ из бота ложится в ветку вопроса;
- миграция 0098 и разовая простановка веток старым репликам по `ref`.

Мир и помощники — test_hub_sessions.py, test_f56_queue.py, test_f68_card_session.py; хост поддельный."""

import pytest

from hub.core import db as db_module
from hub.core import owner_talk
from hub.core.db import Db

from . import test_hub_sessions as base
from .test_f56_queue import _agent, _bot
from .test_f68_card_session import _card, _ctx, _events
from .test_hub_sessions import _h, _mk, _token

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"


async def _say(client, body, *, token=None, ui=None, expect=200) -> dict:
    if ui is not None:
        r = await ui.post(f"{API}/owner_channel/talk", body)
    else:
        r = await client.post(f"{API}/owner_channel/talk", json=body, headers=_h(token))
    assert r.status_code == expect, r.text
    return r.json()


async def _get(client, path, *, token=None, ui=None, expect=200, **params):
    if ui is not None:
        r = await ui.get(f"{API}{path}", params=params or None)
    else:
        r = await client.get(f"{API}{path}", params=params, headers=_h(token))
    assert r.status_code == expect, (path, r.text)
    return r.json()


async def _put_summary(client, branch, body, *, token=None, ui=None, expect=200):
    path = f"{API}/owner_channel/talk/branches/{branch}/summary"
    if ui is not None:
        r = await ui.put(path, body)
    else:
        r = await client.put(path, json=body, headers=_h(token))
    assert r.status_code == expect, r.text
    return r.json()


def _project_name(db, project_id) -> str:
    with db.reader() as conn:
        return conn.execute("SELECT name FROM main.project WHERE id = ?", (project_id,)).fetchone()[0]


# --- ветка новой реплики (A.2) -----


async def test_post_picks_branch_in_order(client, ui, db, world):
    w = world
    agent, _ = _agent(db)
    sid, ref = await _card(client, ui, w)
    other = (await _mk(ui, w, role="тестер", project="p4"))["id"]

    by_ref = await _say(client, {"text": "по карточке", "ref": ref}, ui=ui)
    assert by_ref["session_id"] == sid  # сессия карточки
    reply = await _say(client, {"text": "понял", "reply_to": by_ref["id"]}, token=agent)
    assert reply["session_id"] == sid and reply["ref"] is None  # ветка реплики, на которую ответ
    plain = await _say(client, {"text": "просто так"}, ui=ui)
    assert plain["session_id"] is None  # «Общее»
    # `reply_to` раньше `ref`: ответ на реплику «Общего» по карточке — в «Общем».
    mixed = await _say(client, {"text": "x", "reply_to": plain["id"], "ref": ref}, token=agent)
    assert mixed["session_id"] is None
    # Явный `session_id` — раньше всего; `null` — «Общее» даже с `ref` карточки.
    explicit = await _say(client, {"text": "в ветку", "session_id": other, "reply_to": by_ref["id"]}, ui=ui)
    assert explicit["session_id"] == other
    forced = await _say(client, {"text": "в общее", "ref": ref, "session_id": None}, ui=ui)
    assert forced["session_id"] is None
    # `ref` не карточки (чужая форма, нет такой) — «Общее».
    assert (await _say(client, {"text": "y", "ref": "req:999999"}, ui=ui))["session_id"] is None
    assert (await _say(client, {"text": "y", "ref": "talk:5"}, ui=ui))["session_id"] is None


async def test_post_session_id_validation(client, ui, db, world):
    w = world
    sid = (await _mk(ui, w, role="тестер"))["id"]
    await _say(client, {"text": "x", "session_id": 999_999}, ui=ui, expect=422)
    await _say(client, {"text": "x", "session_id": "1"}, ui=ui, expect=422)
    await _say(client, {"text": "x", "session_id": True}, ui=ui, expect=422)
    await _say(client, {"text": "x", "session_id": 0}, ui=ui, expect=422)
    # Агент со скоупом другого проекта: сессия p3 для него «чужая» — 422.
    scoped, _ = _agent(db, project_ids=[w["p4"]])
    await _say(client, {"text": "x", "session_id": sid}, token=scoped, expect=422)
    # Закрытая сессия — можно, ветка остаётся.
    db.write_sync(lambda conn: conn.execute("UPDATE main.hub_session SET closed_ts = 1 WHERE id = ?", (sid,)))
    assert (await _say(client, {"text": "x", "session_id": sid}, ui=ui))["session_id"] == sid
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.owner_talk").fetchone()[0] == 1


async def test_bot_reply_lands_in_question_branch(client, ui, db, world):
    w = world
    agent, _ = _agent(db)
    bot, _ = _bot(db)
    sid, ref = await _card(client, ui, w)
    question = await _say(client, {"text": "какой код?", "ref": ref, "ask": True}, token=agent)
    assert question["session_id"] == sid
    r = await client.post(
        f"{API}/owner_channel/talk/bot-reply",
        json={"reply_to": question["id"], "text": "1234", "tg_message_id": 7},
        headers=_h(bot),
    )
    assert r.status_code == 200, r.text
    assert r.json()["session_id"] == sid


# --- лента ветки, open, ветки, прочтение (A.3) -----


async def test_feed_by_branch_open_and_branches(client, ui, db, world, clock):
    w = world
    agent, _ = _agent(db)
    s1 = (await _mk(ui, w, role="программист"))["id"]
    s2 = (await _mk(ui, w, role="тестер", project="p4"))["id"]
    g1 = await _say(client, {"text": "общее 1"}, ui=ui)
    a1 = await _say(client, {"text": "в s1", "session_id": s1}, ui=ui)
    clock.advance_ms(1000)
    b1 = await _say(client, {"text": "ответ в s1", "reply_to": a1["id"]}, token=agent)
    clock.advance_ms(1000)
    a2 = await _say(client, {"text": "в s2", "session_id": s2}, ui=ui)

    whole = await _get(client, "/owner_channel/talk", ui=ui)
    assert [m["id"] for m in whole["items"]] == [g1["id"], a1["id"], b1["id"], a2["id"]]
    only1 = await _get(client, "/owner_channel/talk", token=agent, session_id=str(s1))
    assert [m["id"] for m in only1["items"]] == [a1["id"], b1["id"]]
    assert all(m["session_id"] == s1 for m in only1["items"])
    general = await _get(client, "/owner_channel/talk", ui=ui, session_id="general")
    assert [m["id"] for m in general["items"]] == [g1["id"]]
    await _get(client, "/owner_channel/talk", ui=ui, session_id="abc", expect=422)
    await _get(client, "/owner_channel/talk", ui=ui, session_id="999999", expect=404)
    scoped, _ = _agent(db, project_ids=[w["p4"]])
    await _get(client, "/owner_channel/talk", token=scoped, session_id=str(s1), expect=404)

    # `talk/open` несёт ветку.
    opened = {
        m["id"]: m["session_id"] for m in (await _get(client, "/owner_channel/talk/open", token=agent))["items"]
    }
    assert opened == {g1["id"]: None, a1["id"]: s1, a2["id"]: s2}

    # Ветки: «Общее» всегда; с непрочитанным (ответ агента в s1) — сверху; дальше по `last_ts`.
    items = (await _get(client, "/owner_channel/talk/branches", ui=ui))["items"]
    assert [b["session_id"] for b in items] == [s1, s2, None]
    first = items[0]
    assert first == {
        "session_id": s1,
        "title": f"{_project_name(db, w['p3'])} · программист",
        "project_id": w["p3"],
        "role": "программист",
        "last_id": b1["id"],
        "last_ts": b1["ts"],
        "unread": 1,
    }
    assert items[2] == {
        "session_id": None,
        "title": "Общее",
        "project_id": None,
        "role": None,
        "last_id": g1["id"],
        "last_ts": g1["ts"],
        "unread": 0,
    }
    # Агенту со скоупом p4 — только его ветки и «Общее».
    assert [
        b["session_id"] for b in (await _get(client, "/owner_channel/talk/branches", token=scoped))["items"]
    ] == [
        s2,
        None,
    ]
    # Бот и прочий bearer — 403.
    bot, _ = _bot(db)
    other, _ = _token(db)
    for token in (bot, other):
        await _get(client, "/owner_channel/talk/branches", token=token, expect=403)

    # Прочтение по ветке: «Общее» не трогает ответ в s1.
    g_reply = await _say(client, {"text": "ответ в общем", "reply_to": g1["id"]}, token=agent)
    assert g_reply["session_id"] is None
    r = await ui.post(f"{API}/owner_channel/talk/read", {"upto": g_reply["id"], "session_id": None})
    assert r.status_code == 200 and r.json() == {"read": 1}
    with db.reader() as conn:
        assert (
            conn.execute("SELECT read_ts FROM main.owner_talk WHERE id = ?", (b1["id"],)).fetchone()[0]
            is None
        )
    r = await ui.post(f"{API}/owner_channel/talk/read", {"upto": g_reply["id"], "session_id": s1})
    assert r.json() == {"read": 1}
    assert all(b["unread"] == 0 for b in (await _get(client, "/owner_channel/talk/branches", ui=ui))["items"])


async def test_closed_session_branch_hidden_and_not_counted(client, ui, db, world):
    """Владелец 16.10: после переноса старых реплик в ветки всплыли давно закрытые роли — ветки закрытых сессий
    в списке не показываются, их непрочитанное в `talk_unread` не входит."""
    w = world
    agent, _ = _agent(db)
    live = (await _mk(ui, w, role="программист"))["id"]
    gone = (await _mk(ui, w, role="тестер"))["id"]
    await _say(client, {"text": "в live", "session_id": live}, token=agent)
    await _say(client, {"text": "в gone", "session_id": gone}, token=agent)
    with db.reader() as conn:
        assert owner_talk.counts(conn)["talk_unread"] == 2
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.hub_session SET closed_ts = 1 WHERE id = ?", (gone,))
    )
    items = (await _get(client, "/owner_channel/talk/branches", ui=ui))["items"]
    assert [b["session_id"] for b in items] == [live, None]
    with db.reader() as conn:
        assert owner_talk.counts(conn)["talk_unread"] == 1


async def test_empty_feed_still_has_general_branch(client, ui, db, world):
    items = (await _get(client, "/owner_channel/talk/branches", ui=ui))["items"]
    assert items == [
        {
            "session_id": None,
            "title": "Общее",
            "project_id": None,
            "role": None,
            "last_id": None,
            "last_ts": None,
            "unread": 0,
        }
    ]


# --- контекст сессии по id (A.3) -----


async def test_session_context_by_id_matches_ref_route(client, ui, db, world):
    w = world
    agent, _ = _agent(db)
    sid, ref = await _card(client, ui, w)
    await _events(
        client,
        w,
        sid,
        [
            {"kind": "tool", "tool": "Bash", "summary": "git status"},
            {"kind": "text", "summary": "Готово."},
        ],
    )
    by_ref = await _ctx(client, ref, token=agent)
    by_id = await _get(client, f"/owner_channel/sessions/{sid}/context", token=agent)
    assert by_id == by_ref and by_id["session"]["id"] == sid
    assert await _get(client, f"/owner_channel/sessions/{sid}/context", ui=ui) == by_ref
    page = await _get(client, f"/owner_channel/sessions/{sid}/context", token=agent, limit=1)
    assert len(page["events"]) == 1 and page["more_before"] == page["events"][0]["id"]
    bot, _ = _bot(db)
    other, _ = _token(db)
    for token in (bot, other):
        await _get(client, f"/owner_channel/sessions/{sid}/context", token=token, expect=403)
    await _get(client, "/owner_channel/sessions/999999/context", ui=ui, expect=404)
    scoped, _ = _agent(db, project_ids=[w["p4"]])
    await _get(client, f"/owner_channel/sessions/{sid}/context", token=scoped, expect=404)
    # Закрытая сессия — ветка остаётся, контекст читается.
    db.write_sync(lambda conn: conn.execute("UPDATE main.hub_session SET closed_ts = 1 WHERE id = ?", (sid,)))
    assert (await _get(client, f"/owner_channel/sessions/{sid}/context", token=agent))["session"]["id"] == sid


# --- пересказ ветки (A.3) -----


async def test_summary_read_write_and_limits(client, ui, db, world):
    w = world
    agent, _ = _agent(db)
    sid = (await _mk(ui, w, role="тестер"))["id"]
    empty = await _get(client, f"/owner_channel/talk/branches/{sid}/summary", ui=ui)
    assert empty == {"session_id": sid, "summary": "", "upto_id": 0, "updated_ts": None}
    ids = [(await _say(client, {"text": f"r{n}", "session_id": sid}, ui=ui))["id"] for n in range(3)]
    general = await _say(client, {"text": "в общем"}, ui=ui)

    saved = await _put_summary(client, sid, {"summary": "решили: A", "upto_id": ids[1]}, token=agent)
    assert saved["summary"] == "решили: A" and saved["upto_id"] == ids[1] and saved["updated_ts"]
    assert await _get(client, f"/owner_channel/talk/branches/{sid}/summary", token=agent) == saved
    # `upto_id` — не назад, не дальше последней реплики ветки (реплика «Общего» новее — не в счёт).
    await _put_summary(client, sid, {"summary": "x", "upto_id": ids[0]}, token=agent, expect=422)
    await _put_summary(client, sid, {"summary": "x", "upto_id": general["id"]}, token=agent, expect=422)
    await _put_summary(client, sid, {"summary": "я" * 6001, "upto_id": ids[2]}, token=agent, expect=422)
    same = await _put_summary(client, sid, {"summary": "решили: B", "upto_id": ids[1]}, token=agent)
    assert same["summary"] == "решили: B"
    # «Общее» — своя строка.
    g = await _put_summary(client, "general", {"summary": "общее", "upto_id": general["id"]}, token=agent)
    assert g["session_id"] is None and g["upto_id"] == general["id"]
    assert (await _get(client, f"/owner_channel/talk/branches/{sid}/summary", ui=ui))["summary"] == "решили: B"
    with db.reader() as conn:
        rows = conn.execute("SELECT session_id FROM main.owner_talk_branch ORDER BY session_id").fetchall()
    assert rows == [(0,), (sid,)]
    # Пишет только агент: кука, бот, прочий bearer — 403.
    r = await ui.put(f"{API}/owner_channel/talk/branches/{sid}/summary", {"summary": "x", "upto_id": ids[2]})
    assert r.status_code == 403
    bot, _ = _bot(db)
    other, _ = _token(db)
    for token in (bot, other):
        await _put_summary(client, sid, {"summary": "x", "upto_id": ids[2]}, token=token, expect=403)
        await _get(client, f"/owner_channel/talk/branches/{sid}/summary", token=token, expect=403)
    # Нет такой сессии — 404; мусор в адресе — 422.
    await _get(client, "/owner_channel/talk/branches/999999/summary", ui=ui, expect=404)
    await _put_summary(client, 999999, {"summary": "x", "upto_id": 0}, token=agent, expect=404)
    await _get(client, "/owner_channel/talk/branches/abc/summary", ui=ui, expect=422)


# --- миграция 0098 и простановка веток старым репликам (A.1) -----


@pytest.fixture
def stepped(monkeypatch):
    original = db_module._migrations

    def upto(version: int) -> None:
        monkeypatch.setattr(
            db_module,
            "_migrations",
            lambda *, main: {v: f for v, f in original(main=main).items() if v <= version},
        )

    return upto


def test_migration_0098_from_0097(tmp_path, stepped):
    stepped(97)
    old = Db(tmp_path)
    old.start()
    try:
        old.write_sync(
            lambda conn: conn.executemany(
                "INSERT INTO main.owner_talk (ts, author, text, ref) VALUES (1, 'owner_channel', ?, ?)",
                [("по карточке", "req:5"), ("просто", None)],
            )
        )
    finally:
        old.close()

    stepped(98)
    for _ in range(2):  # повторный запуск — без ошибок
        new = Db(tmp_path)
        new.start()
        try:
            with new.reader() as conn:
                assert conn.execute("PRAGMA main.user_version").fetchone()[0] == 98
                cols = [r[1] for r in conn.execute("PRAGMA main.table_info(owner_talk)")]
                indexes = {r[1] for r in conn.execute("PRAGMA main.index_list(owner_talk)")}
                branch_cols = [r[1] for r in conn.execute("PRAGMA main.table_info(owner_talk_branch)")]
                rows = conn.execute("SELECT session_id FROM main.owner_talk ORDER BY id").fetchall()
                key = conn.execute("SELECT 1 FROM main.meta WHERE key = 'f66_talk_branches'").fetchone()
                work_tables = {
                    r[0] for r in conn.execute("SELECT name FROM work.sqlite_master WHERE type = 'table'")
                }
            assert cols[-1] == "session_id" and "idx_owner_talk_branch" in indexes
            assert branch_cols == ["session_id", "summary", "summary_upto_id", "updated_ts"]
            assert rows == [(None,), (None,)]  # карточки `req:5` нет — «Общее», не ошибка
            assert key is None  # разовый шаг снял ключ
            assert "owner_talk_branch" not in work_tables
        finally:
            new.close()


async def test_backfill_by_ref_and_reply_to(client, ui, db, world):
    """Разовый шаг после 0098: старые реплики без ветки получают её по `ref` (и решённой карточки), ответы —
    по `reply_to`; прочие остаются в «Общем»."""
    w = world
    agent, _ = _agent(db)
    sid, ref = await _card(client, ui, w)
    q = await _say(client, {"text": "по карточке", "ref": ref}, ui=ui)
    a = await _say(client, {"text": "ответ", "reply_to": q["id"]}, token=agent)
    g = await _say(client, {"text": "просто"}, ui=ui)
    stray = await _say(client, {"text": "мимо", "ref": "req:999999"}, ui=ui)

    def _reset(conn):
        conn.execute("UPDATE main.owner_talk SET session_id = NULL")
        conn.execute("INSERT OR REPLACE INTO main.meta (key, value) VALUES ('f66_talk_branches', '1')")

    db.write_sync(_reset)
    db._f66_talk_branches()
    with db.reader() as conn:
        got = dict(conn.execute("SELECT id, session_id FROM main.owner_talk").fetchall())
        key = conn.execute("SELECT 1 FROM main.meta WHERE key = 'f66_talk_branches'").fetchone()
    assert got == {q["id"]: sid, a["id"]: sid, g["id"]: None, stray["id"]: None}
    assert key is None


def test_backfill_failure_keeps_general(tmp_path, monkeypatch):
    """Сбой расчёта ветки — реплики остаются в «Общем», ключ снят, база стартует."""
    db = Db(tmp_path)
    db.start()
    try:
        db.write_sync(
            lambda conn: (
                conn.execute(
                    "INSERT INTO main.owner_talk (ts, author, text, ref) VALUES (1, 'owner_channel', 'x', 'req:1')"
                ),
                conn.execute(
                    "INSERT OR REPLACE INTO main.meta (key, value) VALUES ('f66_talk_branches', '1')"
                ),
            )
        )

        def _boom(conn, *, now_ms):
            conn.execute("UPDATE main.owner_talk SET session_id = 5")
            raise RuntimeError("сбой")

        monkeypatch.setattr(owner_talk, "backfill", _boom)
        db._f66_talk_branches()
        with db.reader() as conn:
            assert conn.execute("SELECT session_id FROM main.owner_talk").fetchall() == [(None,)]
            assert conn.execute("SELECT 1 FROM main.meta WHERE key = 'f66_talk_branches'").fetchone() is None
    finally:
        db.close()
