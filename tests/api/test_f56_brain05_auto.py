"""Ф56-brain-05, п. 7.

- заявка «нужен человек» в канале агента — по таблице маршрута проекта (Ф66-1), и у внешней сессии;
- агент берёт карточку канала себе поручением (`POST /owner_channel/{ref}/errand` с `{}`): подпись «решаю сам»,
  `auto` в поручении, отзыв владельца снимает поручение (Ф65-1: кнопки владельца не 409, а закрывают поручение —
  test_f65_errand_life.py); с действием или текстом — 422; второе поручение — 409.

Мир и помощники — из test_f56_queue.py."""

from . import test_hub_sessions as base
from .card_helpers import _route
from .test_f41_autopilot_5b import _auto
from .test_f54_human_requests import _create as _human_create
from .test_f56_queue import _agent, _answer, _channel_on, _queue, _refs
from .test_hub_sessions import ROLE, A, _h, _hs, _sub

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"


async def _take(client, token, ref, body=None, expect=200) -> dict:
    r = await client.post(f"{API}/owner_channel/{ref}/errand", json=body or {}, headers=_h(token))
    assert r.status_code == expect, r.text
    return r.json()


async def test_human_of_external_session_in_channel_by_switch_only(client, ui, db, world):
    w = world
    agent, _ = _agent(db)
    await _sub(client, w, A, role=ROLE)
    made = (await _human_create(client, w))["request"]  # внешняя сессия, не сессия хаба
    ref = f"human:{made['id']}"
    card = _refs(await _queue(client, ui=ui))[ref]
    assert card["session_id"] is None and card["in_channel"] is False
    assert ref not in _refs(await _queue(client, agent))
    await _route(ui, w, {"human": "agent"})
    assert _refs(await _queue(client, ui=ui))[ref]["in_channel"] is True
    assert ref in _refs(await _queue(client, agent))
    await _route(ui, w, {"human": "me"})
    assert ref not in _refs(await _queue(client, agent))
    await _answer(client, ref, {"action": "done"}, token=agent, expect=403)


async def test_agent_takes_card_itself(client, ui, db, world):
    w = world
    agent, _ = _agent(db)
    sid = await _auto(client, ui, w, mode="manual")
    made = (await _human_create(client, w, session=_hs(sid)))["request"]
    ref = f"human:{made['id']}"
    await _take(client, agent, ref, expect=409)  # маршрут `me` — не в канале
    await _channel_on(ui, w, sid)
    await _take(client, agent, ref, {"text": "сам"}, expect=422)
    await _take(client, agent, ref, {"action": "solve"}, expect=422)
    errand = (await _take(client, agent, ref))["errand"]
    assert errand["label"] == "решаю сам" and errand["action"] is None and errand["text"] is None
    await _take(client, agent, ref, expect=409)  # одно открытое поручение на карточку

    shown = _refs(await _queue(client, ui=ui))[ref]
    assert shown["errand"]["id"] == errand["id"] and shown["errand"]["label"] == "решаю сам"
    ecard = _refs(await _queue(client, agent))[f"errand:{errand['id']}"]
    assert ecard["full"]["auto"] is True and ecard["full"]["target"] == ref
    # Владелец забирает — поручение снято, карточка у него.
    r = await ui.c.delete(f"{API}/owner_channel/errands/{errand['id']}", headers=ui.headers)
    assert r.status_code == 200, r.text
    assert _refs(await _queue(client, ui=ui))[ref]["errand"] is None
    await _answer(client, ref, {"action": "done"}, ui=ui)
