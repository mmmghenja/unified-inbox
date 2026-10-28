"""Ф93-1 (docs/architecture.md и B.1, тесты D): строка «Деньги» (`money`) в таблице «Кто
отвечает на карточки».

- `money_text` — таблица примеров;
- денежный ход («ВЛАДЕЛЬЦУ:» о тратах) при `turn` = «Агенту» и `money` = «Мне» — владельцу, агент не видит и не
  отвечает;
  `money` = «Агенту» — агенту; `turn` = «Сразу да» — карточка всё равно есть;
- `hub_ask_owner(money=True)` и вопрос со словом «оплатить» при `agent_msg` = «Агенту» — владельцу;
- заявка `payment` при `human` = «Агенту» — владельцу; вопрос сессии с вариантом «Оплатить» — владельцу;
- PATCH `money: "auto"` — 422, `agent` проходит;
- ручки бота `GET /owner_channel/money`, `POST /owner_channel/card-reply` — только `human_bot`; ответ на `turn` —
реплика
  сессии с `typed`, повтор — без второй реплики, закрытая — 409 со `state`;
- F2: `GET /owner_channel/returned` — возвраты агента (агентом и за зависание, не владельцем), `turn` / `agent_msg` с
  текстом и ответом из бота через `card-reply`, прочие виды — без ответа текстом.

Мир — test_hub_sessions.py; хост поддельный."""

import json

import pytest

from hub.core import card_route, peer_msgs

from . import test_hub_sessions as base
from .card_helpers import QUESTION, _route
from .test_f41_autopilot_5b import _auto
from .test_f41_held_work import _ask as _held_ask
from .test_f54_human_requests import _create as _human_create
from .test_f56_queue import _agent, _answer, _bot, _channel_on, _permission, _queue, _refs
from .test_f65_errand_life import _return, _take
from .test_hub_sessions import A, B, _audits, _h, _hs, _sess, _sub
from .test_owner_agent_msg import _ask
from .test_owner_turn import TAIL, _answer_body, _say, _turn_cards, _Turns

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"
MONEY_ASK = _answer_body(
    "Нашла сервис для откликов.\n\nВЛАДЕЛЬЦУ: Берём подписку signup.example (1 ₽, потом 299 ₽)? Если да, "
    "оформи её сам.",
    "Берём подписку signup.example (1 ₽, потом 299 ₽)? Если да, оформи её сам.",
    "owner_channel",
)
PLAIN_ASK = _answer_body("Готово.\n\nВЛАДЕЛЬЦУ: нужен доступ к серверу", "нужен доступ к серверу", "owner_channel")


# --- признак (A.3) ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "money"),
    [
        ("Берём подписку … (1 ₽, потом 299 ₽)? оформи сам: покупка только за тобой", True),
        ("оплати 1 ₽", True),
        ("отклик за 3000 ₽ отправить?", False),
        ("подписка #249 жива", False),
        ("тариф Pro", True),
        ("Подписка на сервис — 299 руб. в месяц, берём?", True),
        ("Купи домен", True),
        ("Нужно пополнить баланс", True),
        ("Выставили счёт на 5000", True),
        ("Купил бы, но не нужно", False),  # «купи» — только отдельным словом
        ("Бюджет заказа 15 000 ₽, откликаюсь?", False),  # голое ₽ — не признак
        ("", False),
    ],
)
def test_money_text_examples(text, money):
    assert card_route.money_text(text) is money


@pytest.mark.parametrize(
    ("full", "money"),
    [
        ("Посчитала бюджет: оплата 3000 ₽ заказчиком.\n\nВЛАДЕЛЬЦУ: нужен доступ к серверу", False),
        ("Готово.\n\nВЛАДЕЛЬЦУ: оплатить тариф?", True),
        ("Готово.\n\n**ВЛАДЕЛЬЦУ:** оплатить тариф?\nПодробности выше.", True),
        ("Сделала.\n\nТариф Pro за 990 ₽ — берём?", True),  # без «ВЛАДЕЛЬЦУ:» — последний абзац
        ("Тариф Pro за 990 ₽ взяли.\n\nДальше — отклики.", False),
    ],
)
def test_turn_money_by_owner_tail_or_last_paragraph(full, money):
    assert card_route.turn_money(full) is money


# --- ход сессии (A.3, A.5) ----------------------------------------------------------------------------------


async def _signal_turn(turns: _Turns, answer) -> None:
    """Ход по сигналу (не по реплике владельца); один `_Turns` на сессию — `last_turn_ts` растёт."""
    await turns.start()
    await turns.end(answer)


async def test_money_turn_goes_to_owner_while_turn_row_is_agent(client, ui, db, world):
    w = world
    agent, _ = _agent(db)
    await _route(ui, w, {"turn": "agent"})
    sid = await _auto(client, ui, w)
    turns = _Turns(client, w, sid)
    await turns.start()
    await turns.end(MONEY_ASK)
    ref = f"turn:{sid}:1"
    card = _refs(await _queue(client, ui=ui))[ref]
    assert card["money"] is True and card["in_channel"] is False
    assert ref not in _refs(await _queue(client, agent))
    await _answer(client, ref, {"action": "reply", "text": "да"}, token=agent, expect=403)
    with db.reader() as conn:
        assert card_route.for_card(conn, w["p3"], "turn", sid, ref=ref) == card_route.ME

    # Обычный ход того же проекта — агенту, как прежде.
    await turns.start()
    await turns.end(PLAIN_ASK)
    plain = f"turn:{sid}:2"
    card = _refs(await _queue(client, agent))[plain]
    assert card["money"] is False and card["in_channel"] is True

    # Строка «Деньги» — «Агенту»: денежный ход тоже агенту.
    await _route(ui, w, {"money": "agent"})
    await turns.start()
    await turns.end(MONEY_ASK)
    third = f"turn:{sid}:3"
    card = _refs(await _queue(client, agent))[third]
    assert card["money"] is True and card["in_channel"] is True
    done = await _answer(client, third, {"action": "reply", "text": "берём"}, token=agent)
    assert done["status"] == "queued"


async def test_money_turn_gives_card_even_when_turn_is_auto(client, ui, db, world):
    w = world
    await _route(ui, w, {"turn": "auto"})
    sid = await _auto(client, ui, w)
    turns = _Turns(client, w, sid)
    # Ответ на реплику владельца без «ВЛАДЕЛЬЦУ:» и без вопроса — при «Сразу да» карточки нет…
    say_id = await _say(ui, sid)
    await turns.start("say")
    await turns.deliver(say_id)
    await turns.end(TAIL)
    assert _turn_cards(await _queue(client, ui=ui)) == {}
    # …а денежный последний абзац — карточка владельцу.
    body = "Нашла сервис.\n\nТариф Pro стоит 990 ₽ в месяц."
    say_id = await _say(ui, sid, "что дальше")
    await turns.start("say")
    await turns.deliver(say_id)
    await turns.end(_answer_body(body, "Тариф Pro стоит 990 ₽ в месяц.", "tail"))
    cards = _turn_cards(await _queue(client, ui=ui))
    assert list(cards) == [f"turn:{sid}:2"] and cards[f"turn:{sid}:2"]["money"] is True
    # Ход по сигналу с денежным «ВЛАДЕЛЬЦУ:» — тоже карточка (не «сразу да»).
    await _signal_turn(turns, MONEY_ASK)
    cards = _turn_cards(await _queue(client, ui=ui))
    assert list(cards) == [f"turn:{sid}:3"] and cards[f"turn:{sid}:3"]["in_channel"] is False


# --- сообщения агентов, заявки, вопросы (A.2, A.4) ----------------------------------------------------------


async def test_money_agent_msgs_go_to_owner_when_agent_msg_row_is_agent(client, ui, db, world):
    w = world
    agent, _ = _agent(db)
    await _route(ui, w, {"agent_msg": "agent"})
    await _sub(client, w, A, role="оркестратор")
    await _sub(client, w, B, role="менеджер")
    flagged = await _ask(client, w, _sess(A), "Берём сервис?", money=True)
    by_word = await _ask(client, w, _sess(B), "Надо оплатить хостинг до пятницы, ок?")
    plain = await _ask(client, w, _sess(B), "Дашь доступ к серверу?")
    with db.reader() as conn:
        assert [peer_msgs.get(conn, m["id"]).money for m in (flagged, by_word, plain)] == [1, 1, 0]
    mine = _refs(await _queue(client, ui=ui))
    theirs = _refs(await _queue(client, agent))
    for made in (flagged, by_word):
        ref = f"agent_msg:{made['id']}"
        assert mine[ref]["money"] is True and mine[ref]["in_channel"] is False and ref not in theirs
        assert mine[ref]["full"]["money"] is True
        await _answer(client, ref, {"action": "ok"}, token=agent, expect=403)
    ref = f"agent_msg:{plain['id']}"
    assert theirs[ref]["money"] is False and theirs[ref]["in_channel"] is True
    # Флаг — строго bool.
    await _ask(client, w, _sess(A), "x", money="да", expect=422)


async def test_payment_request_and_money_question_go_to_owner(client, ui, db, world):
    w = world
    agent, _ = _agent(db)
    sid = await _auto(client, ui, w)
    await _route(ui, w, {"human": "agent", "question": "agent"})
    payment = (await _human_create(client, w, session=_hs(sid), kind="payment", need="Оплатить тариф"))[
        "request"
    ]
    captcha = (await _human_create(client, w, session=_hs(sid)))["request"]
    money_q = {
        "questions": [
            {"question": "Какой тариф берём?", "options": [{"label": "Оплатить Pro"}, {"label": "Нет"}]}
        ]
    }
    asked = await _held_ask(client, w, sid, kind="question", tool="AskUserQuestion", tool_input=money_q)
    plain_q = await _held_ask(
        client, w, sid, kind="question", tool="AskUserQuestion", tool_input=QUESTION["input"]
    )
    theirs = _refs(await _queue(client, agent))
    mine = _refs(await _queue(client, ui=ui))
    for ref in (f"human:{payment['id']}", f"req:{asked['id']}"):
        assert ref not in theirs and mine[ref]["money"] is True and mine[ref]["in_channel"] is False
    for ref in (f"human:{captcha['id']}", f"req:{plain_q['id']}"):
        assert theirs[ref]["money"] is False and theirs[ref]["in_channel"] is True
    # Ответ агента на денежную заявку — 403; на обычную — проходит. `for_card` по `ref` — то же, что очередь.
    await _answer(client, f"human:{payment['id']}", {"action": "done"}, token=agent, expect=403)
    await _answer(client, f"human:{captcha['id']}", {"action": "done"}, token=agent)
    with db.reader() as conn:
        for kind, ref, route in (
            ("human", f"human:{payment['id']}", card_route.ME),
            ("question", f"req:{asked['id']}", card_route.ME),
            ("question", f"req:{plain_q['id']}", card_route.AGENT),
        ):
            assert card_route.for_card(conn, w["p3"], kind, sid, ref=ref) == route, ref


# --- таблица (A.1) ------------------------------------------------------------------------------------------


async def test_money_row_values_and_default(client, ui, db, world):
    w = world
    with db.reader() as conn:
        assert card_route.table(conn, w["p3"])["money"] == card_route.ME
    await _route(ui, w, {"money": "auto"}, expect=422)
    await _route(ui, w, {"money": "agent"})
    with db.reader() as conn:
        assert card_route.table(conn, w["p3"])["money"] == card_route.AGENT
    assert "money" not in card_route.AUTO_GROUPS and card_route.GROUP_OF.get("money") is None


# --- ручки бота (B.1) ---------------------------------------------------------------------------------------


async def _money(client, token, since_ms=0, expect=200):
    r = await client.get(f"{API}/owner_channel/money", params={"since_ms": since_ms}, headers=_h(token))
    assert r.status_code == expect, r.text
    return r.json()


async def _money_reply(client, token, ref, text="да, бери", tg=11, expect=200):
    body = {"ref": ref, "text": text, "tg_message_id": tg}
    r = await client.post(f"{API}/owner_channel/card-reply", json=body, headers=_h(token))
    assert r.status_code == expect, r.text
    return r.json()


def _says(db, sid) -> list[tuple]:
    with db.reader() as conn:
        return conn.execute(
            "SELECT text, source, typed FROM main.held_say WHERE session_id = ? ORDER BY id", (sid,)
        ).fetchall()


async def test_bot_money_handles_rights(client, ui, db, world):
    w = world
    agent, _ = _agent(db)
    r = await ui.get(f"{API}/owner_channel/money")
    assert r.status_code == 403
    r = await ui.post(f"{API}/owner_channel/card-reply", {"ref": "turn:1:1", "text": "x", "tg_message_id": 1})
    assert r.status_code == 403
    for token in (agent, w["tok"]):
        await _money(client, token, expect=403)
        await _money_reply(client, token, "turn:1:1", expect=403)


async def test_bot_money_list_reply_repeat_and_closed(client, ui, db, world):
    w = world
    bot, _ = _bot(db)
    sid = await _auto(client, ui, w)
    turns = _Turns(client, w, sid)
    await _signal_turn(turns, PLAIN_ASK)  # не денежный — боту не виден
    await _signal_turn(turns, MONEY_ASK)
    ref = f"turn:{sid}:2"
    (item,) = (await _money(client, bot))["items"]
    assert item["ref"] == ref and item["kind"] == "turn" and item["state"] == "open"
    assert item["project"] == "Umbrella" and item["title"] == base.ROLE
    assert "signup.example" in item["text"]

    await _money_reply(client, bot, ref, text="", expect=422)
    done = await _money_reply(client, bot, ref)
    assert done == {"ref": ref, "status": "queued", "repeat": False}
    assert _says(db, sid) == [("да, бери", "ui", 1)]
    # Повтор того же сообщения Telegram — тот же ответ, второй реплики нет.
    again = await _money_reply(client, bot, ref)
    assert again == {"ref": ref, "status": "queued", "repeat": True}
    assert len(_says(db, sid)) == 1
    # Другое сообщение по закрытой карточке — 409 со `state`.
    r = await client.post(
        f"{API}/owner_channel/card-reply",
        json={"ref": ref, "text": "ещё", "tg_message_id": 12},
        headers=_h(bot),
    )
    assert r.status_code == 409 and r.json()["state"] == "closed"
    (item,) = (await _money(client, bot))["items"]
    assert item["state"] == "closed"
    with db.reader() as conn:
        row = conn.execute(
            "SELECT closed_by, payload_json FROM main.owner_item WHERE ref = ?", (ref,)
        ).fetchone()
    payload = json.loads(row[1])
    assert row[0] == "ui" and payload["via"] == "bot" and payload["bot_msg_ids"] == [11]
    audits = [a for a in _audits(db, "owner_answer") if a.get("ref") == ref]
    assert audits and audits[-1]["via"] == "bot" and audits[-1]["money"] is True
    # Нет карточки — 404; не денежная — 409 `not_money`.
    await _money_reply(client, bot, f"turn:{sid}:99", expect=404)
    r = await client.post(
        f"{API}/owner_channel/card-reply",
        json={"ref": f"turn:{sid}:1", "text": "x", "tg_message_id": 13},
        headers=_h(bot),
    )
    assert r.status_code == 409 and r.json()["state"] == "not_money"


async def test_bot_money_agent_msg_reply_and_agent_row(client, ui, db, world):
    w = world
    bot, _ = _bot(db)
    await _sub(client, w, A, role="оркестратор")
    made = await _ask(client, w, _sess(A), "Оплатить домен на год?")
    ref = f"agent_msg:{made['id']}"
    (item,) = (await _money(client, bot))["items"]
    assert item["ref"] == ref and item["kind"] == "agent_msg" and item["title"] == "оркестратор"
    # Строка «Деньги» — у агента: открытая карточка боту не видна, ответ — 409 `agent`.
    await _route(ui, w, {"money": "agent"})
    assert (await _money(client, bot))["items"] == []
    r = await client.post(
        f"{API}/owner_channel/card-reply", json={"ref": ref, "text": "да", "tg_message_id": 5}, headers=_h(bot)
    )
    assert r.status_code == 409 and r.json()["state"] == "agent"
    await _route(ui, w, {"money": "me"})
    done = await _money_reply(client, bot, ref, text="да", tg=5)
    assert done == {"ref": ref, "status": "replied", "repeat": False}
    with db.reader() as conn:
        sent = peer_msgs._select(conn, "1 = 1 ORDER BY m.id DESC LIMIT 1")[0]  # noqa: SLF001
        assert peer_msgs.get(conn, made["id"]).status == "read"
    assert sent.from_user == 1 and sent.reply_to == made["id"] and sent.text == "да"
    again = await _money_reply(client, bot, ref, text="да", tg=5)
    assert again["repeat"] is True
    assert (await _money(client, bot))["items"][0]["state"] == "closed"


# --- возвраты агента (F2) -----------------------------------------------------------------------------------


async def _returned(client, token, since_ms=0, expect=200):
    r = await client.get(f"{API}/owner_channel/returned", params={"since_ms": since_ms}, headers=_h(token))
    assert r.status_code == expect, r.text
    return r.json()


async def test_returned_turn_listed_and_answered_from_bot(client, ui, db, world):
    w = world
    agent, _ = _agent(db)
    bot, _ = _bot(db)
    sid = await _auto(client, ui, w)
    await _channel_on(ui, w, sid)
    turns = _Turns(client, w, sid)
    await _signal_turn(turns, PLAIN_ASK)
    ref = f"turn:{sid}:1"
    # Обычная карточка у агента — из бота текстом не отвечается.
    r = await client.post(
        f"{API}/owner_channel/card-reply", json={"ref": ref, "text": "x", "tg_message_id": 3}, headers=_h(bot)
    )
    assert r.status_code == 409 and r.json()["state"] == "not_money"
    assert (await _returned(client, bot))["items"] == []
    made = (await _take(client, agent, ref))["errand"]
    await _return(client, agent, made["id"], {"reason": "не вышло: нужен доступ", "detail": "пробовал ssh"})
    (item,) = (await _returned(client, bot))["items"]
    assert item == {
        "id": made["id"],
        "ref": ref,
        "ts": item["ts"],
        "kind": "turn",
        "project": "Umbrella",
        "title": base.ROLE,
        "reason": "не вышло: нужен доступ",
        "detail": "пробовал ssh",
        "text": PLAIN_ASK["full"],
        "replyable": True,
        "state": "open",
    }
    done = await _money_reply(client, bot, ref, text="дал доступ", tg=4)
    assert done == {"ref": ref, "status": "queued", "repeat": False}
    assert _says(db, sid) == [("дал доступ", "ui", 1)]
    assert (await _money_reply(client, bot, ref, text="дал доступ", tg=4))["repeat"] is True
    (item,) = (await _returned(client, bot))["items"]
    assert item["state"] == "closed"
    audits = [a for a in _audits(db, "owner_answer") if a.get("ref") == ref]
    assert audits[-1]["via"] == "bot" and audits[-1]["returned"] is True and audits[-1]["money"] is False


async def test_returned_other_kinds_hung_and_owner_stop(client, ui, db, clock, world):
    from hub.core import owner_queue as zq

    w = world
    agent, _ = _agent(db)
    bot, _ = _bot(db)
    sid = await _auto(client, ui, w, mode="manual")
    await _channel_on(ui, w, sid)
    first = f"req:{await _permission(client, w, sid)}"
    second = f"req:{await _permission(client, w, sid)}"
    third = f"req:{await _permission(client, w, sid)}"
    back = (await _take(client, agent, first))["errand"]
    await _return(client, agent, back["id"], {"reason": "нужен владелец"})
    stopped = (await _take(client, agent, second))["errand"]
    r = await ui.c.delete(f"{API}/owner_channel/errands/{stopped['id']}", headers=ui.headers)
    assert r.status_code == 200, r.text
    hung = (await _take(client, agent, third))["errand"]
    clock.advance_ms(zq.HUNG_MS + 1000)
    await db.write(lambda conn: zq.return_hung(conn, now_ms=int(clock() * 1000)))
    items = {i["ref"]: i for i in (await _returned(client, bot))["items"]}
    assert set(items) == {first, third}  # остановил владелец — не возврат ему
    assert items[first]["kind"] == "permission" and items[first]["replyable"] is False
    assert items[first]["text"] is None and items[first]["title"] == f"{base.ROLE} · разрешение"
    assert items[first]["state"] == "open" and items[third]["reason"].startswith("завис")
    assert items[third]["id"] == hung["id"]
    # Ответ текстом из бота по разрешению — 404 (только `turn` / `agent_msg`).
    await _money_reply(client, bot, first, expect=404)
    # Владелец решил в хабе — возврат закрыт.
    await _answer(client, first, {"action": "deny"}, ui=ui)
    items = {i["ref"]: i for i in (await _returned(client, bot))["items"]}
    assert items[first]["state"] == "closed" and items[third]["state"] == "open"
    # Права: только бот.
    await _returned(client, agent, expect=403)
    assert (await ui.get(f"{API}/owner_channel/returned", params={"since_ms": 0})).status_code == 403


async def test_returned_agent_msg_answered_from_bot(client, ui, db, world):
    w = world
    agent, _ = _agent(db)
    bot, _ = _bot(db)
    await _route(ui, w, {"agent_msg": "agent"})
    await _sub(client, w, A, role="оркестратор")
    made = await _ask(client, w, _sess(A), "Дашь доступ к серверу?")
    ref = f"agent_msg:{made['id']}"
    errand = (await _take(client, agent, ref))["errand"]
    await _return(client, agent, errand["id"], {"reason": "решать владельцу"})
    (item,) = (await _returned(client, bot))["items"]
    assert item["kind"] == "agent_msg" and item["title"] == "оркестратор" and item["replyable"] is True
    assert item["text"] == "Дашь доступ к серверу?"
    done = await _money_reply(client, bot, ref, text="дам вечером", tg=9)
    assert done == {"ref": ref, "status": "replied", "repeat": False}


# --- F3: ход после перезапуска не теряет вопрос владельцу --------------------------------------------------------


async def test_turn_card_survives_restart_turn_without_owner_line(client, ui, db, world):
    """Ход, начатый не репликой владельца (подъём после перезапуска хоста: `self`, сигнал, сжатие) и кончившийся
    без «ВЛАДЕЛЬЦУ:», открытую карточку `turn` не закрывает; с «ВЛАДЕЛЬЦУ:» — новая карточка заменяет старую."""
    from .test_hub_sessions import _host

    w = world
    sid = await _auto(client, ui, w, mode="manual")
    turns = _Turns(client, w, sid)
    await _signal_turn(turns, PLAIN_ASK)
    ref = f"turn:{sid}:1"
    assert list(_turn_cards(await _queue(client, ui=ui))) == [ref]
    # Перезапуск хоста: процесс уснул, поднят тем же разговором, ход по сигналу «проверь входящее», сам и
    # сжатие — без «ВЛАДЕЛЬЦУ:».
    await _host(client, w, f"sessions/{sid}/turn", {"state": "asleep"})
    for source in ("self", "signal", "compact"):
        await turns.start(source)
        await turns.end(TAIL)
        assert list(_turn_cards(await _queue(client, ui=ui))) == [ref], source
    # Ход без текста вовсе — тоже.
    await turns.start("self")
    await turns.end()
    assert list(_turn_cards(await _queue(client, ui=ui))) == [ref]
    # С «ВЛАДЕЛЬЦУ:» — новая заменяет старую.
    await turns.start("self")
    await turns.end(MONEY_ASK)
    cards = _turn_cards(await _queue(client, ui=ui))
    assert len(cards) == 1 and ref not in cards
