"""Ф94-1 (docs/architecture.md): агент владельца сообщает владельцу в бот без вопроса.

- `POST /owner_channel/errands/{id}/tell` — только агент; реплика ленты `tell=1`, `ref` и ветка — как у вопроса по
  поручению; повтор по `operation_key` — та же строка; нет поручения — 404, закрыто — 409, пустой — 422;
  текста в аудите нет; в ленте «Мой агент» — обычная реплика агента;
- `GET /owner_channel/talk/tells` — только бот, подрезка суток, `ref_title` карточки;
- миграция `0116_main.sql`.

Мир и помощники — test_hub_sessions.py, test_f56_queue.py, test_f65_errand_life.py,
test_f85_durable_errand.py."""

import pytest

from hub.core import db as db_module
from hub.core.db import Db

from . import test_hub_sessions as base
from .test_f56_queue import _answer, _bot, _permission, _queue, _refs
from .test_f65_errand_life import _errand_row, _setup, _take
from .test_f85_durable_errand import _ask, _talk_count
from .test_hub_sessions import _audits, _h, _now

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"
MIN = 60_000
DAY = 24 * 60 * MIN
SECRET = "пароль-Секрет-123"


async def _tell(client, token, errand_id, body, expect=200) -> dict:
    r = await client.post(f"{API}/owner_channel/errands/{errand_id}/tell", json=body, headers=_h(token))
    assert r.status_code == expect, r.text
    return r.json()


async def _tells(client, token, since_ms=0, expect=200):
    r = await client.get(f"{API}/owner_channel/talk/tells", params={"since_ms": since_ms}, headers=_h(token))
    assert r.status_code == expect, r.text
    return r.json()["items"] if expect == 200 else r.json()


def _tell_flag(db, talk_id) -> int:
    with db.reader() as conn:
        return conn.execute("SELECT tell FROM main.owner_talk WHERE id = ?", (talk_id,)).fetchone()[0]


async def test_tell_by_errand_idempotent_rights_and_audit(client, ui, db, world):
    w = world
    agent, sid = await _setup(client, ui, db, w)
    bot, _ = _bot(db)
    ref = f"req:{await _permission(client, w, sid)}"
    eid = (await _take(client, agent, ref))["errand"]["id"]
    q = await _ask(client, agent, eid, {"operation_key": "q-1", "text": "код?"})

    t = await _tell(client, agent, eid, {"operation_key": "pwd", "text": f"Пароль: {SECRET}"})
    assert t["author"] == "agent" and t["ask"] is False and t["ref"] == ref
    assert t["session_id"] == q["session_id"] and t["text"] == f"Пароль: {SECRET}"
    assert _tell_flag(db, t["id"]) == 1 and _tell_flag(db, q["id"]) == 0
    # Повтор с тем же ключом — та же строка; ключ вопроса и ключ сообщения не путаются.
    count = _talk_count(db)
    again = await _tell(client, agent, eid, {"operation_key": "pwd", "text": "другой"})
    assert again["id"] == t["id"] and again["text"] == t["text"] and _talk_count(db) == count
    other = await _tell(client, agent, eid, {"operation_key": "q-1", "text": "итог"})
    assert other["id"] not in (q["id"], t["id"])
    payload = _errand_row(db, eid)[1]
    assert payload["tell_ops"] == {"pwd": t["id"], "q-1": other["id"]}
    assert payload["ask_ops"] == {"q-1": q["id"]}

    # Пределы и права: пустой — 422, ключ — 422, кука и бот — 403, нет поручения — 404.
    await _tell(client, agent, eid, {"operation_key": "k", "text": " "}, expect=422)
    await _tell(client, agent, eid, {"operation_key": "", "text": "x"}, expect=422)
    await _tell(client, agent, eid, {"operation_key": "k", "text": "x" * 8001}, expect=422)
    await _tell(client, bot, eid, {"operation_key": "k", "text": "x"}, expect=403)
    r = await ui.post(f"{API}/owner_channel/errands/{eid}/tell", {"operation_key": "k", "text": "x"})
    assert r.status_code == 403
    await _tell(client, agent, 10**6, {"operation_key": "k", "text": "x"}, expect=404)

    # Аудит — без текста.
    rows = [d for d in _audits(db, "owner_talk") if d.get("tell")]
    assert [d["id"] for d in rows] == [t["id"], other["id"]]
    assert all(set(d) == {"id", "author", "len", "tell"} for d in rows)
    with db.reader() as conn:
        dump = " ".join(r[0] or "" for r in conn.execute("SELECT detail_json FROM main.audit").fetchall())
    assert SECRET not in dump

    # В ленте «Мой агент» — обычная реплика агента.
    r = await ui.get(f"{API}/owner_channel/talk")
    assert r.status_code == 200
    shown = {i["id"]: i for i in r.json()["items"]}[t["id"]]
    assert shown["author"] == "agent" and shown["text"] == f"Пароль: {SECRET}"

    # Бот видит сообщения (с названием карточки), вопросы — нет; вопрос в `asks` не мешается сообщением.
    card = _refs(await _queue(client, ui=ui))[ref]
    got = await _tells(client, bot)
    assert [i["id"] for i in got] == [t["id"], other["id"]]
    assert set(got[0]) == {"id", "ts", "text", "ref", "ref_title"} | {
        "errand_id",  # Ф96-1: поручение по карточке и руки
        "hands",
        "hands_ts",
        "handoff",
    }
    assert (got[0]["errand_id"], got[0]["hands"], got[0]["handoff"]) == (eid, "agent", False)
    assert got[0]["ref"] == ref and got[0]["ref_title"] and card["brief"] in got[0]["ref_title"]
    r = await client.get(f"{API}/owner_channel/talk/asks", params={"since_ms": 0}, headers=_h(bot))
    assert [a["id"] for a in r.json()["items"]] == [q["id"]]

    # Закрытое поручение — 409 (и повтор старого ключа тоже).
    await _answer(client, ref, {"action": "allow"}, token=agent)
    await _tell(client, agent, eid, {"operation_key": "pwd", "text": "x"}, expect=409)
    await _tell(client, agent, eid, {"operation_key": "new", "text": "x"}, expect=409)


async def test_tells_only_bot_and_clamped_to_a_day(client, ui, db, clock, world):
    w = world
    agent, sid = await _setup(client, ui, db, w)
    bot, _ = _bot(db)
    ref = f"req:{await _permission(client, w, sid)}"
    eid = (await _take(client, agent, ref))["errand"]["id"]
    old = await _tell(client, agent, eid, {"operation_key": "a", "text": "давнее"})
    clock.advance_ms(DAY + MIN)
    ref2 = f"req:{await _permission(client, w, sid)}"
    eid2 = (await _take(client, agent, ref2))["errand"]["id"]
    fresh = await _tell(client, agent, eid2, {"operation_key": "b", "text": "свежее"})
    assert old["id"] < fresh["id"]
    assert [i["id"] for i in await _tells(client, bot, since_ms=0)] == [fresh["id"]]  # 0 подрезан до суток
    assert await _tells(client, bot, since_ms=_now(clock) + 1) == []
    # Только бот: агент, кука — 403; без since_ms — 422.
    await _tells(client, agent, expect=403)
    assert (await ui.get(f"{API}/owner_channel/talk/tells?since_ms=0")).status_code == 403
    r = await client.get(f"{API}/owner_channel/talk/tells", headers=_h(bot))
    assert r.status_code == 422


# --- миграция --------------------------------------------------------------------------------------------


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


def test_migration_0116_from_0115(tmp_path, stepped):
    stepped(115)
    old = Db(tmp_path)
    old.start()
    try:
        old.write_sync(
            lambda conn: conn.execute(
                "INSERT INTO main.owner_talk (ts, author, text) VALUES (1, 'agent', 'было до миграции')"
            )
        )
    finally:
        old.close()
    stepped(116)
    new = Db(tmp_path)
    new.start()
    try:
        with new.reader() as conn:
            assert conn.execute("PRAGMA main.user_version").fetchone()[0] == 116
            assert conn.execute("SELECT tell FROM main.owner_talk").fetchone() == (0,)
            indexes = {r[1] for r in conn.execute("PRAGMA main.index_list(owner_talk)")}
        assert "idx_owner_talk_tell" in indexes
        with pytest.raises(Exception, match="CHECK"):
            new.write_sync(
                lambda conn: conn.execute(
                    "INSERT INTO main.owner_talk (ts, author, text, tell) VALUES (1, 'agent', 'x', 2)"
                )
            )
    finally:
        new.close()
