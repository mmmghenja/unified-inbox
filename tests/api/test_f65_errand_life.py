"""Ф65-1 (docs/architecture.md, проверка — раздел 4): карточка у агента владельца.

- Ж1: ответ владельца при открытом поручении закрывает его (`ui`) и исполняется; агент после этого — 409 на шаг и
  на ответ; агент успел первым — владелец получает обычное «уже решена», не `errand_open`;
- Ж2: поручение без шага 5 минут ядро возвращает владельцу (`system`, «завис: …») — при чтении очереди и минутным
  циклом; шаг и открытый вопрос агента владельцу по карточке держат;
- Ж3: возврат с `detail` (маскируется, до 2000), `errand_returned = {reason, detail, ts, by}`;
- Ж4: итог агента `result` (до 300, только агент) — в поручении и в `GET /owner_channel/closed` (`errand_result`);
- Ж5: открытый вопрос агента по карточке — `agent_ask`; ответ владельца в разговоре его снимает;
- Ж6 (Ф66-1: таблица маршрута вместо `agent_auto`) — `auto_ok` карточки, авторешение 409 `auto_off`, «Реши»
  можно и по карточке `me`, 422 на чужое;
- Ж7: вернувшуюся карточку агент сам не берёт (409 `auto_returned`), после «Реши» — снова его.

Мир и помощники — из test_hub_sessions.py, test_f41_autopilot_5b.py, test_f56_queue.py."""

import json

from hub.core import card_route
from hub.core import owner_queue as zq
from hub.core import runner as core_runner

from . import test_hub_sessions as base
from .test_f41_autopilot_5b import _auto
from .test_f56_queue import _agent, _answer, _channel_on, _permission, _queue, _refs
from .test_hub_sessions import _h, _now

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"
MIN = 60_000


async def _errand(ui, ref, text="реши сам", expect=200) -> dict:
    r = await ui.post(f"{API}/owner_channel/{ref}/errand", {"text": text})
    assert r.status_code == expect, r.text
    return r.json().get("errand")


async def _take(client, agent, ref, expect=200) -> dict:
    r = await client.post(f"{API}/owner_channel/{ref}/errand", json={}, headers=_h(agent))
    assert r.status_code == expect, r.text
    return r.json()


async def _progress(client, agent, errand_id, text, expect=200):
    r = await client.post(
        f"{API}/owner_channel/errands/{errand_id}/progress", json={"text": text}, headers=_h(agent)
    )
    assert r.status_code == expect, r.text
    return r.json()


async def _return(client, agent, errand_id, body, expect=200):
    r = await client.post(f"{API}/owner_channel/errands/{errand_id}/return", json=body, headers=_h(agent))
    assert r.status_code == expect, r.text
    return r.json()


def _errand_row(db, errand_id) -> tuple[str | None, dict]:
    with db.reader() as conn:
        by, payload = conn.execute(
            "SELECT closed_by, payload_json FROM main.owner_item WHERE id = ?", (errand_id,)
        ).fetchone()
    return by, json.loads(payload)


async def _setup(client, ui, db, w):
    agent, _ = _agent(db)
    sid = await _auto(client, ui, w, mode="manual")
    await _channel_on(ui, w, sid)
    return agent, sid


# --- Ж1 ---------------------------------------------------------------------------------------------------


async def test_owner_answer_closes_errand_and_agent_stops(client, ui, db, clock, world):
    w = world
    agent, sid = await _setup(client, ui, db, w)
    ref = f"req:{await _permission(client, w, sid)}"
    made = (await _take(client, agent, ref))["errand"]
    await _progress(client, agent, made["id"], "смотрю команду")

    assert (await _answer(client, ref, {"action": "deny"}, ui=ui))["status"] == "denied"
    by, payload = _errand_row(db, made["id"])
    assert by == "ui" and payload["returned"] == zq.TAKEN_BY_OWNER
    # Агент узнаёт по 409 на шаг и не может ответить.
    await _progress(client, agent, made["id"], "ещё шаг", expect=409)
    await _answer(client, ref, {"action": "allow"}, token=agent, expect=409)
    assert f"errand:{made['id']}" not in _refs(await _queue(client, agent))


async def test_agent_first_then_owner_gets_plain_conflict(client, ui, db, clock, world):
    w = world
    agent, sid = await _setup(client, ui, db, w)
    ref = f"req:{await _permission(client, w, sid)}"
    made = (await _take(client, agent, ref))["errand"]
    assert (await _answer(client, ref, {"action": "allow"}, token=agent))["status"] == "allowed"
    r = await ui.post(f"{API}/owner_channel/{ref}/answer", {"action": "deny"})
    assert r.status_code == 404 or (r.status_code == 409 and "errand_open" not in r.text), r.text
    assert _errand_row(db, made["id"])[0] == "agent"


async def test_failed_owner_answer_keeps_errand_open(client, ui, db, clock, world):
    """Ответ владельца и закрытие поручения — одна транзакция: отказ ответа (422) поручение не трогает."""
    w = world
    agent, sid = await _setup(client, ui, db, w)
    rid = await _permission(client, w, sid)
    made = (await _take(client, agent, f"req:{rid}"))["errand"]
    r = await ui.post(f"{API}/session-requests/{rid}/decide", {"decision": "deny", "message": "x" * 5000})
    assert r.status_code == 422, r.text
    assert _errand_row(db, made["id"])[0] is None


# --- Ж2 ---------------------------------------------------------------------------------------------------


async def test_hung_errand_returns_to_owner_on_read(client, ui, db, clock, world):
    w = world
    agent, sid = await _setup(client, ui, db, w)
    ref = f"req:{await _permission(client, w, sid)}"
    made = await _errand(ui, ref)

    clock.advance_ms(4 * MIN)
    assert _refs(await _queue(client, ui=ui))[ref]["errand"]["id"] == made["id"]
    await _progress(client, agent, made["id"], "жду ответ сайта")  # шаг отодвигает срок
    clock.advance_ms(4 * MIN)
    card = _refs(await _queue(client, ui=ui))[ref]
    assert card["errand"]["progress_ts"] == _now(clock) - 4 * MIN
    clock.advance_ms(MIN + 1)
    card = _refs(await _queue(client, agent))[ref]
    assert card["errand"] is None
    returned = card["errand_returned"]
    assert returned["by"] == "system" and returned["detail"] is None and returned["ts"] == _now(clock)
    assert returned["reason"] == "завис: последний шаг «жду ответ сайта» 5 мин назад"
    assert f"errand:{made['id']}" not in _refs(await _queue(client, agent))
    await _progress(client, agent, made["id"], "ожил", expect=409)
    # Ж7: зависшую агент сам снова не берёт.
    r = await client.post(f"{API}/owner_channel/{ref}/errand", json={}, headers=_h(agent))
    assert r.status_code == 409 and r.json()["detail"].startswith(zq.AUTO_RETURNED_CODE), r.text


async def test_hung_errand_returned_by_minute_loop_and_without_steps(client, ui, db, clock, world):
    w = world
    _agent_token, sid = await _setup(client, ui, db, w)
    ref = f"req:{await _permission(client, w, sid)}"
    made = await _errand(ui, ref)
    clock.advance_ms(zq.HUNG_MS - 1)
    assert db.write_sync(lambda conn: zq.return_hung(conn, now_ms=_now(clock))) == []
    clock.advance_ms(1)
    db.write_sync(lambda conn: core_runner._cleanup_all(conn, _now(clock)))  # noqa: SLF001 — минутный цикл
    by, payload = _errand_row(db, made["id"])
    assert by == "system" and payload["returned"] == "завис: ни одного шага 5 мин"


async def test_open_agent_ask_is_not_hang(client, ui, db, clock, world):
    w = world
    agent, sid = await _setup(client, ui, db, w)
    ref = f"req:{await _permission(client, w, sid)}"
    made = (await _take(client, agent, ref))["errand"]
    r = await client.post(
        f"{API}/owner_channel/talk",
        json={"text": "какой код из СМС?", "ref": ref, "ask": True, "ask_ttl_s": 1800},
        headers=_h(agent),
    )
    assert r.status_code == 200, r.text
    ask_id = r.json()["id"]
    clock.advance_ms(10 * MIN)
    card = _refs(await _queue(client, ui=ui))[ref]
    assert card["errand"]["id"] == made["id"]
    # Ж5: вопрос виден в карточке.
    assert card["agent_ask"] == {
        "id": ask_id,
        "text": "какой код из СМС?",
        "ts": _now(clock) - 10 * MIN,
        "until_ts": _now(clock) - 10 * MIN + 1800 * 1000,
    }
    # Владелец ответил в разговоре — вопрос снят; ответ — тоже шаг (Ф85-1): поручение живо ещё 5 минут.
    r = await ui.post(f"{API}/owner_channel/talk", {"text": "1234", "reply_to": ask_id})
    assert r.status_code == 200, r.text
    card = _refs(await _queue(client, ui=ui))[ref]
    assert card["agent_ask"] is None and card["errand"]["id"] == made["id"]
    clock.advance_ms(4 * MIN)
    assert _refs(await _queue(client, ui=ui))[ref]["errand"]["id"] == made["id"]
    # Шагов после ответа 5 минут не было — поручение вернулось.
    clock.advance_ms(MIN)
    card = _refs(await _queue(client, ui=ui))[ref]
    assert card["errand"] is None and card["errand_returned"]["by"] == "system"


# --- Ж3, Ж4 ------------------------------------------------------------------------------------------------


async def test_return_detail_masked_and_limits(client, ui, db, clock, world):
    w = world
    agent, sid = await _setup(client, ui, db, w)
    ref = f"req:{await _permission(client, w, sid)}"
    made = (await _take(client, agent, ref))["errand"]
    await _return(client, agent, made["id"], {"reason": "не вышло", "detail": "x" * 2001}, expect=422)
    detail = "пробовал: вход token=abc123\nнужно: войди сам"
    back = (await _return(client, agent, made["id"], {"reason": "не вышло: вход", "detail": detail}))[
        "errand"
    ]
    assert back["returned_detail"] == "пробовал: вход token=***\nнужно: войди сам"
    returned = _refs(await _queue(client, ui=ui))[ref]["errand_returned"]
    assert returned == {
        "reason": "не вышло: вход",
        "detail": "пробовал: вход token=***\nнужно: войди сам",
        "ts": _now(clock),
        "by": "agent",
    }


async def test_agent_result_is_kept_and_only_agent_writes_it(client, ui, db, clock, world):
    w = world
    agent, sid = await _setup(client, ui, db, w)
    ref = f"req:{await _permission(client, w, sid)}"
    made = (await _take(client, agent, ref))["errand"]
    r = await ui.post(f"{API}/owner_channel/{ref}/answer", {"action": "allow", "result": "сам"})
    assert r.status_code == 422, r.text
    await _answer(client, ref, {"action": "allow", "result": "x" * 301}, token=agent, expect=422)
    assert _errand_row(db, made["id"])[0] is None  # 422 ничего не закрыл
    body = {"action": "allow", "result": "разрешил: команда только читает token=abc123"}
    await _answer(client, ref, body, token=agent)
    by, payload = _errand_row(db, made["id"])
    assert by == "agent" and payload["result"] == "разрешил: команда только читает token=***"
    closed = (await ui.get(f"{API}/owner_channel/closed")).json()["items"]
    errand_row = next(c for c in closed if c["ref"] == f"errand:{made['id']}")
    answer_row = next(c for c in closed if c["ref"] == ref)
    assert errand_row["errand_result"] == answer_row["errand_result"] == payload["result"]
    assert answer_row["closed_by"] == "agent"


# --- Ж6 ---------------------------------------------------------------------------------------------------


def test_groups_cover_all_kinds():
    assert set(card_route.GROUP_OF) == set(zq.KINDS)


async def test_card_route_patch_auto_ok_and_reshi_on_me_card(client, ui, db, clock, world):
    """Ф66-1 (A.6, B.2, П2): таблица пишется только кукой, кривое — 422; карточка `me` агенту не видна и сам
    он её не берёт (409 `auto_off`), «Реши» владельца — можно, и ответ агента по поручению проходит насквозь."""
    w = world
    agent, sid = await _setup(client, ui, db, w)
    pid = w["p3"]
    projects = {p["id"]: p for p in (await ui.get(f"{API}/projects")).json()}
    assert projects[pid]["card_route"] == {g: "agent" for g in card_route.GROUPS}

    for bad in (
        {"card_route": {"nope": "me"}},
        {"card_route": {"permission": "no"}},
        {"card_route": {}},
        {"card_route": {"question": "auto"}},
    ):
        r = await ui.c.patch(f"{API}/projects/{pid}", json=bad, headers=ui.headers)
        assert r.status_code == 422, (bad, r.text)
    r = await client.patch(
        f"{API}/projects/{pid}", json={"card_route": {"permission": "me"}}, headers=_h(agent)
    )
    assert r.status_code in (401, 403), r.text
    r = await ui.c.patch(
        f"{API}/projects/{pid}", json={"card_route": {"permission": "me"}}, headers=ui.headers
    )
    assert r.status_code == 200, r.text
    projects = {p["id"]: p for p in (await ui.get(f"{API}/projects")).json()}
    assert projects[pid]["card_route"]["permission"] == "me"
    assert projects[pid]["card_route"]["question"] == "agent"  # частичное слияние

    ref = f"req:{await _permission(client, w, sid)}"
    assert ref not in _refs(await _queue(client, agent))  # маршрут `me` — агент не видит
    assert _refs(await _queue(client, ui=ui))[ref]["auto_ok"] is False
    r = await client.post(f"{API}/owner_channel/{ref}/errand", json={}, headers=_h(agent))
    assert r.status_code == 409 and r.json()["detail"].startswith(zq.AUTO_OFF_CODE), r.text
    await _answer(client, ref, {"action": "allow"}, token=agent, expect=403)
    # «Реши» владельца по карточке `me` — агент видит её, отвечает, поручение закрыто им.
    made = await _errand(ui, ref)
    card = _refs(await _queue(client, agent))[ref]
    assert card["auto_ok"] is False
    assert (await _answer(client, ref, {"action": "allow"}, token=agent))["status"] == "allowed"
    assert _errand_row(db, made["id"])[0] == "agent"

    # Снова `agent` — авторешение можно.
    r = await ui.c.patch(
        f"{API}/projects/{pid}", json={"card_route": {"permission": "agent"}}, headers=ui.headers
    )
    assert r.status_code == 200, r.text
    ref2 = f"req:{await _permission(client, w, sid)}"
    assert _refs(await _queue(client, agent))[ref2]["auto_ok"] is True
    await _take(client, agent, ref2)
    audits = [a for a in _audits_of(db, "card_route") if a["project_id"] == pid]
    assert [a["new"]["permission"] for a in audits][-2:] == ["me", "agent"]


def _audits_of(db, action) -> list[dict]:
    with db.reader() as conn:
        rows = conn.execute("SELECT detail_json FROM main.audit WHERE action = ? ORDER BY id", (action,))
        return [json.loads(row[0]) for row in rows.fetchall()]


# --- Ж7 ---------------------------------------------------------------------------------------------------


async def test_returned_card_not_taken_again_until_owner_says(client, ui, db, clock, world):
    w = world
    agent, sid = await _setup(client, ui, db, w)
    ref = f"req:{await _permission(client, w, sid)}"
    made = (await _take(client, agent, ref))["errand"]
    await _return(client, agent, made["id"], {"reason": "нужен владелец"})
    r = await client.post(f"{API}/owner_channel/{ref}/errand", json={}, headers=_h(agent))
    assert r.status_code == 409 and r.json()["detail"].startswith(zq.AUTO_RETURNED_CODE), r.text
    # Владелец говорит в карточке, как дальше, — поручение снова у агента.
    again = await _errand(ui, ref, "попробуй через вход по почте")
    assert _refs(await _queue(client, agent))[f"errand:{again['id']}"]["full"]["text"] == (
        "попробуй через вход по почте"
    )
    await _return(client, agent, again["id"], {"reason": "снова не вышло"})
    await _take(client, agent, ref, expect=409)
    # Остановил владелец — тоже сам не берёт.
    ref2 = f"req:{await _permission(client, w, sid)}"
    third = (await _take(client, agent, ref2))["errand"]
    r = await ui.c.delete(f"{API}/owner_channel/errands/{third['id']}", headers=ui.headers)
    assert r.status_code == 200, r.text
    assert _refs(await _queue(client, ui=ui))[ref2]["errand_returned"]["by"] == "ui"
    await _take(client, agent, ref2, expect=409)


# --- «Реши» по ответу сессии, а сессия сделала ещё ход (живая жалоба 15.10) --------------------------------


async def test_errand_on_turn_moves_to_next_turn_card(client, ui, db, clock, world):
    """Поручение по карточке `turn` не умирает, когда следующий ход сессии вытесняет карточку: оно переезжает
    на новую карточку (шаги агента проходят), ответ агента по старому `ref` — реплика сессии, закрывающая и
    новую карточку, и поручение (`agent`)."""
    from .test_owner_turn import ASK, _turn_cards, _Turns

    w = world
    agent, sid = await _setup(client, ui, db, w)
    turns = _Turns(client, w, sid)
    await turns.start()
    await turns.end(ASK)
    old, new = f"turn:{sid}:1", f"turn:{sid}:2"
    made = await _errand(ui, old, "напиши «накатывай»")
    await _progress(client, agent, made["id"], "читаю карточку")

    await turns.start()  # сессии пришёл сигнал — ещё ход с «ВЛАДЕЛЬЦУ:»
    await turns.end(ASK)
    assert list(_turn_cards(await _queue(client, ui=ui))) == [new]
    await _queue(client, agent)  # чтение очереди закрывает поручения закрытых карточек — это не закрыто
    by, payload = _errand_row(db, made["id"])
    assert by is None and payload["target"] == new and payload["prev_targets"] == [old]
    await _progress(client, agent, made["id"], "пишу ответ")

    body = {"action": "reply", "text": "накатывай", "result": "написал «накатывай»"}
    assert (await _answer(client, old, body, token=agent))["status"] == "queued"
    assert _turn_cards(await _queue(client, ui=ui)) == {}
    by, payload = _errand_row(db, made["id"])
    assert by == "agent" and payload["result"] == "написал «накатывай»"
