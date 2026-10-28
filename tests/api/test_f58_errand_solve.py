"""Ф58-3 (docs/architecture.md–3), с поправкой Ф65-1 Ж1
(docs/architecture.md): пока по карточке открыто поручение агенту, ответ владельца (кука) и
его бота на КАЖДОМ пути ответа не даёт 409 `errand_open`, а закрывает поручение (`closed_by='ui'`, причина
«ответил владелец») и исполняется; агент отвечает как раньше. Выдача заявки «нужен человек» владельцу и боту несёт
`errand` и `errand_returned`, сессии и агенту — нет; возврат виден причиной, новое поручение её прячет.

Мир и помощники — из test_hub_sessions.py, test_f41_autopilot_5b.py, test_f56_queue.py."""

import json

from hub.core import held, held_work
from hub.core import owner_queue as zq

from . import test_hub_sessions as base
from .test_f41_autopilot_5b import _auto, _me, _send
from .test_f54_human_requests import _create as _human_create
from .test_f56_queue import _agent, _answer, _bot, _channel_on, _iso, _pending, _permission, _queue, _refs
from .test_hub_sessions import _h, _host, _hs, _now

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"


async def _errand(ui, ref, text="реши сам") -> dict:
    r = await ui.post(f"{API}/owner_channel/{ref}/errand", {"text": text})
    assert r.status_code == 200, r.text
    return r.json()["errand"]


async def _take_back(ui, errand_id) -> None:
    r = await ui.c.delete(f"{API}/owner_channel/errands/{errand_id}", headers=ui.headers)
    assert r.status_code == 200, r.text


def _errand_rows(db) -> list[tuple]:
    with db.reader() as conn:
        return conn.execute(
            "SELECT id, closed_by FROM main.owner_item WHERE kind = 'errand' ORDER BY id"
        ).fetchall()


def _taken(db, errand_id: int) -> None:
    """Ф65-1 Ж1: поручение закрыто ответом владельца — `ui`, причина «ответил владелец»."""
    with db.reader() as conn:
        by, payload = conn.execute(
            "SELECT closed_by, payload_json FROM main.owner_item WHERE id = ?", (errand_id,)
        ).fetchone()
    assert by == "ui" and json.loads(payload)["returned"] == zq.TAKEN_BY_OWNER


def _ok(r) -> dict:
    assert r.status_code in (200, 201), r.text
    return r.json()


# --- разрешение сессии: очередь и прежняя ручка -------------------------------------------------------------


async def test_permission_card_with_errand_is_answered_by_owner(client, ui, db, world):
    w = world
    agent, _ = _agent(db)
    sid = await _auto(client, ui, w, mode="manual")
    await _channel_on(ui, w, sid)
    rid = await _permission(client, w, sid)
    ref = f"req:{rid}"
    made = await _errand(ui, ref)

    # Ответ из очереди канала — поручение закрыто `ui`, карточка решена.
    assert (await _answer(client, ref, {"action": "allow"}, ui=ui))["status"] == "allowed"
    _taken(db, made["id"])
    assert ref not in _refs(await _queue(client, ui=ui))

    # Прежняя ручка решения карточки сессии — так же.
    rid2 = await _permission(client, w, sid)
    second = await _errand(ui, f"req:{rid2}")
    _ok(await ui.post(f"{API}/session-requests/{rid2}/decide", {"decision": "deny"}))
    _taken(db, second["id"])

    # Агент при открытом поручении отвечает как раньше — его поручение закрывается `agent`.
    rid3 = await _permission(client, w, sid)
    third = await _errand(ui, f"req:{rid3}")
    assert _refs(await _queue(client, agent))[f"errand:{third['id']}"]["kind"] == "errand"
    assert (await _answer(client, f"req:{rid3}", {"action": "allow"}, token=agent))["status"] == "allowed"
    assert _errand_rows(db) == [(made["id"], "ui"), (second["id"], "ui"), (third["id"], "agent")]


async def test_errand_return_shows_reason_and_new_errand_hides_it(client, ui, db, clock, world):
    w = world
    agent, _ = _agent(db)
    sid = await _auto(client, ui, w, mode="manual")
    await _channel_on(ui, w, sid)
    ref = f"req:{await _permission(client, w, sid)}"
    made = await _errand(ui, ref)
    r = await client.post(
        f"{API}/owner_channel/errands/{made['id']}/return",
        json={"reason": "не вышло: нет доступа"},
        headers=_h(agent),
    )
    assert r.status_code == 200, r.text
    card = _refs(await _queue(client, ui=ui))[ref]
    assert card["errand"] is None
    assert card["errand_returned"] == {
        "reason": "не вышло: нет доступа",
        "detail": None,
        "ts": _now(clock),
        "by": "agent",
    }
    # Новое поручение прячет прежнюю причину; отзыв владельца (Ф65-1) — сам возврат «остановил владелец».
    second = await _errand(ui, ref)
    card = _refs(await _queue(client, ui=ui))[ref]
    assert card["errand"]["id"] == second["id"] and card["errand_returned"] is None
    await _take_back(ui, second["id"])
    returned = _refs(await _queue(client, ui=ui))[ref]["errand_returned"]
    assert returned["reason"] == zq.STOPPED_BY_OWNER and returned["by"] == "ui"


async def test_errand_closes_when_card_expires_elsewhere(client, ui, db, world):
    """Срок и снятие — не ответ владельца: карточку закрывает хост, поручение закрывается `system`."""
    w = world
    agent, _ = _agent(db)
    sid = await _auto(client, ui, w, mode="manual")
    await _channel_on(ui, w, sid)
    rid = await _permission(client, w, sid)
    made = await _errand(ui, f"req:{rid}")
    assert (await _host(client, w, f"session-requests/{rid}/expire", {}))["status"] == "expired"
    assert f"errand:{made['id']}" not in _refs(await _queue(client, agent))
    assert _errand_rows(db) == [(made["id"], "system")]


# --- «нужен человек»: кука, бот, агент; поля выдачи ---------------------------------------------------------


async def test_human_request_errand_paths_and_fields(client, ui, db, clock, world):
    w = world
    agent, _ = _agent(db)
    bot, _ = _bot(db)
    sid = await _auto(client, ui, w, mode="manual")
    await _channel_on(ui, w, sid)

    # Владелец (кука) любым исходом, его бот и ответ из очереди — заявка решена, поручение закрыто `ui`.
    paths = [
        lambda hid: ui.post(f"{API}/human-requests/{hid}/done"),
        lambda hid: ui.post(f"{API}/human-requests/{hid}/failed"),
        lambda hid: ui.post(f"{API}/human-requests/{hid}/later"),
        lambda hid: client.post(f"{API}/human-requests/{hid}/done", json={}, headers=_h(bot)),
        lambda hid: ui.post(f"{API}/owner_channel/human:{hid}/answer", {"action": "done"}),
    ]
    for path in paths:
        hid0 = (await _human_create(client, w, session=_hs(sid)))["request"]["id"]
        taken = await _errand(ui, f"human:{hid0}")
        _ok(await path(hid0))
        _taken(db, taken["id"])
        with db.reader() as conn:
            status = conn.execute("SELECT status FROM main.human_request WHERE id = ?", (hid0,)).fetchone()[0]
        assert status in ("done", "failed", "later")

    hid = (await _human_create(client, w, session=_hs(sid)))["request"]["id"]
    ref = f"human:{hid}"
    made = await _errand(ui, ref, "введи код из почты")

    # Поля выдачи: владельцу и боту — есть; сессии и агенту — нет.
    shown = {
        "id": made["id"],
        "action": None,
        "label": None,
        "text": "введи код из почты",
        "created_ts": _now(clock),
        "progress": None,
        "progress_ts": _now(clock),
        "auto": False,
        # Ф96-1: руки — у агента, пока их не передавали.
        "hands": "agent",
        "hands_by": None,
        "hands_ts": None,
        "hands_why": None,
    }
    one = (await ui.get(f"{API}/human-requests/{hid}")).json()
    assert one["status"] == "pending" and one["errand"] == shown and one["errand_returned"] is None
    listed = next(i for i in (await ui.get(f"{API}/human-requests")).json() if i["id"] == hid)
    assert listed["errand"] == shown
    r = await client.get(f"{API}/human-requests/{hid}", headers=_h(bot))
    assert r.status_code == 200 and r.json()["errand"] == shown
    r = await client.get(f"{API}/human-requests", headers=_h(bot))
    assert next(i for i in r.json() if i["id"] == hid)["errand"] == shown
    synced = (await ui.get(f"{API}/sync", params={"since": 0})).json()["human_requests"]
    assert next(i for i in synced if i["id"] == hid)["errand"] == shown
    r = await client.get(f"{API}/human-requests/{hid}", headers=_h(agent))
    assert r.status_code == 200 and "errand" not in r.json() and "errand_returned" not in r.json()
    r = await client.get(f"{API}/human-requests", params=_hs(sid), headers=_h(w["tok"], 8))
    assert r.status_code == 200, r.text
    mine = next(i for i in r.json() if i["id"] == hid)
    assert "errand" not in mine and "errand_returned" not in mine

    # Прогресс агента виден владельцу в заявке.
    r = await client.post(
        f"{API}/owner_channel/errands/{made['id']}/progress", json={"text": "жду письмо"}, headers=_h(agent)
    )
    assert r.status_code == 200, r.text
    assert (await ui.get(f"{API}/human-requests/{hid}")).json()["errand"]["progress"] == "жду письмо"
    assert (await ui.get(f"{API}/human-requests/{hid}")).json()["errand"]["progress_ts"] == _now(clock)

    # Агент вернул — причина в выдаче, кнопки владельца снова работают.
    clock.advance_ms(5_000)
    r = await client.post(
        f"{API}/owner_channel/errands/{made['id']}/return", json={"reason": "письмо не пришло"}, headers=_h(agent)
    )
    assert r.status_code == 200, r.text
    one = (await ui.get(f"{API}/human-requests/{hid}")).json()
    assert one["errand"] is None
    assert one["errand_returned"] == {
        "reason": "письмо не пришло",
        "detail": None,
        "ts": _now(clock),
        "by": "agent",
    }
    r = await client.get(f"{API}/human-requests/{hid}", headers=_h(bot))
    assert r.json()["errand_returned"]["reason"] == "письмо не пришло"
    assert _refs(await _queue(client, ui=ui))[ref]["errand_returned"]["reason"] == "письмо не пришло"

    # Новое поручение прячет причину; агент отвечает своей прежней ручкой — 200.
    again = await _errand(ui, ref)
    one = (await ui.get(f"{API}/human-requests/{hid}")).json()
    assert one["errand"]["id"] == again["id"] and one["errand_returned"] is None
    r = await client.post(f"{API}/human-requests/{hid}/done", json={}, headers=_h(agent))
    assert r.status_code == 200, r.text
    with db.reader() as conn:
        assert conn.execute(
            "SELECT status, decided_by FROM main.human_request WHERE id = ?", (hid,)
        ).fetchone() == ("done", "agent")

    # Вторая заявка: «Забрать у агента» — владелец решает сам.
    hid2 = (await _human_create(client, w, session=_hs(sid)))["request"]["id"]
    second = await _errand(ui, f"human:{hid2}")
    await _take_back(ui, second["id"])
    r = await ui.post(f"{API}/human-requests/{hid2}/failed")
    assert r.status_code == 200 and r.json()["status"] == "failed"


# --- черновики: одиночно, пачкой, отмена ----------------------------------------------------------------


async def test_draft_with_errand(client, ui, db, world):
    w = world
    agent, _ = _agent(db)
    sid = await _auto(client, ui, w, mode="manual")
    await _channel_on(ui, w, sid)
    first = (await _send(client, w, w["ch"], _me(sid, None), text="раз"))["outbox_id"]
    second = (await _send(client, w, w["ch"], _me(sid, None), text="два"))["outbox_id"]
    third = (await _send(client, w, w["ch"], _me(sid, None), text="три"))["outbox_id"]
    fourth = (await _send(client, w, w["ch"], _me(sid, None), text="четыре"))["outbox_id"]
    fifth = (await _send(client, w, w["ch"], _me(sid, None), text="пять"))["outbox_id"]
    sixth = (await _send(client, w, w["ch"], _me(sid, None), text="шесть"))["outbox_id"]
    e_first = await _errand(ui, f"draft:{first}")
    e_second = await _errand(ui, f"draft:{second}")
    e_third = await _errand(ui, f"draft:{third}")
    e_fourth = await _errand(ui, f"draft:{fourth}")

    # Каждый путь владельца: черновик решён, поручение закрыто `ui`.
    _ok(await ui.post(f"{API}/outbox/{first}/approve", {}))
    _taken(db, e_first["id"])
    _ok(await ui.post(f"{API}/outbox/{third}/cancel"))
    _taken(db, e_third["id"])
    _ok(await ui.post(f"{API}/owner_channel/draft:{fourth}/answer", {"action": "approve"}))
    _taken(db, e_fourth["id"])
    # Пачкой: черновик с поручением подтверждается, поручение закрыто; ошибка соседа его не откатывает.
    e_fifth = await _errand(ui, f"draft:{fifth}")
    r = await ui.post(f"{API}/outbox/approve-batch", {"ids": [fifth, sixth, first]})
    assert r.status_code == 200, r.text
    results = {i["id"]: i for i in r.json()["results"]}
    assert results[fifth]["status"] == "queued" and results[sixth]["status"] == "queued"
    assert results[first]["status"] == "error"  # уже подтверждён
    _taken(db, e_fifth["id"])
    # Сессия снимает свой черновик как раньше (это не ответ владельца): поручение закрывается `system`.
    r = await client.post(f"{API}/outbox/{second}/cancel", json={}, headers=_h(w["tok"]))
    assert r.status_code == 200, r.text
    await _queue(client, agent)  # чтение закрывает поручения карточек, закрытых другим путём
    assert dict(_errand_rows(db))[e_second["id"]] == "system"


# --- заявки: правило, выход, вступление, нажатие, заявка агента ---------------------------------------------


async def test_request_kinds_with_errand(client, ui, db, clock, world):
    w = world
    agent, _ = _agent(db)
    sid = await _auto(client, ui, w, mode="manual")
    await _channel_on(ui, w, sid)
    me = _me(sid, None)
    r = await client.post(
        f"{API}/rule-requests",
        json={"project_id": w["p3"], "account_id": w["acc"], "kind": "all", **me},
        headers=_h(w["tok"]),
    )
    rule_id = r.json()["id"]
    r = await client.post(f"{API}/leaves", json={"channel_id": w["ch2"], **me}, headers=_h(w["tok"]))
    leave_id = r.json()["id"]
    r = await client.post(
        f"{API}/joins", json={"project_id": w["p3"], "target": "@some_channel", **me}, headers=_h(w["tok"])
    )
    join_id = r.json()["id"]
    _pending(db, "join_request", join_id)
    body = {"kind": "new_session", "project_id": w["p3"], "role": "Тестер", "task": "прогнать тесты", **me}
    r = await client.post(f"{API}/agent-requests", json=body, headers=_h(w["tok"]))
    agent_req_id = r.json()["id"]
    with db.reader() as conn:
        sub_id = conn.execute(
            "SELECT id FROM main.session_sub WHERE session_pid = ? AND ended_ts IS NULL",
            (held.held_pid(sid),),
        ).fetchone()[0]
    now = _now(clock)
    press_id = db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.button_press (account_id, channel_id, project_id, message_id, ext_id, row, "
                "col, text, by, status, created_ts, updated_ts, author_sub_id, seq) "
                "VALUES (?, ?, ?, ?, '100', 0, 0, 'OK', 'session', 'pending', ?, ?, ?, 1)",
                (w["acc"], w["ch"], w["p3"], w["last"], now, now, sub_id),
            ).lastrowid
        )
    )
    routes = {
        f"rule:{rule_id}": [f"/rule-requests/{rule_id}/approve", f"/rule-requests/{rule_id}/decline"],
        f"leave:{leave_id}": [
            f"/leaves/{leave_id}/approve",
            f"/leaves/{leave_id}/decline",
            f"/channels/{w['ch2']}/leave",
        ],
        f"join:{join_id}": [f"/joins/{join_id}/approve", f"/joins/{join_id}/decline"],
        f"agent_request:{agent_req_id}": [
            f"/agent-requests/{agent_req_id}/approve",
            f"/agent-requests/{agent_req_id}/decline",
        ],
        f"press:{press_id}": [f"/button-presses/{press_id}/approve", f"/button-presses/{press_id}/decline"],
    }
    assert set(routes) <= set(_refs(await _queue(client, ui=ui)))  # все ждут
    assert agent
    for ref, paths in routes.items():
        made = await _errand(ui, ref)
        _ok(await ui.post(f"{API}{paths[1]}", {}))  # отказ прежней ручкой владельца
        _taken(db, made["id"])
    assert not set(routes) & set(_refs(await _queue(client, ui=ui)))


# --- сессия: прерванная реплика, пауза; напоминание ---------------------------------------------------------


async def test_session_waits_and_reminder_with_errand(client, ui, db, clock, world):
    w = world
    agent, _ = _agent(db)
    sid = await _auto(client, ui, w, mode="manual")
    await _channel_on(ui, w, sid)
    r = await ui.post(f"{API}/sessions/{sid}/say", {"text": "сделай отчёт"})
    say_id = r.json()["id"]
    db.write_sync(lambda conn: held_work.mark_interrupted(conn, held.get(conn, sid), say_id, _now(clock)))
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.hub_session SET wait_reason = 'paused' WHERE id = ?", (sid,))
    )
    say_errand = await _errand(ui, f"say:{say_id}")
    paused_errand = await _errand(ui, f"paused:{sid}")
    _ok(await ui.post(f"{API}/sessions/{sid}/says/{say_id}/resend"))
    _taken(db, say_errand["id"])
    _ok(await ui.post(f"{API}/sessions/{sid}/wake"))
    _taken(db, paused_errand["id"])
    assert agent

    with db.reader() as conn:
        sub_id = conn.execute(
            "SELECT id FROM main.session_sub WHERE session_pid = ? AND ended_ts IS NULL",
            (held.held_pid(sid),),
        ).fetchone()[0]
    r = await client.post(
        f"{API}/reminders",
        json={"channel_id": w["ch"], "at": _iso(clock, 60), "note": "проверить", **_me(sid, None)},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 201, r.text
    rem_id = r.json()["id"] if "id" in r.json() else r.json()["reminder"]["id"]
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.reminder SET status = 'fired', fired_ts = ?, signal_sub_id = ? WHERE id = ?",
            (_now(clock), sub_id, rem_id),
        )
    )
    rem_errand = await _errand(ui, f"reminder:{rem_id}")
    r = await ui.c.patch(f"{API}/reminders/{rem_id}", json={"close": True}, headers=ui.headers)
    assert r.status_code == 200, r.text
    _taken(db, rem_errand["id"])
