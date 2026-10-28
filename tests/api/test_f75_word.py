"""Ф75-1 «по слову» (docs/architecture.md): агент отвечает
по карточке вне своего канала (маршрут «Мне», ручная сессия хаба) и запускает по ней «Реши», только когда
передаёт `word: {talk_id}` — годную реплику владельца из разговора «Мой агент» (`core/owner_word.py`).

- `GET /owner_channel/queue?scope=all` — агенту вся открытая очередь (с `in_channel=false`), по умолчанию — как было;
- без `word` вне канала — 403 `not_in_channel`; с чужой, агентской, старой, ранней, из чужой ветки репликой —
  403 `word_rejected`; с верной — решено, аудит `owner_answer` с `word_talk_id`;
- одна реплика открывает не больше 3 карточек (ответы и «Реши» вместе); `word` от куки — 422."""

from . import test_hub_sessions as base
from .test_f41_autopilot_5b import _auto
from .test_f54_human_requests import _create as _human_create
from .test_f56_queue import _agent, _answer, _permission, _queue, _refs
from .test_hub_sessions import _audits, _h, _hs

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"


async def _say(ui, text: str, session_id=None) -> int:
    body = {"text": text} if session_id is None else {"text": text, "session_id": session_id}
    r = await ui.post(f"{API}/owner_channel/talk", body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _errand(client, ref, body, *, token, expect=200) -> dict:
    r = await client.post(f"{API}/owner_channel/{ref}/errand", json=body, headers=_h(token))
    assert r.status_code == expect, r.text
    return r.json()


async def test_scope_all_shows_cards_outside_the_channel(client, ui, db, world):
    agent, _ = _agent(db)
    sid = await _auto(client, ui, world, mode="manual")  # ручная сессия — всегда маршрут «Мне»
    ref = f"req:{await _permission(client, world, sid)}"
    assert ref not in _refs(await _queue(client, agent))
    r = await client.get(f"{API}/owner_channel/queue?scope=all", headers=_h(agent))
    assert r.status_code == 200, r.text
    card = _refs(r.json())[ref]
    assert card["in_channel"] is False and card["auto_ok"] is False and card["agent_actions"] == []
    r = await client.get(f"{API}/owner_channel/queue?scope=everything", headers=_h(agent))
    assert r.status_code == 422


async def test_word_lets_agent_answer_card_routed_to_owner(client, ui, db, clock, world):
    agent, _ = _agent(db)
    sid = await _auto(client, ui, world, mode="manual")
    other = await _auto(client, ui, world, mode="manual", role="аналитик")
    first = f"req:{await _permission(client, world, sid)}"
    clock.advance_ms(1000)

    await _answer(client, first, {"action": "allow"}, token=agent, expect=403)  # без слова — как было
    word_other_branch = await _say(ui, "разреши", session_id=other)
    r = await ui.post(f"{API}/owner_channel/talk", {"text": "думаю"})
    assert r.status_code == 200
    agent_talk = (await client.post(f"{API}/owner_channel/talk", json={"text": "сделаю"}, headers=_h(agent))).json()[
        "id"
    ]
    for talk_id, why in (
        (agent_talk, "не владельца"),
        (word_other_branch, "другой сессии"),
        (10**9, "реплики нет"),
    ):
        r = await client.post(
            f"{API}/owner_channel/{first}/answer",
            json={"action": "allow", "word": {"talk_id": talk_id}},
            headers=_h(agent),
        )
        assert (
            r.status_code == 403
            and r.json()["detail"].startswith("word_rejected")
            and why in r.json()["detail"]
        )

    word = await _say(ui, "разреши", session_id=sid)
    clock.advance_ms(31 * 60 * 1000)
    stale = {"action": "allow", "word": {"talk_id": word}}
    r = await client.post(f"{API}/owner_channel/{first}/answer", json=stale, headers=_h(agent))
    assert r.status_code == 403 and "старше 30 минут" in r.json()["detail"]

    # Реплика раньше карточки — не слово для неё.
    early = await _say(ui, "разреши")
    clock.advance_ms(1000)
    late = f"req:{await _permission(client, world, sid)}"
    r = await client.post(
        f"{API}/owner_channel/{late}/answer", json={"action": "allow", "word": {"talk_id": early}}, headers=_h(agent)
    )
    assert r.status_code == 403 and "раньше карточки" in r.json()["detail"]

    fresh = await _say(ui, "разреши обе", session_id=sid)
    done = await _answer(client, first, {"action": "allow", "word": {"talk_id": fresh}}, token=agent)
    assert done == {"ref": first, "status": "allowed"}
    general = await _say(ui, "и эту")  # «Общее» годится для карточки любой сессии
    await _answer(client, late, {"action": "deny", "word": {"talk_id": general}}, token=agent)
    audits = [a for a in _audits(db, "owner_answer") if "word_talk_id" in a]
    assert [(a["ref"], a["word_talk_id"]) for a in audits] == [(first, fresh), (late, general)]
    queue = _refs(await _queue(client, ui=ui))
    assert first not in queue and late not in queue

    # `word` — только у агента.
    third = f"req:{await _permission(client, world, sid)}"
    r = await ui.post(f"{API}/owner_channel/{third}/answer", {"action": "allow", "word": {"talk_id": fresh}})
    assert r.status_code == 422


async def test_one_word_opens_at_most_three_cards(client, ui, db, clock, world):
    agent, _ = _agent(db)
    sid = await _auto(client, ui, world, mode="manual")
    refs = [f"req:{await _permission(client, world, sid)}" for _ in range(4)]
    clock.advance_ms(1000)
    word = {"talk_id": await _say(ui, "разреши все", session_id=sid)}
    await _answer(client, refs[0], {"action": "allow", "word": word}, token=agent)
    await _answer(client, refs[1], {"action": "deny", "word": word}, token=agent)
    taken = await _errand(client, refs[2], {"word": word}, token=agent)  # «Реши» по слову — третья
    assert taken["errand"]["label"] == "по твоему слову в разговоре"
    r = await client.post(
        f"{API}/owner_channel/{refs[3]}/answer", json={"action": "allow", "word": word}, headers=_h(agent)
    )
    assert r.status_code == 403 and "не больше 3" in r.json()["detail"]
    r = await client.post(f"{API}/owner_channel/{refs[3]}/errand", json={"word": word}, headers=_h(agent))
    assert r.status_code == 403
    # Новая реплика — новый счёт.
    again = {"talk_id": await _say(ui, "и последнюю", session_id=sid)}
    await _answer(client, refs[3], {"action": "allow", "word": again}, token=agent)
    errands = [a for a in _audits(db, "owner_errand") if a.get("word_talk_id") == word["talk_id"]]
    assert [a["ref"] for a in errands] == [refs[2]]


async def test_errand_by_word_skips_auto_off(client, ui, db, clock, world):
    agent, _ = _agent(db)
    sid = await _auto(client, ui, world, mode="manual")
    ref = f"req:{await _permission(client, world, sid)}"
    r = await client.post(f"{API}/owner_channel/{ref}/errand", json={}, headers=_h(agent))
    assert r.status_code == 409 and r.json()["detail"].startswith("auto_off")  # без слова — как было
    clock.advance_ms(1000)
    word = {"talk_id": await _say(ui, "реши", session_id=sid)}
    errand = (await _errand(client, ref, {"word": word}, token=agent))["errand"]
    assert errand["auto"] is True and errand["label"] == "по твоему слову в разговоре"
    card = _refs(await _queue(client, agent))[ref]  # с открытым поручением карточка у агента и в канале
    assert card["errand"]["id"] == errand["id"]
    r = await ui.post(f"{API}/owner_channel/{ref}/errand", {"text": "реши", "word": word})
    assert r.status_code == 422


async def test_human_request_routed_to_owner_decided_by_word(client, ui, db, clock, world):
    """Заявка «нужен человек» сверяет маршрут сама (`human_requests.agent_sees`): по слову владельца — проходит,
    без слова — 403 очереди (раньше функции вида)."""
    agent, _ = _agent(db)
    sid = await _auto(client, ui, world, mode="manual")
    made = (await _human_create(client, world, session=_hs(sid)))["request"]
    ref = f"human:{made['id']}"
    await _answer(client, ref, {"action": "done"}, token=agent, expect=403)
    clock.advance_ms(1000)
    word = {"talk_id": await _say(ui, "отметь как готово", session_id=sid)}
    done = await _answer(client, ref, {"action": "done", "word": word}, token=agent)
    assert done["ref"] == ref and done["status"] == "done"
    r = await ui.get(f"{API}/human-requests/{made['id']}")
    assert r.status_code == 200 and r.json()["decided_by"] == "agent", r.text
