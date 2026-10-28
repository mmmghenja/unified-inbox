"""Ф93-2 (docs/architecture.md): причина исхода заявки «нужен человек» и тишина бота.

- A: `decided_note` — телом `{"note"}` ручек `done|failed|later` (агент, кука, бот) и текстом ответа канала
  `POST /owner_channel/human:<id>/answer`; видна автору (`GET /human-requests` линии), владельцу и боту; без тела — как
  прежде (NULL); длиннее 1000 — 422;
- B: `at_owner` в выдаче бота: маршрут `me` без поручения — да; маршрут `agent` или открытое поручение — нет;
  агент вернул поручение — да (и новый `seq`, бот узнаёт страницей изменений); возврат заявки в
  `GET /owner_channel/returned` не попадает (бот шлёт саму заявку)."""

from api import test_hub_sessions as base
from api.card_helpers import _route
from api.test_f54_human_requests import API, _create, _mine, _row
from api.test_f56_queue import _agent, _bot
from api.test_f65_errand_life import _errand, _return, _take
from api.test_hub_sessions import ROLE, A, _h, _sub

procs = base.procs
ui = base.ui
world = base.world

NOTE = "сайт просит СМС на номер, которого нет в моих данных"


async def _changes(client, bot, since=0) -> dict[int, dict]:
    r = await client.get(f"{API}/human-requests/changes", params={"since": since}, headers=_h(bot, 8))
    assert r.status_code == 200, r.text
    return {i["id"]: i for i in r.json()["items"]}


async def _bot_one(client, bot, human_id) -> dict:
    r = await client.get(f"{API}/human-requests/{human_id}", headers=_h(bot, 8))
    assert r.status_code == 200, r.text
    return r.json()


async def _new(client, w, **fields) -> int:
    return (await _create(client, w, project_id=w["p3"], **fields))["request"]["id"]


# --- A: причина исхода -------------------------------------------------------------------------------------


async def test_note_from_agent_rest_seen_by_author_owner_and_bot(client, ui, db, world):
    w = world
    await _sub(client, w, A, role=ROLE)
    await _route(ui, w, {"human": "agent"})
    agent, _ = _agent(db)
    bot, _ = _bot(db)
    hid = await _new(client, w, kind="other")
    r = await client.post(
        f"{API}/human-requests/{hid}/failed", json={"note": f"  {NOTE}  "}, headers=_h(agent)
    )
    assert r.status_code == 200, r.text
    assert r.json()["decided_note"] == NOTE
    assert _row(db, hid).decided_note == NOTE
    [mine] = await _mine(client, w, status="failed")
    assert mine["decided_note"] == NOTE and "decided_by" not in mine  # автору — причина, не источник
    seen = await _bot_one(client, bot, hid)
    assert seen["decided_note"] == NOTE and seen["decided_by"] == "agent"
    assert (await ui.get(f"{API}/human-requests", params={"status": "failed"})).json()[0][
        "decided_note"
    ] == NOTE


async def test_note_optional_empty_and_too_long(client, ui, db, world):
    w = world
    await _sub(client, w, A, role=ROLE)
    await _route(ui, w, {"human": "agent"})
    agent, _ = _agent(db)
    one, two, three = (
        await _new(client, w, url="u1"),
        await _new(client, w, url="u2"),
        await _new(client, w, url="u3"),
    )
    r = await client.post(f"{API}/human-requests/{one}/failed", headers=_h(agent))  # без тела — как раньше
    assert r.status_code == 200 and r.json()["decided_note"] is None
    r = await client.post(f"{API}/human-requests/{two}/later", json={"note": "   "}, headers=_h(agent))
    assert r.status_code == 200 and _row(db, two).decided_note is None
    r = await client.post(f"{API}/human-requests/{three}/done", json={"note": "я" * 1001}, headers=_h(agent))
    assert r.status_code == 422 and _row(db, three).status == "pending"
    r = await ui.post(f"{API}/human-requests/{three}/done", {"note": "сделал сам"})
    assert r.status_code == 200 and _row(db, three).decided_note == "сделал сам"


async def test_note_from_channel_answer_text(client, ui, db, world):
    w = world
    await _sub(client, w, A, role=ROLE)
    await _route(ui, w, {"human": "agent"})
    agent, _ = _agent(db)
    hid = await _new(client, w, url="a")
    r = await client.post(
        f"{API}/owner_channel/human:{hid}/answer", json={"action": "failed", "text": NOTE}, headers=_h(agent)
    )
    assert r.status_code == 200, r.text
    assert (_row(db, hid).status, _row(db, hid).decided_note) == ("failed", NOTE)
    other = await _new(client, w, url="b")
    r = await ui.post(f"{API}/owner_channel/human:{other}/answer", {"action": "later", "text": "завтра"})
    assert r.status_code == 200, r.text
    assert _row(db, other).decided_note == "завтра"
    late = await _new(client, w, url="c")
    r = await client.post(
        f"{API}/owner_channel/human:{late}/answer", json={"action": "done", "text": "я" * 1001}, headers=_h(agent)
    )
    assert r.status_code == 422 and _row(db, late).status == "pending"


# --- B: у владельца ли заявка (бот) ------------------------------------------------------------------------------


async def test_at_owner_route_me_and_agent(client, ui, db, world):
    w = world
    await _sub(client, w, A, role=ROLE)
    bot, _ = _bot(db)
    mine = await _new(client, w, url="me")
    assert (await _changes(client, bot))[mine]["at_owner"] is True  # маршрут по умолчанию — «Мне»
    await _route(ui, w, {"human": "agent"})
    agents = await _new(client, w, url="agent")
    assert (await _changes(client, bot))[agents]["at_owner"] is False
    # «Деньги» — по умолчанию «Мне»: заявка `payment` у владельца и при маршруте `agent`.
    pay = await _new(client, w, url="pay", kind="payment")
    assert (await _bot_one(client, bot, pay))["at_owner"] is True
    # Список ждущих (сверка бота) несёт то же поле; маршрут — на момент выдачи (первая теперь тоже у агента).
    pending = (
        await client.get(f"{API}/human-requests", params={"status": "pending"}, headers=_h(bot, 8))
    ).json()
    assert {i["id"]: i["at_owner"] for i in pending} == {mine: False, agents: False, pay: True}


async def test_at_owner_errand_taken_returned_and_owner_solve(client, ui, db, world):
    w = world
    await _sub(client, w, A, role=ROLE)
    await _route(ui, w, {"human": "agent"})
    agent, _ = _agent(db)
    bot, _ = _bot(db)
    hid = await _new(client, w)
    seq0 = _row(db, hid).seq
    errand = (await _take(client, agent, f"human:{hid}"))["errand"]
    seq1 = _row(db, hid).seq
    assert seq1 > seq0  # поручение открылось — новый `seq` заявки
    assert (await _changes(client, bot, seq0))[hid]["at_owner"] is False
    await _return(
        client, agent, errand["id"], {"reason": "не вышло: нужен владелец", "detail": "капча не грузится"}
    )
    item = (await _changes(client, bot, seq1))[hid]
    assert item["at_owner"] is True and item["status"] == "pending"  # возврат — заявка у владельца
    r = await client.get(f"{API}/owner_channel/returned", params={"since_ms": 0}, headers=_h(bot))
    assert r.status_code == 200 and r.json()["items"] == []  # без второго сообщения «↩ … открой хаб»
    # «Реши» владельца по заявке с маршрутом «Мне» — пока поручение открыто, не у владельца.
    await _route(ui, w, {"human": "me"})
    other = await _new(client, w, url="solve")
    assert (await _bot_one(client, bot, other))["at_owner"] is True
    await _errand(ui, f"human:{other}")
    assert (await _bot_one(client, bot, other))["at_owner"] is False
