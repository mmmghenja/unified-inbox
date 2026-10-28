"""Ф89-1 (docs/architecture.md): повторяемое напоминание и таблица «Кто
отвечает на карточки».

- пока сессия ведёт срок сама (`missed = 0`), карточки очереди владельца нет, «сразу да» и минутный проход её не
  трогают;
- пропущен целый срок (`missed >= 1`) — карточка с ответами «Сделано» / «Снять»; `auto` у группы `reminder`
  для повторяемого не исполняется — карточка идёт агенту; ответ агента «done» — «сделано» (роль — владелец,
  `NULL`), «stop» — регламент снят;
- разовое — как прежде («сразу да» закрывает после сигнала).

Мир и помощники — test_f66_card_route.py (test_hub_sessions.py)."""

from hub.core import held, reminders

from . import test_hub_sessions as base
from .test_f41_autopilot_5b import _auto, _me
from .test_f56_queue import _agent, _answer, _iso, _queue, _refs
from .test_f66_card_route import _route, _sweep
from .test_hub_sessions import _h, _now

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"
DAY_MS = 24 * 60 * 60 * 1000


def _rec(db, rid) -> reminders.Rec:
    with db.reader() as conn:
        rec = reminders.get(conn, rid)
    assert rec is not None
    return rec


def _tick(db, clock) -> dict:
    now = _now(clock)
    return db.write_sync(lambda conn: reminders.fire_due(conn, now))


def _signal(db, rid, sub_id, now) -> None:
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.reminder SET signal_sub_id = ?, signaled_ts = ? WHERE id = ?", (sub_id, now, rid)
        )
    )


async def test_repeating_card_only_after_missed_auto_goes_to_agent(client, ui, db, clock, world):
    w = world
    sid = await _auto(client, ui, w)
    await _route(ui, w, {"reminder": "auto"})
    with db.reader() as conn:
        sub_id = conn.execute(
            "SELECT id FROM main.session_sub WHERE session_pid = ? AND ended_ts IS NULL",
            (held.held_pid(sid),),
        ).fetchone()[0]
    r = await client.post(
        f"{API}/reminders",
        json={
            "channel_id": w["ch"],
            "at": _iso(clock, 60),
            "note": "лента",
            "every": "day",
            **_me(sid, None),
        },
        headers=_h(w["tok"]),
    )
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    ref = f"reminder:{rid}"

    clock.advance_ms(61 * 60 * 1000)
    _tick(db, clock)
    _signal(db, rid, sub_id, _now(clock))
    assert _rec(db, rid).status == "fired"
    assert ref not in _refs(await _queue(client, ui=ui))  # срок ведёт сессия — карточки нет
    assert _sweep(db, clock) == []  # П1: повторяемые проход не берёт
    assert (_rec(db, rid).status, _rec(db, rid).done_count) == ("fired", 0)

    # Пропущен целый срок: карточка есть, `auto` исполняется как «агенту», ответы «Сделано» / «Снять».
    clock.advance_ms(DAY_MS)
    _tick(db, clock)
    _signal(db, rid, sub_id, _now(clock))
    assert _rec(db, rid).missed == 1
    card = _refs(await _queue(client, ui=ui))[ref]
    assert card["in_channel"] is True
    assert [a["action"] for a in card["answers"]] == ["done", "stop"]
    assert [a["label"] for a in card["answers"]] == ["Сделано", "Снять"]
    assert "пропущено 1" in card["brief"]
    assert _sweep(db, clock) == []
    assert _rec(db, rid).status == "fired"

    agent, _ = _agent(db)
    assert ref in _refs(await _queue(client, agent))
    await _answer(client, ref, {"action": "done"}, token=agent)
    rec = _rec(db, rid)
    assert (rec.status, rec.missed, rec.done_count, rec.last_done_role) == ("pending", 0, 1, None)
    assert ref not in _refs(await _queue(client, ui=ui))

    # Следующий срок сработал — карточки нет; ещё один пропуск — агент снимает регламент.
    clock.advance_ms(DAY_MS)
    _tick(db, clock)
    await _answer(client, ref, {"action": "done"}, token=agent, expect=409)  # срок ведёт сессия
    clock.advance_ms(DAY_MS)
    _tick(db, clock)
    await _answer(client, ref, {"action": "stop"}, token=agent)
    rec = _rec(db, rid)
    assert (rec.status, rec.end_reason) == ("done", "stopped")


async def test_one_off_reminder_card_answers_unchanged(client, ui, db, clock, world):
    w = world
    sid = await _auto(client, ui, w)
    r = await client.post(
        f"{API}/reminders",
        json={"channel_id": w["ch"], "at": _iso(clock, 60), "note": "x", **_me(sid, None)},
        headers=_h(w["tok"]),
    )
    rid = r.json()["id"]
    clock.advance_ms(61 * 60 * 1000)
    _tick(db, clock)
    card = _refs(await _queue(client, ui=ui))[f"reminder:{rid}"]
    assert [a["action"] for a in card["answers"]] == ["done"]
    await _answer(client, f"reminder:{rid}", {"action": "stop"}, ui=ui, expect=422)
    await _answer(client, f"reminder:{rid}", {"action": "done"}, ui=ui)
    assert _rec(db, rid).status == "done"
