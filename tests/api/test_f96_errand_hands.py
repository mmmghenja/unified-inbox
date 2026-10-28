"""Ф96-1 (docs/architecture.md, B.1, поправки П1–П8): руки посреди поручения агента.

- `POST /owner_channel/errands/{id}/hands` — кука и бот (агент — 403): туда-обратно, повтор — без изменений
  (`changed: false`, `seq` не двигается), закрытое — 409, нет — 404, иное тело — 422; кнопка бота со старым
  `hands_s` — 409 `stale` с поручением; аудит без текста; записка маскируется; возврат агенту — срок зависания
  с нуля; бот ответом свайпом (`reply_to`) пишет реплику владельца (`via='bot'`, сразу «обработана»), повтор по
  `tg_message_id` — без второй;
- `POST /owner_channel/errands/{id}/handoff` — только агент: руки у владельца и одна реплика `tell` «Передаю тебе: …» на
  ключ; повтор ключа после возврата рук руки не трогает (П7);
- `errand_state`, ответ `progress`, карточка у владельца и заявка боту несут руки; зависание не срабатывает при
  руках у владельца до 2 ч и срабатывает после; ответ владельца и «Остановить агента» закрывают как раньше;
- заявка «нужен человек»: срок сдвигается (П1), поручение не снимается по сроку, `at_owner` при руках у владельца
  (П3), `seq` заявки движется (П4);
- `asks`/`tells` бота: `errand_id`, `hands`, `hands_ts`, `handoff`; поручение, переехавшее с карточки вопроса,
  находится (П6).

Мир и помощники — test_hub_sessions.py, test_f56_queue.py, test_f65_errand_life.py,
test_f85_durable_errand.py, test_f93_2_human_note.py."""

import json

from hub.core import owner_queue as zq

from . import test_hub_sessions as base
from .card_helpers import _route
from .test_f54_human_requests import _create, _row
from .test_f56_queue import _agent, _answer, _bot, _permission, _queue, _refs
from .test_f65_errand_life import _errand_row, _progress, _setup, _take
from .test_f85_durable_errand import _ask, _state, _talk_count
from .test_hub_sessions import ROLE, A, _h, _now, _sub

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"
MIN = 60_000
HOUR = 60 * MIN


async def _hands(client, token, errand_id, body, expect=200) -> dict:
    r = await client.post(f"{API}/owner_channel/errands/{errand_id}/hands", json=body, headers=_h(token))
    assert r.status_code == expect, r.text
    return r.json()


async def _ui_hands(ui, errand_id, body, expect=200) -> dict:
    r = await ui.post(f"{API}/owner_channel/errands/{errand_id}/hands", body)
    assert r.status_code == expect, r.text
    return r.json()


async def _handoff(client, agent, errand_id, body, expect=200) -> dict:
    r = await client.post(f"{API}/owner_channel/errands/{errand_id}/handoff", json=body, headers=_h(agent))
    assert r.status_code == expect, r.text
    return r.json()


def _seq(db, errand_id) -> int:
    with db.reader() as conn:
        return conn.execute("SELECT seq FROM main.owner_item WHERE id = ?", (errand_id,)).fetchone()[0]


def _errand_audit(db) -> list[tuple[str, dict]]:
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT result, detail_json FROM main.audit WHERE action = 'owner_errand' ORDER BY id"
        ).fetchall()
    return [(r[0], json.loads(r[1])) for r in rows]


def _talk(db, talk_id) -> dict:
    with db.reader() as conn:
        row = conn.execute(
            "SELECT author, via, reply_to, tg_msg_id, handled_ts, tell, text, read_ts FROM main.owner_talk "
            "WHERE id = ?",
            (talk_id,),
        ).fetchone()
    keys = ("author", "via", "reply_to", "tg_msg_id", "handled_ts", "tell", "text", "read_ts")
    return dict(zip(keys, row, strict=True))


# --- A.1: руки туда и обратно ------------------------------------------------------------------------------


async def test_hands_ui_and_bot_roundtrip_rights_repeat_audit(client, ui, db, clock, world):
    w = world
    agent, sid = await _setup(client, ui, db, w)
    bot, _ = _bot(db)
    ref = f"req:{await _permission(client, w, sid)}"
    eid = (await _take(client, agent, ref))["errand"]["id"]
    assert (await _state(client, agent, eid))["hands"] == "agent"

    # Владелец забирает руки (кука).
    got = await _ui_hands(ui, eid, {"to": "owner_channel"})
    assert got["changed"] is True
    e = got["errand"]
    assert (e["hands"], e["hands_by"], e["hands_ts"], e["hands_why"]) == ("owner_channel", "ui", _now(clock), None)
    seq = _seq(db, eid)
    again = await _ui_hands(ui, eid, {"to": "owner_channel"})  # повтор кнопки — без изменений
    assert again["changed"] is False and again["errand"] == e and _seq(db, eid) == seq
    # Агенту ручка рук закрыта; он видит руки в состоянии, в ответе шага и в карточке у владельца.
    await _hands(client, agent, eid, {"to": "agent"}, expect=403)
    st = await _state(client, agent, eid)
    assert (st["state"], st["can_act"], st["hands"], st["hands_by"]) == ("open", True, "owner_channel", "ui")
    stepped = (await _progress(client, agent, eid, "жду: руки у владельца"))["errand"]
    assert stepped["hands"] == "owner_channel"
    card = _refs(await _queue(client, ui=ui))[ref]
    assert card["errand"]["hands"] == "owner_channel" and card["errand"]["hands_ts"] == e["hands_ts"]

    # Бот отдаёт руки агенту с запиской — маскируется; срок зависания с нуля.
    clock.advance_ms(4 * MIN)
    back = await _hands(client, bot, eid, {"to": "agent", "note": "дату ввёл, token=abc12345 продолжай"})
    assert back["changed"] is True and back["errand"]["hands"] == "agent"
    assert back["errand"]["hands_by"] == "bot" and back["errand"]["progress_ts"] == _now(clock)
    st = await _state(client, agent, eid)
    assert st["hands_note"] == "дату ввёл, token=*** продолжай"
    assert (await _hands(client, bot, eid, {"to": "agent", "note": "ещё"}))["changed"] is False
    assert (await _state(client, agent, eid))["hands_note"] == "дату ввёл, token=*** продолжай"
    # Пустая записка — «Владелец вернул управление».
    await _ui_hands(ui, eid, {"to": "owner_channel"})
    await _ui_hands(ui, eid, {"to": "agent"})
    assert (await _state(client, agent, eid))["hands_note"] == zq.HANDS_NOTE_DEFAULT

    # Неверное тело — 422; нет поручения — 404.
    await _ui_hands(ui, eid, {"to": "кто-то"}, expect=422)
    await _ui_hands(ui, eid, {"to": "owner_channel", "note": "x"}, expect=422)
    await _ui_hands(ui, eid, {"to": "agent", "note": "x" * (zq.HANDS_NOTE_MAX + 1)}, expect=422)
    await _ui_hands(ui, eid, {"to": "agent", "note": "x", "reply_to": 1}, expect=422)  # reply_to — только бот
    await _ui_hands(ui, 10**6, {"to": "owner_channel"}, expect=404)

    # Аудит — без текста записки.
    rows = [(r, d) for r, d in _errand_audit(db) if r.startswith("hands_")]
    assert [r for r, _ in rows] == ["hands_owner_channel", "hands_agent", "hands_owner_channel", "hands_agent"]
    assert all(set(d) == {"errand_id", "ref", "by"} for _, d in rows)
    with db.reader() as conn:
        dump = " ".join(r[0] or "" for r in conn.execute("SELECT detail_json FROM main.audit").fetchall())
    assert "дату ввёл" not in dump

    # «Остановить агента» при руках у владельца закрывает как раньше; дальше — 409.
    await _ui_hands(ui, eid, {"to": "owner_channel"})
    r = await ui.c.delete(f"{API}/owner_channel/errands/{eid}", headers=ui.headers)
    assert r.status_code == 200, r.text
    assert _errand_row(db, eid)[0] == "ui"
    await _ui_hands(ui, eid, {"to": "agent"}, expect=409)
    await _hands(client, bot, eid, {"to": "owner_channel"}, expect=409)


async def test_owner_answer_closes_errand_with_hands_at_owner(client, ui, db, clock, world):
    w = world
    agent, sid = await _setup(client, ui, db, w)
    ref = f"req:{await _permission(client, w, sid)}"
    eid = (await _take(client, agent, ref))["errand"]["id"]
    await _ui_hands(ui, eid, {"to": "owner_channel"})
    assert (await _answer(client, ref, {"action": "deny"}, ui=ui))["status"] == "denied"
    by, payload = _errand_row(db, eid)
    assert by == "ui" and payload["returned"] == zq.TAKEN_BY_OWNER
    assert (await _state(client, agent, eid))["state"] == "closed"


async def test_stale_button_returns_current_errand(client, ui, db, clock, world):
    w = world
    agent, sid = await _setup(client, ui, db, w)
    bot, _ = _bot(db)
    ref = f"req:{await _permission(client, w, sid)}"
    eid = (await _take(client, agent, ref))["errand"]["id"]
    # Кнопка «Беру сам», нарисованная до любой передачи рук, — `hands_s = 0`.
    taken = await _hands(client, bot, eid, {"to": "owner_channel", "hands_s": 0})
    sec = taken["errand"]["hands_ts"] // 1000
    clock.advance_ms(5_000)
    await _ui_hands(ui, eid, {"to": "agent"})  # владелец вернул в интерфейсе
    # Старая «▶ Продолжай» (секунды прошлой передачи) и старая «Беру сам» — устарели.
    for body in ({"to": "agent", "hands_s": sec}, {"to": "owner_channel", "hands_s": 0}):
        r = await client.post(f"{API}/owner_channel/errands/{eid}/hands", json=body, headers=_h(bot))
        assert r.status_code == 409, r.text
        assert r.json()["detail"].startswith(zq.HANDS_STALE_CODE)
        assert r.json()["errand"]["hands"] == "agent" and r.json()["errand"]["id"] == eid
    assert (await _state(client, agent, eid))["hands"] == "agent"
    now_s = (await _state(client, agent, eid))["hands_ts"] // 1000
    assert (await _hands(client, bot, eid, {"to": "owner_channel", "hands_s": now_s}))["changed"] is True


# --- A.2: агент отдаёт руки сам ----------------------------------------------------------------------------


async def test_handoff_one_tell_per_key_and_repeat_after_return(client, ui, db, clock, world):
    w = world
    agent, sid = await _setup(client, ui, db, w)
    bot, _ = _bot(db)
    ref = f"req:{await _permission(client, w, sid)}"
    eid = (await _take(client, agent, ref))["errand"]["id"]
    why = (
        "кнопка «Далее» не жмётся; окно: profile hooli, вкладка X; нужно: пройти шаг руками token=s3cr3t1"
    )

    count = _talk_count(db)
    got = await _handoff(client, agent, eid, {"operation_key": "h-1", "why": why})
    e, talk = got["errand"], got["talk"]
    assert (e["hands"], e["hands_by"], e["hands_ts"]) == ("owner_channel", "agent", _now(clock))
    assert e["hands_why"] == why.replace("token=s3cr3t1", "token=***")
    assert talk["author"] == "agent" and talk["ref"] == ref and talk["ask"] is False
    assert talk["text"] == zq.HANDOFF_PREFIX + e["hands_why"] and _talk(db, talk["id"])["tell"] == 1
    assert _talk_count(db) == count + 1
    # Повтор того же ключа — та же строка, без второй реплики.
    again = await _handoff(client, agent, eid, {"operation_key": "h-1", "why": "другое"})
    assert again["talk"]["id"] == talk["id"] and again.get("repeat") is True and _talk_count(db) == count + 1

    # Бот видит сообщение как «Передаю тебе» по поручению; обычный `tell` — не `handoff`.
    plain = await client.post(
        f"{API}/owner_channel/errands/{eid}/tell", json={"operation_key": "t-1", "text": "жду"}, headers=_h(agent)
    )
    r = await client.get(f"{API}/owner_channel/talk/tells", params={"since_ms": 0}, headers=_h(bot))
    items = {i["id"]: i for i in r.json()["items"]}
    mine = items[talk["id"]]
    assert (mine["errand_id"], mine["hands"], mine["hands_ts"], mine["handoff"]) == (
        eid,
        "owner_channel",
        e["hands_ts"],
        True,
    )
    assert items[plain.json()["id"]]["handoff"] is False

    # П7: руки вернулись агенту, повтор старого ключа — та же строка, руки остаются у агента.
    await _ui_hands(ui, eid, {"to": "agent"})
    seq = _seq(db, eid)
    third = await _handoff(client, agent, eid, {"operation_key": "h-1", "why": why})
    assert third["talk"]["id"] == talk["id"] and third["errand"]["hands"] == "agent" and _seq(db, eid) == seq
    # Новый ключ — новая передача.
    fourth = await _handoff(client, agent, eid, {"operation_key": "h-2", "why": "снова застрял"})
    assert fourth["talk"]["id"] != talk["id"] and fourth["errand"]["hands"] == "owner_channel"
    assert [r for r, _ in _errand_audit(db) if r == "handoff"] == ["handoff", "handoff"]

    # Права и пределы.
    await _handoff(client, agent, eid, {"operation_key": "k", "why": "  "}, expect=422)
    too_long = "x" * (zq.HANDS_WHY_MAX + 1)
    await _handoff(client, agent, eid, {"operation_key": "k", "why": too_long}, expect=422)
    await _handoff(client, agent, eid, {"operation_key": "", "why": "x"}, expect=422)
    await _handoff(client, bot, eid, {"operation_key": "k", "why": "x"}, expect=403)
    r = await ui.post(f"{API}/owner_channel/errands/{eid}/handoff", {"operation_key": "k", "why": "x"})
    assert r.status_code == 403
    await _handoff(client, agent, 10**6, {"operation_key": "k", "why": "x"}, expect=404)
    await _answer(client, ref, {"action": "allow"}, token=agent)
    await _handoff(client, agent, eid, {"operation_key": "k2", "why": "x"}, expect=409)


async def test_bot_reply_to_handoff_returns_hands_and_writes_talk(client, ui, db, clock, world):
    """B.3, П2: ответ владельца свайпом на «Передаю тебе» — руки агенту и реплика владельца (одна на сообщение)."""
    w = world
    agent, sid = await _setup(client, ui, db, w)
    bot, _ = _bot(db)
    ref = f"req:{await _permission(client, w, sid)}"
    eid = (await _take(client, agent, ref))["errand"]["id"]
    tell_id = (await _handoff(client, agent, eid, {"operation_key": "h", "why": "капча"}))["talk"]["id"]
    count = _talk_count(db)

    body = {"to": "agent", "note": "  прошёл капчу, жми дальше  ", "reply_to": tell_id, "tg_message_id": 77}
    got = await _hands(client, bot, eid, body)
    assert got["changed"] is True and got["errand"]["hands"] == "agent"
    assert (await _state(client, agent, eid))["hands_note"] == "прошёл капчу, жми дальше"
    assert _talk_count(db) == count + 1
    with db.reader() as conn:
        new_id = conn.execute("SELECT MAX(id) FROM main.owner_talk").fetchone()[0]
    row = _talk(db, new_id)
    assert (row["author"], row["via"], row["reply_to"], row["tg_msg_id"]) == ("owner_channel", "bot", tell_id, 77)
    assert row["text"] == "прошёл капчу, жми дальше" and row["handled_ts"] == _now(clock)
    assert _talk(db, tell_id)["read_ts"] == _now(clock)
    # Повтор того же сообщения Telegram — без второй реплики и без изменений.
    again = await _hands(client, bot, eid, body)
    assert again.get("repeat") is True and again["changed"] is False and _talk_count(db) == count + 1
    # Ответ на чужую реплику, без текста, без номера сообщения Telegram у не-бота — 422.
    await _hands(client, bot, eid, {"to": "agent", "note": "x", "reply_to": 10**6}, expect=422)
    await _hands(client, bot, eid, {"to": "agent", "note": " ", "reply_to": tell_id}, expect=422)
    await _hands(client, bot, eid, {"to": "agent", "tg_message_id": 5}, expect=422)


# --- A.4: зависание ---------------------------------------------------------------------------------------


async def test_hung_waits_two_hours_with_hands_at_owner(client, ui, db, clock, world):
    w = world
    agent, sid = await _setup(client, ui, db, w)
    ref = f"req:{await _permission(client, w, sid)}"
    eid = (await _take(client, agent, ref))["errand"]["id"]
    await _ui_hands(ui, eid, {"to": "owner_channel"})
    clock.advance_ms(2 * HOUR - MIN)
    card = _refs(await _queue(client, ui=ui))[ref]  # чтение очереди возвращает зависшие
    assert card["errand"] is not None and card["errand"]["hands"] == "owner_channel"
    clock.advance_ms(MIN)
    card = _refs(await _queue(client, ui=ui))[ref]
    assert card["errand"] is None
    assert card["errand_returned"]["reason"] == zq.HANDS_HUNG and card["errand_returned"]["by"] == "system"
    assert _errand_row(db, eid)[0] == "system"


async def test_return_to_agent_restarts_hung_clock(client, ui, db, clock, world):
    w = world
    agent, sid = await _setup(client, ui, db, w)
    ref = f"req:{await _permission(client, w, sid)}"
    eid = (await _take(client, agent, ref))["errand"]["id"]
    await _ui_hands(ui, eid, {"to": "owner_channel"})
    clock.advance_ms(HOUR)
    await _ui_hands(ui, eid, {"to": "agent", "note": "сделал"})
    clock.advance_ms(zq.HUNG_MS - 1)
    assert _refs(await _queue(client, ui=ui))[ref]["errand"]["id"] == eid
    clock.advance_ms(1)
    card = _refs(await _queue(client, ui=ui))[ref]
    assert card["errand"] is None and card["errand_returned"]["reason"].startswith("завис:")


# --- П1, П3, П4: заявка «нужен человек» --------------------------------------------------------------------


async def test_human_request_held_while_hands_at_owner(client, ui, db, clock, world):
    w = world
    await _sub(client, w, A, role=ROLE)
    await _route(ui, w, {"human": "agent"})
    agent, _ = _agent(db)
    bot, _ = _bot(db)
    t0 = _now(clock)
    hid = (await _create(client, w, project_id=w["p3"], ttl_min=30))["request"]["id"]
    assert _row(db, hid).expires_ts == t0 + 30 * MIN
    eid = (await _take(client, agent, f"human:{hid}"))["errand"]["id"]
    item = await _bot_one(client, bot, hid)
    assert item["at_owner"] is False and item["errand"]["hands"] == "agent"

    # Руки к владельцу (кнопка бота): срок заявки — не раньше двух часов, `seq` заявки новый, заявка у владельца.
    clock.advance_ms(MIN)
    seq0 = _row(db, hid).seq
    await _hands(client, bot, eid, {"to": "owner_channel"})
    assert _row(db, hid).seq > seq0
    assert _row(db, hid).expires_ts == _now(clock) + zq.HANDS_MAX_MS
    item = await _bot_one(client, bot, hid)
    assert item["at_owner"] is True and item["errand"]["hands"] == "owner_channel" and item["errand"]["id"] == eid
    assert (await ui.get(f"{API}/human-requests/{hid}")).json()["errand"]["hands"] == "owner_channel"

    # Прежний срок (30 мин) прошёл — заявка ждёт, поручение открыто (чтение очереди снимает просроченное).
    clock.advance_ms(99 * MIN)
    await _queue(client, ui=ui)
    await _queue(client, agent)
    assert _row(db, hid).status == "pending" and _errand_row(db, eid)[0] is None

    # Руки агенту на сотой минуте: срок — не меньше получаса от возврата.
    seq1 = _row(db, hid).seq
    await _ui_hands(ui, eid, {"to": "agent", "note": "капчу прошёл"})
    assert _row(db, hid).seq > seq1
    assert _row(db, hid).expires_ts == _now(clock) + zq.HANDS_BACK_MS
    assert (await _bot_one(client, bot, hid))["at_owner"] is False
    # Готово по заявке («h:») при открытом поручении закрывает его, как раньше.
    r = await client.post(f"{API}/human-requests/{hid}/done", json={}, headers=_h(bot, 8))
    assert r.status_code == 200, r.text
    assert _errand_row(db, eid)[0] == "ui"


async def test_human_request_without_ttl_gets_deadline_only_when_needed(client, ui, db, clock, world):
    w = world
    await _sub(client, w, A, role=ROLE)
    await _route(ui, w, {"human": "agent"})
    agent, _ = _agent(db)
    t0 = _now(clock)
    hid = (await _create(client, w, project_id=w["p3"], ttl_min=0))["request"]["id"]
    eid = (await _take(client, agent, f"human:{hid}"))["errand"]["id"]
    await _ui_hands(ui, eid, {"to": "owner_channel"})
    assert _row(db, hid).expires_ts is None  # сутки от создания длиннее двух часов — срока нет
    await _ui_hands(ui, eid, {"to": "agent"})
    clock.advance_ms(23 * HOUR)
    await _progress(client, agent, eid, "иду дальше")
    await _ui_hands(ui, eid, {"to": "owner_channel"})
    assert _row(db, hid).expires_ts == t0 + 25 * HOUR


# --- B.1, П6: вопросы и сообщения агента для бота ---------------------------------------------------------


async def test_asks_carry_errand_and_follow_moved_errand(client, ui, db, clock, world):
    w = world
    agent, sid = await _setup(client, ui, db, w)
    bot, _ = _bot(db)
    ref = f"req:{await _permission(client, w, sid)}"
    eid = (await _take(client, agent, ref))["errand"]["id"]
    q = await _ask(client, agent, eid, {"operation_key": "q", "text": "код?"})
    general = (
        await client.post(f"{API}/owner_channel/talk", json={"text": "общий", "ask": True}, headers=_h(agent))
    ).json()

    async def asks() -> dict[int, dict]:
        r = await client.get(f"{API}/owner_channel/talk/asks", params={"since_ms": 0}, headers=_h(bot))
        assert r.status_code == 200, r.text
        return {i["id"]: i for i in r.json()["items"]}

    got = await asks()
    mine = got[q["id"]]
    assert (mine["errand_id"], mine["hands"], mine["hands_ts"]) == (eid, "agent", None)
    assert (got[general["id"]]["errand_id"], got[general["id"]]["hands"]) == (None, None)  # без поручения
    await _ui_hands(ui, eid, {"to": "owner_channel"})
    assert (await asks())[q["id"]]["hands"] == "owner_channel"
    # Поручение переехало на другую карточку (`_carry_errands`): вопрос по старой цели его находит.
    now = _now(clock)
    db.write_sync(lambda conn: zq._carry_errands(conn, [ref], "turn:1:1", now_ms=now))  # noqa: SLF001
    assert (await asks())[q["id"]]["errand_id"] == eid
    # Поручение закрыто — у вопроса поручения нет.
    db.write_sync(lambda conn: zq.close_errands(conn, [eid], now_ms=now))
    assert (await asks())[q["id"]]["errand_id"] is None


async def _bot_one(client, bot, human_id) -> dict:
    r = await client.get(f"{API}/human-requests/{human_id}", headers=_h(bot, 8))
    assert r.status_code == 200, r.text
    return r.json()
