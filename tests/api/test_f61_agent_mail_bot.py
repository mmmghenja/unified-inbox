"""Ф61-1 (docs/architecture.md, D и поправки П1–П3, П8, П10, П11):
почта агенту владельца и вопросы агента для бота (`ask`, `asks`, `bot-reply`, `closed`).

Доступ: почта — только агент `human`; ручки бота — только `human_bot`; закрытие вопроса — только агент.
Почта: обе схемы, рассылки видны, исходящее и удалённое — нет, подрезка `since_ms`, поиск `sender`/`q`,
проекты токена. Вопросы: `ask` только у агента, срок, состояния `open|answered|closed|expired`, повтор
`tg_message_id`, 409 со `state`, отметка прочитанного, `ref_title` карточки. Миграция с версии 0092."""

import pytest

from hub.core import db as db_module
from hub.core.db import Db

from . import test_hub_sessions as base
from .test_f41_autopilot_5b import _auto
from .test_f54_human_requests import _create as _human_create
from .test_f56_queue import _agent, _bot, _queue
from .test_hub_sessions import _audits, _h, _hs, _now, _token

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"
DAY = 24 * 60 * 60_000
MIN = 60_000


async def _say(client, body, *, token=None, ui=None, expect=200) -> dict:
    if ui is not None:
        r = await ui.post(f"{API}/owner_channel/talk", body)
    else:
        r = await client.post(f"{API}/owner_channel/talk", json=body, headers=_h(token))
    assert r.status_code == expect, r.text
    return r.json()


async def _asks(client, bot, since_ms=0) -> list[dict]:
    r = await client.get(f"{API}/owner_channel/talk/asks", params={"since_ms": since_ms}, headers=_h(bot))
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def _reply(client, bot, reply_to, text="ответ", tg=1, expect=200):
    body = {"reply_to": reply_to, "text": text, "tg_message_id": tg}
    r = await client.post(f"{API}/owner_channel/talk/bot-reply", json=body, headers=_h(bot))
    assert r.status_code == expect, r.text
    return r.json()


async def _close(client, agent, talk_id, outcome="answered", expect=200):
    r = await client.post(f"{API}/owner_channel/talk/{talk_id}/closed", json={"outcome": outcome}, headers=_h(agent))
    assert r.status_code == expect, r.text
    return r.json()


def _rows(db) -> list[tuple]:
    with db.reader() as conn:
        return conn.execute(
            "SELECT id, author, via, reply_to, read_ts FROM main.owner_talk ORDER BY id"
        ).fetchall()


# --- доступ (П11) -------------------------------------------------------------------------------------------


async def test_rights(client, ui, db):
    agent, _ = _agent(db)
    bot, _ = _bot(db)
    other, _ = _token(db)
    q = await _say(client, {"text": "какой пароль?", "ask": True}, token=agent)

    # Бот: всё прежнее и почта — 403.
    for method, path, body in (
        ("get", "/owner_channel/talk", None),
        ("post", "/owner_channel/talk", {"text": "x"}),
        ("get", "/owner_channel/queue", None),
        ("post", f"/owner_channel/talk/{q['id']}/closed", {"outcome": "gave_up"}),
    ):
        kw = {} if body is None else {"json": body}
        r = await client.request(method, f"{API}{path}", headers=_h(bot), **kw)
        assert r.status_code == 403, (path, r.status_code)
    # Агент и прочий bearer — 403 на ручки бота; прочий bearer — и на почту, и на закрытие.
    for token in (agent, other):
        r = await client.get(f"{API}/owner_channel/talk/asks?since_ms=0", headers=_h(token))
        assert r.status_code == 403
        body = {"reply_to": q["id"], "text": "x", "tg_message_id": 5}
        r = await client.post(f"{API}/owner_channel/talk/bot-reply", json=body, headers=_h(token))
        assert r.status_code == 403
    r = await client.post(
        f"{API}/owner_channel/talk/{q['id']}/closed", json={"outcome": "answered"}, headers=_h(other)
    )
    assert r.status_code == 403
    # Кука — 403 на ручки бота, почту и закрытие вопроса.
    assert (await ui.get(f"{API}/owner_channel/talk/asks?since_ms=0")).status_code == 403
    assert (
        await ui.post(f"{API}/owner_channel/talk/bot-reply", {"reply_to": q["id"], "text": "x", "tg_message_id": 5})
    ).status_code == 403
    assert (await ui.post(f"{API}/owner_channel/talk/{q['id']}/closed", {"outcome": "answered"})).status_code == 403
    # Бот видит вопрос; ничего из отказанного не записалось.
    assert [a["id"] for a in await _asks(client, bot)] == [q["id"]]
    assert _rows(db) == [(q["id"], "agent", None, None, None)]


# --- вопрос агента: запись и состояние (П2) --------------------------------------------------


async def test_ask_only_from_agent_and_ttl(client, ui, db, clock):
    agent, _ = _agent(db)
    await _say(client, {"text": "x", "ask": True}, ui=ui, expect=422)
    await _say(client, {"text": "x", "ask_ttl_s": 600}, ui=ui, expect=422)
    for ttl in (59, 7201, True):
        await _say(client, {"text": "x", "ask": True, "ask_ttl_s": ttl}, token=agent, expect=422)
    await _say(client, {"text": "x", "ask_ttl_s": 600}, token=agent, expect=422)  # срок без вопроса
    now = _now(clock)
    q = await _say(client, {"text": "пароль?", "ask": True, "ref": "human:2"}, token=agent)
    assert q["ask"] is True and q["ask_until_ts"] == now + 1800 * 1000 and q["via"] is None
    q2 = await _say(client, {"text": "код?", "ask": True, "ask_ttl_s": 60}, token=agent)
    assert q2["ask_until_ts"] == now + 60_000
    plain = await _say(client, {"text": "думаю"}, token=agent)
    assert plain["ask"] is False and plain["ask_until_ts"] is None
    feed = (await ui.get(f"{API}/owner_channel/talk")).json()["items"]
    assert [(m["id"], m["ask"], m["via"]) for m in feed] == [
        (q["id"], True, None),
        (q2["id"], True, None),
        (plain["id"], False, None),
    ]
    assert _audits(db, "owner_talk")[0] == {"id": q["id"], "author": "agent", "len": 7, "ask": True}


async def test_ask_states_and_close(client, ui, db, clock):
    agent, _ = _agent(db)
    bot, _ = _bot(db)
    by_reply = await _say(client, {"text": "1?", "ask": True}, token=agent)
    by_close = await _say(client, {"text": "2?", "ask": True}, token=agent)
    gave_up = await _say(client, {"text": "3?", "ask": True}, token=agent)
    expires = await _say(client, {"text": "4?", "ask": True, "ask_ttl_s": 60}, token=agent)
    still = await _say(client, {"text": "5?", "ask": True}, token=agent)
    await _say(client, {"text": "не вопрос"}, token=agent)
    await _say(client, {"text": "вот", "reply_to": by_reply["id"]}, ui=ui)
    await _close(client, agent, by_close["id"], "answered")
    closed = await _close(client, agent, gave_up["id"], "gave_up")
    assert closed["closed_outcome"] == "gave_up" and closed["closed_ts"] == _now(clock)
    clock.advance_ms(61_000)
    states = {a["id"]: a["state"] for a in await _asks(client, bot)}
    assert states == {
        by_reply["id"]: "answered",
        by_close["id"]: "answered",
        gave_up["id"]: "closed",
        expires["id"]: "expired",
        still["id"]: "open",
    }
    # Повтор закрытия — 200 без изменений (и с другим исходом); не вопрос — 409; чужой исход — 422; нет — 404.
    again = await _close(client, agent, gave_up["id"], "answered")
    assert again == closed
    plain = await _say(client, {"text": "реплика"}, token=agent)
    await _close(client, agent, plain["id"], expect=409)
    mine = await _say(client, {"text": "моё"}, ui=ui)
    await _close(client, agent, mine["id"], expect=409)
    await _close(client, agent, still["id"], "later", expect=422)
    await _close(client, agent, 999_999, expect=404)


async def test_asks_since_clamped_to_a_day(client, db, clock):
    agent, _ = _agent(db)
    bot, _ = _bot(db)
    old = await _say(client, {"text": "давний?", "ask": True}, token=agent)
    clock.advance_ms(DAY + MIN)
    fresh = await _say(client, {"text": "свежий?", "ask": True}, token=agent)
    assert [a["id"] for a in await _asks(client, bot, since_ms=0)] == [fresh["id"]]  # 0 подрезан до суток
    assert [a["id"] for a in await _asks(client, bot, since_ms=_now(clock) - DAY - 5)] == [fresh["id"]]
    assert await _asks(client, bot, since_ms=_now(clock) + 1) == []
    assert old["id"] < fresh["id"]
    item = (await _asks(client, bot))[0]
    # Ф96-1: ещё поручение по карточке вопроса и руки.
    assert set(item) == {"id", "ts", "text", "ref", "ref_title", "state", "errand_id", "hands", "hands_ts"}
    assert item["ref"] is None and item["ref_title"] is None
    r = await client.get(f"{API}/owner_channel/talk/asks", headers=_h(bot))
    assert r.status_code == 422  # since_ms обязателен


async def test_ref_title_of_queue_card(client, ui, db, world):
    w = world
    agent, _ = _agent(db)
    bot, _ = _bot(db)
    sid = await _auto(client, ui, w, mode="manual")
    made = (await _human_create(client, w, session=_hs(sid), site="hoolijob.ru"))["request"]
    ref = f"human:{made['id']}"
    card = {c["ref"]: c for c in (await _queue(client, ui=ui))["items"]}[ref]
    await _say(client, {"text": "пароль?", "ask": True, "ref": ref}, token=agent)
    await _say(client, {"text": "а это?", "ask": True, "ref": "human:999999"}, token=agent)
    got = await _asks(client, bot)
    assert got[0]["ref_title"] == f"{card['project_name']} · {card['brief']}"
    assert "hoolijob.ru" in got[0]["ref_title"]
    assert got[1]["ref_title"] is None  # карточки нет


# --- ответ из бота (B1, П2, П3, мелочи) --------------------------------------------------


async def test_bot_reply_writes_owner_reply_once(client, ui, db, clock):
    agent, _ = _agent(db)
    bot, _ = _bot(db)
    q = await _say(client, {"text": "логин?", "ask": True, "ref": "human:2"}, token=agent)
    agent_block = (await _queue(client, ui=ui))["agent"]
    assert (agent_block["talk_unread"], agent_block["talk_open"]) == (1, 0)
    since = agent_block["talk_last_id"]

    clock.advance_ms(1000)
    row = await _reply(client, bot, q["id"], "  myself@example.com  ", tg=77)
    assert row["author"] == "owner_channel" and row["via"] == "bot" and row["ref"] == "human:2"
    assert row["reply_to"] == q["id"] and row["text"] == "myself@example.com" and row["ask"] is False
    agent_block = (await _queue(client, agent))["agent"]
    # Вопрос прочитан (красное число гаснет), реплика ждёт агента (`talk_open`), лента сдвинулась.
    assert (agent_block["talk_unread"], agent_block["talk_open"]) == (0, 1)
    assert agent_block["talk_last_id"] > since
    opened = (await client.get(f"{API}/owner_channel/talk/open", headers=_h(agent))).json()["items"]
    assert [m["id"] for m in opened] == [row["id"]] and opened[0]["via"] == "bot"
    # Повтор с тем же номером Telegram — та же строка, второй реплики нет (и вопрос уже отвечен).
    again = await _reply(client, bot, q["id"], "другой текст", tg=77)
    assert again == row
    assert len(_rows(db)) == 2
    assert (await _asks(client, bot))[0]["state"] == "answered"
    # Новый номер на отвеченный вопрос — 409 со `state`.
    body = await _reply(client, bot, q["id"], "ещё", tg=78, expect=409)
    assert body["state"] == "answered" and "ask_not_open" in body["detail"]
    got = _audits(db, "owner_talk")
    assert got[-1] == {"id": row["id"], "author": "owner_channel", "via": "bot", "reply_to": q["id"], "len": 18}


async def test_bot_reply_refused(client, ui, db, clock):
    agent, _ = _agent(db)
    bot, _ = _bot(db)
    plain = await _say(client, {"text": "просто реплика"}, token=agent)
    mine = await _say(client, {"text": "моя"}, ui=ui)
    closed = await _say(client, {"text": "1?", "ask": True}, token=agent)
    expired = await _say(client, {"text": "2?", "ask": True, "ask_ttl_s": 60}, token=agent)
    await _close(client, agent, closed["id"], "gave_up")
    clock.advance_ms(60_000)
    for target in (plain["id"], mine["id"]):
        await _reply(client, bot, target, tg=target, expect=409)
    await _reply(client, bot, 999_999, tg=3, expect=404)
    assert (await _reply(client, bot, closed["id"], tg=4, expect=409))["state"] == "closed"
    assert (await _reply(client, bot, expired["id"], tg=5, expect=409))["state"] == "expired"
    open_q = await _say(client, {"text": "3?", "ask": True}, token=agent)
    for body in (
        {"reply_to": open_q["id"], "text": "", "tg_message_id": 6},
        {"reply_to": open_q["id"], "text": "я" * 8001, "tg_message_id": 6},
        {"reply_to": open_q["id"], "text": "x", "tg_message_id": 0},
        {"reply_to": open_q["id"], "text": "x"},
    ):
        r = await client.post(f"{API}/owner_channel/talk/bot-reply", json=body, headers=_h(bot))
        assert r.status_code == 422, body
    assert all(row[1] == "agent" or row[0] == mine["id"] for row in _rows(db))


async def test_bot_reply_wakes_changes(client, db):
    import asyncio

    agent, _ = _agent(db)
    bot, _ = _bot(db)
    q = await _say(client, {"text": "?", "ask": True}, token=agent)
    since = (await _queue(client, agent))["seq"]

    async def later():
        await asyncio.sleep(0.2)
        await _reply(client, bot, q["id"], tg=9)

    r, _ = await asyncio.gather(
        client.get(f"{API}/owner_channel/changes?since={since}&wait=3", headers=_h(agent)), later()
    )
    assert r.status_code == 200 and r.json()["agent"]["talk_open"] == 1


# --- миграция --------------------------------------------------------------------------------


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


def test_migration_0093_from_0092(tmp_path, stepped):
    stepped(92)
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
    stepped(93)
    new = Db(tmp_path)
    new.start()
    try:
        with new.reader() as conn:
            assert conn.execute("PRAGMA main.user_version").fetchone()[0] == 93
            cols = [r[1] for r in conn.execute("PRAGMA main.table_info(owner_talk)")]
            row = conn.execute(
                "SELECT ask, ask_until_ts, closed_ts, via, tg_msg_id FROM main.owner_talk"
            ).fetchone()
            indexes = {r[1] for r in conn.execute("PRAGMA main.index_list(owner_talk)")}
        assert cols[-6:] == ["ask", "ask_until_ts", "closed_ts", "closed_outcome", "via", "tg_msg_id"]
        assert row == (0, None, None, None, None)
        assert {"idx_owner_talk_bot_tg", "idx_owner_talk_ask"} <= indexes
        for bad in (
            "INSERT INTO main.owner_talk (ts, author, text, via) VALUES (1, 'owner_channel', 'x', 'ui')",
            "INSERT INTO main.owner_talk (ts, author, text, closed_outcome) "
            "VALUES (1, 'agent', 'x', 'maybe')",
        ):
            with pytest.raises(Exception, match="CHECK"):
                new.write_sync(lambda conn, sql=bad: conn.execute(sql))
        ask = "INSERT INTO main.owner_talk (ts, author, text, ask) VALUES (1, 'agent', 'q', 1)"
        new.write_sync(lambda conn: conn.execute(ask))
        new.write_sync(lambda conn: conn.execute(ask))
        ins = (
            "INSERT INTO main.owner_talk (ts, author, text, reply_to, via, tg_msg_id) "
            "VALUES (1, 'owner_channel', 'x', (SELECT MIN(id) FROM main.owner_talk WHERE ask = 1), 'bot', 5)"
        )
        new.write_sync(lambda conn: conn.execute(ins))
        with pytest.raises(Exception, match="UNIQUE"):
            new.write_sync(lambda conn: conn.execute(ins))
        # Тот же номер сообщения Telegram на ДРУГОЙ вопрос — не повтор (новый чат бота нумерует заново).
        other = ins.replace("MIN(id)", "MAX(id)")
        new.write_sync(lambda conn: conn.execute(other))
    finally:
        new.close()
