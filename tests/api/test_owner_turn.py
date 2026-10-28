"""Ф56-1б (docs/architecture.md): карточка `turn` — сессия хаба закончила ход и
ждёт владельца.

- карточка заводится всегда после хода с доставленной репликой владельца из интерфейса (и с хвостом ответа, и без
  ответа), а после хода по сигналу или по реплике агента — только по строке «ВЛАДЕЛЬЦУ:»;
- одна открытая карточка на сессию (`superseded`), прерванный ход карточки не даёт;
- ответ `reply` — реплика сессии с источником ответа, второй ответ — 409; реплика из окна закрывает карточку;
  `ok`; закрытие сессии; маскировка; агент видит только карточки «в канале»; номер хода — в ядре и не
  повторяется после рестарта хоста.

Мир — test_hub_sessions.py; хост поддельный (bearer `work_host` зовёт ручки ядра напрямую)."""

import json

from hub.core import card_route, held

from . import test_hub_sessions as base
from .test_f41_autopilot import PROC
from .test_f41_autopilot_5b import _auto
from .test_f56_queue import _agent, _answer, _channel_on, _queue, _refs
from .test_hub_sessions import _host

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"
OWNER_CHANNEL = "Готово, ветка f1.\n\nВЛАДЕЛЬЦУ: нужен доступ к серверу"


def _answer_body(full: str, brief: str, kind: str, cut: bool = False) -> dict:
    return {"full": full, "brief": brief, "brief_kind": kind, "cut": cut}


TAIL = _answer_body("Сделал всё.\n\nПоследний абзац", "Последний абзац", "tail")
ASK = _answer_body(OWNER_CHANNEL, "нужен доступ к серверу", "owner_channel")


class _Turns:
    """Поддельный хост: начало и конец хода, доставка реплик; `last_turn_ts` растёт сам."""

    def __init__(self, client, w, sid):
        self.client, self.w, self.sid, self.n = client, w, sid, 0

    async def start(self, source="signal"):
        await _host(self.client, self.w, f"sessions/{self.sid}/turn", {"state": "running", "source": source})

    async def deliver(self, say_id):
        await _host(self.client, self.w, f"session-says/{say_id}/delivered", {})

    async def end(self, answer=None, **extra):
        self.n += 1
        body = {"state": "idle", "last_turn_ts": 1_000 + self.n, "summary": "ход окончен: шагов 2", **extra}
        if answer is not None:
            body["answer"] = answer
        return await _host(self.client, self.w, f"sessions/{self.sid}/turn", body)


async def _say(ui, sid, text="сделай") -> int:
    r = await ui.post(f"{API}/sessions/{sid}/say", {"text": text, "priority": "next"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _turn_rows(db) -> list[tuple]:
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT ref, closed_by, closed_token_id, payload_json FROM main.owner_item WHERE kind = 'turn' "
            "ORDER BY id"
        ).fetchall()
    return [(r[0], r[1], r[2], json.loads(r[3])) for r in rows]


def _turn_cards(queue: dict) -> dict[str, dict]:
    return {ref: c for ref, c in _refs(queue).items() if c["kind"] == "turn"}


def _say_source(db, say_id) -> str:
    with db.reader() as conn:
        return conn.execute("SELECT source FROM main.held_say WHERE id = ?", (say_id,)).fetchone()[0]


# --- когда карточка заводится (3.3) -------------------------------------------------------------------------


async def test_ui_say_turn_always_gives_card_tail_and_no_answer(client, ui, db, world):
    w = world
    sid = await _auto(client, ui, w, mode="manual")
    turns = _Turns(client, w, sid)
    say_id = await _say(ui, sid)
    await turns.start("say")
    await turns.deliver(say_id)
    await turns.end(TAIL)
    cards = _turn_cards(await _queue(client, ui=ui))
    assert list(cards) == [f"turn:{sid}:1"]
    card = cards[f"turn:{sid}:1"]
    assert card["brief"] == "Последний абзац" and card["session_id"] == sid
    assert card["full"]["answer"] == TAIL["full"] and card["full"]["by_ui_say"] is True
    assert card["full"]["turns"] == 1 and card["full"]["state"] == "idle" and card["until_ts"] is None
    assert [(a["action"], a["needs_text"]) for a in card["answers"]] == [("reply", True), ("ok", False)]
    assert card["in_channel"] is False

    # Ход по реплике владельца без текста — карточка «ход окончен без ответа», полное — строка конца хода.
    say_id = await _say(ui, sid, "ещё")
    await turns.start("say")
    await turns.deliver(say_id)
    await turns.end()
    cards = _turn_cards(await _queue(client, ui=ui))
    assert list(cards) == [f"turn:{sid}:2"]
    assert cards[f"turn:{sid}:2"]["brief"] == "ход окончен без ответа"
    assert cards[f"turn:{sid}:2"]["full"]["answer"] == "ход окончен: шагов 2"
    # Первую карточку закрыла новая реплика из окна (владелец ответил в окне), не новый ход.
    rows = _turn_rows(db)
    assert rows[0][0] == f"turn:{sid}:1" and rows[0][1] == "ui"
    assert rows[0][3]["action"] == "reply" and rows[0][3]["say_id"] == say_id


async def test_signal_turn_needs_owner_line_and_new_card_supersedes(client, ui, db, world):
    w = world
    sid = await _auto(client, ui, w, mode="manual")
    turns = _Turns(client, w, sid)
    await turns.start()
    await turns.end(TAIL)  # ход по сигналу, хвост — без карточки
    await turns.end()  # и без текста — тоже
    assert _turn_cards(await _queue(client, ui=ui)) == {}
    await turns.start()
    await turns.end(ASK)
    cards = _turn_cards(await _queue(client, ui=ui))
    assert list(cards) == [f"turn:{sid}:3"] and cards[f"turn:{sid}:3"]["brief"] == "нужен доступ к серверу"
    assert cards[f"turn:{sid}:3"]["full"]["by_ui_say"] is False
    await turns.start()
    await turns.end(_answer_body("x", "второй вопрос", "owner_channel"))
    cards = _turn_cards(await _queue(client, ui=ui))
    assert list(cards) == [f"turn:{sid}:4"] and cards[f"turn:{sid}:4"]["brief"] == "второй вопрос"
    first = _turn_rows(db)[0]
    assert first[0] == f"turn:{sid}:3" and first[1] == "system" and first[3]["action"] == "superseded"
    # По вытесненной карточке «Принял» — 200 без действия (ревью Ф56 (4), А6); актуальная остаётся.
    done = await _answer(client, f"turn:{sid}:3", {"action": "ok"}, ui=ui)
    assert done == {"ref": f"turn:{sid}:3", "status": "closed"}
    assert list(_turn_cards(await _queue(client, ui=ui))) == [f"turn:{sid}:4"]


async def test_interrupted_turn_gives_no_card(client, ui, db, world):
    w = world
    sid = await _auto(client, ui, w, mode="manual")
    turns = _Turns(client, w, sid)
    say_id = await _say(ui, sid)
    await turns.start("say")
    await turns.deliver(say_id)
    await turns.end(ASK, interrupted_say=say_id)
    refs = _refs(await _queue(client, ui=ui))
    assert not any(c["kind"] == "turn" for c in refs.values())
    assert refs[f"say:{say_id}"]["kind"] == "interrupted"
    with db.reader() as conn:
        assert held.get(conn, sid).turns == 1


# --- ответ (3.5, 3.6) ---------------------------------------------------------------------------------------


async def test_agent_reply_is_agent_say_and_its_turn_needs_owner_line(client, ui, db, world):
    w = world
    agent, agent_id = _agent(db)
    sid = await _auto(client, ui, w, mode="manual")
    turns = _Turns(client, w, sid)
    await turns.start()
    await turns.end(ASK)
    ref = f"turn:{sid}:1"
    # Не в канале — агенту карточки не видно, ответ — 403; в канале — видно.
    assert ref not in _refs(await _queue(client, agent))
    await _answer(client, ref, {"action": "reply", "text": "дал"}, token=agent, expect=403)
    await _channel_on(ui, w, sid)
    card = _refs(await _queue(client, agent))[ref]
    assert card["in_channel"] is True and card["role"] == base.ROLE
    await _answer(client, ref, {"action": "reply"}, token=agent, expect=422)  # текст обязателен
    done = await _answer(client, ref, {"action": "reply", "text": "дал доступ"}, token=agent)
    assert done == {"ref": ref, "status": "queued"}
    await _answer(client, ref, {"action": "reply", "text": "ещё раз"}, token=agent, expect=409)
    await _answer(client, ref, {"action": "ok"}, ui=ui, expect=409)
    row = _turn_rows(db)[0]
    assert row[1] == "agent" and row[2] == agent_id and row[3]["action"] == "reply"
    say_id = row[3]["say_id"]
    assert _say_source(db, say_id) == "agent"
    # Строки `owner_item` «ответ агента» отдельно не пишется — у `turn` строка своя.
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.owner_item").fetchone()[0] == 1
    # Ход по реплике агента с хвостом — без карточки (без петли «агент ↔ сессия»).
    await turns.start("say")
    await turns.deliver(say_id)
    await turns.end(TAIL)
    assert _turn_cards(await _queue(client, ui=ui)) == {}
    # …а со строкой «ВЛАДЕЛЬЦУ:» — с карточкой.
    await turns.start()
    await turns.end(ASK)
    assert list(_turn_cards(await _queue(client, ui=ui))) == [f"turn:{sid}:3"]


async def test_ui_ok_and_reply_from_queue(client, ui, db, world):
    w = world
    sid = await _auto(client, ui, w, mode="manual")
    turns = _Turns(client, w, sid)
    await turns.start()
    await turns.end(ASK)
    done = await _answer(client, f"turn:{sid}:1", {"action": "ok"}, ui=ui)
    assert done == {"ref": f"turn:{sid}:1", "status": "closed"}
    await _answer(client, f"turn:{sid}:1", {"action": "ok"}, ui=ui, expect=409)
    assert _turn_rows(db)[0][1:3] == ("ui", None) and _turn_rows(db)[0][3]["action"] == "ok"
    await turns.start()
    await turns.end(ASK)
    done = await _answer(client, f"turn:{sid}:2", {"action": "reply", "text": "держи"}, ui=ui)
    assert done["status"] == "queued"
    row = _turn_rows(db)[1]
    assert row[1] == "ui" and _say_source(db, row[3]["say_id"]) == "ui"
    assert _turn_cards(await _queue(client, ui=ui)) == {}


async def test_close_session_closes_card(client, ui, db, world):
    w = world
    sid = await _auto(client, ui, w, mode="manual")
    turns = _Turns(client, w, sid)
    await turns.start()
    await turns.end(ASK)
    r = await ui.post(f"{API}/sessions/{sid}/close")
    assert r.status_code == 200, r.text
    assert _turn_cards(await _queue(client, ui=ui)) == {}
    row = _turn_rows(db)[0]
    assert row[1] == "system" and row[3]["action"] == "closed"
    await _answer(client, f"turn:{sid}:1", {"action": "reply", "text": "x"}, ui=ui, expect=409)


async def test_secret_in_answer_is_masked(client, ui, db, world):
    w = world
    sid = await _auto(client, ui, w, mode="manual")
    turns = _Turns(client, w, sid)
    secret = "API_KEY=sk-live-0123456789"
    await turns.start()
    await turns.end(_answer_body(f"вот {secret}\n\nВЛАДЕЛЬЦУ: вставь {secret}", f"вставь {secret}", "owner_channel"))
    card = _turn_cards(await _queue(client, ui=ui))[f"turn:{sid}:1"]
    assert "sk-live" not in card["brief"] and "API_KEY=***" in card["brief"]
    assert "sk-live" not in card["full"]["answer"]
    with db.reader() as conn:
        raw = conn.execute("SELECT payload_json FROM main.owner_item").fetchone()[0]
    assert "sk-live" not in raw  # маска — до записи


async def test_turn_number_is_kept_by_core_across_host_restart(client, ui, db, world):
    w = world
    sid = await _auto(client, ui, w, mode="manual")
    turns = _Turns(client, w, sid)
    await turns.start()
    await turns.end(ASK)
    # Повтор той же отметки конца хода номер не двигает.
    await _host(client, w, f"sessions/{sid}/turn", {"state": "idle", "last_turn_ts": 1_000 + turns.n})
    await _host(client, w, "sessions/recover", {})  # рестарт хоста: сессия спит, номер хода — в ядре
    await _host(client, w, f"sessions/{sid}/state", {"state": "idle", **PROC})
    await turns.start()
    await turns.end(ASK)
    with db.reader() as conn:
        assert held.get(conn, sid).turns == 2
    assert [r[0] for r in _turn_rows(db)] == [f"turn:{sid}:1", f"turn:{sid}:2"]
    session = (await ui.get(f"{API}/sessions/{sid}")).json()
    assert session["session"]["turns"] == 2


# --- ревью Ф56 (4), А6: ответ по карточке, вытесненной следующим ходом ------------------------------------


def _says(db, sid) -> list[str]:
    with db.reader() as conn:
        rows = conn.execute("SELECT text FROM main.held_say WHERE session_id = ? ORDER BY id", (sid,))
        return [r[0] for r in rows.fetchall()]


async def test_reply_and_ok_on_turn_superseded_by_next_turn(client, ui, db, world):
    """По карточке `turn` ответа не было, следующий ход с «ВЛАДЕЛЬЦУ:» закрыл её `superseded`: `reply` по старому
    ref — реплика сессии, закрывающая актуальную карточку; `ok` — 200 без реплики. Принятый ответ записан в
    саму вытесненную строку (круг 2 (1)): повтор — 409, второй реплики нет. 409 и тогда, когда актуальную
    карточку уже закрыли ответом, и у закрытой сессии."""
    w = world
    sid = await _auto(client, ui, w, mode="manual")
    turns = _Turns(client, w, sid)
    for _ in range(3):  # turn:<sid>:1 и :2 закрыты superseded, :3 открыта
        await turns.start()
        await turns.end(ASK)
    first, old, new = f"turn:{sid}:1", f"turn:{sid}:2", f"turn:{sid}:3"
    assert list(_turn_cards(await _queue(client, ui=ui))) == [new]
    # `ok` по вытесненной — 200, актуальная карточка остаётся; повтор — 409.
    assert await _answer(client, first, {"action": "ok"}, ui=ui) == {"ref": first, "status": "closed"}
    assert list(_turn_cards(await _queue(client, ui=ui))) == [new]
    await _answer(client, first, {"action": "ok"}, ui=ui, expect=409)
    await _answer(client, first, {"action": "reply", "text": "после ok"}, ui=ui, expect=409)
    # `reply` по вытесненной — реплика сессии и закрытие актуальной карточки ответом.
    body = {"action": "reply", "text": "держи доступ"}
    assert await _answer(client, old, body, ui=ui) == {"ref": old, "status": "queued"}
    assert _turn_cards(await _queue(client, ui=ui)) == {}
    rows = _turn_rows(db)
    assert rows[0][1] == "ui" and rows[0][3]["action"] == "ok"
    assert rows[1][1] == "ui" and rows[1][3]["action"] == "reply"
    assert rows[2][1] == "ui" and rows[2][3]["action"] == "reply"
    assert rows[1][3]["say_id"] == rows[2][3]["say_id"] and _say_source(db, rows[2][3]["say_id"]) == "ui"
    # Тот же ответ повторно (клиент не получил 200 и повторил) — 409, реплика одна.
    await _answer(client, old, body, ui=ui, expect=409)
    await _answer(client, new, {"action": "ok"}, ui=ui, expect=409)
    assert _says(db, sid) == ["держи доступ"]
    # Актуальную карточку закрыли ответом — по вытесненной ею прежней ответа уже не принять.
    for _ in range(2):  # :4 — superseded, :5 открыта
        await turns.start()
        await turns.end(ASK)
    await _answer(client, f"turn:{sid}:5", {"action": "ok"}, ui=ui)
    await _answer(client, f"turn:{sid}:4", {"action": "reply", "text": "вдогонку"}, ui=ui, expect=409)
    await _answer(client, f"turn:{sid}:5", {"action": "ok"}, ui=ui, expect=409)
    assert _says(db, sid) == ["держи доступ"]
    # Сессия закрыта — 409 и по вытесненной.
    for _ in range(2):  # :6 — superseded, :7 открыта
        await turns.start()
        await turns.end(ASK)
    r = await ui.post(f"{API}/sessions/{sid}/close")
    assert r.status_code == 200, r.text
    await _answer(client, f"turn:{sid}:6", {"action": "reply", "text": "поздно"}, ui=ui, expect=409)


async def test_turn_after_owner_own_say_stays_with_owner_not_agent(client, ui, db, world, clock):
    """24.10: ответ сессии на его же реплику из окна — читает он сам; в канале агента карточка
    остаётся у владельца (агенту не видна, ответ агента — 403), «сразу да» её тоже не берёт. Ход по сигналу со
    «ВЛАДЕЛЬЦУ:» — по-прежнему агенту."""
    w = world
    agent, _agent_id = _agent(db)
    sid = await _auto(client, ui, w, mode="manual")
    await _channel_on(ui, w, sid)
    turns = _Turns(client, w, sid)
    say_id = await _say(ui, sid, "что с релизом?")
    await turns.start("say")
    await turns.deliver(say_id)
    await turns.end(ASK)
    ref = f"turn:{sid}:1"
    card = _turn_cards(await _queue(client, ui=ui))[ref]
    assert card["full"]["by_ui_say"] is True and card["in_channel"] is False
    assert ref not in _refs(await _queue(client, agent))
    await _answer(client, ref, {"action": "reply", "text": "за владельца"}, token=agent, expect=403)
    with db.reader() as conn:
        assert card_route.for_card(conn, w["p3"], "turn", sid, ref=ref) == card_route.ME
    # 24.10: владелец не ответил за 30 минут — минутный цикл отпускает карточку агенту.
    clock.advance_ms(29 * 60_000)
    now = int(clock() * 1000)
    assert db.write_sync(lambda conn: card_route.release_owner_turns(conn, now_ms=now)) == []
    clock.advance_ms(60_000)
    now = int(clock() * 1000)
    assert db.write_sync(lambda conn: card_route.sweep(conn, now_ms=now)) == []
    assert _refs(await _queue(client, agent))[ref]["in_channel"] is True
    assert db.write_sync(lambda conn: card_route.release_owner_turns(conn, now_ms=now)) == []  # один раз
    # Следующий ход по сигналу со «ВЛАДЕЛЬЦУ:» — сразу агенту.
    clock.advance_ms(1_000)
    await turns.start()
    await turns.end(ASK)
    cards = _turn_cards(await _queue(client, ui=ui))
    assert list(cards) == [f"turn:{sid}:2"]
    second = cards[f"turn:{sid}:2"]
    assert second["full"]["by_ui_say"] is False and second["in_channel"] is True
