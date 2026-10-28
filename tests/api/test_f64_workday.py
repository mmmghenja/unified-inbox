"""Ф64-1 (docs/architecture.md, 5 и поправки П6–П13; тесты раздела 7): рабочий
день проекта в ядре — настройка и ручка (права, 409, идемпотентность), выдача проектов и записи «для хоста»,
заявки «нужен человек» для бота и
агента, очередь агента владельца, карточка простоя, своя отписка сессии хаба, пометки агентам.

Помощники и «мир» — из test_hub_sessions.py; хост поддельный (bearer `work_host`)."""

import json
import sys

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from hub import cli as hub_cli
from hub.core import agent_state, held, workday
from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret

from . import test_hub_sessions as base
from .card_helpers import _route
from .test_f41_autopilot_5b import _auto
from .test_f54_human_bot import _bot_token, _changes
from .test_f54_human_requests import _agent, _create, _human_token
from .test_f56_queue import _agent as _owner_agent
from .test_f56_queue import _channel_on, _permission, _queue, _refs
from .test_hub_sessions import ROLE, A, _audits, _h, _host, _hs, _hsub, _mk, _now, _sub
from .test_owner_state import MIN

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"
ADMIN_SECRET = ADMIN_PREFIX + "f64f64f64f64f64f64f64f64f64f64f64f64f64f64f"  # выдуманная строка теста
ADMIN = {"Authorization": f"Bearer {ADMIN_SECRET}"}


async def _patch(ui, pid, body, expect=200) -> dict:
    r = await ui.c.patch(f"{API}/projects/{pid}", json=body, headers=ui.headers)
    assert r.status_code == expect, r.text
    return r.json()


async def _day(ui, pid, open_, expect=200) -> dict:
    r = await ui.post(f"{API}/projects/{pid}/day", {"open": open_})
    assert r.status_code == expect, r.text
    return r.json()


async def _project(ui, pid) -> dict:
    rows = (await ui.get(f"{API}/projects")).json()
    return next(p for p in rows if p["id"] == pid)


async def _closed(ui, w, project="p3") -> int:
    await _patch(ui, w[project], {"workday": True})
    return (await _day(ui, w[project], False))["ts"]


def _policy(db, pid) -> dict:
    with db.reader() as conn:
        raw = conn.execute("SELECT agent_policy_json FROM main.project WHERE id = ?", (pid,)).fetchone()[0]
    return json.loads(raw) if raw else {}


@pytest.fixture
async def admin_client(db, clock, app):
    internal = create_internal_app(
        db,
        clock=clock,
        notifier=app.state.notifier,
        admin_key_hash=hash_secret(ADMIN_SECRET),
        sub_waiters=app.state.sub_waiters,
        chan_sub_waiters=app.state.chan_sub_waiters,
    )
    async with AsyncClient(transport=ASGITransport(app=internal), base_url="http://hub-internal") as c:
        yield c


# --- настройка и ручка (раздел 1) ---------------------------------------------------------------------------


async def test_day_route_rights_conflict_and_idempotence(client, ui, db, clock, world):  # noqa: F811
    w = world
    pid = w["p3"]
    # Bearer любого скоупа — 403: сессия, хост, агент владельца, бот.
    agent, _ = _human_token(db)
    bot, _ = _bot_token(db)
    for token in (w["tok"], w["host"], agent, bot):
        r = await client.post(f"{API}/projects/{pid}/day", json={"open": False}, headers=_h(token))
        assert r.status_code == 403, r.text
    await _day(ui, 999_999, False, expect=404)
    await _day(ui, -1, False, expect=404)
    # Проект не «по времени» — 409 `workday_off`.
    assert (await _day(ui, pid, False, expect=409))["detail"] == workday.WORKDAY_OFF
    r = await ui.post(f"{API}/projects/{pid}/day", {"open": "no"})
    assert r.status_code == 422
    assert (await _project(ui, pid))["workday"] is False

    await _patch(ui, pid, {"workday": True})
    row = await _project(ui, pid)
    assert (row["workday"], row["day_open"], row["day_ts"]) == (True, True, None)
    # «Начать день», когда день и так открыт, ничего не пишет.
    assert await _day(ui, pid, True) == {"open": True, "ts": None}
    assert _audits(db, "project_day") == []
    closed = await _day(ui, pid, False)
    assert closed == {"open": False, "ts": _now(clock)}
    clock.advance_ms(5 * MIN)
    assert await _day(ui, pid, False) == closed  # повтор — прежний `ts`, без новой записи
    row = await _project(ui, pid)
    assert (row["workday"], row["day_open"], row["day_ts"]) == (True, False, closed["ts"])
    assert _audits(db, "project_day") == [{"project_id": pid, "open": False, "via": "ui"}]
    opened = await _day(ui, pid, True)
    assert opened == {"open": True, "ts": _now(clock)}
    assert [a["open"] for a in _audits(db, "project_day")] == [False, True]
    # Другие проекты не тронуты.
    other = await _project(ui, w["p4"])
    assert (other["workday"], other["day_open"], other["day_ts"]) == (False, True, None)


async def test_workday_patch_needs_bool_and_off_opens_the_day(client, ui, db, clock, world):  # noqa: F811
    w = world
    pid = w["p3"]
    r = await client.patch(f"{API}/projects/{pid}", json={"workday": True}, headers=_h(w["tok"]))
    assert r.status_code == 403
    await _patch(ui, pid, {"workday": None}, expect=422)
    await _patch(ui, pid, {"workday": "on"}, expect=422)
    await _patch(ui, pid, {"workday": True})
    audit = _audits(db, "project_update")[-1]
    assert audit["keys"] == ["workday"] and audit["workday"] is True
    await _day(ui, pid, False)
    assert _policy(db, pid)["day"]["open"] is False
    await _patch(ui, pid, {"workday": False})
    policy = _policy(db, pid)
    assert "workday" not in policy and "day" not in policy
    row = await _project(ui, pid)
    assert (row["workday"], row["day_open"], row["day_ts"]) == (False, True, None)
    # Прочие ключи политики живут рядом и не стираются.
    await _patch(ui, pid, {"session_handled": True, "workday": True})
    assert _policy(db, pid)["workday"] is True and (await _project(ui, pid))["session_handled"] is True


async def test_admin_socket_sets_workday_and_day(client, ui, db, clock, world, admin_client):  # noqa: F811
    w = world
    pid = w["p3"]
    r = await admin_client.post(f"/internal/v1/admin/projects/{pid}/day", json={"open": False})
    assert r.status_code == 401
    r = await admin_client.post(f"/internal/v1/admin/projects/{pid}/day", json={"open": False}, headers=ADMIN)
    assert r.status_code == 409 and r.json()["detail"] == workday.WORKDAY_OFF
    r = await admin_client.patch(f"/internal/v1/admin/projects/{pid}", json={"workday": True}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["workday"] is True, r.text
    assert _audits(db, "project_workday")[-1] == {"project_id": pid, "workday": True}
    r = await admin_client.post(f"/internal/v1/admin/projects/{pid}/day", json={"open": False}, headers=ADMIN)
    assert r.status_code == 200 and r.json() == {"open": False, "ts": _now(clock)}
    assert _audits(db, "project_day")[-1] == {"project_id": pid, "open": False, "via": "cli"}
    rows = (await admin_client.get("/internal/v1/admin/projects", headers=ADMIN)).json()
    mine = next(p for p in rows if p["id"] == pid)
    assert (mine["workday"], mine["day_open"]) == (True, False)
    r = await admin_client.post("/internal/v1/admin/projects/999999/day", json={"open": True}, headers=ADMIN)
    assert r.status_code == 404


# --- запись «для хоста» и `seq` (раздел 2, П12) -------------------------------------------------------------


async def test_host_record_carries_day_closed_ts_and_seq_moves(client, ui, db, clock, world):  # noqa: F811
    w = world
    s1 = (await _mk(ui, w, role=ROLE))["id"]
    s2 = (await _mk(ui, w, role="программист"))["id"]
    other = (await _mk(ui, w, project="p4", role=ROLE))["id"]

    async def host_view() -> dict[int, dict]:
        return {s["id"]: s for s in (await _host(client, w, "sessions"))["items"]}

    before = await host_view()
    assert {before[s]["day_closed_ts"] for s in (s1, s2, other)} == {None}
    await _patch(ui, w["p3"], {"workday": True})
    closed = (await _day(ui, w["p3"], False))["ts"]
    after = await host_view()
    assert after[s1]["day_closed_ts"] == closed == after[s2]["day_closed_ts"]
    assert after[other]["day_closed_ts"] is None
    assert after[s1]["seq"] > before[s1]["seq"] and after[s2]["seq"] > before[s2]["seq"]
    assert after[s1]["seq"] == after[s2]["seq"]  # П12: один UPDATE на проект
    assert after[other]["seq"] == before[other]["seq"]
    await _day(ui, w["p3"], True)
    opened = await host_view()
    assert opened[s1]["day_closed_ts"] is None and opened[s1]["seq"] > after[s1]["seq"]


# --- делегат карточек (П6, П8) ------------------------------------------------------------------------------


async def test_open_day_sends_no_note(client, ui, db, clock, world):  # noqa: F811
    """Ф66-1: делегата нет — записки «ОТ ВЛАДЕЛЬЦА» при открытии дня некому."""
    w = world
    await _closed(ui, w)
    await _day(ui, w["p3"], True)
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.peer_msg").fetchone()[0] == 0


# --- заявки «нужен человек»: бот и агент (П9) ---------------------------------------------------------------


async def test_bot_and_agent_do_not_see_pending_requests_of_closed_day(client, ui, db, clock, world):  # noqa: F811
    w = world
    await _sub(client, w, A, role=ROLE)
    bot, _ = _bot_token(db)
    agent, _ = _human_token(db)
    await _route(ui, w, {"human": "agent"})  # Ф66-1: заявки «нужен человек» — агенту
    made = (await _create(client, w))["request"]
    assert [i["id"] for i in (await _changes(client, bot)).json()["items"]] == [made["id"]]
    assert [i["id"] for i in (await _agent(client, agent, "GET", "/human-requests")).json()] == [made["id"]]
    await _closed(ui, w)
    mark = (await _changes(client, bot)).json()["seq"]
    later = (await _create(client, w, kind="login"))["request"]
    assert (await _changes(client, bot)).json()["items"] == []
    assert (await _changes(client, bot, since=mark)).json()["items"] == []
    assert (await _agent(client, agent, "GET", "/human-requests")).json() == []
    assert (await _agent(client, agent, "GET", f"/human-requests/{made['id']}")).status_code == 404
    # Кука интерфейса видит всё.
    mine = (await ui.get(f"{API}/human-requests")).json()
    assert {i["id"] for i in mine} >= {made["id"], later["id"]}
    # «Начать день»: бот получает обе по отметке, агент снова видит.
    await _day(ui, w["p3"], True)
    got = (await _changes(client, bot, since=mark)).json()["items"]
    assert sorted(i["id"] for i in got) == sorted([made["id"], later["id"]])
    seen = (await _agent(client, agent, "GET", "/human-requests")).json()
    assert sorted(i["id"] for i in seen) == sorted([made["id"], later["id"]])


# --- очередь агента владельца (раздел 3) -------------------------------------------------------------------------


async def test_owner_queue_hides_closed_project_from_agent(client, ui, db, clock, world):  # noqa: F811
    w = world
    agent, _ = _owner_agent(db)
    sid = await _auto(client, ui, w, mode="manual")
    await _channel_on(ui, w, sid)
    ref = f"req:{await _permission(client, w, sid)}"
    assert ref in _refs(await _queue(client, agent))
    await _closed(ui, w)
    assert ref not in _refs(await _queue(client, agent))
    assert _refs(await _queue(client, ui=ui))[ref]["in_channel"] is True  # кука видит всё
    await _day(ui, w["p3"], True)
    assert ref in _refs(await _queue(client, agent))


# --- карточка простоя (П7) ----------------------------------------------------------------------------------


def _want(db, sid):
    with db.reader() as conn:
        return conn.execute("SELECT want_sub_json FROM main.hub_session WHERE id = ?", (sid,)).fetchone()[0]


async def test_own_unsubscribe_clears_want_but_ui_unsubscribe_does_not(client, ui, db, clock, world):  # noqa: F811
    w = world
    want = {"mode": "direct"}
    own = (await _mk(ui, w, role=ROLE, want_sub=want))["id"]
    by_ui = (await _mk(ui, w, role="программист", want_sub=want))["id"]
    own_sub = (await _hsub(client, w, own, cwd=w["dir3"]))["sub"]["id"]
    ui_sub = (await _hsub(client, w, by_ui, cwd=w["dir3"]))["sub"]["id"]
    assert _want(db, own) is not None and _want(db, by_ui) is not None
    r = await client.post(
        f"{API}/subs/{own_sub}/end",
        json={"session": _hs(own), "reason": "unsubscribed"},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text
    assert _want(db, own) is None
    audit = _audits(db, "hub_session_want_sub")[-1]
    assert audit["hub_session_id"] == own and audit["want_sub"] is None and audit["before"] is not None
    r = await ui.post(f"{API}/subs/{ui_sub}/end", {})
    assert r.status_code == 200, r.text
    assert _want(db, by_ui) is not None  # отписка владельцем из интерфейса желаемое не трогает
    # Внешняя сессия: желаемого нет — ничего не ломается.
    ext = (await _sub(client, w, A, role="внешний"))["sub"]["id"]
    r = await client.post(
        f"{API}/subs/{ext}/end",
        json={"session": base._sess(A), "reason": "unsubscribed"},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text


# --- пометки агентам (раздел 3) -----------------------------------------------------------------------------


async def test_agent_state_and_peer_send_mark_closed_day(client, ui, db, clock, world):  # noqa: F811
    w = world
    sid = await _auto(client, ui, w, role=ROLE, mode="manual")
    await _sub(client, w, A, role="оркестратор")
    await _closed(ui, w)
    with db.reader() as conn:
        state = agent_state.of_hub(conn, held.get(conn, sid), now_ms=_now(clock))
    assert state["day_closed_ts"] is not None
    assert any(line.startswith(agent_state.DAY_CLOSED) for line in state["lines"])
    body = {"text": "привет", "to": {"project_id": w["p3"], "role": ROLE}, "session": base._sess(A)}
    r = await client.post(f"{API}/peer-msgs", json=body, headers=_h(w["tok"]))
    assert r.status_code == 201, r.text
    assert r.json()["recipient"]["day_closed"] is True
    await _day(ui, w["p3"], True)
    r = await client.post(f"{API}/peer-msgs", json=body, headers=_h(w["tok"]))
    assert r.status_code == 201, r.text
    assert "day_closed" not in r.json()["recipient"]
    with db.reader() as conn:
        state = agent_state.of_hub(conn, held.get(conn, sid), now_ms=_now(clock))
    assert state["day_closed_ts"] is None
    assert not any(line.startswith(agent_state.DAY_CLOSED) for line in state["lines"])


# --- CLI ----------------------------------------------------------------------------------------------------


def test_cli_workday_and_day(monkeypatch, capsys):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/projects/9/day"):
            return httpx.Response(409, json={"detail": workday.WORKDAY_OFF})
        if request.url.path.endswith("/day"):
            body = json.loads(request.content)
            return httpx.Response(200, json={"open": body["open"], "ts": None if body["open"] else 1_000})
        if request.method == "GET":
            rows = [
                {
                    "id": 3,
                    "name": "П",
                    "muted": False,
                    "workdir": None,
                    "workday": True,
                    "day_open": False,
                    "day_ts": 1_000,
                },
                {
                    "id": 4,
                    "name": "Q",
                    "muted": False,
                    "workdir": None,
                    "workday": False,
                    "day_open": True,
                    "day_ts": None,
                },
            ]
            return httpx.Response(200, json=rows)
        return httpx.Response(200, json={"project_id": 3, **json.loads(request.content)})

    monkeypatch.setattr(
        hub_cli,
        "_admin_client",
        lambda *, timeout=None: httpx.Client(
            transport=httpx.MockTransport(handler), base_url="http://hub-internal/internal/v1"
        ),
    )
    for argv in (["project", "set", "3", "--workday", "on"], ["project", "day", "3", "close"]):
        monkeypatch.setattr(sys, "argv", ["hub", *argv])
        hub_cli.main()
    assert [(r.method, r.url.path, json.loads(r.content)) for r in seen] == [
        ("PATCH", "/internal/v1/admin/projects/3", {"workday": True}),
        ("POST", "/internal/v1/admin/projects/3/day", {"open": False}),
    ]
    assert capsys.readouterr().out.splitlines()[-1].startswith("день закрыт с ")
    monkeypatch.setattr(sys, "argv", ["hub", "project", "day", "9", "open"])
    with pytest.raises(SystemExit, match="--workday on"):
        hub_cli.main()
    monkeypatch.setattr(sys, "argv", ["hub", "project", "list"])
    hub_cli.main()
    listed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert listed[0]["workday"].startswith("день закрыт с ") and "workday" not in listed[1]
