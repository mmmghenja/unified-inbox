"""Ф89-1 (docs/architecture.md и поправки П2–П11): повторяемые напоминания и
напоминания без беседы — ядро.

- разовое без беседы: срабатывает (не `cancelled`), сигнал с `channel_id: 0`, «Сделано»;
- `day`: срок, пропуск -> `missed` 1 и снова сигнал, «сделано» -> `missed` 0, следующий срок, роль; цикл
  `_reminders_loop` через `has_work` (П2); `weekdays` через пятницу, `4h` после простоя, `week`;
- `stop`, «сделано» раньше срока (пропустить ближайший), перенос (`at`) и смена периода (П6);
- `new_since_done` (П10), `cancel_if_reply` (П4), архив и удалённая беседа (П8), пределы, владелец без беседы с
  `project_id`, права, уведомление владельцу (П9), аудит без беседы (П11).

Мир и помощники — test_f34_reminders.py; часы — свои: пятница 23.10.2026 08:00 по времени хаба (пояс аккаунта
беседы и напоминаний без беседы — `Europe/Moscow`, задан явно)."""

import asyncio
import datetime as dt
import zoneinfo

import pytest

from hub.core import reminders, runner

from . import test_f34_reminders as base
from .conftest import FakeClock, insert_channel, insert_message
from .test_f34_reminders import (
    A,
    B,
    _due,
    _h,
    _list,
    _now,
    _old_limits,  # noqa: F401 — автофикстура: прежние пределы для тестов пределов
    _rec,
    _session,
    _sub,
    _tick,
    _wait,
)

procs = base.procs
ui = base.ui
world = base.world

MSK = zoneinfo.ZoneInfo("Europe/Moscow")
ROLE = "маркетолог"


def _msk(day: int, hour: int, minute: int = 0, month: int = 10) -> int:
    return int(dt.datetime(2026, month, day, hour, minute, tzinfo=MSK).timestamp() * 1000)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(_msk(23, 8) / 1000)  # пятница


def _to(clock, ts_ms: int) -> None:
    clock.value = ts_ms / 1000


async def _post(client, w, who, *, expect=201, token=None, **body) -> dict:
    body.setdefault("note", "прочитать ленту")
    if who is not None:
        body["session"] = _session(w, who, full=False)
    r = await client.post("/api/v1/reminders", json=body, headers=_h(token or w["tok"]))
    assert r.status_code == expect, r.text
    return r.json()


async def _patch(client, w, who, rid, *, expect=200, **body) -> dict:
    body["session"] = _session(w, who, full=False)
    r = await client.patch(f"/api/v1/reminders/{rid}", json=body, headers=_h(w["tok"]))
    assert r.status_code == expect, r.text
    return r.json()


def _has_work(db, clock) -> bool:
    with db.reader() as conn:
        return reminders.has_work(conn, _now(clock))


# --- разовое без беседы -------------------------------------------------------------------------------------


async def test_one_off_without_channel_fires_signals_zero_and_closes(client, db, clock, world):
    w = world
    sub_id = (await _sub(client, w, A, role=ROLE))["sub"]["id"]
    item = await _post(client, w, A, at="2026-10-23 13:01", note="запустить run-redaktor.sh")
    assert item["channel_id"] is None and item["channel"] is None
    assert item["project_id"] == w["p3"] and item["role"] == ROLE
    assert item["cancel_if_reply"] is False  # П4: без беседы — false по умолчанию
    assert item["due_ts"] == _msk(23, 13, 1) and item["tz"] == "Europe/Moscow"
    assert item["note"] == "запустить run-redaktor.sh"  # видна по проекту напоминания
    assert item["every"] is None and item["every_text"] is None
    with db.reader() as conn:
        assert conn.execute(
            "SELECT channel_id FROM main.reminder WHERE id = ?", (item["id"],)
        ).fetchone() == (0,)
        audit = conn.execute(
            "SELECT channel_id FROM main.audit WHERE action = 'reminder_create' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert audit == (None,)  # П11

    _to(clock, _msk(23, 13, 2))
    assert _tick(db, clock) == {"fired": 1, "answered": 0, "cancelled": 0}
    assert _rec(db, item["id"]).status == "fired"
    # П3: в сигнале беседа «нет» — 0 (старый клиент требует целое).
    assert await _wait(client, w, sub_id, A) == [
        {"kind": "reminder", "reminder_id": item["id"], "channel_id": 0}
    ]
    listed = await _list(client, w, A)
    assert [(i["id"], i["channel_id"], i["status"]) for i in listed] == [(item["id"], None, "fired")]
    done = await _patch(client, w, A, item["id"], close=True)
    assert (done["status"], done["end_reason"]) == ("done", "session")


async def test_ui_without_channel_needs_project_id(client, ui, db, clock, world):
    w = world
    r = await ui.post("/api/v1/reminders", {"at": "2026-10-23 13:00", "note": "x"})
    assert r.status_code == 422 and "project_id" in r.text
    r = await ui.post("/api/v1/reminders", {"at": "2026-10-23 13:00", "note": "x", "project_id": 9999})
    assert r.status_code == 404
    r = await ui.post(
        "/api/v1/reminders", {"at": "2026-10-23 13:00", "note": "x", "project_id": w["p3"], "every": "day"}
    )
    assert r.status_code == 201, r.text
    item = r.json()
    assert (item["by"], item["channel_id"], item["project_id"], item["every"]) == ("me", None, w["p3"], "day")
    # С беседой `project_id` не нужен и не принимается.
    r = await ui.post(
        "/api/v1/reminders", {"channel_id": w["ch3"], "at": "2026-10-23 13:00", "note": "x", "project_id": 1}
    )
    assert r.status_code == 422
    # Сессия: проект без беседы — только проект её подписки.
    await _sub(client, w, A, role=ROLE)
    await _post(client, w, A, at="2026-10-23 13:00", project_id=w["p4"], expect=422)
    assert (await _post(client, w, A, at="2026-10-23 13:00", project_id=w["p3"]))["project_id"] == w["p3"]


# --- day: срок, пропуск, «сделано», цикл -------------------------------------------------------------------


async def test_day_miss_and_done_cycle_via_has_work(client, db, clock, world):
    w = world
    sub_id = (await _sub(client, w, A, role=ROLE))["sub"]["id"]
    item = await _post(client, w, A, channel_id=w["ch3"], at="2026-10-24 09:00", every="day")
    assert (item["every"], item["at_hm"], item["every_text"]) == ("day", "09:00", "каждый день в 09:00")
    assert item["cancel_if_reply"] is False and item["missed"] == 0 and item["done_count"] == 0

    _to(clock, _msk(24, 8, 59))
    assert not _has_work(db, clock)
    _to(clock, _msk(24, 9) + 30_000)
    assert _has_work(db, clock)
    _tick(db, clock)
    rec = _rec(db, item["id"])
    # П2: `due_ts` — ближайший будущий срок, `fired_ts` — когда сработал текущий.
    assert (rec.status, rec.fired_ts, rec.due_ts, rec.missed) == ("fired", _now(clock), _msk(25, 9), 0)
    assert not _has_work(db, clock)  # сработавшее повторяемое ждёт следующего срока, не каждого тика
    signals = await _wait(client, w, sub_id, A)
    assert signals == [{"kind": "reminder", "reminder_id": item["id"], "channel_id": w["ch3"]}]

    # Сессия пропустила срок: наступил следующий -> missed 1, сигнал заново (новый seq).
    seq = rec.seq
    _to(clock, _msk(25, 9, 1))
    assert _has_work(db, clock)
    assert _tick(db, clock)["missed"] == 1
    rec = _rec(db, item["id"])
    assert (rec.status, rec.missed, rec.fired_ts, rec.due_ts) == ("fired", 1, _now(clock), _msk(26, 9))
    assert rec.signaled_ts is None and rec.delivered_ts is None and rec.seq > seq
    signals = await _wait(client, w, sub_id, A)
    assert signals == [{"kind": "reminder", "reminder_id": item["id"], "channel_id": w["ch3"], "missed": 1}]

    # «Сделано»: pending, missed 0, следующий срок — завтра в 09:00, роль отметившего.
    _to(clock, _msk(25, 11))
    done = await _patch(client, w, A, item["id"], close=True)
    assert (done["status"], done["missed"], done["done_count"], done["due_ts"]) == (
        "pending",
        0,
        1,
        _msk(26, 9),
    )
    assert (done["last_done_ts"], done["last_done_role"], done["fired_ts"]) == (_now(clock), ROLE, None)
    assert done["closed_ts"] is None


async def test_day_loop_through_reminders_loop(db, clock, client, world):
    """П2: цикл ядра (`has_work` -> `fire_due`) сам ведёт регламент — срабатывание и пропуск."""
    w = world
    await _sub(client, w, A, role=ROLE)
    item = await _post(client, w, A, at="2026-10-23 09:00", every="day")
    moments = iter([_msk(23, 9, 0) + 1000, _msk(23, 12), _msk(24, 9, 0) + 1000])
    stop = asyncio.Event()

    async def _sleep(_seconds: float) -> None:
        moment = next(moments, None)
        if moment is None:
            stop.set()
        else:
            _to(clock, moment)

    await runner._reminders_loop(db, clock, sleep=_sleep, stop_event=stop)
    rec = _rec(db, item["id"])
    assert (rec.status, rec.missed, rec.due_ts, rec.fired_ts) == ("fired", 1, _msk(25, 9), _msk(24, 9) + 1000)


async def test_weekdays_friday_to_monday_and_week(client, db, clock, world):
    w = world
    await _sub(client, w, A, role=ROLE)
    wd = await _post(client, w, A, at="2026-10-23 10:00", every="weekdays")  # пятница
    wk = await _post(client, w, A, at="2026-10-23 10:00", every="week")
    assert wd["every_text"] == "по будням в 10:00"
    assert wk["every_text"] == "каждую неделю: пт в 10:00"
    _to(clock, _msk(23, 10, 1))
    _tick(db, clock)
    assert _rec(db, wd["id"]).due_ts == _msk(26, 10)  # пт -> пн, суббота и воскресенье пропущены
    assert _rec(db, wk["id"]).due_ts == _msk(30, 10)  # через неделю, тоже пятница
    # Пропуск в понедельник -> вторник.
    _to(clock, _msk(26, 10, 0) + 5000)
    _tick(db, clock)
    rec = _rec(db, wd["id"])
    assert (rec.missed, rec.due_ts) == (1, _msk(27, 10))
    assert _rec(db, wk["id"]).missed == 0


async def test_hours_grid_after_downtime(client, db, clock, world):
    w = world
    await _sub(client, w, A, role=ROLE)
    item = await _post(client, w, A, at="2026-10-23 10:00", every="4h")
    assert (item["every_text"], item["at_hm"]) == ("каждые 4 ч", None)
    _to(clock, _msk(23, 10, 0) + 30_000)
    _tick(db, clock)
    assert _rec(db, item["id"]).due_ts == _msk(23, 14)
    # Ядро лежало до 23:00: один пропуск, следующий срок — на сетке от 10:00 (02:00 следующего дня).
    _to(clock, _msk(23, 23))
    _tick(db, clock)
    rec = _rec(db, item["id"])
    assert (rec.missed, rec.due_ts) == (1, _msk(24, 2))


def test_next_after_rules_directly():
    friday_10 = _msk(23, 10)
    assert reminders.next_after("day", "09:30", MSK, friday_10, friday_10) == _msk(24, 9, 30)
    assert reminders.next_after("weekdays", "10:00", MSK, friday_10, friday_10) == _msk(26, 10)
    assert reminders.next_after("weekdays", "10:00", MSK, friday_10, _msk(24, 12)) == _msk(26, 10)
    assert reminders.next_after("week", "10:00", MSK, friday_10, _msk(24, 12)) == _msk(30, 10)
    assert reminders.next_after("24h", None, MSK, friday_10, friday_10) == _msk(24, 10)
    assert reminders.next_after("1h", None, MSK, friday_10, _msk(23, 9)) == friday_10  # срок ещё впереди
    for bad in ("hourly", "0h", "25h", "2d", ""):
        with pytest.raises(reminders.ReminderError):
            reminders.every_form(bad)


# --- stop, «сделано» раньше срока, перенос, смена периода ---------------------------------------------------


async def test_stop_and_done_before_due_and_one_off_stop(client, db, clock, world):
    w = world
    await _sub(client, w, A, role=ROLE)
    item = await _post(client, w, A, at="2026-10-23 09:00", every="day")
    # Ревью Ф89-1: «сделано» по ждущему ничего не меняет (повтор, двойной клик не съедают следующий срок).
    early = await _patch(client, w, A, item["id"], close=True)
    assert (early["status"], early["due_ts"], early["done_count"]) == ("pending", _msk(23, 9), 0)
    stopped = await _patch(client, w, A, item["id"], stop=True)
    assert (stopped["status"], stopped["end_reason"]) == ("done", "stopped")
    _to(clock, _msk(24, 9, 1))
    _tick(db, clock)
    assert _rec(db, item["id"]).status == "done"
    # У разового `stop` — как `close`.
    one = await _post(client, w, A, channel_id=w["ch3"], at="2026-10-25 10:00")
    assert one["cancel_if_reply"] is True  # П4: разовое по беседе — как было
    closed = await _patch(client, w, A, one["id"], stop=True)
    assert (closed["status"], closed["end_reason"]) == ("cancelled", "session")
    await _patch(client, w, A, one["id"], stop=True, note="x", expect=422)


async def test_postpone_moves_only_nearest_and_every_change(client, db, clock, world):
    w = world
    await _sub(client, w, A, role=ROLE)
    item = await _post(client, w, A, at="2026-10-23 09:00", every="day")
    _to(clock, _msk(23, 9, 1))
    _tick(db, clock)
    # П6: «отложить» — только ближайший срок, `at_hm` остаётся 09:00.
    moved = await _patch(client, w, A, item["id"], at="2026-10-23 15:00")
    assert (moved["status"], moved["due_ts"], moved["at_hm"]) == ("pending", _msk(23, 15), "09:00")
    _to(clock, _msk(23, 15, 1))
    _tick(db, clock)
    assert _rec(db, item["id"]).due_ts == _msk(24, 9)
    # Новый период пересчитывает `at_hm` из `at`; `every: null` — разовое.
    changed = await _patch(client, w, A, item["id"], every="weekdays", at="2026-10-26 11:30")
    assert (changed["every"], changed["at_hm"], changed["status"]) == ("weekdays", "11:30", "pending")
    one = await _patch(client, w, A, item["id"], every=None)
    assert (one["every"], one["at_hm"], one["due_ts"], one["cancel_if_reply"]) == (
        None,
        None,
        _msk(26, 11, 30),
        False,
    )
    # Без `every` в теле период не трогается.
    again = await _patch(client, w, A, item["id"], note="другая заметка")
    assert again["every"] is None
    hours = await _patch(client, w, A, item["id"], every="6h")
    assert (hours["every"], hours["at_hm"], hours["due_ts"]) == ("6h", None, _msk(26, 11, 30))


# --- cancel_if_reply, new_since_done ------------------------------------------------------------------------


async def test_cancel_if_reply_rules(client, db, clock, world):
    w = world
    await _sub(client, w, A, role=ROLE)
    at = "2026-10-24 09:00"
    await _post(client, w, A, channel_id=w["ch3"], at=at, every="day", cancel_if_reply=True, expect=422)
    await _post(client, w, A, at=at, cancel_if_reply=True, expect=422)
    item = await _post(client, w, A, channel_id=w["ch3"], at=at, every="day", cancel_if_reply=False)
    assert item["cancel_if_reply"] is False
    await _patch(client, w, A, item["id"], cancel_if_reply=True, expect=422)
    # Разовое с флагом стало повторяемым — флаг снимается сам.
    one = await _post(client, w, A, channel_id=w["ch3"], at=at)
    assert one["cancel_if_reply"] is True
    assert (await _patch(client, w, A, one["id"], every="day"))["cancel_if_reply"] is False


async def test_new_since_done_counts_only_foreign_messages(client, db, clock, world):
    w = world
    await _sub(client, w, A, role=ROLE)
    item = await _post(client, w, A, channel_id=w["ch3"], at="2026-10-24 09:00", every="day")
    now = _now(clock)
    insert_message(db, w["ch3"], ext_id="n1", ts=now + 1000)
    insert_message(db, w["ch3"], ext_id="n2", ts=now + 2000)
    insert_message(db, w["ch3"], ext_id="n3", ts=now + 3000, is_me=1)  # своё — не считается
    insert_message(db, w["ch3"], ext_id="n4", ts=now + 4000, kind="event")  # служебное — не считается
    insert_message(db, w["ch3"], ext_id="n5", ts=now + 5000, deleted=1)
    listed = await _list(client, w, A, counts=1)
    assert [i["new_since_done"] for i in listed] == [2]
    assert "new_since_done" not in (await _list(client, w, A))[0]  # П10: только по `counts=1`
    _to(clock, _msk(24, 9, 1))
    _tick(db, clock)
    await _patch(client, w, A, item["id"], close=True)
    assert [i["new_since_done"] for i in await _list(client, w, A, counts=1)] == [0]
    insert_message(db, w["ch3"], ext_id="n6", ts=_now(clock) + 1000)
    assert [i["new_since_done"] for i in await _list(client, w, A, counts=1)] == [1]
    # Без беседы — `null`.
    await _post(client, w, A, at="2026-10-24 10:00", every="day")
    assert sorted(
        (i["channel_id"] is None, i["new_since_done"]) for i in await _list(client, w, A, counts=1)
    ) == [(False, 1), (True, None)]


# --- архив, удалённая беседа, пределы, права ----------------------------------------------------------------


async def test_archive_keeps_repeating_gone_channel_cancels(client, db, clock, world):
    w = world
    await _sub(client, w, A, role=ROLE)
    arch = insert_channel(db, w["acc"], project_id=w["p3"], ext_id="arch", title="Архив", archived=1)
    gone = insert_channel(db, w["acc"], project_id=w["p3"], ext_id="gone", title="Уйдёт")
    await _post(client, w, A, channel_id=arch, at="2026-10-23 09:00", expect=409)  # разовое в архиве — нет
    rep = await _post(client, w, A, channel_id=arch, at="2026-10-23 09:00", every="day")  # П8
    lost = await _post(client, w, A, channel_id=gone, at="2026-10-23 09:00", every="day")
    db.write_sync(lambda conn: conn.execute("DELETE FROM main.channel WHERE id = ?", (gone,)))
    _to(clock, _msk(23, 9, 1))
    _tick(db, clock)
    assert _rec(db, rep["id"]).status == "fired"
    rec = _rec(db, lost["id"])
    assert (rec.status, rec.end_reason) == ("cancelled", "channel_gone")
    # Сработавшее повторяемое, чья беседа исчезла к следующему сроку, — тоже снимается.
    db.write_sync(lambda conn: conn.execute("DELETE FROM main.channel WHERE id = ?", (arch,)))
    _to(clock, _msk(24, 9, 1))
    _tick(db, clock)
    rec = _rec(db, rep["id"])
    assert (rec.status, rec.end_reason) == ("cancelled", "channel_gone")


async def test_limits_count_fired_repeating_and_skip_no_channel(client, db, clock, world, monkeypatch):
    w = world
    await _sub(client, w, A, role=ROLE)
    for i in range(4):
        await _post(client, w, A, channel_id=w["ch3"], at=f"2026-10-24 1{i}:00")
    rep = await _post(client, w, A, channel_id=w["ch3"], at="2026-10-23 08:30", every="day")
    _to(clock, _msk(23, 8, 31))
    _tick(db, clock)
    assert _rec(db, rep["id"]).status == "fired"
    # Сработавшее повторяемое — в счёте ждущих: на беседу уже 5.
    await _post(client, w, A, channel_id=w["ch3"], at="2026-10-24 10:00", expect=429)
    # Без беседы в счёт «на беседу» не идёт; «сделано» пределы не проверяет.
    await _post(client, w, A, at="2026-10-24 10:00")
    await _patch(client, w, A, rep["id"], close=True)
    monkeypatch.setattr(reminders, "MAX_PENDING_PER_PROJECT", 7)
    await _post(client, w, A, at="2026-10-24 11:00", every="4h")
    await _post(client, w, A, at="2026-10-24 12:00", expect=429)


async def test_other_role_cannot_mark_done(client, db, clock, world):
    w = world
    await _sub(client, w, A, role=ROLE)
    await _sub(client, w, B, role="исследователь")
    item = await _post(client, w, A, at="2026-10-24 09:00", every="day")
    await _patch(client, w, B, item["id"], close=True, expect=403)
    await _patch(client, w, B, item["id"], stop=True, expect=403)
    assert _rec(db, item["id"]).done_count == 0


async def test_ui_done_is_done_and_stop_stops(client, ui, db, clock, world):
    w = world
    await _sub(client, w, A, role=ROLE)
    item = await _post(client, w, A, at="2026-10-23 09:00", every="day")
    _to(clock, _msk(23, 9, 1))
    _tick(db, clock)
    r = await ui.patch(f"/api/v1/reminders/{item['id']}", {"close": True})
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["last_done_role"], r.json()["done_count"]) == ("pending", None, 1)
    r = await ui.patch(f"/api/v1/reminders/{item['id']}", {"stop": True})
    assert (r.json()["status"], r.json()["end_reason"]) == ("done", "stopped")
    # Панель «⋯ → Напоминания»: все открытые с `counts=1` (кука).
    await _post(client, w, A, channel_id=w["ch3"], at="2026-10-24 09:00", every="weekdays")
    r = await ui.get("/api/v1/reminders", params={"counts": 1})
    assert r.status_code == 200
    assert [(i["every"], i["new_since_done"]) for i in r.json()["items"]] == [("weekdays", 0)]


# --- уведомление владельцу (П9) ----------------------------------------------------------------------------------


async def test_due_notify_only_after_missed(client, db, clock, world):
    w = world
    await _sub(client, w, A, role=ROLE)
    item = await _post(client, w, A, at="2026-10-23 09:00", every="day")
    _to(clock, _msk(23, 9, 1))
    _tick(db, clock)
    _to(clock, _msk(23, 10))
    assert await _due(client, w) == []  # срок ведёт сессия — владельцу не шлём
    _to(clock, _msk(24, 9, 1))
    _tick(db, clock)
    _to(clock, _msk(24, 9, 30))
    due = await _due(client, w)
    assert [(i["id"], i["missed"], i["channel_id"], i["role"]) for i in due] == [(item["id"], 1, None, ROLE)]


def test_migration_columns_exist(db):
    with db.reader() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA main.table_info(reminder)")}
    assert {"every", "at_hm", "tz", "missed", "done_count", "last_done_ts", "last_done_role"} <= columns


async def test_home_shows_repeating_only_after_missed(client, ui, db, clock, world):
    """Головная 23.10: на главной («Нужна реакция») повторяемое — только когда сессия пропустила срок."""
    w = world
    await _sub(client, w, A, role=ROLE)
    item = await _post(client, w, A, channel_id=w["ch3"], at="2026-10-24 09:00", every="day")
    _to(clock, _msk(24, 9) + 30_000)
    _tick(db, clock)
    assert [i["reminder"]["id"] for i in await base._home_reminders(ui)] == []
    _to(clock, _msk(25, 9, 1))
    _tick(db, clock)
    assert [i["reminder"]["id"] for i in await base._home_reminders(ui)] == [item["id"]]


# --- ревью Ф89-1: воспроизведения находок (`.runs/critic-f89/`) ---------------------------------------------


async def test_week_snooze_moves_weekday_forever(client, db, clock, world):
    """П6: «Отложить» у повторяемого сдвигает только ближайший срок. У `week` день недели берётся из
    `due_ts` — перенос на другой день недели меняет день регламента навсегда."""
    w = world
    await _sub(client, w, A, role=ROLE)
    # Понедельник 26.10 10:00, раз в неделю.
    item = await _post(client, w, A, at="2026-10-26 10:00", every="week")
    assert item["every_text"] == "каждую неделю: пн в 10:00"
    # Отложить ближайший срок на среду 28.10 12:00.
    moved = await _patch(client, w, A, item["id"], at="2026-10-28 12:00")
    assert moved["at_hm"] == "10:00"
    _to(clock, _msk(28, 12, 1))
    _tick(db, clock)
    rec = _rec(db, item["id"])
    # ожидается следующий понедельник 02.11 10:00
    assert rec.due_ts == _msk(2, 10, month=11)


async def test_double_done_skips_next_deadline(client, db, clock, world):
    """Повторная доставка «сделано» (ретрай MCP, владелец и сессия одновременно, двойной клик): второй вызов по
    уже `pending` строке молча пропускает следующий срок."""
    w = world
    await _sub(client, w, A, role=ROLE)
    item = await _post(client, w, A, at="2026-10-24 09:00", every="day")
    _to(clock, _msk(24, 9, 1))
    _tick(db, clock)
    assert _rec(db, item["id"]).status == "fired"
    first = await _patch(client, w, A, item["id"], close=True)
    assert first["due_ts"] == _msk(25, 9)
    second = await _patch(client, w, A, item["id"], close=True)
    print("after second done: due", second["due_ts"], "done_count", second["done_count"])
    assert second["due_ts"] == _msk(25, 9), "второе «сделано» потеряло срок 25.10 09:00"


async def test_owner_own_repeat_first_deadline_invisible(client, ui, db, clock, world):
    """Повторяемое, поставленное владельцем в UI (сессии нет, сигнал не отдаётся никому): в первый срок владелец не
    получает ни уведомления, ни строки на главной — только через период (missed >= 1)."""
    w = world
    r = await ui.post(
        "/api/v1/reminders", {"at": "2026-10-24 09:00", "note": "x", "project_id": w["p3"], "every": "day"}
    )
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    _to(clock, _msk(24, 9, 1))
    _tick(db, clock)
    assert _rec(db, rid).status == "fired"
    due = [i["id"] for i in await base._due(client, w)]
    home = [i["reminder"]["id"] for i in await base._home_reminders(ui)]
    print("due_notify:", due, "home:", home)
    assert rid in due or rid in home, "своё повторяемое владельца в первый срок не видно нигде"


def test_limits_raised_by_owner():
    """Владелец 23.10: «пять ждущих — почему так мало?» — пределы — предохранители, а не рабочие ограничения."""
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(reminders.__file__).read_text(encoding="utf-8"))
    values = {
        node.targets[0].id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
    }
    assert (values["MAX_PENDING_PER_CHANNEL"], values["MAX_PENDING_PER_PROJECT"], values["NOTE_MAX"]) == (
        100,
        1000,
        1000,
    )
