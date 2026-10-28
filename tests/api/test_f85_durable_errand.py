"""Ф85-1 (docs/architecture.md): долгое поручение агента владельца — хабовая часть ядра.

- A.1: вопрос агента без срока (`ask_ttl_s = 0` -> `ask_until_ts = NULL`): открыт часами — в `agent_ask`
  карточки, у бота (`asks`), поручение не «зависает»; кука вопросов не задаёт;
- A.2: `GET /owner_channel/talk/{id}/answer` — только агент; последний ответ владельца, и после «обработано»;
- E: `POST /owner_channel/errands/{id}/ask` — вопрос по поручению с `operation_key` (повтор — тот же вопрос);
- F: `GET /owner_channel/errands/{id}/state` — состояние поручения и `can_act`;
- C: отказ модели (`refusal`) и временный сбой (`transient`) — повод перейти на запасной только у агента;
  запасной агента — тот же движок с другой моделью; 422 — только при полном совпадении с основным.

Мир и помощники — test_hub_sessions.py, test_f56_queue.py, test_f65_errand_life.py, test_f83_fallback.py."""

from hub.core import owner_queue as zq

from . import test_hub_sessions as base
from .test_f56_queue import _agent, _answer, _bot, _permission, _queue, _refs
from .test_f65_errand_life import _errand, _errand_row, _progress, _setup, _take
from .test_f78_engine_choice import _engine, home  # noqa: F401 — фикстура
from .test_f83_fallback import _facts
from .test_hub_sessions import _h, _now

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"
MIN = 60_000
HOUR = 60 * MIN


async def _say(client, body, *, token=None, ui=None, expect=200) -> dict:
    if ui is not None:
        r = await ui.post(f"{API}/owner_channel/talk", body)
    else:
        r = await client.post(f"{API}/owner_channel/talk", json=body, headers=_h(token))
    assert r.status_code == expect, r.text
    return r.json()


async def _answer_of(client, token, talk_id, expect=200) -> dict:
    r = await client.get(f"{API}/owner_channel/talk/{talk_id}/answer", headers=_h(token))
    assert r.status_code == expect, r.text
    return r.json()


async def _ask(client, agent, errand_id, body, expect=200) -> dict:
    r = await client.post(f"{API}/owner_channel/errands/{errand_id}/ask", json=body, headers=_h(agent))
    assert r.status_code == expect, r.text
    return r.json()


async def _state(client, agent, errand_id, expect=200, **params) -> dict:
    r = await client.get(f"{API}/owner_channel/errands/{errand_id}/state", params=params, headers=_h(agent))
    assert r.status_code == expect, r.text
    return r.json()


def _talk_count(db) -> int:
    with db.reader() as conn:
        return conn.execute("SELECT COUNT(*) FROM main.owner_talk").fetchone()[0]


# --- A.1: вопрос без срока ----------------------------------------------------------------------------------


async def test_ask_without_ttl_lives_until_answered(client, ui, db, clock, world):
    w = world
    agent, sid = await _setup(client, ui, db, w)
    bot, _ = _bot(db)
    ref = f"req:{await _permission(client, w, sid)}"
    made = (await _take(client, agent, ref))["errand"]
    # Кука вопросов не задаёт и без срока; вне 0 и 60…7200 — 422.
    await _say(client, {"text": "x", "ask": True, "ask_ttl_s": 0}, ui=ui, expect=422)
    for ttl in (-1, 1, 59, 7201):
        await _say(client, {"text": "x", "ask": True, "ask_ttl_s": ttl}, token=agent, expect=422)
    q = await _say(client, {"text": "код из СМС?", "ask": True, "ask_ttl_s": 0, "ref": ref}, token=agent)
    assert q["ask"] is True and q["ask_until_ts"] is None
    asked_ts = _now(clock)

    clock.advance_ms(40 * MIN)  # 5 минут без шага давно прошли
    # Поручение не «зависло» (открытый вопрос), вопрос виден в карточке и у бота открытым.
    assert db.write_sync(lambda conn: zq.return_hung(conn, now_ms=_now(clock))) == []  # минутный цикл
    assert _errand_row(db, made["id"])[0] is None
    card = _refs(await _queue(client, ui=ui))[ref]
    assert card["errand"]["id"] == made["id"]
    assert card["agent_ask"] == {"id": q["id"], "text": "код из СМС?", "ts": asked_ts, "until_ts": None}
    r = await client.get(f"{API}/owner_channel/talk/asks", params={"since_ms": 0}, headers=_h(bot))
    assert {a["id"]: a["state"] for a in r.json()["items"]}[q["id"]] == "open"
    assert (await _answer_of(client, agent, q["id"])) == {"id": q["id"], "state": "open", "answer": None}

    # Владелец ответил из бота — вопрос отвечен.
    r = await client.post(
        f"{API}/owner_channel/talk/bot-reply",
        json={"reply_to": q["id"], "text": "4321", "tg_message_id": 7},
        headers=_h(bot),
    )
    assert r.status_code == 200, r.text
    got = await _answer_of(client, agent, q["id"])
    assert got["state"] == "answered" and got["answer"]["text"] == "4321" and got["answer"]["via"] == "bot"


async def test_answer_reads_last_reply_after_handled_and_rights(client, ui, db, clock, world):
    agent, _ = _agent(db)
    q = await _say(client, {"text": "как назвать?", "ask": True, "ask_ttl_s": 0}, token=agent)
    first = await _say(client, {"text": "Альфа", "reply_to": q["id"]}, ui=ui)
    clock.advance_ms(1000)
    second = await _say(client, {"text": "нет, Бета", "reply_to": q["id"]}, ui=ui)
    r = await client.post(f"{API}/owner_channel/talk/{first['id']}/handled", json={}, headers=_h(agent))
    assert r.status_code == 200
    r = await client.post(f"{API}/owner_channel/talk/{second['id']}/handled", json={}, headers=_h(agent))
    assert r.status_code == 200
    got = await _answer_of(client, agent, q["id"])
    assert got["state"] == "answered"
    assert got["answer"]["id"] == second["id"] and got["answer"]["text"] == "нет, Бета"
    assert got["answer"]["handled_ts"] is not None and got["answer"]["reply_to"] == q["id"]
    # Не вопрос агента, нет реплики — 404; кука и бот — 403.
    plain = await _say(client, {"text": "просто реплика"}, token=agent)
    await _answer_of(client, agent, plain["id"], expect=404)
    await _answer_of(client, agent, first["id"], expect=404)
    await _answer_of(client, agent, 10**6, expect=404)
    assert (await ui.get(f"{API}/owner_channel/talk/{q['id']}/answer")).status_code == 403
    bot, _ = _bot(db)
    await _answer_of(client, bot, q["id"], expect=403)
    # Закрытый агентом и истёкший — состояния.
    q2 = await _say(client, {"text": "?", "ask": True}, token=agent)
    r = await client.post(
        f"{API}/owner_channel/talk/{q2['id']}/closed", json={"outcome": "gave_up"}, headers=_h(agent)
    )
    assert r.status_code == 200
    assert (await _answer_of(client, agent, q2["id"]))["state"] == "closed"
    q3 = await _say(client, {"text": "?", "ask": True, "ask_ttl_s": 60}, token=agent)
    clock.advance_ms(61_000)
    assert (await _answer_of(client, agent, q3["id"]))["state"] == "expired"


# --- E: вопрос по поручению ---------------------------------------------------------------------------------


async def test_errand_ask_idempotent_by_operation_key(client, ui, db, clock, world):
    w = world
    agent, sid = await _setup(client, ui, db, w)
    ref = f"req:{await _permission(client, w, sid)}"
    made = (await _take(client, agent, ref))["errand"]
    eid = made["id"]
    q = await _ask(client, agent, eid, {"operation_key": "sms-1", "text": "код из СМС?", "ask_ttl_s": 0})
    assert q["ask"] is True and q["ref"] == ref and q["author"] == "agent" and q["ask_until_ts"] is None
    count = _talk_count(db)
    again = await _ask(client, agent, eid, {"operation_key": "sms-1", "text": "другой текст"})
    assert again["id"] == q["id"] and again["text"] == "код из СМС?" and _talk_count(db) == count
    other = await _ask(client, agent, eid, {"operation_key": "sms-2", "text": "ещё?", "ask_ttl_s": 600})
    assert other["id"] != q["id"] and other["ask_until_ts"] == _now(clock) + 600_000
    assert _errand_row(db, eid)[1]["ask_ops"] == {"sms-1": q["id"], "sms-2": other["id"]}
    # Открытый вопрос виден в карточке (последний) — поручение не «зависает».
    assert _refs(await _queue(client, ui=ui))[ref]["agent_ask"]["id"] == other["id"]
    # Пределы и права.
    await _ask(client, agent, eid, {"operation_key": "", "text": "x"}, expect=422)
    await _ask(client, agent, eid, {"operation_key": "k" * 101, "text": "x"}, expect=422)
    await _ask(client, agent, eid, {"operation_key": "k", "text": "x", "ask_ttl_s": 30}, expect=422)
    await _ask(client, agent, eid, {"operation_key": "k", "text": " "}, expect=422)
    await _ask(client, agent, 10**6, {"operation_key": "k", "text": "x"}, expect=404)
    r = await ui.post(f"{API}/owner_channel/errands/{eid}/ask", {"operation_key": "k", "text": "x"})
    assert r.status_code == 403
    # Не больше 50 ключей: старые вытесняются.
    for n in range(55):
        await _ask(client, agent, eid, {"operation_key": f"op-{n}", "text": f"вопрос {n}"})
    ops = _errand_row(db, eid)[1]["ask_ops"]
    assert len(ops) == zq.ASK_OPS_MAX and "sms-1" not in ops and "op-54" in ops
    # Закрытое поручение — 409 (и повтор старого ключа тоже).
    await _answer(client, ref, {"action": "allow"}, token=agent)
    await _ask(client, agent, eid, {"operation_key": "op-54", "text": "x"}, expect=409)


# --- F: состояние поручения ---------------------------------------------------------------------------------


async def test_errand_state_open_moved_cancelled(client, ui, db, clock, world):
    w = world
    agent, sid = await _setup(client, ui, db, w)
    ref = f"req:{await _permission(client, w, sid)}"
    made = await _errand(ui, ref)
    eid = made["id"]
    st = await _state(client, agent, eid)
    assert st == {
        "id": eid,
        "state": "open",
        "can_act": True,
        "target_ref": ref,
        "closed_by": None,
        "closed_ts": None,
        "result": None,
        "returned": None,
        # Ф96-1: руки — у агента, пока их не передавали.
        "hands": "agent",
        "hands_by": None,
        "hands_ts": None,
        "hands_why": None,
        "hands_note": None,
    }
    assert (await _state(client, agent, eid, target=ref))["can_act"] is True
    assert (await _state(client, agent, eid, target="req:999999"))["can_act"] is False
    assert (await ui.get(f"{API}/owner_channel/errands/{eid}/state")).status_code == 403
    await _state(client, agent, 10**6, expect=404)
    # Владелец отозвал — закрыто, читается.
    r = await ui.c.delete(f"{API}/owner_channel/errands/{eid}", headers=ui.headers)
    assert r.status_code == 200, r.text
    st = await _state(client, agent, eid)
    assert st["state"] == "closed" and st["can_act"] is False and st["closed_by"] == "ui"
    assert st["returned"] == zq.STOPPED_BY_OWNER and st["closed_ts"] == _now(clock)


async def test_errand_state_card_closed_elsewhere_and_result(client, ui, db, clock, world):
    w = world
    agent, sid = await _setup(client, ui, db, w)
    ref = f"req:{await _permission(client, w, sid)}"
    made = (await _take(client, agent, ref))["errand"]
    # Карточка ещё открыта, но поручение ещё не закрыто ядром — `can_act` по карточке.
    rid = int(ref.split(":")[1])
    r = await ui.post(f"{API}/session-requests/{rid}/decide", {"decision": "deny"})
    assert r.status_code == 200, r.text
    st = await _state(client, agent, made["id"])
    assert st["can_act"] is False
    # Агент ответил с итогом — `result`.
    ref2 = f"req:{await _permission(client, w, sid)}"
    made2 = (await _take(client, agent, ref2))["errand"]
    await _answer(client, ref2, {"action": "allow", "result": "разрешил"}, token=agent)
    st2 = await _state(client, agent, made2["id"])
    assert st2["state"] == "closed" and st2["closed_by"] == "agent" and st2["result"] == "разрешил"


# --- D: что закрывает поручение -----------------------------------------------------------------------------


async def test_errand_with_open_question_is_not_closed_by_core(client, ui, db, clock, world):
    """Поручение с бессрочным вопросом без шагов ядро само не закрывает (минутный цикл и чтение очереди)."""
    w = world
    agent, sid = await _setup(client, ui, db, w)
    ref = f"req:{await _permission(client, w, sid)}"
    made = (await _take(client, agent, ref))["errand"]
    await _ask(client, agent, made["id"], {"operation_key": "wait", "text": "ждать?", "ask_ttl_s": 0})
    for _ in range(4):
        clock.advance_ms(10 * MIN)
        assert db.write_sync(lambda conn: zq.return_hung(conn, now_ms=_now(clock))) == []
        await _queue(client, agent)
        await _queue(client, ui=ui)
    assert _errand_row(db, made["id"])[0] is None
    assert (await _state(client, agent, made["id"]))["can_act"] is True
    await _progress(client, agent, made["id"], "жду ответа владельца")
    assert _errand_row(db, made["id"])[1]["progress"] == "жду ответа владельца"


# --- C: отказ модели — повод перейти на запасной ------------------------------------------------------------


async def test_refusal_and_transient_switch_only_agent(client, ui, db, app, home, world):  # noqa: F811
    agent, _ = _agent(db)
    facts = _facts(db)
    await ui.put(f"{API}/engine/fallback/agent", {"engine": "codex"})
    await ui.put(f"{API}/engine/facts", {"engine": "claude"})
    await ui.put(f"{API}/engine/fallback/facts", {"engine": "codex"})
    # У фактов отказ модели и временный сбой — не повод.
    for kind in ("refusal", "transient"):
        r = await client.post(f"{API}/engine/fallback/facts/failed", json={"kind": kind}, headers=_h(facts))
        assert r.status_code == 200 and r.json()["switched"] is False, r.text
    # Кривой вид — 422 (после прав).
    r = await client.post(f"{API}/engine/fallback/agent/failed", json={"kind": "nope"}, headers=_h(agent))
    assert r.status_code == 422 and "refusal|transient" in r.text
    for kind in ("refusal", "transient"):
        r = await client.post(f"{API}/engine/fallback/agent/failed", json={"kind": kind}, headers=_h(agent))
        body = r.json()
        assert r.status_code == 200 and body["switched"] is True, r.text
        assert body["on_fallback"]["kind"] == kind and body["effective"]["engine"] == "codex"
        await ui.post(f"{API}/engine/fallback/agent/return")
    # `retry` и `other` у агента — по-прежнему не повод.
    for kind in ("retry", "other"):
        r = await client.post(f"{API}/engine/fallback/agent/failed", json={"kind": kind}, headers=_h(agent))
        assert r.json()["switched"] is False


async def test_agent_fallback_same_engine_other_model(client, ui, db, home, world):  # noqa: F811
    agent, _ = _agent(db)
    main = await _engine(ui)
    assert main["engine"] == "claude" and main["model"] == "sonnet"
    # Совпадает с основным целиком (явно и по умолчанию) — 422; у фактов тот же движок — 422, как было.
    for body in ({"engine": "claude"}, {"engine": "claude", "model": "sonnet"}):
        r = await ui.put(f"{API}/engine/fallback/agent", body)
        assert r.status_code == 422, r.text
    await ui.put(f"{API}/engine/facts", {"engine": "claude"})
    r = await ui.put(f"{API}/engine/fallback/facts", {"engine": "claude", "model": "opus"})
    assert r.status_code == 422, r.text
    # Тот же движок с другой моделью — запасной агента.
    r = await ui.put(f"{API}/engine/fallback/agent", {"engine": "claude", "model": "opus"})
    assert r.status_code == 200, r.text
    assert r.json()["fallback"] == {"engine": "claude", "model": "opus", "effort": None}
    r = await client.post(f"{API}/engine/fallback/agent/failed", json={"kind": "refusal"}, headers=_h(agent))
    body = r.json()
    assert body["switched"] is True
    assert body["effective"] == {"engine": "claude", "model": "opus", "effort": None}
    assert body["on_fallback"]["main"] == {"engine": "claude", "model": "sonnet", "effort": None}
    # Выбор владельца не изменился; снимает «на запасном» только владелец.
    status = await _engine(ui)
    assert status["model"] == "sonnet" and status["agent_fallback"]["model"] == "opus"
    r = await client.post(f"{API}/engine/fallback/agent/failed", json={"kind": "limit"}, headers=_h(agent))
    assert r.json()["switched"] is False
    assert (await ui.post(f"{API}/engine/fallback/agent/return")).json()["returned"] is True
    # Основной стал таким же, как запасной, — перехода нет.
    await ui.put(f"{API}/engine", {"engine": "claude", "model": "opus"})
    r = await client.post(f"{API}/engine/fallback/agent/failed", json={"kind": "refusal"}, headers=_h(agent))
    assert r.json()["switched"] is False and "совпадает" in r.json()["why"]


# --- 24.10: вопросы закрытого поручения не висят открытыми ------------------------------------------------


async def test_returned_errand_closes_its_open_asks(client, ui, db, clock, world):
    """Случай 24.10: агент спросил по одной карточке и вернул её владельцу, потом спросил по другой. Первый вопрос
    висел открытым до срока, бот видел два открытых и ответ владельца без reply не принял («Ответь на сообщение
    с вопросом»). Возврат поручения закрывает его вопросы — у бота открыт только свежий."""
    from .test_f65_errand_life import _return

    w = world
    agent, sid = await _setup(client, ui, db, w)
    bot, _ = _bot(db)
    ref1 = f"req:{await _permission(client, w, sid)}"
    ref2 = f"req:{await _permission(client, w, sid)}"
    e1 = (await _take(client, agent, ref1))["errand"]["id"]
    q1 = await _ask(client, agent, e1, {"operation_key": "a", "text": "первый?"})
    await _return(client, agent, e1, {"reason": "нужен владелец"})
    e2 = (await _take(client, agent, ref2))["errand"]["id"]
    q2 = await _ask(client, agent, e2, {"operation_key": "b", "text": "второй?"})

    r = await client.get(f"{API}/owner_channel/talk/asks", params={"since_ms": 0}, headers=_h(bot))
    states = {a["id"]: a["state"] for a in r.json()["items"]}
    assert states[q1["id"]] == "closed" and states[q2["id"]] == "open"
    # Ответ на закрытый — 409, на открытый — проходит.
    r = await client.post(
        f"{API}/owner_channel/talk/bot-reply",
        json={"reply_to": q1["id"], "text": "x", "tg_message_id": 1},
        headers=_h(bot),
    )
    assert r.status_code == 409, r.text
    r = await client.post(
        f"{API}/owner_channel/talk/bot-reply",
        json={"reply_to": q2["id"], "text": "да", "tg_message_id": 2},
        headers=_h(bot),
    )
    assert r.status_code == 200, r.text
    # Отвеченный вопрос закрытие поручения не портит: остаётся «answered».
    await _answer(client, ref2, {"action": "allow"}, token=agent)
    assert (await _answer_of(client, agent, q2["id"]))["state"] == "answered"
