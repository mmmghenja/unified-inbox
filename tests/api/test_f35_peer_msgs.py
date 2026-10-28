"""Ф35-2a (docs/architecture.md 5–8, 12 (обмен «проект <-> вне проектов»), 13–14 —
часть ядра; П3–П5, П7, П9, П12, П14; Н4, Н6–Н8): сообщения между агентами и между владельцем и агентом — ядро.

Стиль — как test_f35_agents.py: публичное приложение через ASGITransport, `/proc` подменён словарём, часы —
`FakeClock`."""

import asyncio
import json
import sys

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from hub import cli as hub_cli
from hub.core import feed, peer_msgs, queries, runner, security, subs
from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.feed import HEARTBEAT_FRAME, Notifier

from .conftest import BASE_URL, csrf_headers, insert_project, login_via_code

ORIGIN = "http://127.0.0.1:8787"
ADMIN_SECRET = ADMIN_PREFIX + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHI"
A = (4242, 777_000, "sess-aaaaaaaa-1111")
B = (5151, 888_000, "sess-bbbbbbbb-2222")
C = (6161, 999_000, "sess-cccccccc-3333")
D = (7171, 111_000, "sess-dddddddd-4444")
E = (8181, 222_000, "sess-eeeeeeee-5555")
MIN = 60_000
HOUR = 60 * MIN
DAY = 24 * HOUR
MARKETER = "маркетолог"
CODER = "программист"
TEXT = "передай: договор у клиента, СЕКРЕТНЫЙ-ТЕКСТ"


@pytest.fixture
def procs(monkeypatch):
    table = {who[0]: who[1] for who in (A, B, C, D, E)}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


def _h(token: str, proto: int = 4) -> dict:
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN, "X-Hub-Sub-Wait": str(proto)}


def _token(db, *, name="mcp:all", kind="interactive", scopes=("read", "draft"), project_ids=None):
    return db.write_sync(
        lambda conn: security.create_api_token(
            conn, name=name, kind=kind, scopes=list(scopes), project_ids=project_ids, expires_ts=None
        )
    ).token


def _now(clock) -> int:
    return int(clock() * 1000)


@pytest.fixture
def world(db, tmp_path, clock, procs):
    dirs = {}
    for name in ("w1", "w2", "free"):
        (tmp_path / name).mkdir()
        dirs[name] = str(tmp_path / name)
    p1 = insert_project(db, name="Umbrella", workdir=dirs["w1"])
    p2 = insert_project(db, name="Хаб", workdir=dirs["w2"])
    for pid in (p1, p2):
        db.write_sync(lambda conn, pid=pid: subs.set_project_subs(conn, pid, True, now_ms=0))
    return {
        "p1": p1,
        "p2": p2,
        **dirs,
        "tok": _token(db),
        "narrow": _token(db, name="mcp:iip", project_ids=[p1]),
        "triage": _token(db, name="agent:triage", kind="background", scopes=("read", "triage")),
    }


@pytest.fixture
async def ui(app, db, clock):
    """UI-сессия владельца — своим клиентом (bearer и cookie в одном запросе ядро не принимает)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as c:
        cookie, _csrf = await login_via_code(c, db, clock)
        headers = csrf_headers(cookie)

        class _Ui:
            async def get(self, path, **kw):
                return await c.get(path, **kw)

            async def post(self, path, body):
                return await c.post(path, json=body, headers=headers)

        yield _Ui()


@pytest.fixture
async def admin_client(db, clock, app):
    internal = create_internal_app(
        db,
        clock=clock,
        notifier=app.state.notifier,
        admin_key_hash=hash_secret(ADMIN_SECRET),
        sub_waiters=app.state.sub_waiters,
    )
    async with AsyncClient(transport=ASGITransport(app=internal), base_url="http://hub-internal") as c:
        yield c


def _session(who, *, cwd=None):
    pid, start, sid = who
    body = {"pid": pid, "proc_start": start, "session_id": sid}
    if cwd is not None:
        body.update({"cwd": cwd, "kind": "interactive", "name": None, "name_source": None})
    return body


async def _sub(client, w, who, cwd, *, expect=200, token=None, **extra):
    r = await client.post(
        "/api/v1/subs", json={"session": _session(who, cwd=w[cwd]), **extra}, headers=_h(token or w["tok"])
    )
    assert r.status_code == expect, r.text
    return r.json()


async def _sub_id(client, w, who, cwd, **extra) -> int:
    return (await _sub(client, w, who, cwd, **extra))["sub"]["id"]


async def _end(client, w, who, sub_id):
    r = await client.post(
        f"/api/v1/subs/{sub_id}/end",
        json={"session": _session(who), "reason": "unsubscribed"},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text


async def _wait(client, w, sub_id, who, *, proto=4):
    r = await client.post(
        f"/api/v1/subs/{sub_id}/wait",
        json={"session": _session(who), "waiter": "0123456789abcdef", "timeout_s": 1},
        headers=_h(w["tok"], proto),
    )
    assert r.status_code == 200, r.text
    return r.json()["signals"]


async def _send(client, w, who, *, expect=201, token=None, text="привет", to=None, reply_to=None, **extra):
    body: dict = {"text": text, **extra}
    if to is not None:
        body["to"] = to
    if reply_to is not None:
        body["reply_to"] = reply_to
    if who is not None:
        body["session"] = _session(who)
    r = await client.post("/api/v1/peer-msgs", json=body, headers=_h(token or w["tok"]))
    assert r.status_code == expect, r.text
    return r.json()


async def _ui_send(ui, *, expect=201, text="от владельца", **body):
    r = await ui.post("/api/v1/peer-msgs", {"text": text, **body})
    assert r.status_code == expect, r.text
    return r.json()


async def _inbox(client, w, who, *, token=None, expect=200, **params):
    params.update(_session(who))
    r = await client.get("/api/v1/peer-msgs", params=params, headers=_h(token or w["tok"]))
    assert r.status_code == expect, r.text
    return r.json().get("items")


async def _read(client, w, who, ids) -> int:
    r = await client.post(
        "/api/v1/peer-msgs/read", json={"ids": ids, "session": _session(who)}, headers=_h(w["tok"])
    )
    assert r.status_code == 200, r.text
    return r.json()["marked"]


async def _ui_list(ui, **params) -> list[dict]:
    r = await ui.get("/api/v1/peer-msgs", params=params)
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _rec(db, msg_id) -> peer_msgs.Rec:
    with db.reader() as conn:
        rec = peer_msgs.get(conn, msg_id)
    assert rec is not None
    return rec


def _statuses(db) -> list[str]:
    with db.reader() as conn:
        return [r[0] for r in conn.execute("SELECT status FROM main.peer_msg ORDER BY id").fetchall()]


def _expire(db, clock) -> int:
    return db.write_sync(lambda conn: peer_msgs.expire(conn, _now(clock)))


def _to(w, project="p2", role=CODER, **extra) -> dict:
    to = {"project_id": w[project] if isinstance(project, str) else project, **extra}
    if role is not None:
        to["role"] = role
    return to


async def _pair(client, w):
    """Маркетолог (A, проект 1) и программист (B, проект 2)."""
    a = await _sub_id(client, w, A, "w1", role=MARKETER)
    b = await _sub_id(client, w, B, "w2", role=CODER)
    return a, b


# --- B5–B6: отправка, сигнал, чтение ------------------------------------------------------------------------


async def test_b5_b6_send_signal_once_numbers_only_read(client, ui, db, clock, world):
    w = world
    a, b = await _pair(client, w)
    sent = await _send(client, w, A, text=TEXT, to=_to(w))
    msg = sent["msg"]
    assert sent["recipient"] == {"live": True, "waiting": False}
    assert (msg["from_user"], msg["to_user"], msg["status"], msg["hop"]) == (False, False, "pending", 0)
    assert msg["from"].pop("machine")["core"] is True  # Ф77-1 (C4): машина отправителя
    assert msg["from"] == {
        "project_id": w["p1"],
        "role": MARKETER,
        "role_no": 1,
        "sub_id": a,
        "engine": "claude",
    }
    assert msg["to"] == {"project_id": w["p2"], "role": CODER, "role_no": 1, "sub_id": None}
    assert msg["chain_id"] == msg["id"] and msg["text"] == TEXT

    # сигнал — только числа и ровно один раз
    assert await _wait(client, w, b, B) == [
        {"kind": "peer", "msg_id": msg["id"], "from_sub": a, "from_project": w["p1"]}
    ]
    clock.advance_ms(2 * MIN)
    assert await _wait(client, w, b, B) == []

    [got] = await _inbox(client, w, B)
    assert (got["id"], got["text"], got["signals"]) == (msg["id"], TEXT, 1)
    assert await _inbox(client, w, A) == []  # отправитель у себя во входящих его не видит
    assert await _read(client, w, B, [msg["id"]]) == 1
    assert await _read(client, w, B, [msg["id"]]) == 0  # один раз
    assert await _inbox(client, w, B) == []
    assert [(m["id"], m["status"]) for m in await _inbox(client, w, B, all=1)] == [(msg["id"], "read")]
    assert [(m["id"], m["status"]) for m in await _inbox(client, w, A, all=1)] == [(msg["id"], "read")]

    # аудит — без текста и ролей
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT action, detail_json FROM main.audit "
            "WHERE action LIKE 'peer_msg%' OR action = 'sub_signal'"
        ).fetchall()
    assert sorted(action for action, _ in rows) == ["peer_msg_read", "peer_msg_send", "sub_signal"]
    assert all("СЕКРЕТНЫЙ" not in detail and MARKETER not in detail for _action, detail in rows)
    assert {"kind": "peer", "msg_id": msg["id"], "sub_id": b} == json.loads(
        next(detail for action, detail in rows if action == "sub_signal")
    )


async def test_b5_role_without_number_two_live_unknown_self_unsubscribed(client, db, clock, world):
    w = world
    await _pair(client, w)
    second = await _sub(client, w, C, "w2", role=CODER)
    assert second["sub"]["role_no"] == 2

    r = await _send(client, w, A, to=_to(w), expect=409)
    assert "1, 2" in r["detail"]
    sent = await _send(client, w, A, to=_to(w, number=2))
    assert sent["msg"]["to"]["role_no"] == 2
    assert [m["id"] for m in await _inbox(client, w, C)] == [sent["msg"]["id"]]
    assert await _inbox(client, w, B) == []  # линия №1 письмо линии №2 не видит

    await _send(client, w, A, to=_to(w, role="бухгалтер"), expect=404)  # такой линии не было
    await _send(client, w, A, to=_to(w, number=7), expect=404)
    await _send(client, w, A, to=_to(w, "p1", MARKETER), expect=422)  # себе
    await _send(client, w, A, to=_to(w, 999_999), expect=404)
    await _send(client, w, D, to=_to(w, number=1), expect=409)  # не подписан
    await _send(client, w, A, to=_to(w, number=1), reply_to=1, expect=422)  # и to, и reply_to
    await _send(client, w, A, expect=422)  # ни того, ни другого
    assert _statuses(db) == ["pending"]


async def test_b5_offline_role_line_waits_first_signal_after_subscribe_then_expires(client, db, clock, world):
    w = world
    a, b = await _pair(client, w)
    await _end(client, w, B, b)
    sent = await _send(client, w, A, to=_to(w))
    assert sent["recipient"] == {"live": False, "waiting": False}
    assert sent["msg"]["expires_ts"] == _now(clock) + 7 * DAY

    # линию занимает другая сессия — письмо её: счётчик в ответе подписки и первый сигнал
    again = await _sub(client, w, C, "w2", role=CODER)
    assert again["peer_unread"] == 1
    assert await _wait(client, w, again["sub"]["id"], C) == [
        {"kind": "peer", "msg_id": sent["msg"]["id"], "from_sub": a, "from_project": w["p1"]}
    ]

    # 7 дней без чтения — `expired`; запись — только когда есть что
    clock.advance_ms(7 * DAY - MIN)
    assert _expire(db, clock) == 0
    clock.advance_ms(2 * MIN)
    with db.reader() as conn:
        assert peer_msgs.has_expired(conn, _now(clock))
    assert _expire(db, clock) == 1
    rec = _rec(db, sent["msg"]["id"])
    assert (rec.status, rec.closed_ts) == ("expired", _now(clock))
    assert await _inbox(client, w, C) == []


async def test_b5_line_not_alive_for_7_days_is_404(client, db, clock, world):
    w = world
    _a, b = await _pair(client, w)
    await _end(client, w, B, b)
    clock.advance_ms(7 * DAY - MIN)
    await _send(client, w, A, to=_to(w))  # ещё ждёт
    clock.advance_ms(2 * MIN)
    await _send(client, w, A, to=_to(w), expect=404)


async def test_b5_expire_loop_writes_only_when_there_is_work(client, db, clock, world):
    w = world
    _a, b = await _pair(client, w)
    sent = await _send(client, w, A, to=_to(w))
    commits: list[int] = []
    db.on_commit(lambda: commits.append(1))
    stop = asyncio.Event()
    ticks: list[float] = []

    async def _sleep(seconds: float) -> None:
        ticks.append(seconds)
        if len(ticks) == 2:
            clock.advance_ms(8 * DAY)
        if len(ticks) >= 4:
            stop.set()

    await runner._reminders_loop(db, clock, sleep=_sleep, stop_event=stop)
    assert _rec(db, sent["msg"]["id"]).status == "expired"
    assert len(commits) == 1  # первый тик только прочитал, второй записал, дальше — снова чтение


async def test_b5_roleless_only_live_and_lost_when_sub_ends(client, ui, db, clock, world):
    w = world
    await _sub_id(client, w, A, "w1", role=MARKETER)
    c = await _sub_id(client, w, C, "w2")
    sent = await _send(client, w, A, to=_to(w, role=None))
    assert sent["msg"]["to"] == {"project_id": w["p2"], "role": None, "role_no": 1, "sub_id": c}
    await _end(client, w, C, c)
    rec = _rec(db, sent["msg"]["id"])
    assert (rec.status, rec.reason) == ("lost", "sub_ended")
    r = await _send(client, w, A, to=_to(w, role=None), expect=409)
    assert "не в сети" in r["detail"]
    r = await _send(client, w, A, to=_to(w, role=None, number=1), expect=409)
    await _ui_send(ui, to=_to(w, role=None), expect=409)


async def test_b5_rights_wake_orchestrator_scope_and_project_switch(client, admin_client, db, clock, world):
    w = world
    a, _b = await _pair(client, w)
    for name in ("mcp:wake-1", "mcp:orchestrator"):
        token = _token(db, name=name)
        await _send(client, w, A, to=_to(w), token=token, expect=403)
    r = await client.get("/api/v1/peer-msgs", params=_session(A), headers=_h(_token(db, name="mcp:wake-2")))
    assert r.status_code == 403

    # проект адресата не виден токену — 404; проект 0 токену со списком проектов — тоже
    await _end(client, w, A, a)
    await _sub(client, w, A, "w1", role=MARKETER, token=w["narrow"])
    await _send(client, w, A, to=_to(w), token=w["narrow"], expect=404)
    await _send(client, w, A, to=_to(w, 0, "вольный"), token=w["narrow"], expect=404)

    # выключатель обмена — у любого из двух проектов
    headers = {"Authorization": f"Bearer {ADMIN_SECRET}"}
    for project in ("p1", "p2"):
        r = await admin_client.patch(
            f"/internal/v1/admin/projects/{w[project]}", json={"agent_msgs": False}, headers=headers
        )
        assert r.status_code == 200 and r.json()["agent_msgs"] is False, r.text
        r = await client.get("/api/v1/projects", headers=_h(w["tok"]))
        flags = {p["id"]: p["agent_msgs"] for p in r.json() if p["id"] > 0}
        assert flags == {w["p1"]: project != "p1", w["p2"]: project != "p2"}
        r = await _send(client, w, B, to=_to(w, "p1", MARKETER), expect=403)
        assert "выключен" in r["detail"]
        r = await admin_client.patch(
            f"/internal/v1/admin/projects/{w[project]}", json={"agent_msgs": True}, headers=headers
        )
        assert r.status_code == 200
    await _send(client, w, B, to=_to(w, "p1", MARKETER))
    assert _statuses(db) == ["pending"]


async def test_b6_protocol_3_gets_nothing_and_sleeps_protocol_4_gets(client, db, clock, world, monkeypatch):
    w = world
    _a, b = await _pair(client, w)
    sent = await _send(client, w, A, to=_to(w))
    calls: list[int] = []
    real = subs.deliver
    monkeypatch.setattr(subs, "deliver", lambda *a, **kw: calls.append(1) or real(*a, **kw))

    assert await _wait(client, w, b, B, proto=3) == []
    rec = _rec(db, sent["msg"]["id"])
    assert (rec.signals, rec.signaled_ts) == (0, None) and calls == []  # ни записи, ни цикла

    signals = await _wait(client, w, b, B, proto=4)
    assert [s["msg_id"] for s in signals] == [sent["msg"]["id"]] and calls == [1]


async def test_p12_recipient_waiting_only_with_protocol_4(client, app, db, clock, world):
    w = world
    _a, b = await _pair(client, w)
    waiters = app.state.sub_waiters
    mark, _ = waiters.enter(b, 3)
    assert (await _send(client, w, A, to=_to(w)))["recipient"] == {"live": True, "waiting": False}
    waiters.leave(b, mark)
    mark, _ = waiters.enter(b, 4)
    assert (await _send(client, w, A, to=_to(w)))["recipient"] == {"live": True, "waiting": True}
    waiters.leave(b, mark)
    assert waiters.protocol(b) == 0


async def test_p4_second_signal_after_10_minutes_and_never_a_third(client, db, clock, world):
    w = world
    a, b = await _pair(client, w)
    msg_id = (await _send(client, w, A, to=_to(w)))["msg"]["id"]
    signal = {"kind": "peer", "msg_id": msg_id, "from_sub": a, "from_project": w["p1"]}
    assert await _wait(client, w, b, B) == [signal]
    clock.advance_ms(9 * MIN)
    assert await _wait(client, w, b, B) == []
    clock.advance_ms(2 * MIN)
    assert await _wait(client, w, b, B) == [signal]
    clock.advance_ms(30 * MIN)
    assert await _wait(client, w, b, B) == []
    assert _rec(db, msg_id).signals == 2

    # сигнал выдан, текст не прочитан, подписка сменилась — счётчик в ответе подписки (решение 7)
    await _end(client, w, B, b)
    assert (await _sub(client, w, C, "w2", role=CODER))["peer_unread"] == 1
    # прочитанное повторно не сигналит
    other = (await _send(client, w, A, to=_to(w)))["msg"]["id"]
    assert await _read(client, w, C, [msg_id, other]) == 2
    clock.advance_ms(11 * MIN)
    with db.reader() as conn:
        assert subs.wait_state(conn, 3, now_ms=_now(clock), peers=True)[0] == 0


async def test_p5_counted_is_delivered_wait_does_not_spin(client, ui, db, clock, world, monkeypatch):
    """П5/П14: всё посчитанное выдаётся или остаётся только из-за общего предела; выключатель проекта при
    выдаче не проверяется."""
    w = world
    _a, b = await _pair(client, w)
    first = (await _send(client, w, A, to=_to(w)))["msg"]["id"]
    db.write_sync(lambda conn: peer_msgs.set_project_enabled(conn, w["p1"], False))
    db.write_sync(lambda conn: peer_msgs.set_project_enabled(conn, w["p2"], False))
    ids = [first] + [(await _ui_send(ui, to=_to(w)))["msg"]["id"] for _ in range(24)]

    calls: list[int] = []
    real = subs.deliver
    monkeypatch.setattr(subs, "deliver", lambda *a, **kw: calls.append(1) or real(*a, **kw))
    with db.reader() as conn:
        assert subs.wait_state(conn, b, now_ms=_now(clock), peers=True)[0] == 25
        assert subs.wait_state(conn, b, now_ms=_now(clock), peers=False)[0] == 0
    got = await _wait(client, w, b, B)
    assert [s["msg_id"] for s in got] == ids[:20] and calls == [1]
    assert got[0].get("from_user") is None and got[1] == {"kind": "peer", "msg_id": ids[1], "from_user": 1}
    clock.advance_ms(2 * MIN)
    assert [s["msg_id"] for s in await _wait(client, w, b, B)] == ids[20:]
    assert calls == [1, 1]


# --- B7 + Н6: кто читает ------------------------------------------------------------------------------------


async def test_b7_foreign_line_sees_nothing_bearer_without_session_403_sync_only_ui(
    client, ui, db, clock, world
):
    w = world
    await _sub_id(client, w, A, "w1", role=MARKETER)
    await _sub_id(client, w, C, "w2")
    d = await _sub_id(client, w, D, "w2")
    msg_id = (await _send(client, w, A, text=TEXT, to=_to(w, role=None, number=1)))["msg"]["id"]

    assert await _inbox(client, w, D, all=1) == []  # «без роли 2» письмо «без роли 1» не видит
    assert await _read(client, w, D, [msg_id]) == 0
    assert _rec(db, msg_id).status == "pending"
    assert await _wait(client, w, d, D) == []
    await _inbox(client, w, E, expect=409)  # сессия без подписки
    r = await client.get("/api/v1/peer-msgs", headers=_h(w["tok"]))
    assert r.status_code == 403
    r = await client.post("/api/v1/peer-msgs/read", json={"ids": [msg_id]}, headers=_h(w["tok"]))
    assert r.status_code == 422

    r = await client.get("/api/v1/sync", params={"since": 0}, headers=_h(w["tok"]))
    assert r.status_code == 200 and "peer_msgs" not in r.json() and TEXT not in r.text
    body = (await ui.get("/api/v1/sync", params={"since": 0})).json()
    assert [(m["id"], m["text"], m["status"]) for m in body["peer_msgs"]] == [(msg_id, TEXT, "pending")]
    assert body["max_seq"] == body["peer_msgs"][0]["seq"]
    assert [m["id"] for m in await _ui_list(ui)] == [msg_id]


async def test_n6_new_roleless_does_not_see_dialog_of_predecessor(client, ui, db, clock, world):
    w = world
    c = await _sub_id(client, w, C, "w2")
    to_c = {**_to(w, role=None, number=1), "sub_id": c}
    asked = (await _ui_send(ui, text="вопрос предшественнику", to=to_c))["msg"]["id"]
    await _read(client, w, C, [asked])
    await _send(client, w, C, text="ответ предшественника", reply_to=asked)
    await _end(client, w, C, c)

    fresh = await _sub(client, w, D, "w2")
    assert (fresh["sub"]["role_no"], fresh["peer_unread"]) == (1, 0)
    assert await _inbox(client, w, D, all=1) == []
    # линия с ролью в настоящем проекте историю линии читает
    await _sub_id(client, w, A, "w1", role=MARKETER)
    b = await _sub_id(client, w, B, "w2", role=CODER)
    line_msg = (await _send(client, w, A, to=_to(w)))["msg"]["id"]
    await _read(client, w, B, [line_msg])
    await _end(client, w, B, b)
    await _sub_id(client, w, E, "w2", role=CODER)
    assert [m["id"] for m in await _inbox(client, w, E, all=1)] == [line_msg]


async def test_p9_ui_stream_does_not_repeat_peer_msg_frame(client, db, clock, world):
    w = world
    await _pair(client, w)
    msg_id = (await _send(client, w, A, text=TEXT, to=_to(w)))["msg"]["id"]
    seq = _rec(db, msg_id).seq
    notifier = Notifier()
    notifier.attach(asyncio.get_running_loop())

    async def _two(principal) -> list[str]:
        gen = feed.stream(db, principal, last_seq=seq - 1, notifier=notifier, heartbeat_s=0.05)
        try:
            return [await anext(gen), await anext(gen)]
        finally:
            await gen.aclose()

    ui_principal = security.Principal(token_id=0, kind="ui", scopes=frozenset({"read"}), project_ids=None)
    first, second = await asyncio.wait_for(_two(ui_principal), 5)
    assert first.startswith(f"id: {seq}\nevent: peer_msg\n") and "СЕКРЕТНЫЙ" not in first
    assert json.loads(first.split("data: ")[1]) == {"id": msg_id, "status": "pending", "seq": seq}
    assert second == HEARTBEAT_FRAME  # кадр не повторяется

    bearer = security.Principal(token_id=1, kind="interactive", scopes=frozenset({"read"}), project_ids=None)
    assert await asyncio.wait_for(_two(bearer), 5) == [HEARTBEAT_FRAME, HEARTBEAT_FRAME]
    with db.reader() as conn:
        assert queries.changes_since(conn, bearer, since=0, limit=50).peer_msgs == []


# --- Ф42-2: пределов переписки нет -------------------------------------------------------------------------


def _blocked(db) -> list[tuple]:
    with db.reader() as conn:
        return conn.execute(
            "SELECT id, reason, text FROM main.peer_msg WHERE status = 'blocked' ORDER BY id"
        ).fetchall()


def _old_blocked_row(db, clock, *, to_user: int, text: str = "старая попытка") -> int:
    """Строка `blocked` из боевой базы времён пределов (схема и данные остаются)."""
    with db.reader() as conn:
        base = conn.execute("SELECT id FROM main.session_sub ORDER BY id LIMIT 1").fetchone()[0]

    def _ins(conn) -> int:
        cur = conn.execute(
            "INSERT INTO main.peer_msg (from_user, from_sub_id, from_project_id, from_role, from_role_key, "
            "from_no, to_user, to_project_id, to_role, to_role_key, to_no, text, hop, status, reason, "
            "created_ts, updated_ts, closed_ts, seq) "
            "SELECT 0, ?, project_id, role, role_key, role_no, ?, project_id, role, role_key, role_no, ?, 0, "
            "'blocked', 'pair_limit', ?, ?, ?, 1000000 FROM main.session_sub WHERE id = ?",
            (base, to_user, text, _now(clock), _now(clock), _now(clock), base),
        )
        return cur.lastrowid

    return db.write_sync(_ins)


async def test_no_limits_pair_300_in_hour_all_delivered(client, db, clock, world):
    w = world
    await _pair(client, w)
    for _ in range(300):
        await _send(client, w, A, to=_to(w))
    assert _blocked(db) == []
    assert len(await _inbox(client, w, B)) == 200  # выдача — не больше LIST_MAX, отказов нет
    with db.reader() as conn:
        n = conn.execute("SELECT COUNT(*) FROM main.peer_msg WHERE status = 'pending'").fetchone()[0]
        audit = conn.execute(
            "SELECT COUNT(*) FROM main.audit WHERE action = 'peer_msg_blocked'"
        ).fetchone()[0]
    assert (n, audit) == (300, 0)


async def test_no_limits_reply_chain_of_50_delivered(client, db, clock, world):
    w = world
    await _pair(client, w)
    last = (await _send(client, w, A, to=_to(w)))["msg"]
    chain = last["chain_id"]
    for i in range(50):
        clock.advance_ms(1000)
        last = (await _send(client, w, B if i % 2 == 0 else A, reply_to=last["id"]))["msg"]
        assert (last["chain_id"], last["reply_to"] is not None) == (chain, True)
    assert _blocked(db) == []


async def test_no_limits_500_unread_new_one_delivered(client, db, clock, world):
    w = world
    await _pair(client, w)
    for _ in range(500):
        await _send(client, w, A, to=_to(w))
    r = await _send(client, w, A, text="пятьсот первое", to=_to(w))
    assert r["msg"]["status"] == "pending" and _blocked(db) == []
    await _send(client, w, A, text="я" * 4001, to=_to(w), expect=422)  # прочие отказы на месте
    await _send(client, w, A, text="   ", to=_to(w), expect=422)


async def test_old_blocked_rows_are_invisible_and_do_not_alert(client, ui, db, clock, world):
    w = world
    await _pair(client, w)
    to_agent = _old_blocked_row(db, clock, to_user=0)
    to_owner = _old_blocked_row(db, clock, to_user=1)
    assert len(_blocked(db)) == 2
    assert await _alerts(client, w["triage"]) == []
    assert await _inbox(client, w, B) == [] and await _inbox(client, w, A, all=1) == []
    r = await ui.get("/api/v1/peer-msgs")
    assert r.status_code == 200 and r.json()["items"] == []
    with db.reader() as conn:
        assert peer_msgs.get(conn, to_agent) is None and peer_msgs.get(conn, to_owner) is None
        assert peer_msgs.changes(conn, since=0, watermark=10**9, limit=50) == []
    # новое сообщение той же пары идёт как обычно и одно в выдаче
    await _send(client, w, A, to=_to(w))
    assert len(await _inbox(client, w, B)) == 1


# --- B12: проект <-> вне проектов (Н4) ----------------------------------------------------------------------


async def test_b12_n4_exchange_with_agent_out_of_projects_only_while_alive(client, ui, db, clock, world):
    w = world
    a = await _sub_id(client, w, A, "w1", role=MARKETER)
    free = await _sub(client, w, D, "free", mode="direct", role="вольный")
    d = free["sub"]["id"]
    assert free["sub"]["project_id"] == 0

    sent = (await _send(client, w, A, to=_to(w, 0, "вольный")))["msg"]
    assert sent["to"] == {"project_id": 0, "role": "вольный", "role_no": 1, "sub_id": d}
    assert await _wait(client, w, d, D) == [
        {"kind": "peer", "msg_id": sent["id"], "from_sub": a, "from_project": w["p1"]}
    ]
    back = (await _send(client, w, D, reply_to=sent["id"]))["msg"]
    assert back["from"]["project_id"] == 0 and back["to"]["sub_id"] is None and back["hop"] == 0
    assert await _wait(client, w, a, A) == [
        {"kind": "peer", "msg_id": back["id"], "from_sub": d, "from_project": 0}
    ]
    again = (await _send(client, w, A, reply_to=back["id"]))["msg"]
    assert again["to"]["sub_id"] == d  # ответ агенту вне проектов — его подписке

    # не в сети — 409 и агенту, и владельцу; ждавшее — `lost`
    await _ui_send(ui, to={**_to(w, 0, "вольный"), "sub_id": d})
    await _end(client, w, D, d)
    assert _statuses(db)[-2:] == ["lost", "lost"]
    await _send(client, w, A, to=_to(w, 0, "вольный"), expect=409)
    await _send(client, w, A, reply_to=back["id"], expect=409)
    await _ui_send(ui, to=_to(w, 0, "вольный"), expect=409)
    await _ui_send(ui, to=_to(w, 0, "никогда не было"), expect=404)


# --- B13: владелец -> агент -------------------------------------------------------------------------------------


async def test_b13_owner_writes_from_ui_no_limits_signal_has_from_user(client, ui, db, clock, world):
    w = world
    _a, b = await _pair(client, w)
    first = await _ui_send(ui, text=TEXT, to=_to(w))
    msg = first["msg"]
    assert (msg["from_user"], msg["from"], msg["to"]["role"]) == (True, None, CODER)
    assert first["recipient"] == {"live": True, "waiting": False}
    # лимиты пары и глубины владельцу не писаны
    last = msg
    for _ in range(14):
        last = (await _ui_send(ui, reply_to=last["id"]))["msg"]
    assert (last["hop"], last["chain_id"], last["to"]["role"]) == (14, msg["id"], CODER)
    assert _blocked(db) == []

    signals = await _wait(client, w, b, B)
    assert signals[0] == {"kind": "peer", "msg_id": msg["id"], "from_user": 1}
    assert all(set(s) == {"kind", "msg_id", "from_user"} for s in signals) and len(signals) == 15
    [got] = [m for m in await _inbox(client, w, B) if m["id"] == msg["id"]]
    assert (got["from_user"], got["text"]) == (True, TEXT)

    # признак «от владельца» ставит только ядро и только UI-сессии
    await _send(client, w, A, to=_to(w), from_user=True, expect=422)
    r = await ui.post("/api/v1/peer-msgs", {"text": "x", "to": _to(w), "session": _session(A)})
    assert r.status_code == 422
    r = await ui.get("/api/v1/peer-msgs", params=_session(A))
    assert r.status_code == 422
    agent_msg = (await _send(client, w, A, to=_to(w)))["msg"]["id"]
    await _ui_send(ui, reply_to=agent_msg, expect=422)  # чужая переписка — не диалог владельца
    await _ui_send(ui, to=_to(w, role="бухгалтер"), expect=404)


async def test_n4_owner_message_to_offline_role_line_expires_in_2_days(client, ui, db, clock, world):
    w = world
    _a, b = await _pair(client, w)
    await _end(client, w, B, b)
    sent = await _ui_send(ui, to=_to(w))
    assert sent["recipient"] == {"live": False, "waiting": False}
    assert sent["msg"]["expires_ts"] == _now(clock) + 2 * DAY
    agent = (await _send(client, w, A, to=_to(w)))["msg"]["id"]
    clock.advance_ms(2 * DAY + MIN)
    assert _expire(db, clock) == 1
    assert (_rec(db, sent["msg"]["id"]).status, _rec(db, agent).status) == ("expired", "pending")


# --- B14 + Н7, Н8: агент -> владелец ----------------------------------------------------------------------------


async def _alerts(client, token) -> list[dict]:
    r = await client.get("/api/v1/agent/peer-msgs/alerts", headers=_h(token))
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def test_b14_agent_answers_owner_only_by_reply_ui_reads_alerts_once(client, ui, db, clock, world):
    w = world
    await _pair(client, w)
    asked = (await _ui_send(ui, to=_to(w)))["msg"]["id"]
    await _send(client, w, A, reply_to=asked, expect=404)  # не ему написано
    answer = (await _send(client, w, B, text=TEXT, reply_to=asked))["msg"]
    assert (answer["to_user"], answer["to"], answer["expires_ts"]) == (True, None, None)
    assert (answer["hop"], answer["chain_id"], answer["from"]["role"]) == (0, asked, CODER)
    again = (await _send(client, w, B, reply_to=asked))["msg"]["id"]  # отвечать можно не раз
    agent_msg = (await _send(client, w, A, to=_to(w)))["msg"]["id"]

    # ответ владельцу виден UI, но не другой линии; сессии он не сигналит
    assert [m["id"] for m in await _ui_list(ui)] == [agent_msg, again, answer["id"], asked]
    assert [m["id"] for m in await _ui_list(ui, limit=2, before=again)] == [answer["id"], asked]
    assert [m["id"] for m in await _inbox(client, w, A, all=1)] == [agent_msg]
    assert answer["id"] in [m["id"] for m in await _inbox(client, w, B, all=1)]  # свои отправленные

    # уведомление — один раз на строку; ручки — только скоуп triage, со списком проектов — только свои
    assert [m["id"] for m in await _alerts(client, w["triage"])] == [answer["id"], again]
    narrow = _token(
        db, name="agent:narrow", kind="background", scopes=("read", "triage"), project_ids=[w["p1"]]
    )
    assert await _alerts(client, narrow) == []
    r = await client.post(
        "/api/v1/agent/peer-msgs/notified", json={"ids": [answer["id"]]}, headers=_h(narrow)
    )
    assert r.json() == {"marked": 0}
    r = await client.post(
        "/api/v1/agent/peer-msgs/notified", json={"ids": [answer["id"], again]}, headers=_h(w["triage"])
    )
    assert r.json() == {"marked": 2}
    assert await _alerts(client, w["triage"]) == []
    r = await client.get("/api/v1/agent/peer-msgs/alerts", headers=_h(w["tok"]))
    assert r.status_code == 403

    # UI отмечает прочитанным только адресованное владельцу
    r = await ui.post("/api/v1/peer-msgs/read", {"ids": [answer["id"], agent_msg, asked]})
    assert r.status_code == 200 and r.json() == {"marked": 1}
    assert (_rec(db, answer["id"]).status, _rec(db, agent_msg).status) == ("read", "pending")
    r = await ui.post("/api/v1/peer-msgs/read", {"ids": [again], "session": _session(B)})
    assert r.status_code == 422
    # Владелец продолжает диалог ответом на ответ агента
    more = (await _ui_send(ui, reply_to=answer["id"]))["msg"]
    assert (more["to"]["role"], more["chain_id"], more["hop"]) == (CODER, asked, 1)


async def test_n7_n8_answer_to_owner_never_expires_but_only_within_a_day(client, ui, db, clock, world):
    w = world
    await _pair(client, w)
    asked = (await _ui_send(ui, to=_to(w)))["msg"]["id"]
    clock.advance_ms(23 * HOUR)
    answer = (await _send(client, w, B, reply_to=asked))["msg"]["id"]
    clock.advance_ms(2 * HOUR)
    r = await _send(client, w, B, reply_to=asked, expect=422)  # через 25 часов
    assert "в течение суток" in r["detail"]
    clock.advance_ms(30 * DAY)
    assert _expire(db, clock) == 1
    # сообщение владельца истекло, ответ ему — нет
    assert (_rec(db, asked).status, _rec(db, answer).status) == ("expired", "pending")


def test_migration_0049_main_only(db):
    with db.reader() as conn:
        assert conn.execute("SELECT 1 FROM main.sqlite_master WHERE name = 'peer_msg'").fetchone()
        assert conn.execute("SELECT 1 FROM work.sqlite_master WHERE name = 'peer_msg'").fetchone() is None


def test_cli_project_set_agent_msgs(monkeypatch, capsys):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"project_id": 3, **json.loads(request.content)})

    monkeypatch.setattr(
        hub_cli,
        "_admin_client",
        lambda *, timeout=None: httpx.Client(
            transport=httpx.MockTransport(handler), base_url="http://hub-internal/internal/v1"
        ),
    )
    for value in ("off", "on"):
        monkeypatch.setattr(sys, "argv", ["hub", "project", "set", "3", "--agent-msgs", value])
        hub_cli.main()
    assert [(r.method, r.url.path, json.loads(r.content)) for r in seen] == [
        ("PATCH", "/internal/v1/admin/projects/3", {"agent_msgs": False}),
        ("PATCH", "/internal/v1/admin/projects/3", {"agent_msgs": True}),
    ]
    out = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert out == [{"project_id": 3, "agent_msgs": False}, {"project_id": 3, "agent_msgs": True}]


# --- ревью Ф35 (03.10): глубина при reply_to, реплика владельца — подписке, своё — не входящее ------------------


async def test_review_owner_message_to_bound_agent_is_pinned_to_sub(client, ui, db, clock, world):
    """Диалог владельца с безрольным и с агентом вне проектов привязан к подписке: номер заняла другая сессия —
    реплика владельца ей не уходит (409); без `sub_id` привязанному владелец не пишет (422)."""
    w = world
    c = await _sub_id(client, w, C, "w1")  # «без роли 1»
    to = _to(w, "p1", None, number=1)
    assert (await _ui_send(ui, to={**to, "sub_id": c}))["msg"]["to"]["sub_id"] == c
    await _end(client, w, C, c)
    d = await _sub_id(client, w, D, "w1")  # другая сессия заняла «без роли 1»
    before = len(_statuses(db))
    r = await _ui_send(ui, text="сделай пуш", to={**to, "sub_id": c}, expect=409)
    assert "не в сети" in r["detail"]
    await _ui_send(ui, text="сделай пуш", to=to, expect=422)
    assert len(_statuses(db)) == before  # ни одной строки чужой сессии
    assert (await _ui_send(ui, to={**to, "sub_id": d}))["msg"]["to"]["sub_id"] == d

    # вне проектов — то же
    free = await _sub(client, w, E, "free", role="вольный", mode="direct")
    e = free["sub"]["id"]
    assert free["sub"]["project_id"] == 0
    await _ui_send(ui, to=_to(w, 0, "вольный"), expect=422)
    await _ui_send(ui, to={**_to(w, 0, "вольный"), "sub_id": d}, expect=409)
    assert (await _ui_send(ui, to={**_to(w, 0, "вольный"), "sub_id": e}))["msg"]["to"]["sub_id"] == e

    # линии с ролью `sub_id` не нужен; заданный — сверяется с живой подпиской линии
    b = await _sub_id(client, w, B, "w2", role=CODER)
    assert (await _ui_send(ui, to=_to(w)))["msg"]["to"]["sub_id"] is None
    assert (await _ui_send(ui, to={**_to(w), "sub_id": b}))["msg"]["to"]["sub_id"] is None
    await _ui_send(ui, to={**_to(w), "sub_id": d}, expect=409)
    # агенту (bearer) `sub_id` не положен
    await _sub_id(client, w, A, "w1", role=MARKETER)
    await _send(client, w, A, to={**_to(w), "sub_id": b}, expect=422)


async def test_f36_owner_message_follows_sub_after_role_change_in_place(client, ui, db, clock, world):
    """Ф36-2 A1: страница владельца держит адрес «без роли 1» с `sub_id`, а сессия сменила роль на месте — реплика
    доходит той же подписке (не 409); чужой проект в адресе и завершённая подписка — прежние отказы."""
    w = world
    c = await _sub_id(client, w, C, "w1")  # «без роли 1»
    stale = {**_to(w, "p1", None, number=1), "sub_id": c}
    await _ui_send(ui, to=stale)
    assert (await _sub(client, w, C, "w1", role="тестер"))["sub"]["id"] == c  # роль — на месте
    sent = (await _ui_send(ui, text="после смены роли", to=stale))["msg"]
    assert sent["to"]["sub_id"] == c and sent["to"]["role"] == "тестер"
    assert "после смены роли" in [m["text"] for m in await _inbox(client, w, C)]

    before = len(_statuses(db))
    await _ui_send(ui, to={**_to(w, "p2", None, number=1), "sub_id": c}, expect=409)  # проект адреса — не её
    await _end(client, w, C, c)
    await _ui_send(ui, to=stale, expect=409)  # подписка кончилась — никому
    assert len(_statuses(db)) == before


async def test_review_own_message_not_incoming_after_role_change_in_place(client, db, clock, world):
    """Сессия написала линии, затем сменой роли на месте сама встала на неё: своё сообщение — не входящее
    (ни сигнала, ни счётчика, ни ответа на него); следующей сессии линии оно дойдёт."""
    w = world
    a = await _sub_id(client, w, A, "w1", role="альфа")
    b = await _sub_id(client, w, B, "w1", role="бета")
    sent = (await _send(client, w, A, to=_to(w, "p1", "бета")))["msg"]
    await _end(client, w, B, b)
    same = await _sub(client, w, A, "w1", role="бета")  # A на месте становится «бета 1»
    assert (same["sub"]["id"], same["peer_unread"]) == (a, 0)
    assert await _wait(client, w, a, A) == []
    assert await _inbox(client, w, A) == []
    await _send(client, w, A, reply_to=sent["id"], expect=404)
    assert await _read(client, w, A, [sent["id"]]) == 0
    # A ушла с линии, на неё встала другая сессия — сообщение её
    await _sub(client, w, A, "w1", role="альфа")
    b2 = await _sub_id(client, w, B, "w1", role="бета")
    assert [s["msg_id"] for s in await _wait(client, w, b2, B)] == [sent["id"]]
