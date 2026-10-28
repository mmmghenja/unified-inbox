"""Ф66-1, починка по ревью (docs/architecture.md Р1–Р7): регрессия.

- Р1: ручная сессия хаба без живой подписки — её черновик и нажатие кнопки бота владельцу (по таблице не идут);
- Р2: напоминание при `reminder = auto` прячется от владельца, только если сигнал сессии отдан или его есть кому
  отдать; напоминание владельца и роль без живой подписки — владельцу;
- Р3: день проекта закрыт — «сразу да» не действует; отложенное берёт минутный проход после «Начать день»;
- Р4: перенос прежних настроек — один раз; новый проект получает «всё мне» сразу;
- Р5: пределы «сразу да» общие с авто-отправкой и авто-нажатием;
- Р6: пауза лимита подписки при `states = auto` — владельцу, «сразу да» её не снимает;
- Р7: отказ ядра в минутном проходе не повторяется каждую минуту, карточка остаётся владельцу.

Сценарии Р1–Р4 — тесты ревьюера (`.runs/critic-f66/`). Мир и помощники — test_hub_sessions.py,
test_f66_card_route.py; хост поддельный."""

from hub.core import admin, autosend, card_route, held, held_signals, presses, workday
from hub.core import owner_queue as zq

from . import test_hub_sessions as base
from .test_f41_autopilot_5b import DIGEST, PROC, _auto, _me, _send
from .test_f41_autopilot_5b import _outbox as _outbox_row
from .test_f44_presses import _bot_msg, _ident, _press
from .test_f56_queue import _iso, _queue, _refs
from .test_f66_card_route import (
    ALL_ME,
    _auto_audits,
    _global_hold,
    _migrate,
    _route,
    _sweep,
    _system_items,
    _table,
)
from .test_hub_sessions import A, _h, _host, _mk, _now, _sub

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"
AUDIT = {"actor": "ui", "api_token_id": None, "client_name": None, "request_id": ""}


def _status(db, outbox_id) -> str:
    return _outbox_row(db, outbox_id)[0]


def _press_status(db, press_id) -> str:
    with db.reader() as conn:
        return presses.get(conn, press_id).status


def _close_day(db, clock, pid) -> None:
    now = _now(clock)

    def _close(conn):
        workday.set_workday(conn, pid, True, now_ms=now)
        workday.set_day(conn, pid, False, now_ms=now, audit=AUDIT)

    db.write_sync(_close)


def _open_day(db, clock, pid) -> None:
    now = _now(clock)
    db.write_sync(lambda conn: workday.set_day(conn, pid, True, now_ms=now, audit=AUDIT))


async def _manual_without_sub(client, ui, w) -> int:
    """Ручная сессия хаба с идущим процессом и ключом запуска, но без подписки на проект."""
    sid = (await _mk(ui, w, role="ручная", mode="manual"))["id"]
    await _host(client, w, f"sessions/{sid}/state", {"state": "idle", **PROC, "launch_key_hash": DIGEST})
    return sid


def _fired(db, clock, rem_id, *, sub_id=None, signaled=False) -> None:
    now = _now(clock)
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.reminder SET status = 'fired', fired_ts = ?, signal_sub_id = ?, signaled_ts = ? "
            "WHERE id = ?",
            (now, sub_id, now if signaled else None, rem_id),
        )
    )


async def _session_reminder(client, w, clock, sid) -> int:
    r = await client.post(
        f"{API}/reminders",
        json={"channel_id": w["ch"], "at": _iso(clock, 60), "note": "проверить", **_me(sid, None)},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    return body["id"] if "id" in body else body["reminder"]["id"]


def _sub_of(db, sid) -> int:
    with db.reader() as conn:
        return conn.execute(
            "SELECT id FROM main.session_sub WHERE session_pid = ? AND ended_ts IS NULL",
            (held.held_pid(sid),),
        ).fetchone()[0]


# --- Р1: ручная сессия хаба без подписки --------------------------------------------------------------------


async def test_r1_manual_hub_session_without_sub_draft_stays_with_owner(client, ui, db, clock, world):
    w = world
    await _route(ui, w, {"draft": "auto"})
    sid = await _manual_without_sub(client, ui, w)
    made = await _send(client, w, w["ch"], _me(sid), text="ручная сессия, без подписки")
    assert _status(db, made["outbox_id"]) == "pending_approval", "черновик ручной сессии хаба ушёл без клика"
    card = _refs(await _queue(client, ui=ui))[f"draft:{made['outbox_id']}"]
    assert card["session_id"] == sid and card["in_channel"] is False
    # Та же сессия в авто — черновик идёт по таблице («сразу да»).
    db.write_sync(lambda conn: conn.execute("UPDATE main.hub_session SET mode = 'auto' WHERE id = ?", (sid,)))
    auto = await _send(client, w, w["ch"], _me(sid), text="теперь в авто")
    assert _status(db, auto["outbox_id"]) == "queued"
    assert _auto_audits(db, "draft")[-1]["session_id"] == sid


async def test_r1_manual_hub_session_with_sub_stays_with_owner(client, ui, db, clock, world):
    w = world
    await _route(ui, w, {"draft": "auto"})
    sid = await _auto(client, ui, w, role="ручная", mode="manual")
    made = await _send(client, w, w["ch"], _me(sid), text="ручная сессия с подпиской")
    assert _status(db, made["outbox_id"]) == "pending_approval"


async def test_r1_manual_hub_session_without_sub_draft_new_stays(client, ui, db, clock, world):
    """Заготовка новому адресату (`/drafts/new`) — та же метка сессии хаба в журнале создания."""
    w = world
    await _route(ui, w, {"draft": "auto"})
    sid = await _manual_without_sub(client, ui, w)
    r = await client.post(
        f"{API}/drafts/new",
        json={
            "project_id": w["p3"],
            "account_id": w["acc"],
            "to": "@new_person",
            "text": "привет",
            "idempotency_key": "r1-new",
            **_me(sid),
        },
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text
    outbox_id = r.json()["outbox_id"]
    assert _status(db, outbox_id) == "pending_approval"
    with db.reader() as conn:
        assert zq._locate(conn, "draft", outbox_id, _now(clock)).hub_id == sid  # noqa: SLF001


async def test_r1_manual_hub_session_without_sub_press_stays_with_owner(client, ui, db, clock, world):
    w = world
    await _route(ui, w, {"press": "auto"})
    sid = await _manual_without_sub(client, ui, w)
    mid = _bot_msg(db, w, clock)
    made = await _press(client, w, mid, _me(sid), 1)
    assert _press_status(db, made["press"]["id"]) == "pending", "нажатие ручной сессии хаба ушло без клика"
    card = _refs(await _queue(client, ui=ui))[f"press:{made['press']['id']}"]
    assert card["session_id"] == sid


# --- Р2: напоминание при `reminder = auto` ------------------------------------------------------------------


async def test_r2_owner_own_reminder_shown_under_auto(client, ui, db, clock, world):
    w = world
    r = await ui.post(f"{API}/reminders", {"channel_id": w["ch"], "at": _iso(clock, 60), "note": "моё"})
    assert r.status_code == 201, r.text
    body = r.json()
    rem_id = body["id"] if "id" in body else body["reminder"]["id"]
    _fired(db, clock, rem_id)
    ref = f"reminder:{rem_id}"
    assert ref in _refs(await _queue(client, ui=ui))
    await _route(ui, w, {"reminder": "auto"})
    for _ in range(3):
        _sweep(db, clock)
    assert ref in _refs(await _queue(client, ui=ui)), "напоминание владельца при «сразу да» пропало"


async def test_r2_reminder_of_role_without_live_sub_shown(client, ui, db, clock, world):
    w = world
    sid = await _auto(client, ui, w)
    await _route(ui, w, {"reminder": "auto"})
    rem_id = await _session_reminder(client, w, clock, sid)
    _fired(db, clock, rem_id)
    ref = f"reminder:{rem_id}"
    assert ref not in _refs(await _queue(client, ui=ui))  # подписка роли жива — сигнал уйдёт ей
    now = _now(clock)
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.session_sub SET ended_ts = ?, end_reason = 'unsubscribed' WHERE id = ?",
            (now, _sub_of(db, sid)),
        )
    )
    assert ref in _refs(await _queue(client, ui=ui)), "сигнал отдать некому, а владельцу напоминание не видно"


async def test_r2_reminder_heard_only_by_manual_hub_session_shown(client, ui, db, clock, world):
    w = world
    sid = await _auto(client, ui, w)
    await _route(ui, w, {"reminder": "auto"})
    rem_id = await _session_reminder(client, w, clock, sid)
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.hub_session SET mode = 'manual' WHERE id = ?", (sid,))
    )
    _fired(db, clock, rem_id)
    assert f"reminder:{rem_id}" in _refs(await _queue(client, ui=ui))


# --- Р3: закрытый день --------------------------------------------------------------------------------------


async def test_r3_auto_wake_not_in_closed_day(client, ui, db, clock, world, monkeypatch):
    w = world
    sid = await _auto(client, ui, w)
    await _route(ui, w, {"states": "auto"})
    _close_day(db, clock, w["p3"])
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.hub_session SET wait_reason = 'heavy' WHERE id = ?", (sid,))
    )
    monkeypatch.setattr(held_signals, "pending_total", lambda *a, **k: 3)
    answered = _sweep(db, clock)
    with db.reader() as conn:
        woke = held.get(conn, sid).wake_requested_ts
    assert answered == [] and woke is None, f"день закрыт, а «сразу да» подняло сессию: {answered}"
    _open_day(db, clock, w["p3"])
    assert _sweep(db, clock) == [f"heavy:{sid}"]


async def test_r3_draft_in_closed_day_waits_for_day_open(client, ui, db, clock, world):
    w = world
    await _sub(client, w, A, role="внешний")
    ident = {"session": base._sess(A)}
    await _route(ui, w, {"draft": "auto"})
    _close_day(db, clock, w["p3"])
    made = await _send(client, w, w["ch"], ident, text="ночью")
    ref = f"draft:{made['outbox_id']}"
    assert _status(db, made["outbox_id"]) == "pending_approval"
    assert [(a["ref"], a["why"]) for a in _auto_audits(db, "held")] == [(ref, card_route.DAY_CLOSED)]
    assert _sweep(db, clock) == []  # день ещё закрыт
    assert _status(db, made["outbox_id"]) == "pending_approval"
    _open_day(db, clock, w["p3"])
    assert _sweep(db, clock) == [ref]
    assert _status(db, made["outbox_id"]) == "queued"
    assert _sweep(db, clock) == []  # второй раз не берёт


async def test_r3_deferred_card_answered_by_owner_is_not_retaken(client, ui, db, clock, world):
    w = world
    await _sub(client, w, A, role="внешний")
    await _route(ui, w, {"draft": "auto"})
    _close_day(db, clock, w["p3"])
    made = await _send(client, w, w["ch"], {"session": base._sess(A)}, text="ночью")
    ref = f"draft:{made['outbox_id']}"
    r = await ui.post(f"{API}/owner_channel/{ref}/answer", {"action": "cancel"})
    assert r.status_code == 200, r.text
    _open_day(db, clock, w["p3"])
    assert _sweep(db, clock) == []
    assert _auto_audits(db, "held")[-1]["why"] == "gone"
    count = len(_auto_audits(db))
    assert _sweep(db, clock) == [] and len(_auto_audits(db)) == count  # больше не трогает


async def test_r3_permission_in_closed_day_is_a_card(client, ui, db, clock, world):
    w = world
    sid = await _auto(client, ui, w)
    await _route(ui, w, {"permission": "auto"})
    _close_day(db, clock, w["p3"])
    r = await _host(
        client,
        w,
        f"sessions/{sid}/requests",
        {"kind": "permission", "tool": "Bash", "input": {"command": "ls"}},
    )
    assert r["status"] == "pending" and "decided" not in r
    assert _auto_audits(db, "held")[-1]["why"] == card_route.DAY_CLOSED


# --- Р4: перенос один раз -----------------------------------------------------------------------------------


async def test_r4_new_project_keeps_all_me_after_restart(client, ui, db, clock, world):
    _global_hold(db, {"human_agent": False})  # прежняя общая настройка, оставшаяся в meta
    _migrate(db, clock)  # первый старт после выкладки
    new_pid = db.write_sync(lambda conn: admin.create_project(conn, name="Новый", color=None, workdir=None))
    assert _table(db, new_pid) == ALL_ME
    assert _migrate(db, clock) == []  # следующий рестарт ядра
    assert _table(db, new_pid) == ALL_ME, f"таблица нового проекта сменилась сама: {_table(db, new_pid)}"


async def test_r4_project_without_key_after_first_start_is_not_migrated(client, ui, db, clock, world):
    w = world
    _migrate(db, clock)
    db.write_sync(
        lambda conn: conn.execute(
            'UPDATE main.project SET agent_policy_json = \'{"autopilot_hold": {"human_agent": false}}\' '
            "WHERE id = ?",
            (w["p4"],),
        )
    )
    assert _migrate(db, clock) == []
    assert _table(db, w["p4"]) == ALL_ME


# --- Р5: общие пределы --------------------------------------------------------------------------------------


async def test_r5_route_auto_and_list_autosend_share_the_channel_limit(
    client, ui, db, clock, world, monkeypatch
):
    w = world
    monkeypatch.setattr(autosend, "AUTOSEND_PER_HOUR", 1)
    await _sub(client, w, A, role="внешний")
    ident = {"session": base._sess(A)}
    await _route(ui, w, {"draft": "auto"})
    first = await _send(client, w, w["ch"], ident, text="сразу да")
    assert _status(db, first["outbox_id"]) == "queued"
    r = await ui.put(f"{API}/channels/{w['ch']}/autosend", {"role": "внешний", "on": True})
    assert r.status_code == 200, r.text
    second = await _send(client, w, w["ch"], ident, text="по списку")
    assert _status(db, second["outbox_id"]) == "pending_approval", "список обошёл общий предел беседы"
    assert second.get("auto_limit") == "channel"


async def test_r5_list_autosend_counts_for_route_auto(client, ui, db, clock, world, monkeypatch):
    w = world
    monkeypatch.setattr(autosend, "AUTOSEND_PER_HOUR", 1)
    await _sub(client, w, A, role="внешний")
    ident = {"session": base._sess(A)}
    r = await ui.put(f"{API}/channels/{w['ch']}/autosend", {"role": "внешний", "on": True})
    assert r.status_code == 200, r.text
    first = await _send(client, w, w["ch"], ident, text="по списку")
    assert _status(db, first["outbox_id"]) == "queued"
    r = await ui.put(f"{API}/channels/{w['ch']}/autosend", {"role": "внешний", "on": False})
    assert r.status_code == 200, r.text
    await _route(ui, w, {"draft": "auto"})
    second = await _send(client, w, w["ch"], ident, text="сразу да")
    assert _status(db, second["outbox_id"]) == "pending_approval", "«сразу да» обошло общий предел беседы"
    assert _auto_audits(db, "held")[-1]["why"] == "limit_channel"


async def test_r5_hub_session_limit_counts_route_auto(client, ui, db, clock, world, monkeypatch):
    w = world
    monkeypatch.setattr(autosend, "AUTOPILOT_SESSION_PER_HOUR", 1)
    sid = await _auto(client, ui, w)
    await _route(ui, w, {"draft": "auto"})
    first = await _send(client, w, w["ch"], _me(sid), text="раз")
    second = await _send(client, w, w["ch2"], _me(sid), text="два")
    assert _status(db, first["outbox_id"]) == "queued"
    assert _status(db, second["outbox_id"]) == "pending_approval"
    assert _auto_audits(db, "held")[-1]["why"] == "limit_session"
    with db.reader() as conn:
        assert autosend._session_count(conn, sid, _now(clock) - 3_600_000) == 1  # noqa: SLF001


async def test_r5_list_press_counts_for_route_auto_press(client, ui, db, clock, world, monkeypatch):
    w = world
    monkeypatch.setattr(presses, "PRESS_PER_HOUR", 1)
    await _sub(client, w, A, role="внешний")
    r = await ui.put(f"{API}/channels/{w['ch']}/autosend", {"role": "внешний", "on": True})
    assert r.status_code == 200, r.text
    mid = _bot_msg(db, w, clock)
    listed = await _press(client, w, mid, _ident(A), 1)
    assert _press_status(db, listed["press"]["id"]) != "pending"
    r = await ui.put(f"{API}/channels/{w['ch']}/autosend", {"role": "внешний", "on": False})
    assert r.status_code == 200, r.text
    await _route(ui, w, {"press": "auto"})
    made = await _press(client, w, mid, _ident(A), 2)
    assert _press_status(db, made["press"]["id"]) == "pending", "«сразу да» обошло общий предел нажатий"
    assert _auto_audits(db, "held")[-1]["why"] == "limit_channel"


# --- Р6: пауза лимита подписки ------------------------------------------------------------------------------


async def test_r6_paused_is_never_auto_answered(client, ui, db, clock, world):
    w = world
    sid = await _auto(client, ui, w)
    await _route(ui, w, {"states": "auto"})
    r = await ui.post(f"{API}/sessions/{sid}/say", {"text": "сделай отчёт"})
    assert r.status_code == 200, r.text
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.hub_session SET wait_reason = 'paused' WHERE id = ?", (sid,))
    )
    assert _sweep(db, clock) == []
    with db.reader() as conn:
        assert held.get(conn, sid).wake_requested_ts is None
        assert card_route.for_card(conn, w["p3"], "paused", sid) == card_route.ME
    card = _refs(await _queue(client, ui=ui))[f"paused:{sid}"]
    assert card["in_channel"] is False
    ref = f"paused:{sid}"
    assert db.write_sync(lambda conn: card_route.auto_answer(conn, ref, now_ms=_now(clock))).done is False


# --- Р7: отказ ядра в минутном проходе — один раз -----------------------------------------------------------


async def test_r7_heavy_error_is_not_retried_every_minute(client, ui, db, clock, world, monkeypatch):
    w = world
    sid = await _auto(client, ui, w)
    await _route(ui, w, {"states": "auto"})
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.hub_session SET wait_reason = 'heavy' WHERE id = ?", (sid,))
    )
    monkeypatch.setattr(held_signals, "pending_total", lambda *a, **k: 3)

    def _boom(*a, **k):
        raise RuntimeError("ядро отказало")

    monkeypatch.setattr(held_signals, "wake", _boom)
    assert _sweep(db, clock) == []
    errors = [a for a in _auto_audits(db, "held") if a["why"].startswith("error:")]
    assert len(errors) == 1 and errors[0]["turns"] == 0
    for _ in range(3):
        clock.advance_ms(base.MIN)
        assert _sweep(db, clock) == []
    assert len(_auto_audits(db, "held")) == 1, "отказ повторяется каждую минуту"
    assert f"heavy:{sid}" in _refs(await _queue(client, ui=ui))
    # Новый ход — новое состояние: попытка снова (один раз).
    monkeypatch.undo()
    monkeypatch.setattr(held_signals, "pending_total", lambda *a, **k: 3)
    db.write_sync(lambda conn: conn.execute("UPDATE main.hub_session SET turns = 1 WHERE id = ?", (sid,)))
    assert _sweep(db, clock) == [f"heavy:{sid}"]


async def test_r7_reminder_error_is_not_retried_and_shown(client, ui, db, clock, world, monkeypatch):
    w = world
    sid = await _auto(client, ui, w)
    await _route(ui, w, {"reminder": "auto"})
    rem_id = await _session_reminder(client, w, clock, sid)
    _fired(db, clock, rem_id, sub_id=_sub_of(db, sid), signaled=True)
    ref = f"reminder:{rem_id}"
    assert ref not in _refs(await _queue(client, ui=ui))

    def _boom(*a, **k):
        raise RuntimeError("ядро отказало")

    monkeypatch.setattr(zq, "_execute", _boom)
    for _ in range(3):
        assert _sweep(db, clock) == []
        clock.advance_ms(base.MIN)
    assert len(_auto_audits(db, "held")) == 1
    assert ref in _refs(await _queue(client, ui=ui)), "после отказа напоминание владельцу не видно"
    assert _system_items(db) == []
