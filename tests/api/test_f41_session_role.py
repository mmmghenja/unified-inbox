"""Смена роли существующей сессии хаба: `PATCH /sessions/{id}` с `role`, `number` и
`autosend_ok`. Помощники и «мир» — из test_hub_sessions.py."""

from hub.core import autosend, held

from . import test_hub_sessions as base
from .test_hub_sessions import (
    ROLE,
    A,
    B,
    _audits,
    _h,
    _hsub,
    _mk,
    _peer_row,
    _peer_to_sub,
    _sub,
    _sub_rec,
)

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"
TESTER = "тестер"


def _rec(db, sid) -> held.HubSession:
    with db.reader() as conn:
        return held.get(conn, sid)


async def _patch(ui, sid, body, *, expect=200) -> dict:  # noqa: F811
    r = await ui.c.patch(f"{API}/sessions/{sid}", json=body, headers=ui.headers)
    assert r.status_code == expect, r.text
    return r.json()


async def _events(ui, sid) -> list[dict]:  # noqa: F811
    return (await ui.get(f"{API}/sessions/{sid}/events")).json()


def _allow(db, channel_id, role) -> None:
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.channel_autosend (channel_id, role_key, role, created_ts) VALUES (?, ?, ?, 0)",
            (channel_id, role, role),
        )
    )


async def test_role_change_moves_live_sub_in_place(client, ui, db, world):  # noqa: F811
    w = world
    sid = (await _mk(ui, w, role=ROLE))["id"]
    sub_id = (await _hsub(client, w, sid))["sub"]["id"]
    msg_id = await _peer_to_sub(ui, w, sub_id)
    seq0 = _rec(db, sid).seq

    row = (await _patch(ui, sid, {"role": f" {TESTER} "}))["session"]
    assert (row["role"], row["role_no"], row["sub_id"]) == (TESTER, 1, sub_id)
    rec = _rec(db, sid)
    assert (rec.role, rec.role_key, rec.role_no) == (TESTER, TESTER, 1) and rec.seq > seq0
    # Подписка та же — линия новая; привязанное к подписке (диалог владельца) осталось у сессии.
    sub = _sub_rec(db, sub_id)
    assert (sub.role, sub.role_key, sub.role_no, sub.ended_ts) == (TESTER, TESTER, 1, None)
    assert _peer_row(db, msg_id)[:2] == ("pending", sub_id)
    assert _audits(db, "hub_session_role") == [
        {
            "hub_session_id": sid,
            "old": {"role": True, "role_no": 1},
            "new": {"role": True, "role_no": 1},
            "role_changed": True,
            "subs": [sub_id],
            "autosend_reset": True,
        }
    ]
    (event,) = [e for e in await _events(ui, sid) if (e.get("brief") or {}).get("type") == "role"]
    assert event["kind"] == "status" and event["brief"] == {"type": "role", "role": "тестер 1"}
    assert event["summary"] == "роль: тестер 1 — со следующего входа"

    # Та же роль (и тот же номер) — ничего не пишется.
    seq1 = _rec(db, sid).seq
    await _patch(ui, sid, {"role": TESTER})
    await _patch(ui, sid, {"role": TESTER, "number": 1})
    assert _rec(db, sid).seq == seq1 and len(_audits(db, "hub_session_role")) == 1
    # Старую линию сессия больше не держит: её берёт внешняя сессия.
    taken = await _sub(client, w, B, role=ROLE, number=1)
    assert taken["sub"]["id"] != sub_id
    # Сама сессия хаба подписывается уже по новой роли — на месте.
    assert (await _hsub(client, w, sid))["sub"]["id"] == sub_id


async def test_role_change_number_and_same_role_number(client, ui, db, world):  # noqa: F811
    w = world
    sid = (await _mk(ui, w, role=ROLE))["id"]
    sub_id = (await _hsub(client, w, sid))["sub"]["id"]
    row = (await _patch(ui, sid, {"role": ROLE, "number": 3}))["session"]
    assert (row["role"], row["role_no"]) == (ROLE, 3)
    assert _sub_rec(db, sub_id).role_no == 3
    event = [e for e in await _events(ui, sid) if (e.get("brief") or {}).get("type") == "role"][-1]
    assert event["brief"]["role"] == "маркетолог 3"
    # Ключ роли тот же — признак «без подтверждения» не сброшен.
    assert row["autosend_ok"] is True
    assert _audits(db, "hub_session_role")[-1]["autosend_reset"] is False


async def test_role_line_taken_by_hub_session(ui, db, world):  # noqa: F811
    w = world
    first = (await _mk(ui, w, role=TESTER, title="Первая"))["id"]
    second = (await _mk(ui, w, role=ROLE))["id"]
    detail = (await _patch(ui, second, {"role": TESTER, "number": 1}, expect=409))["detail"]
    assert "линию держит сессия хаба «Первая»" in detail
    assert _rec(db, second).role == ROLE and not _audits(db, "hub_session_role")
    # Номер не задан — наименьший свободный.
    row = (await _patch(ui, second, {"role": TESTER}))["session"]
    assert (row["role"], row["role_no"]) == (TESTER, 2)
    assert _rec(db, first).role_no == 1


async def test_role_line_taken_by_alien_live_sub(client, ui, db, world):  # noqa: F811
    w = world
    alien = (await _sub(client, w, A, role=TESTER))["sub"]["id"]
    sid = (await _mk(ui, w, role=ROLE))["id"]
    own = (await _hsub(client, w, sid))["sub"]["id"]
    detail = (await _patch(ui, sid, {"role": TESTER, "number": 1}, expect=409))["detail"]
    assert f"уже слушает сессия #{alien}" in detail
    assert _sub_rec(db, own).role == ROLE and _rec(db, sid).role == ROLE
    row = (await _patch(ui, sid, {"role": TESTER}))["session"]
    assert (row["role"], row["role_no"]) == (TESTER, 2)
    assert (_sub_rec(db, own).role_key, _sub_rec(db, own).role_no) == (TESTER, 2)
    assert _sub_rec(db, alien).ended_ts is None


async def test_own_live_sub_does_not_hold_the_line(client, ui, db, world):  # noqa: F811
    """Своя живая подписка на линии «маркетолог №1» не мешает вернуться на неё же."""
    w = world
    sid = (await _mk(ui, w, role=ROLE))["id"]
    own = (await _hsub(client, w, sid))["sub"]["id"]
    await _patch(ui, sid, {"role": ROLE, "number": 2})
    row = (await _patch(ui, sid, {"role": ROLE, "number": 1}))["session"]
    assert row["role_no"] == 1 and _sub_rec(db, own).role_no == 1


async def test_drop_role(client, ui, db, world):  # noqa: F811
    w = world
    for value in (None, "  "):
        sid = (await _mk(ui, w, role=ROLE))["id"]
        sub_id = (await _hsub(client, w, sid))["sub"]["id"]
        row = (await _patch(ui, sid, {"role": value}))["session"]
        assert (row["role"], row["role_no"]) == (None, None)
        sub = _sub_rec(db, sub_id)
        assert (sub.role, sub.role_key, sub.ended_ts) == (None, None, None)
        event = [e for e in await _events(ui, sid) if (e.get("brief") or {}).get("type") == "role"][-1]
        assert event["brief"] == {"type": "role", "role": None}
        assert event["summary"] == "роль снята — со следующего входа"
        # Без роли — ещё раз «снять» ничего не пишет.
        seq = _rec(db, sid).seq
        await _patch(ui, sid, {"role": None})
        assert _rec(db, sid).seq == seq
    # Роль ставится и сессии без роли.
    row = (await _patch(ui, sid, {"role": TESTER}))["session"]
    assert (row["role"], row["role_no"]) == (TESTER, 1)
    assert _sub_rec(db, sub_id).role_key == TESTER


async def test_role_refusals(client, ui, db, world):  # noqa: F811
    w = world
    sid = (await _mk(ui, w, role=ROLE))["id"]
    for body in ({"number": 2}, {"role": None, "number": 2}, {"role": " ", "number": 2}):
        assert "number" in (await _patch(ui, sid, body, expect=422))["detail"]
    assert "занято" in (await _patch(ui, sid, {"role": "владелец"}, expect=422))["detail"]
    assert "role" in (await _patch(ui, sid, {"role": "а--б"}, expect=422))["detail"]
    await _patch(ui, sid, {"role": TESTER, "number": 0}, expect=422)
    await _patch(ui, sid, {"autosend_ok": "yes"}, expect=422)
    assert "role" in (await _patch(ui, sid, {}, expect=422))["detail"]
    await _patch(ui, 999, {"role": TESTER}, expect=404)
    # Bearer-токены — отказ: роль меняет только владелец из интерфейса.
    for token in (w["tok"], w["host"]):
        r = await client.patch(f"{API}/sessions/{sid}", json={"role": TESTER}, headers=_h(token))
        assert r.status_code == 403, r.text
        r = await client.patch(f"{API}/sessions/{sid}", json={"autosend_ok": True}, headers=_h(token))
        assert r.status_code == 403, r.text
    # Закрытая — 409.
    r = await ui.post(f"{API}/sessions/{sid}/close")
    assert r.status_code == 200, r.text
    assert "закрыта" in (await _patch(ui, sid, {"role": TESTER}, expect=409))["detail"]
    assert "закрыта" in (await _patch(ui, sid, {"autosend_ok": False}, expect=409))["detail"]
    rec = _rec(db, sid)
    assert rec.role == ROLE and not _audits(db, "hub_session_role")


async def test_autosend_flag_is_not_carried_to_new_role(ui, db, world):  # noqa: F811
    w = world
    _allow(db, w["ch"], TESTER)
    sid = (await _mk(ui, w, role=ROLE))["id"]
    assert (await ui.get(f"{API}/sessions/{sid}")).json()["session"]["autosend_ok"] is True
    # Смена роли: у новой роли есть беседа «без подтверждения», но признак сброшен — молча не переносится.
    row = (await _patch(ui, sid, {"role": TESTER}))["session"]
    assert row["autosend_ok"] is False and row["autosend_channels"] == 1
    assert _rec(db, sid).autosend_ok == 0

    class _S:
        pid = held.held_pid(sid)

    with db.reader() as conn:
        assert autosend._list_closed(conn, _S) is True
    # Явное `autosend_ok: true` в том же запросе — признак стоит.
    row = (await _patch(ui, sid, {"role": ROLE, "autosend_ok": True}))["session"]
    assert row["autosend_ok"] is True
    assert _audits(db, "hub_session_autosend") == [{"hub_session_id": sid, "old": False, "new": True}]
    row = (await _patch(ui, sid, {"role": TESTER, "autosend_ok": True}))["session"]
    assert row["autosend_ok"] is True and _audits(db, "hub_session_role")[-1]["autosend_reset"] is False
    # Явное `false` вместе со сменой и снятие роли — «нет».
    row = (await _patch(ui, sid, {"role": ROLE, "autosend_ok": False}))["session"]
    assert row["autosend_ok"] is False
    # Отдельно от роли: ставит и снимает владелец; то же значение — ничего не пишется.
    seq = _rec(db, sid).seq
    await _patch(ui, sid, {"autosend_ok": False})
    assert _rec(db, sid).seq == seq
    row = (await _patch(ui, sid, {"autosend_ok": True}))["session"]
    assert row["autosend_ok"] is True and _rec(db, sid).seq > seq
    row = (await _patch(ui, sid, {"role": None}))["session"]
    assert row["autosend_ok"] is False and row["autosend_channels"] == 0


async def test_dead_holder_of_new_line_is_replaced(client, ui, db, procs, world):  # noqa: F811
    """Мёртвая подписка, ещё не снятая чисткой, новую линию не держит: завершается `replaced`."""
    w = world
    dead = (await _sub(client, w, A, role=TESTER))["sub"]["id"]
    sid = (await _mk(ui, w, role=ROLE))["id"]
    own = (await _hsub(client, w, sid))["sub"]["id"]
    procs.pop(A[0])  # процесс внешней сессии умер
    row = (await _patch(ui, sid, {"role": TESTER, "number": 1}))["session"]
    assert (row["role"], row["role_no"]) == (TESTER, 1)
    assert _sub_rec(db, dead).end_reason == "replaced"
    assert (_sub_rec(db, own).role_key, _sub_rec(db, own).role_no) == (TESTER, 1)
