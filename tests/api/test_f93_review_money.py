"""Ревью Ф93-1 (docs/architecture.md, отступления 21–24): денежная карточка `turn` при строке
«Деньги» = «Мне» — только владельцу, и обходом через вытесненную карточку и через переехавшее поручение тоже;
денежные и возвращённые карточки проекта с закрытым днём боту не идут, а после «Начать день» доходят.
Воспроизведения ревьюера — `.runs/critic-f93/`."""

import json

from api import test_hub_sessions as base
from api.card_helpers import _route
from api.test_f41_autopilot_5b import _auto
from api.test_f56_queue import _agent, _answer, _bot, _queue, _refs
from api.test_f65_errand_life import _errand, _take
from api.test_f66_review import _close_day, _open_day
from api.test_f93_money_row import _return, _returned
from api.test_hub_sessions import A, _h, _sess, _sub
from api.test_owner_agent_msg import _ask
from api.test_owner_turn import _answer_body, _Turns

procs = base.procs
ui = base.ui
world = base.world

MONEY_ASK = _answer_body(
    "Нашла сервис.\n\nВЛАДЕЛЬЦУ: Берём подписку (1 ₽, потом 299 ₽)? Если да, оформи её сам.",
    "Берём подписку (1 ₽, потом 299 ₽)? Если да, оформи её сам.",
    "owner_channel",
)
PLAIN_ASK = _answer_body("Готово.\n\nВЛАДЕЛЬЦУ: нужен доступ к серверу", "нужен доступ к серверу", "owner_channel")
HOUR = 3_600_000


def _row(db, ref):
    with db.reader() as conn:
        return conn.execute(
            "SELECT closed_ts, closed_by, payload_json FROM main.owner_item WHERE ref = ? ORDER BY id DESC",
            (ref,),
        ).fetchone()


def _errands(db):
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT closed_ts, closed_by, payload_json FROM main.owner_item "
            "WHERE kind = 'errand' ORDER BY id"
        ).fetchall()
    return [(c, b, json.loads(p)) for c, b, p in rows]


async def _plain_then_money(client, ui, w):
    await _route(ui, w, {"turn": "agent"})  # «Деньги» — по умолчанию «Мне»
    sid = await _auto(client, ui, w)
    turns = _Turns(client, w, sid)
    await turns.start()
    await turns.end(PLAIN_ASK)
    return sid, turns


# --- Б1: ответ агента по вытесненной карточке не закрывает денежную -----------------------------------------


async def test_agent_reply_on_superseded_card_refused_when_current_is_money(client, ui, db, world):
    agent, _ = _agent(db)
    sid, turns = await _plain_then_money(client, ui, world)
    plain = f"turn:{sid}:1"
    assert _refs(await _queue(client, agent))[plain]["in_channel"] is True
    await turns.start()
    await turns.end(MONEY_ASK)
    money = f"turn:{sid}:2"
    assert _refs(await _queue(client, ui=ui))[money]["money"] is True
    await _answer(client, plain, {"action": "reply", "text": "да, берём"}, token=agent, expect=409)
    assert _row(db, money)[0] is None  # денежная открыта
    # Владелец (кука) по вытесненной — как прежде: реплика сессии, актуальная закрыта.
    await _answer(client, plain, {"action": "reply", "text": "да, берём"}, ui=ui)
    assert _row(db, money)[0] is not None


async def test_agent_reply_on_superseded_card_still_works_for_plain_current(client, ui, db, world):
    agent, _ = _agent(db)
    sid, turns = await _plain_then_money(client, ui, world)
    await turns.start()
    await turns.end(PLAIN_ASK)  # новая карточка — обычная, маршрут «агент»
    await _answer(client, f"turn:{sid}:1", {"action": "reply", "text": "держи доступ"}, token=agent)
    assert _row(db, f"turn:{sid}:2")[0] is not None


# --- Б2: поручение не переезжает на денежную карточку -------------------------------------------------------


async def test_auto_errand_not_carried_to_money_card(client, ui, db, world):
    agent, _ = _agent(db)
    sid, turns = await _plain_then_money(client, ui, world)
    await _take(client, agent, f"turn:{sid}:1")  # агент взял обычную сам
    await turns.start()
    await turns.end(MONEY_ASK)
    money = f"turn:{sid}:2"
    r = await client.post(
        f"/api/v1/owner_channel/{money}/answer", json={"action": "reply", "text": "да"}, headers=_h(agent)
    )
    assert r.status_code in (403, 409), r.text
    [(closed_ts, closed_by, payload)] = _errands(db)
    assert closed_ts is not None and closed_by == "system"  # закрыто, как при вытеснении
    assert payload["target"] == f"turn:{sid}:1"
    assert not isinstance(payload.get("returned"), str)  # боту «вернул тебе» не идёт
    assert _row(db, money)[0] is None


async def test_owner_errand_not_carried_to_money_card(client, ui, db, world):
    agent, _ = _agent(db)
    await _route(ui, world, {"turn": "me"})
    sid = await _auto(client, ui, world)
    turns = _Turns(client, world, sid)
    await turns.start()
    await turns.end(PLAIN_ASK)
    await _errand(ui, f"turn:{sid}:1")  # «Реши» владельца по обычной
    await turns.start()
    await turns.end(MONEY_ASK)
    money = f"turn:{sid}:2"
    r = await client.post(
        f"/api/v1/owner_channel/{money}/answer", json={"action": "reply", "text": "да"}, headers=_h(agent)
    )
    assert r.status_code in (403, 409), r.text
    assert all(c is not None for c, _b, _p in _errands(db))


async def test_errand_still_carried_to_plain_card(client, ui, db, world):
    agent, _ = _agent(db)
    await _route(ui, world, {"turn": "me"})
    sid = await _auto(client, ui, world)
    turns = _Turns(client, world, sid)
    await turns.start()
    await turns.end(PLAIN_ASK)
    await _errand(ui, f"turn:{sid}:1")
    await turns.start()
    await turns.end(PLAIN_ASK)
    [(closed_ts, _by, payload)] = _errands(db)
    assert closed_ts is None and payload["target"] == f"turn:{sid}:2"
    await _answer(client, f"turn:{sid}:2", {"action": "reply", "text": "держи"}, token=agent)


# --- В4: закрытый день проекта — бот молчит, утром получает ------------------------------------------------


async def test_money_card_of_closed_day_hidden_then_sent_with_open_time(client, ui, db, clock, world):
    w = world
    bot, _ = _bot(db)
    await _sub(client, w, A, role="оркестратор")
    _close_day(db, clock, w["p3"])
    made = await _ask(client, w, _sess(A), "Берём сервис за 299 ₽?", money=True)
    ref = f"agent_msg:{made['id']}"

    async def money():
        r = await client.get("/api/v1/owner_channel/money", params={"since_ms": 0}, headers=_h(bot))
        assert r.status_code == 200, r.text
        return {i["ref"]: i for i in r.json()["items"]}

    assert ref not in await money()
    clock.advance_ms(5 * HOUR)  # ночь прошла
    _open_day(db, clock, w["p3"])
    item = (await money())[ref]
    assert item["state"] == "open" and item["ts"] == int(clock() * 1000)  # не старше часа для бота


async def test_returned_card_of_closed_day_hidden_then_sent_with_open_time(client, ui, db, clock, world):
    w = world
    agent, _ = _agent(db)
    bot, _ = _bot(db)
    await _route(ui, w, {"agent_msg": "agent"})
    await _sub(client, w, A, role="оркестратор")
    made = await _ask(client, w, _sess(A), "Дашь доступ к серверу?")
    errand = (await _take(client, agent, f"agent_msg:{made['id']}"))["errand"]
    _close_day(db, clock, w["p3"])
    await _return(client, agent, errand["id"], {"reason": "решать владельцу"})
    assert (await _returned(client, bot))["items"] == []
    clock.advance_ms(5 * HOUR)
    _open_day(db, clock, w["p3"])
    (item,) = (await _returned(client, bot))["items"]
    assert item["state"] == "open" and item["ts"] == int(clock() * 1000)
