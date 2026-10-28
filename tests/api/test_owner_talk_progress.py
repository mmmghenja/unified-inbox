"""Строка хода агента у реплики владельца в «Мой агент»: `PUT /owner_channel/talk/{id}/progress` — только агент
(`human`); строка заменяется, новой реплики
нет; у реплики агента и у обработанной — 409; пустая и длинная — 422; снимок очереди несёт `talk_progress`."""

from . import test_hub_sessions as base
from .test_f56_queue import _agent, _bot, _queue
from .test_hub_sessions import _h
from .test_owner_talk import _feed, _say

procs = base.procs
ui = base.ui

API = "/api/v1"


async def _progress(client, talk_id, text, token, expect=200):
    r = await client.put(f"{API}/owner_channel/talk/{talk_id}/progress", json={"text": text}, headers=_h(token))
    assert r.status_code == expect, r.text
    return r.json()


async def test_progress_replaces_line_and_wakes_queue(client, ui, db, clock):
    agent, _ = _agent(db)
    mine = await _say(client, {"text": "что сегодня по проектам"}, ui=ui)
    before = (await _queue(client, ui=ui))["agent"]["talk_progress"]
    row = await _progress(client, mine["id"], "1. сводки дня — идёт", agent)
    assert row["progress"] == "1. сводки дня — идёт" and row["progress_ts"] == int(clock() * 1000)
    clock.advance_ms(5000)
    row = await _progress(client, mine["id"], "1. сводки дня — готово, 7 с\n2. поиск — идёт", agent)
    assert row["progress"].endswith("поиск — идёт")
    feed = (await _feed(client, ui=ui))["items"]
    assert len(feed) == 1 and feed[0]["progress"] == row["progress"]  # новой реплики нет
    assert (await _queue(client, ui=ui))["agent"]["talk_progress"] == row["progress_ts"] != before
    # Обработана — строка больше не пишется, снимок её не считает.
    r = await client.post(f"{API}/owner_channel/talk/{mine['id']}/handled", json={}, headers=_h(agent))
    assert r.status_code == 200
    await _progress(client, mine["id"], "ещё", agent, expect=409)
    assert (await _queue(client, ui=ui))["agent"]["talk_progress"] == 0


async def test_progress_rights_and_checks(client, ui, db):
    agent, _ = _agent(db)
    bot, _ = _bot(db)
    mine = await _say(client, {"text": "вопрос"}, ui=ui)
    theirs = await _say(client, {"text": "ответ"}, token=agent)
    await _progress(client, mine["id"], "x", bot, expect=403)
    r = await ui.c.put(f"{API}/owner_channel/talk/{mine['id']}/progress", json={"text": "x"}, headers=ui.headers)
    assert r.status_code == 403
    await _progress(client, theirs["id"], "x", agent, expect=409)
    await _progress(client, mine["id"], "  ", agent, expect=422)
    await _progress(client, mine["id"], "я" * 2001, agent, expect=422)
    await _progress(client, 999_999, "x", agent, expect=404)
