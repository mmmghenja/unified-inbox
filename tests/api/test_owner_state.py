"""Ф56-1в (docs/architecture.md): карточки состояния сессии хаба в канале владельца.

- `stall` — отменена 22.10: новых нет, старые закрываются минутным циклом (`retired`);
- `broken` — пишется в той же транзакции, что усыпление (рестарт хоста, хост замолчал, обрыв хабом) при ходе
  и при снятых запросах, не пишется у простаивающей без запросов; ответы `wake`/`say`/`sleep`; `worked`;
- `dead` — на третьей неудаче запуска; `retry`; `launched`;
- `stuck` — доклад хоста: завести, обновить, закрыть `null`/концом хода; `stop`/`interrupt`/`wait`; повторный
  `running` не двигает начало хода;
- `superseded`; закрытие сессии закрывает всё; токен `human` видит только карточки «в канале».

Мир — test_hub_sessions.py; хост поддельный (bearer `work_host` зовёт ручки ядра напрямую)."""

import json

from hub.core import held, held_signals, held_work, owner_queue, runner

from . import test_hub_sessions as base
from .test_f41_autopilot import PROC
from .test_f41_autopilot_5b import _auto
from .test_f56_queue import _answer, _permission, _queue, _refs
from .test_hub_sessions import _host, _now
from .test_owner_turn import _say, _say_source

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"
MIN = 60_000


# --- помощники ----------------------------------------------------------------------------------------------


def _rec(db, sid) -> held.HubSession:
    with db.reader() as conn:
        return held.get(conn, sid)


def _set(db, sid, **cols) -> None:
    names = list(cols)
    db.write_sync(
        lambda conn: conn.execute(
            f"UPDATE main.hub_session SET {', '.join(f'{n} = ?' for n in names)} WHERE id = ?",  # noqa: S608
            [*(cols[n] for n in names), sid],
        )
    )


def _rows(db, kind) -> list[tuple]:
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT ref, closed_by, closed_token_id, hold_ts, payload_json FROM main.owner_item "
            "WHERE kind = ? ORDER BY id",
            (kind,),
        ).fetchall()
    return [(r[0], r[1], r[2], r[3], json.loads(r[4])) for r in rows]


def _cards(queue: dict, kind: str) -> dict[str, dict]:
    return {ref: c for ref, c in _refs(queue).items() if c["kind"] == kind}


async def _turn(client, w, sid, body) -> dict:
    return await _host(client, w, f"sessions/{sid}/turn", body)


async def _ended(client, w, sid, ts) -> None:
    """Ход по сигналу: начало и конец (`last_turn_ts = ts`) — без карточки `turn`."""
    await _turn(client, w, sid, {"state": "running", "source": "signal"})
    await _turn(client, w, sid, {"state": "idle", "last_turn_ts": ts})


async def _quiet(client, ui, w, clock) -> tuple[int, int]:
    """Сессия после хода, кончившегося «сейчас», — (номер сессии, конец хода)."""
    sid = await _auto(client, ui, w, mode="manual")
    end = _now(clock)
    await _ended(client, w, sid, end)
    return sid, end


def _sub_id(db, sid) -> int:
    with db.reader() as conn:
        return held_signals.project_sub(conn, held.get(conn, sid)).id


def _signal(db, w, sub_id, now_ms) -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.session_signal (sub_id, item_id, channel_id, project_id, account_id, "
                "label, covered_seq, msg_count, status, created_ts, updated_ts) "
                "VALUES (?, 1, ?, ?, ?, 'reply', 0, 1, 'pending', ?, ?)",
                (sub_id, w["ch"], w["p3"], w["acc"], now_ms, now_ms),
            ).lastrowid
        )
    )


def _bg(items, wake_at=None) -> str:
    value: dict = {"items": items}
    if wake_at is not None:
        value["wake_at_ts"] = wake_at
    return json.dumps(value)


def _bg_item(kind, started_ts) -> dict:
    return {"id": "t1", "kind": kind, "title": "x", "state": "running", "started_ts": started_ts}


# --- broken (раздел 3) --------------------------------------------------------------------------------------


async def _tool_event(client, w, sid, ts) -> None:
    await _host(client, w, f"sessions/{sid}/events", {"events": [{"kind": "tool", "tool": "Bash", "ts": ts}]})


async def test_broken_on_host_restart_mid_turn_and_answers(client, ui, db, clock, world):
    w = world
    sid = await _auto(client, ui, w, mode="manual")
    await _turn(client, w, sid, {"state": "running", "source": "self"})
    await _tool_event(client, w, sid, _now(clock))
    await _host(client, w, "sessions/recover", {})
    ref = f"broken:{sid}:0"
    card = _cards(await _queue(client, ui=ui), "broken")[ref]
    assert card["brief"] == f"ход оборван: {held_work.NOTE_RECOVER}, занималась: Bash"
    full = card["full"]
    assert full["was_running"] is True and full["expired"] == 0 and full["origin"] == "self"
    assert full["step"]["tool"] == "Bash" and full["state"] == "asleep" and full["since_ts"] is not None
    assert [a["action"] for a in card["answers"]] == ["wake", "say", "sleep"]
    # «Поднять и продолжить»: спящая без ждущего поднимается под постоянную строку.
    assert await _answer(client, ref, {"action": "wake"}, ui=ui) == {"ref": ref, "status": "wake_requested"}
    rec = _rec(db, sid)
    assert rec.wake_requested_ts is not None and rec.signal_lost_ts is not None
    assert _rows(db, "broken")[0][4]["action"] == "wake"
    await _answer(client, ref, {"action": "sleep"}, ui=ui, expect=409)


async def test_broken_written_by_recover_with_expired_not_by_idle(client, ui, db, clock, world):
    w = world
    idle = await _auto(client, ui, w, mode="manual")
    busy = await _auto(client, ui, w, mode="manual", role="маркетолог")
    await _permission(client, w, busy)
    await _host(client, w, "sessions/recover", {})
    cards = _cards(await _queue(client, ui=ui), "broken")
    assert list(cards) == [f"broken:{busy}:0"]  # простаивающая без запросов — без карточки
    full = cards[f"broken:{busy}:0"]["full"]
    assert full["was_running"] is False and full["expired"] == 1 and full["step"] is None
    assert idle != busy
    # «Написать» — реплика сессии; «Пусть спит» у второй — только закрытие.
    ref = f"broken:{busy}:0"
    done = await _answer(client, ref, {"action": "say", "text": "продолжай"}, ui=ui)
    assert done["status"] == "queued"
    row = _rows(db, "broken")[0]
    assert row[1] == "ui" and row[4]["action"] == "reply" and _say_source(db, row[4]["say_id"]) == "ui"


async def test_broken_by_stale_host_and_by_cut_closed_by_work(client, ui, db, clock, app, world):
    w = world
    sid = await _auto(client, ui, w, mode="manual")
    await _turn(client, w, sid, {"state": "running", "source": "say"})
    clock.advance_ms(held.HOST_STALE_MS + 1_000)
    base._cleanup(db, clock, app)  # хост замолчал: минутный цикл ядра усыпляет
    ref = f"broken:{sid}:0"
    card = _cards(await _queue(client, ui=ui), "broken")[ref]
    assert card["full"]["note"] == held_work.NOTE_STALE
    assert await _answer(client, ref, {"action": "sleep"}, ui=ui) == {"ref": ref, "status": "closed"}
    assert _rows(db, "broken")[0][4]["action"] == "sleep"
    # Обрыв хабом (ограничение автопилота) — тоже `broken`; ход пошёл снова — закрыта `worked`.
    await _host(client, w, f"sessions/{sid}/state", {"state": "idle", **PROC})
    await _turn(client, w, sid, {"state": "running", "source": "say"})
    await _turn(client, w, sid, {"state": "asleep", "cut": True})
    rows = _rows(db, "broken")
    assert rows[1][0] == ref and rows[1][1] is None and rows[1][4]["note"] == owner_queue.NOTE_CUT
    await _turn(client, w, sid, {"state": "running", "source": "signal"})
    assert _rows(db, "broken")[1][4]["action"] == "worked"


async def test_broken_superseded_by_next(client, ui, db, clock, world):
    w = world
    sid = await _auto(client, ui, w, mode="manual")
    await _turn(client, w, sid, {"state": "running", "source": "self"})
    await _host(client, w, "sessions/recover", {})
    await _host(client, w, f"sessions/{sid}/state", {"state": "idle", **PROC})
    await _permission(client, w, sid)
    await _host(client, w, "sessions/recover", {})
    rows = _rows(db, "broken")
    assert len(rows) == 2 and rows[0][1] == "system" and rows[0][4]["action"] == "superseded"
    assert rows[1][1] is None and rows[1][4]["expired"] == 1
    assert len(_cards(await _queue(client, ui=ui), "broken")) == 1


# --- dead (раздел 4) ----------------------------------------------------------------------------------------


async def test_dead_on_third_failure_retry_and_launched(client, ui, db, clock, world):
    w = world
    sid = await _auto(client, ui, w, mode="manual")
    for _ in range(2):
        await _turn(client, w, sid, {"state": "asleep", "failure": "claude не запустился"})
    assert _cards(await _queue(client, ui=ui), "dead") == {}
    await _turn(client, w, sid, {"state": "asleep", "failure": "claude не запустился (OSError)"})
    ref = f"dead:{sid}:0"
    card = _cards(await _queue(client, ui=ui), "dead")[ref]
    assert card["brief"] == "не запускается (3 раз): claude не запустился (OSError)"
    assert card["full"] == {"failure": "claude не запустился (OSError)", "fails": 3, "engine": "claude"}
    assert [a["action"] for a in card["answers"]] == ["retry", "say"]
    # Четвёртая неудача второй карточки не заводит.
    await _turn(client, w, sid, {"state": "asleep", "failure": "снова"})
    assert len(_rows(db, "dead")) == 1
    # «Попробовать снова»: счётчик обнулён, сессия поднимается под постоянную строку.
    assert await _answer(client, ref, {"action": "retry"}, ui=ui) == {"ref": ref, "status": "wake_requested"}
    rec = _rec(db, sid)
    assert rec.launch_fails == 0 and rec.failure is None and rec.signal_lost_ts is not None
    assert _rows(db, "dead")[0][4]["action"] == "retry"
    # Снова три неудачи — новая карточка; запуск удался — закрыта `launched`.
    for _ in range(3):
        await _turn(client, w, sid, {"state": "asleep", "failure": "нет"})
    assert _rows(db, "dead")[1][1] is None
    await _host(client, w, f"sessions/{sid}/state", {"state": "idle", **PROC})
    await _turn(client, w, sid, {"state": "running", "source": "signal"})
    row = _rows(db, "dead")[1]
    assert row[1] == "system" and row[4]["action"] == "launched"


async def test_dead_say_answer(client, ui, db, clock, world):
    w = world
    sid = await _auto(client, ui, w, mode="manual")
    for _ in range(3):
        await _turn(client, w, sid, {"state": "asleep", "failure": "нет"})
    ref = f"dead:{sid}:0"
    done = await _answer(client, ref, {"action": "say", "text": "попробуй ещё"}, ui=ui)
    assert done["status"] == "queued"
    rec = _rec(db, sid)
    assert rec.launch_fails == 0  # реплика сама обнуляет счётчик
    assert _rows(db, "dead")[0][4]["action"] == "reply"


# --- stuck (раздел 5) ---------------------------------------------------------------------------------------


async def test_stuck_report_update_close_and_turn_ts_kept(client, ui, db, clock, world):
    w = world
    sid = await _auto(client, ui, w, mode="manual")
    await _turn(client, w, sid, {"state": "running", "source": "say"})
    began = _rec(db, sid)
    since = _now(clock) - 16 * MIN
    await _turn(client, w, sid, {"state": "running", "stuck": {"since_ts": since, "step": {"tool": "Bash"}}})
    rec = _rec(db, sid)
    assert (rec.turn_ts, rec.turn_origin) == (began.turn_ts, "say")  # повторный `running` начало не двигает
    ref = f"stuck:{sid}:1"
    card = _cards(await _queue(client, ui=ui), "stuck")[ref]
    assert card["brief"] == "завис: шаг Bash не возвращается 16 мин"
    assert card["full"]["since_ts"] == since and card["full"]["origin"] == "say"
    assert card["full"]["turn_since_ts"] == began.turn_ts and card["full"]["state"] == "running"
    assert [a["action"] for a in card["answers"]] == ["stop", "interrupt", "wait"]
    # Повторный доклад обновляет ту же строку.
    await _turn(client, w, sid, {"state": "running", "stuck": {"since_ts": since - MIN, "step": None}})
    rows = _rows(db, "stuck")
    assert len(rows) == 1 and rows[0][4]["since_ts"] == since - MIN and rows[0][4]["step"]["tool"] is None
    assert _cards(await _queue(client, ui=ui), "stuck")[ref]["brief"] == "завис на 17 мин"
    # Шаг пришёл — закрыта `stepped`.
    await _turn(client, w, sid, {"state": "running", "stuck": None})
    assert _rows(db, "stuck")[0][4]["action"] == "stepped"
    # Снова завис, ход кончился — `turn_end`.
    await _turn(client, w, sid, {"state": "running", "stuck": {"since_ts": since}})
    await _turn(client, w, sid, {"state": "idle", "last_turn_ts": _now(clock)})
    assert _rows(db, "stuck")[1][4]["action"] == "turn_end"
    assert _cards(await _queue(client, ui=ui), "stuck") == {}


async def test_stuck_answers_stop_interrupt_wait(client, ui, db, clock, world):
    w = world
    sid = await _auto(client, ui, w, mode="manual")
    await _turn(client, w, sid, {"state": "running", "source": "say"})
    stuck = {"since_ts": _now(clock), "step": {"tool": "Bash"}}
    await _turn(client, w, sid, {"state": "running", "stuck": stuck})
    ref = f"stuck:{sid}:1"
    assert await _answer(client, ref, {"action": "wait"}, ui=ui) == {"ref": ref, "status": "closed"}
    await _turn(client, w, sid, {"state": "running", "stuck": stuck})
    done = await _answer(client, ref, {"action": "interrupt", "text": "брось, сделай иначе"}, ui=ui)
    assert done["status"] == "queued"
    rows = _rows(db, "stuck")
    assert rows[1][4]["action"] == "interrupt"
    with db.reader() as conn:
        priority = conn.execute(
            "SELECT priority FROM main.held_say WHERE id = ?", (rows[1][4]["say_id"],)
        ).fetchone()[0]
    assert priority == "now"
    await _turn(client, w, sid, {"state": "running", "stuck": stuck})
    assert await _answer(client, ref, {"action": "stop"}, ui=ui) == {"ref": ref, "status": "sleep_requested"}
    assert _rec(db, sid).sleep_requested_ts is not None
    assert _rows(db, "stuck")[2][4]["action"] == "stop"
    # Сон кончает зависший ход: открытая закрыта `asleep`.
    await _turn(client, w, sid, {"state": "running", "stuck": stuck})
    await _turn(client, w, sid, {"state": "asleep"})
    assert _rows(db, "stuck")[3][4]["action"] == "asleep"


async def test_stuck_unknown_key_rejected(client, ui, db, clock, world):
    w = world
    sid = await _auto(client, ui, w, mode="manual")
    await _host(
        client,
        w,
        f"sessions/{sid}/turn",
        {"state": "running", "stuck": {"since_ts": 1, "extra": 1}},
        expect=422,
    )


# --- закрытие сессии, канал ---------------------------------------------------------------------------------


async def test_close_session_closes_all_states(client, ui, db, clock, world):
    w = world
    sid, end = await _quiet(client, ui, w, clock)
    await _turn(client, w, sid, {"state": "running", "source": "self"})
    await _turn(client, w, sid, {"state": "running", "stuck": {"since_ts": end}})
    r = await ui.post(f"{API}/sessions/{sid}/close")
    assert r.status_code == 200, r.text
    row = _rows(db, "stuck")[0]
    assert row[1] == "system" and row[4]["action"] == "closed"
    await _answer(client, f"stuck:{sid}:2", {"action": "wait"}, ui=ui, expect=409)


async def test_stall_retired_no_new_cards(client, ui, db, clock, app, world):
    """Владелец 22.10: «стоит без дела» отменена — сессия молчит хоть час, карточки нет; старая открытая и
    спрятанная «Пусть ждёт» закрываются минутным циклом (`retired`)."""
    w = world
    sid, _end = await _quiet(client, ui, w, clock)
    for n, hold_ts in ((1, None), (2, 5)):
        db.write_sync(
            lambda conn, n=n, hold_ts=hold_ts: conn.execute(
                "INSERT INTO main.owner_item (ref, kind, project_id, session_id, created_ts, seq, hold_ts, "
                "payload_json) VALUES (?, 'stall', ?, ?, 1, ?, ?, '{}')",
                (f"stall:{sid}:{n}", _rec(db, sid).project_id, sid, 1000 + n, hold_ts),
            )
        )
    clock.advance_ms(61 * MIN)
    base._cleanup(db, clock, app)
    assert _cards(await _queue(client, ui=ui), "stall") == {}
    rows = _rows(db, "stall")
    assert len(rows) == 2 and all(r[1] == "system" and r[4]["action"] == "retired" for r in rows)
    assert runner.owner_queue is owner_queue


async def test_bg_wake_at_ts_kept_and_returned(client, ui, db, clock, world):
    w = world
    sid = await _auto(client, ui, w, mode="manual")
    bg = {"items": [], "wake_at_ts": 123_456}
    await _host(client, w, f"sessions/{sid}/state", {"state": "idle", **PROC, "bg": bg})
    session = (await ui.get(f"{API}/sessions/{sid}")).json()["session"]
    assert session["bg"] == {"items": [], "wake_at_ts": 123_456}
    # Снимок без пробуждения — ключ уходит; без задач и без пробуждения — «задач нет».
    await _host(client, w, f"sessions/{sid}/state", {"state": "idle", **PROC, "bg": {"items": []}})
    assert (await ui.get(f"{API}/sessions/{sid}")).json()["session"]["bg"] == {"items": []}


# --- ревью Ф56 (приёмка программиста хаба, круг 1) -----------------------------------------------------


async def test_dead_closes_when_turn_starts_after_window_say(client, ui, db, clock, world):
    """Ревью (1): владелец написал сессии из окна (не по карточке) — `say` обнулил счётчик неудач; ход пошёл —
    карточка `dead` закрыта `launched`, а не висит, держа сессию занятой."""
    w = world
    sid = await _auto(client, ui, w, mode="manual")
    for _ in range(3):
        await _turn(client, w, sid, {"state": "asleep", "failure": "claude не запустился"})
    ref = f"dead:{sid}:0"
    assert ref in _refs(await _queue(client, ui=ui))
    say_id = await _say(ui, sid, "попробуй ещё раз")
    assert _rec(db, sid).launch_fails == 0
    await _host(client, w, f"sessions/{sid}/state", {"state": "idle", **PROC})
    await _turn(client, w, sid, {"state": "running", "source": "say"})
    await _host(client, w, f"session-says/{say_id}/delivered", {})
    await _turn(client, w, sid, {"state": "idle", "last_turn_ts": 5_000})
    assert ref not in _refs(await _queue(client, ui=ui))
    row = _rows(db, "dead")[0]
    assert row[1] == "system" and row[4]["action"] == "launched"


async def test_broken_wake_when_session_already_up_closes_card(client, ui, db, clock, world):
    """Ревью (6): сессию уже подняли из окна (она `idle`) — «Поднять» по `broken` закрывает карточку, 200."""
    w = world
    sid = await _auto(client, ui, w, mode="manual")
    await _turn(client, w, sid, {"state": "running", "source": "self"})
    await _host(client, w, "sessions/recover", {})
    ref = f"broken:{sid}:0"
    assert ref in _cards(await _queue(client, ui=ui), "broken")
    # Поднялась сама (реплика из окна, подъём хостом) — и стоит `idle`, карточка ещё открыта.
    await _host(client, w, f"sessions/{sid}/state", {"state": "idle", **PROC})
    assert ref in _cards(await _queue(client, ui=ui), "broken")  # `idle` — не ход, карточку не закрыл
    assert await _answer(client, ref, {"action": "wake"}, ui=ui) == {"ref": ref, "status": "closed"}
    assert _rec(db, sid).wake_requested_ts is None
    row = _rows(db, "broken")[0]
    assert row[1] == "ui" and row[4]["action"] == "wake"
    assert ref not in _cards(await _queue(client, ui=ui), "broken")
