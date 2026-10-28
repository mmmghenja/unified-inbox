"""Ф41-2б-1а (docs/architecture.md, 7 и поправки 4а П1–П4, П8, П12, П13, П15):
работа постоянной сессии хаба на уровне ядра — очередь реплик, лента, запросы, просьбы «прервать» и «усыпить»,
ручки хоста, `/sync` и SSE.

Хост в тестах поддельный: bearer `work_host` зовёт ручки ядра напрямую. Стиль — как test_hub_sessions.py:
публичное приложение через ASGITransport, часы — `FakeClock`."""

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import held, held_work, runner, security, subs
from hub.core.app_public import create_public_app
from hub.core.db import WORK_ID_BASE
from hub.core.feed import Notifier, stream
from hub.core.security import Principal

from .conftest import (
    BASE_URL,
    PORT,
    csrf_headers,
    insert_account,
    insert_channel,
    insert_project,
    login_via_code,
)

ORIGIN = "http://127.0.0.1:8787"
API = "/api/v1"
PROC = {"proc_pid": 4242, "proc_start": 777_000}


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN}


def _token(db, *, name, kind="interactive", scopes=("read", "draft"), project_ids=None) -> str:
    return db.write_sync(
        lambda conn: security.create_api_token(
            conn, name=name, kind=kind, scopes=list(scopes), project_ids=project_ids, expires_ts=None
        )
    ).token


def _now(clock) -> int:
    return int(clock() * 1000)


@pytest.fixture
def world(db, tmp_path):
    dirs = {}
    for name in ("p3", "p4", "pw"):
        (tmp_path / name).mkdir()
        dirs[name] = str(tmp_path / name)
    p3 = insert_project(db, name="Umbrella", workdir=dirs["p3"])
    p4 = insert_project(db, name="Initech", workdir=dirs["p4"])
    pw = insert_project(db, name="Globex", workdir=dirs["pw"])
    for pid in (p3, p4, pw):
        db.write_sync(lambda conn, pid=pid: subs.set_project_subs(conn, pid, True, now_ms=0))
    work_acc = insert_account(db, source="slack", db_name="work")
    insert_channel(db, work_acc, project_id=pw, ext_id="C1", kind="group", db_name="work", title="dev")
    return {
        "p3": p3,
        "p4": p4,
        "pw": pw,
        "work_acc": work_acc,
        "host": _token(db, name="work:host", kind="background", scopes=("work_host", "read")),
        "tok": _token(db, name="mcp:all"),
        "tok3": _token(db, name="mcp:p3", project_ids=[p3]),
        "tok4": _token(db, name="mcp:p4", project_ids=[p4]),
    }


class _Ui:
    def __init__(self, client, headers):
        self.c, self.headers = client, headers

    async def get(self, path, **kw):
        return await self.c.get(path, **kw)

    async def post(self, path, body=None, *, expect=200):
        r = await self.c.post(path, json=body if body is not None else {}, headers=self.headers)
        assert r.status_code == expect, r.text
        return r.json()


@pytest.fixture
async def ui(app, db, clock):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as c:
        cookie, _csrf = await login_via_code(c, db, clock)
        yield _Ui(c, csrf_headers(cookie))


async def _mk(ui, w, project="p3", **body) -> int:
    body.setdefault("engine", "claude")
    return (await ui.post(f"{API}/sessions", {"project_id": w[project], **body}))["session"]["id"]


async def _host(client, w, path, body=None, *, expect=200, params=None):
    headers = _bearer(w["host"])
    if body is None:
        r = await client.get(f"{API}/work/host/{path}", headers=headers, params=params)
    else:
        r = await client.post(f"{API}/work/host/{path}", json=body, headers=headers)
    assert r.status_code == expect, r.text
    return r.json()


async def _run(client, w, sid, state="running"):
    """Поддельный хост «запустил процесс»."""
    return await _host(client, w, f"sessions/{sid}/state", {"state": state, **PROC})


async def _say(ui, sid, text="привет", priority="next", *, expect=200):
    return await ui.post(f"{API}/sessions/{sid}/say", {"text": text, "priority": priority}, expect=expect)


async def _get(ui, sid) -> dict:
    r = await ui.get(f"{API}/sessions/{sid}")
    assert r.status_code == 200, r.text
    return r.json()


async def _events(ui, sid, **params) -> list[dict]:
    r = await ui.get(f"{API}/sessions/{sid}/events", params=params)
    assert r.status_code == 200, r.text
    return r.json()


async def _ask(client, w, sid, *, kind="permission", tool="Bash", tool_input=None, expect=200):
    body = {"kind": kind, "tool": tool, "input": tool_input or {"command": "git push"}}
    return await _host(client, w, f"sessions/{sid}/requests", body, expect=expect)


def _rec(db, sid) -> held.HubSession:
    with db.reader() as conn:
        return held.get(conn, sid)


def _audits(db, action) -> list[dict]:
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT detail_json FROM main.audit WHERE action = ? ORDER BY id", (action,)
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def _cleanup(db, clock, app):
    now = _now(clock)
    return db.write_sync(
        lambda conn: runner._cleanup_all(conn, now, app.state.sub_waiters, app.state.chan_sub_waiters)
    )


# --- права --------------------------------------------------------------------------------------------------


async def test_bearer_cannot_act_on_session_and_ui_cannot_use_host_routes(client, ui, db, world):
    w = world
    sid = await _mk(ui, w)
    await _run(client, w, sid)
    rid = (await _ask(client, w, sid))["id"]
    denied = 0
    for token in (w["tok"], w["tok3"], w["host"]):
        for path, body in (
            (f"sessions/{sid}/say", {"text": "x"}),
            (f"sessions/{sid}/interrupt", {}),
            (f"sessions/{sid}/sleep", {}),
            (f"session-requests/{rid}/decide", {"decision": "allow"}),
        ):
            r = await client.post(f"{API}/{path}", json=body, headers=_bearer(token))
            assert r.status_code == 403, (path, r.text)
            denied += 1
    assert len(_audits(db, "ui_only_denied")) == denied
    data = await _get(ui, sid)
    assert data["says"] == [] and data["requests"][0]["status"] == "pending"
    assert data["session"]["interrupt_requested"] is False and data["session"]["sleep_requested"] is False

    # Кука UI и обычный bearer в ручки хоста не ходят.
    for path, body in (
        (f"sessions/{sid}/events", {"events": [{"kind": "text", "summary": "x"}]}),
        (f"sessions/{sid}/requests", {"kind": "permission", "tool": "Bash"}),
        (f"sessions/{sid}/turn", {"state": "idle"}),
        ("sessions/recover", {}),
        (f"session-requests/{rid}/expire", {}),
        (f"session-requests/{rid}/delivered", {}),
    ):
        r = await ui.c.post(f"{API}/work/host/{path}", json=body, headers=ui.headers)
        assert r.status_code == 403, (path, r.text)
        r = await client.post(f"{API}/work/host/{path}", json=body, headers=_bearer(w["tok"]))
        assert r.status_code == 403, (path, r.text)


async def test_read_is_scoped_by_token_projects_and_private_fields_are_ui_only(client, ui, world):
    w = world
    sid = await _mk(ui, w)
    await _host(
        client, w, f"sessions/{sid}/bind", {"engine_session_id": "conv-1", "state": "running", **PROC}
    )
    await _say(ui, sid, "секрет")
    for path in (f"sessions/{sid}", f"sessions/{sid}/events"):
        assert (await client.get(f"{API}/{path}", headers=_bearer(w["tok4"]))).status_code == 404
        assert (await client.get(f"{API}/{path}", headers=_bearer(w["tok3"]))).status_code == 200
    row = (await client.get(f"{API}/sessions/{sid}", headers=_bearer(w["tok3"]))).json()["session"]
    assert not {"workdir", "engine_session_id", "cache", "proc_pid"} & set(row)
    mine = (await _get(ui, sid))["session"]
    assert mine["engine_session_id"] == "conv-1" and mine["workdir"] and "cache" in mine
    assert "proc_pid" not in mine
    assert (await ui.get(f"{API}/sessions/999")).status_code == 404


# --- очередь реплик (3.1, П3а, П4) --------------------------------------------------------------------------


async def test_say_queue_is_delivered_one_by_one(client, ui, db, world):
    w = world
    sid = await _mk(ui, w)
    first = await _say(ui, sid, "первая")
    second = await _say(ui, sid, "вторая", "now")
    assert first["status"] == "queued" and second["priority"] == "now"
    data = await _get(ui, sid)
    assert [s["text"] for s in data["says"]] == ["первая", "вторая"]
    assert data["session"]["queued_says"] == 2 and data["session"]["state"] == "asleep"
    # П3а: `now` флага прерывания не ставит.
    assert _rec(db, sid).interrupt_requested_ts is None

    opened = await _host(client, w, "open")
    assert [s["id"] for s in opened["sessions"]["says"]] == [first["id"], second["id"]]
    assert opened["sessions"]["says"][1]["priority"] == "now"
    assert opened["sessions"]["items"][0]["id"] == sid and opened["sessions"]["items"][0]["workdir"]

    await _run(client, w, sid)
    done = await _host(client, w, f"session-says/{first['id']}/delivered", {})
    assert done == {"id": first["id"], "session_id": sid, "status": "delivered"}
    await _host(client, w, f"session-says/{first['id']}/delivered", {}, expect=409)
    data = await _get(ui, sid)
    assert [s["id"] for s in data["says"]] == [second["id"]] and data["session"]["queued_says"] == 1
    assert (await _host(client, w, "session-says/999999/delivered", {}, expect=404))["detail"] == "not found"

    # Ф55-1 (2.5): событие `say` пишется при доставке — недоставленной второй реплики в ленте нет.
    events = await _events(ui, sid)
    assert [(e["kind"], e["summary"]) for e in events] == [("say", "владелец (next): первая")]
    await _host(client, w, f"session-says/{second['id']}/delivered", {})
    events = await _events(ui, sid)
    assert [(e["kind"], e["summary"]) for e in events] == [
        ("say", "владелец (next): первая"),
        ("say", "владелец (now): вторая"),
    ]
    assert [e["ref"] for e in events] == [str(first["id"]), str(second["id"])]
    assert (await _get(ui, sid))["session"]["last_event_id"] == events[-1]["id"]
    assert _audits(db, "hub_session_say")[0] == {
        "hub_session_id": sid,
        "say_id": first["id"],
        "priority": "next",
        "text_len": 6,
    }


async def test_say_validation_and_closed(client, ui, world):
    w = world
    sid = await _mk(ui, w)
    assert (await _say(ui, sid, "   ", expect=422))["detail"] == "text: пустая реплика"
    out = await _say(ui, sid, "x" * (held_work.SAY_MAX + 1), expect=422)
    assert out["detail"] == "text: не длиннее 16000 символов"  # П13: готовый текст, не список pydantic
    assert (await _say(ui, sid, "", expect=422))["detail"] == "text: пустая реплика"
    await _say(ui, sid, "x" * held_work.SAY_MAX)
    await ui.post(f"{API}/sessions/{sid}/say", {"text": "x", "priority": "later"}, expect=422)
    await ui.post(f"{API}/sessions/{sid}/close")
    assert (await _say(ui, sid, expect=409))["detail"] == "сессия хаба закрыта"
    assert (await ui.post(f"{API}/sessions/{sid}/interrupt", expect=409))["detail"] == "сессия хаба закрыта"
    assert (await ui.post(f"{API}/sessions/{sid}/sleep", expect=409))["detail"] == "сессия хаба закрыта"
    await ui.post(f"{API}/sessions/999/say", {"text": "x"}, expect=404)


async def test_two_now_says_keep_order_and_never_set_interrupt(client, ui, db, world):
    """П15: `now` спящей и два `now` подряд — обе в очереди по порядку, флага нет ни у спящей, ни у идущей."""
    w = world
    sid = await _mk(ui, w)
    a = await _say(ui, sid, "раз", "now")
    await _run(client, w, sid)
    b = await _say(ui, sid, "два", "now")
    c = await _say(ui, sid, "три", "now")
    rec = _rec(db, sid)
    assert rec.interrupt_requested_ts is None and rec.state == "running"
    assert [s["id"] for s in (await _get(ui, sid))["says"]] == [a["id"], b["id"], c["id"]]


# --- «прервать» (3.1, П3) -----------------------------------------------------------------------------------


async def test_interrupt_needs_running_turn_and_is_cleared_conditionally(client, ui, db, clock, world):
    w = world
    sid = await _mk(ui, w)
    detail = (await ui.post(f"{API}/sessions/{sid}/interrupt", expect=409))["detail"]
    assert detail == "хода нет — прерывать нечего"
    await _run(client, w, sid, "idle")
    await ui.post(f"{API}/sessions/{sid}/interrupt", expect=409)

    await _run(client, w, sid)
    await _say(ui, sid, "в очереди")
    row = (await ui.post(f"{API}/sessions/{sid}/interrupt"))["session"]
    asked = _now(clock)
    assert row["interrupt_requested"] is True and row["queued_says"] == 1  # П4: очередь не трогается
    assert _rec(db, sid).interrupt_requested_ts == asked

    # П3б: хост снимал просьбу, которую видел раньше, — более новая остаётся.
    row = (
        await _host(
            client, w, f"sessions/{sid}/turn", {"state": "running", "clear_interrupt_upto": asked - 1}
        )
    )["session"]
    assert row["interrupt_requested_ts"] == asked
    row = (
        await _host(client, w, f"sessions/{sid}/turn", {"state": "running", "clear_interrupt_upto": asked})
    )["session"]
    assert row["interrupt_requested_ts"] is None

    # П15: просьба на границе хода — ход кончился, просьба к нему и относилась.
    clock.advance_ms(1000)
    await ui.post(f"{API}/sessions/{sid}/interrupt")
    row = (await _host(client, w, f"sessions/{sid}/turn", {"state": "idle", "last_turn_ts": _now(clock)}))[
        "session"
    ]
    assert row["interrupt_requested_ts"] is None and row["last_turn_ts"] == _now(clock)
    assert (await _get(ui, sid))["session"]["last_turn_ts"] == _now(clock)


# --- «усыпить» (3.1, П4) ------------------------------------------------------------------------------------


async def test_sleep_drops_queue_and_asks_host(client, ui, db, clock, world):
    w = world
    sid = await _mk(ui, w)
    await _run(client, w, sid)
    a = await _say(ui, sid, "раз")
    b = await _say(ui, sid, "два")
    out = await ui.post(f"{API}/sessions/{sid}/sleep")
    assert (out["changed"], out["dropped_says"]) == (True, 2)
    assert out["session"]["sleep_requested"] is True and out["session"]["queued_says"] == 0
    assert out["session"]["state"] == "running"  # усыпляет хост
    events = await _events(ui, sid)
    assert [(e["kind"], e["summary"], e["ref"]) for e in events[-2:]] == [
        ("say", "не отправлено: сессия усыплена", str(a["id"])),
        ("say", "не отправлено: сессия усыплена", str(b["id"])),
    ]
    await _host(client, w, f"session-says/{a['id']}/delivered", {}, expect=409)
    waited = await _host(client, w, "wait", params={"since": 0, "timeout_s": 0})
    item = waited["sessions"]["items"][0]
    assert item["sleep_requested_ts"] == _now(clock) and waited["sessions"]["says"] == []

    # Хост исполнил: `asleep` снимает просьбу сам; новая реплика после этого — обычная очередь.
    row = (await _host(client, w, f"sessions/{sid}/turn", {"state": "asleep"}))["session"]
    assert row["sleep_requested_ts"] is None and row["proc_pid"] is None
    out = await ui.post(f"{API}/sessions/{sid}/sleep")
    assert (out["changed"], out["dropped_says"]) == (False, 0)
    assert len(_audits(db, "hub_session_sleep")) == 1

    # Спящая с очередью: «усыпить» снимает реплики (иначе они подняли бы её), просьбы хосту нет.
    await _say(ui, sid, "три")
    out = await ui.post(f"{API}/sessions/{sid}/sleep")
    assert (out["changed"], out["dropped_says"]) == (True, 1)
    assert out["session"]["sleep_requested"] is False and out["session"]["queued_says"] == 0


async def test_say_mid_turn_then_sleep_then_interrupt(client, ui, db, world):
    """П15: реплика посреди хода, затем «усыпить» и «прервать» — очередь снята, обе просьбы у хоста,
    переход в `asleep` снимает обе."""
    w = world
    sid = await _mk(ui, w)
    await _run(client, w, sid)
    await _say(ui, sid, "посреди хода")
    await ui.post(f"{API}/sessions/{sid}/sleep")
    await ui.post(f"{API}/sessions/{sid}/interrupt")
    rec = _rec(db, sid)
    assert rec.sleep_requested_ts is not None and rec.interrupt_requested_ts is not None
    with db.reader() as conn:
        assert held_work.counts(conn, rec) == (0, 0, 0)  # Ф48-2: третье — прерванных
    await _host(client, w, f"sessions/{sid}/state", {"state": "asleep"})
    rec = _rec(db, sid)
    assert (rec.state, rec.sleep_requested_ts, rec.interrupt_requested_ts) == ("asleep", None, None)


async def test_turn_clear_sleep_explicitly(client, ui, db, world):
    w = world
    sid = await _mk(ui, w)
    await _run(client, w, sid, "idle")
    await ui.post(f"{API}/sessions/{sid}/sleep")
    row = (await _host(client, w, f"sessions/{sid}/turn", {"state": "idle", "clear": ["sleep"]}))["session"]
    assert row["sleep_requested_ts"] is None and row["state"] == "idle"
    await _host(client, w, f"sessions/{sid}/turn", {"state": "idle", "clear": ["interrupt"]}, expect=422)


# --- запросы и решения (3.1, 3.2) ---------------------------------------------------------------------------


async def test_request_decide_deliver(client, ui, db, clock, world):
    w = world
    sid = await _mk(ui, w)
    assert (await _ask(client, w, sid, expect=409))["detail"] == "сессия спит — запрос создавать некому"
    await _run(client, w, sid)
    made = await _ask(client, w, sid)
    rid = made["id"]
    # Ф66-1: поля `delegated` больше нет; «сразу да» (маршрут `auto`) отвечает `decided` — здесь его нет.
    assert made == {"id": rid, "session_id": sid, "status": "pending"}
    data = await _get(ui, sid)
    assert data["session"]["pending_requests"] == 1
    card = data["requests"][0]
    assert (card["id"], card["session_id"], card["project_id"]) == (rid, sid, w["p3"])
    assert (card["kind"], card["tool"], card["input"]) == ("permission", "Bash", {"command": "git push"})
    assert card["delivered"] is False and "task_id" not in card

    since = (await _host(client, w, "wait", params={"since": 0, "timeout_s": 0}))["max_seq"]
    await _host(client, w, f"session-requests/{rid}/delivered", {}, expect=409)  # ещё не решён
    out = await ui.post(f"{API}/session-requests/{rid}/decide", {"decision": "deny", "message": "не пушь"})
    assert out == {"id": rid, "session_id": sid, "status": "denied"}
    detail = (await ui.post(f"{API}/session-requests/{rid}/decide", {"decision": "allow"}, expect=409))[
        "detail"
    ]
    assert detail == "запрос уже решён или протух"

    waited = await _host(client, w, "wait", params={"since": since, "timeout_s": 0})
    got = waited["sessions"]["requests"][0]
    assert (got["id"], got["status"], got["answer"]) == (
        rid,
        "denied",
        {"decision": "deny", "message": "не пушь"},
    )
    assert waited["max_seq"] > since
    await _host(client, w, f"session-requests/{rid}/delivered", {})
    await _host(client, w, f"session-requests/{rid}/delivered", {})  # повтор — без изменений
    await _host(client, w, f"session-requests/{rid}/expire", {}, expect=409)
    assert (await _get(ui, sid))["requests"] == []
    kinds = [(e["kind"], e["summary"]) for e in await _events(ui, sid)]
    assert kinds == [("request", f"просит Bash (запрос #{rid})"), ("decision", "владелец отказал: Bash")]
    assert _audits(db, "hub_session_decide")[0]["request_id"] == rid


async def test_question_answers_and_expire(client, ui, world):
    w = world
    sid = await _mk(ui, w)
    await _run(client, w, sid)
    questions = {"questions": [{"question": "Куда?"}, {"question": "Когда?"}]}
    rid = (await _ask(client, w, sid, kind="question", tool="AskUserQuestion", tool_input=questions))["id"]
    out = await ui.post(f"{API}/session-requests/{rid}/decide", {"decision": "allow"}, expect=422)
    assert out["detail"] == "answers: ответы на вопросы сессии"
    body = {"decision": "allow", "answers": {"Куда?": "в main", "Когда?": "сейчас"}}
    assert (await ui.post(f"{API}/session-requests/{rid}/decide", body))["status"] == "answered"

    other = (await _ask(client, w, sid))["id"]
    assert (await _host(client, w, f"session-requests/{other}/expire", {}))["status"] == "expired"
    await ui.post(f"{API}/session-requests/{other}/decide", {"decision": "allow"}, expect=409)
    # Решён, но не доставлен — хост может снять (процесс умер до доставки).
    await _host(client, w, f"session-requests/{rid}/expire", {})
    await ui.post(f"{API}/session-requests/999999/decide", {"decision": "allow"}, expect=404)


# --- события ------------------------------------------------------------------------------------------------


async def test_host_events_feed_and_cursors(client, ui, db, world):
    w = world
    sid = await _mk(ui, w)
    batch = [{"kind": "start", "summary": "запуск"}] + [
        {"kind": "tool", "tool": "Bash", "summary": f"ls {i}", "ref": f"t{i}"} for i in range(5)
    ]
    batch += [
        {"kind": "text", "summary": "я" * 9000},
        {"kind": "status", "summary": "с" * 2000},
        {"kind": "interrupt", "summary": "прервано"},
        {"kind": "compact", "summary": "сжатие"},
        {"kind": "error", "summary": "сбой", "is_error": True},
    ]
    r = await client.post(
        f"{API}/work/host/sessions/{sid}/events", json={"events": batch}, headers=_bearer(w["host"])
    )
    assert r.status_code == 422  # текст длиннее 8000 — отказ модели запроса
    batch[6]["summary"] = "я" * 8000
    assert (await _host(client, w, f"sessions/{sid}/events", {"events": batch}))["accepted"] == len(batch)
    events = await _events(ui, sid)
    assert [e["kind"] for e in events][-4:] == ["status", "interrupt", "compact", "error"]
    assert len(events[6]["summary"]) == 8000 and len(events[7]["summary"]) == 1000
    assert events[-1]["is_error"] is True and events[1]["tool"] == "Bash"
    row = (await _get(ui, sid))["session"]
    assert row["last_event_id"] == events[-1]["id"]

    ids = [e["id"] for e in events]
    assert [e["id"] for e in await _events(ui, sid, after=ids[3], limit=2)] == ids[4:6]
    assert [e["id"] for e in await _events(ui, sid, before=ids[-1], limit=3)] == ids[-4:-1]
    await _host(client, w, f"sessions/{sid}/events", {"events": [{"kind": "go", "summary": "x"}]}, expect=422)
    await _host(client, w, "sessions/999/events", {"events": batch[:1]}, expect=404)


# --- закрытие (3.1, П8) -------------------------------------------------------------------------------------


async def test_close_drops_queue_expires_requests_and_host_gets_410(client, ui, db, world):
    w = world
    sid = await _mk(ui, w)
    await _run(client, w, sid)
    say = await _say(ui, sid, "не успела")
    pending = (await _ask(client, w, sid))["id"]
    decided = (await _ask(client, w, sid, tool="Edit"))["id"]
    await ui.post(f"{API}/session-requests/{decided}/decide", {"decision": "allow"})
    since = (await _host(client, w, "wait", params={"since": 0, "timeout_s": 0}))["max_seq"]

    out = await ui.post(f"{API}/sessions/{sid}/close")
    assert out["closed"] is True and out["session"]["queued_says"] == 0
    assert out["session"]["pending_requests"] == 0
    with db.reader() as conn:
        statuses = dict(conn.execute("SELECT id, status FROM main.held_request").fetchall())
        says = dict(conn.execute("SELECT id, status FROM main.held_say").fetchall())
    assert statuses == {pending: "expired", decided: "expired"} and says == {say["id"]: "dropped"}
    tail = [(e["kind"], e["summary"]) for e in await _events(ui, sid)][-2:]
    assert tail == [("say", "не отправлено: сессия закрыта"), ("status", "сессия закрыта")]

    # Хост узнаёт о закрытии из ожидания и получает 410 на всё, что пишет.
    waited = await _host(client, w, "wait", params={"since": since, "timeout_s": 0})
    item = waited["sessions"]["items"][0]
    assert item["id"] == sid and item["closed_ts"] is not None and waited["sessions"]["says"] == []
    for path, body in (
        (f"sessions/{sid}/events", {"events": [{"kind": "text", "summary": "x"}]}),
        (f"sessions/{sid}/turn", {"state": "asleep"}),
        (f"sessions/{sid}/requests", {"kind": "permission", "tool": "Bash"}),
        (f"session-requests/{pending}/expire", {}),
        (f"session-requests/{decided}/delivered", {}),
        (f"session-says/{say['id']}/delivered", {}),
    ):
        out = await _host(client, w, path, body, expect=410)
        assert out["detail"] == "сессия хаба закрыта"
    empty_section = {"items": [], "requests": [], "says": [], "signals": []}
    assert (await _host(client, w, "open"))["sessions"] == empty_section


# --- восстановление и молчащий хост (П1, П8) ----------------------------------------------------------------


async def test_recover_is_atomic_and_keeps_queue(client, ui, db, clock, world):
    """П8 + П15 «решение принято — хост перезапущен»: карточка не остаётся «разрешил»."""
    w = world
    running = await _mk(ui, w)
    idle = await _mk(ui, w)
    asleep = await _mk(ui, w, project="p4")
    await _run(client, w, running)
    await _host(client, w, f"sessions/{idle}/state", {"state": "idle", "proc_pid": 5151, "proc_start": 9})
    say = await _say(ui, running, "ждёт хода")
    pending = (await _ask(client, w, running))["id"]
    decided = (await _ask(client, w, running, tool="Edit"))["id"]
    delivered = (await _ask(client, w, running, tool="Read"))["id"]
    for rid in (decided, delivered):
        await ui.post(f"{API}/session-requests/{rid}/decide", {"decision": "allow"})
    await _host(client, w, f"session-requests/{delivered}/delivered", {})
    await ui.post(f"{API}/sessions/{running}/interrupt")
    await ui.post(f"{API}/sessions/{idle}/sleep")
    await _host(client, w, f"sessions/{asleep}/turn", {"state": "asleep", "wait_reason": "slot"})

    opened = await _host(client, w, "open")
    assert [r["id"] for r in opened["sessions"]["requests"]] == [decided]  # решён и не доставлен

    out = await _host(client, w, "sessions/recover", {})
    assert out == {
        "touched": [running, idle, asleep],
        # Ф89-1 (П12 б): процессы, оборванные восстановлением, — хосту для строки о перезапуске.
        "bg": {str(running): {"items": []}, str(idle): {"items": []}},
    }
    with db.reader() as conn:
        statuses = dict(conn.execute("SELECT id, status FROM main.held_request").fetchall())
    assert statuses == {pending: "expired", decided: "expired", delivered: "allowed"}
    for sid in (running, idle, asleep):
        rec = _rec(db, sid)
        assert (rec.state, rec.proc_pid, rec.interrupt_requested_ts, rec.sleep_requested_ts) == (
            "asleep",
            None,
            None,
            None,
        )
        assert rec.wait_reason is None
    data = await _get(ui, running)
    assert [s["id"] for s in data["says"]] == [say["id"]]  # очередь поднимет сессию обычным порядком
    last = (await _events(ui, running))[-1]
    assert (last["kind"], last["summary"]) == (
        "status",
        "ход прерван остановкой хоста или выключением; запросов снято: 2",
    )
    assert await _events(ui, idle) == []  # простаивала — события нет
    assert (await _host(client, w, "sessions/recover", {})) == {"touched": []}


async def test_stale_host_expires_requests_and_moves_seq(client, ui, db, clock, app, world):
    w = world
    sid = await _mk(ui, w)
    await _run(client, w, sid)
    rid = (await _ask(client, w, sid))["id"]
    seq = _rec(db, sid).seq
    clock.advance_ms(held.HOST_STALE_MS + 1000)
    _cleanup(db, clock, app)
    rec = _rec(db, sid)
    assert rec.state == "asleep" and rec.seq > seq
    data = await _get(ui, sid)
    assert data["requests"] == [] and data["session"]["pending_requests"] == 0
    await ui.post(f"{API}/session-requests/{rid}/decide", {"decision": "allow"}, expect=409)
    last = (await _events(ui, sid))[-1]
    assert last["summary"] == "хост не отвечает — ход прерван, сессия спит; запросов снято: 1"


async def test_idle_longer_than_stale_stays_alive_while_host_confirms(client, ui, db, clock, app, world):
    """П1 + П15: простой дольше 120 с при живом процессе — подтверждения хоста (`state`, `events`, `turn`)
    держат запись, а `seq` от них не растёт."""
    w = world
    sid = await _mk(ui, w)
    await _run(client, w, sid, "idle")
    seq = _rec(db, sid).seq
    step = held.HOST_STALE_MS // 2
    for confirm in (
        lambda: _run(client, w, sid, "idle"),
        lambda: _host(client, w, f"sessions/{sid}/turn", {"state": "idle"}),
        lambda: _run(client, w, sid, "idle"),
    ):
        clock.advance_ms(step)
        await confirm()
        _cleanup(db, clock, app)
        assert _rec(db, sid).state == "idle"
    assert _rec(db, sid).seq == seq  # подтверждение без изменений ожидание хоста не будит
    clock.advance_ms(step)
    await _host(client, w, f"sessions/{sid}/events", {"events": [{"kind": "status", "summary": "жив"}]})
    clock.advance_ms(step + 1000)
    _cleanup(db, clock, app)
    assert _rec(db, sid).state == "idle"  # и событие — подтверждение
    clock.advance_ms(held.HOST_STALE_MS + 1000)
    _cleanup(db, clock, app)
    assert _rec(db, sid).state == "asleep"


async def test_seq_moves_only_on_real_change(client, ui, db, world):
    """П1: `bind`, `state`, `close` двигают `seq`; повтор того же состояния — нет."""
    w = world
    sid = await _mk(ui, w)
    created = _rec(db, sid).seq
    assert created > 0
    bind = {"engine_session_id": "conv-1", "state": "running", **PROC}
    await _host(client, w, f"sessions/{sid}/bind", bind)
    bound = _rec(db, sid).seq
    assert bound > created
    await _host(client, w, f"sessions/{sid}/bind", bind)
    await _run(client, w, sid)
    assert _rec(db, sid).seq == bound
    await _run(client, w, sid, "idle")
    idle = _rec(db, sid).seq
    assert idle > bound
    await _host(client, w, f"sessions/{sid}/state", {"state": "idle", "proc_pid": 6000, "proc_start": 5})
    moved = _rec(db, sid).seq
    assert moved > idle  # сменился процесс
    await ui.post(f"{API}/sessions/{sid}/close")
    assert _rec(db, sid).seq > moved


# --- ход: неудачный запуск и причина ожидания (3.2, П13) ----------------------------------------------------


async def test_turn_failure_counts_and_new_say_resets(client, ui, db, world):
    w = world
    sid = await _mk(ui, w)
    await _say(ui, sid, "поднимись")
    for attempt in range(1, held_work.MAX_LAUNCH_FAILS + 1):
        row = (await _host(client, w, f"sessions/{sid}/turn", {"state": "asleep", "failure": "нет входа"}))[
            "session"
        ]
        assert (row["launch_fails"], row["failure"], row["state"]) == (attempt, "нет входа", "asleep")
    shown = (await _get(ui, sid))["session"]
    assert shown["failure"] == "нет входа" and shown["queued_says"] == 1 and "launch_fails" not in shown
    out = await _host(client, w, f"sessions/{sid}/turn", {"state": "running", "failure": "x"}, expect=422)
    assert out["detail"] == "failure — только вместе с state: asleep"

    await _say(ui, sid, "ещё раз")  # новая реплика владельца — новая попытка
    rec = _rec(db, sid)
    assert (rec.launch_fails, rec.failure) == (0, None)
    await _host(client, w, f"sessions/{sid}/turn", {"state": "asleep", "failure": "снова"})
    row = (await _host(client, w, f"sessions/{sid}/turn", {"state": "running"}))["session"]
    assert (row["launch_fails"], row["failure"]) == (0, None)  # запуск удался


async def test_wait_reason_set_keep_clear(client, ui, db, world):
    w = world
    sid = await _mk(ui, w)
    await _say(ui, sid, "x")
    row = (await _host(client, w, f"sessions/{sid}/turn", {"state": "asleep", "wait_reason": "slot"}))[
        "session"
    ]
    assert row["wait_reason"] == "slot"
    seq = _rec(db, sid).seq
    await _host(client, w, f"sessions/{sid}/turn", {"state": "asleep", "wait_reason": "slot"})
    await _host(client, w, f"sessions/{sid}/turn", {"state": "asleep"})  # ключа нет — причина остаётся
    assert _rec(db, sid).seq == seq and _rec(db, sid).wait_reason == "slot"
    assert (await _get(ui, sid))["session"]["wait_reason"] == "slot"
    row = (await _host(client, w, f"sessions/{sid}/turn", {"state": "asleep", "wait_reason": "elsewhere"}))[
        "session"
    ]
    assert row["wait_reason"] == "elsewhere"
    row = (await _host(client, w, f"sessions/{sid}/turn", {"state": "running"}))["session"]
    assert row["wait_reason"] is None  # ход пошёл
    await _host(client, w, f"sessions/{sid}/turn", {"state": "asleep", "wait_reason": "paused"})
    row = (await _host(client, w, f"sessions/{sid}/turn", {"state": "asleep", "wait_reason": None}))[
        "session"
    ]
    assert row["wait_reason"] is None
    await _host(client, w, f"sessions/{sid}/turn", {"state": "asleep", "wait_reason": "скучно"}, expect=422)


# --- файл базы по проекту (2.1, П2) -------------------------------------------------------------------------


async def test_work_project_session_lives_in_work_file(client, ui, db, world):
    w = world
    sid = await _mk(ui, w, project="pw")
    assert _rec(db, sid).store == "work"
    say = await _say(ui, sid, "корпоративное")
    await _run(client, w, sid)
    rid = (await _ask(client, w, sid))["id"]
    await _host(client, w, f"sessions/{sid}/events", {"events": [{"kind": "text", "summary": "ответ"}]})
    assert say["id"] > WORK_ID_BASE and rid > WORK_ID_BASE
    events = await _events(ui, sid)
    # Ф55-1 (2.5): события `say` пока нет — реплика не доставлена; оно ляжет в тот же файл при доставке.
    assert all(e["id"] > WORK_ID_BASE for e in events) and len(events) == 2
    with db.reader() as conn:
        for table in ("held_event", "held_request", "held_say"):
            assert conn.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0] == 0
            assert conn.execute(f"SELECT COUNT(*) FROM work.{table}").fetchone()[0] >= 1
    await ui.post(f"{API}/session-requests/{rid}/decide", {"decision": "allow"})
    await _host(client, w, f"session-requests/{rid}/delivered", {})
    await _host(client, w, f"session-says/{say['id']}/delivered", {})
    said = [e for e in await _events(ui, sid) if e["kind"] == "say"]
    assert [e["summary"] for e in said] == ["владелец (next): корпоративное"] and said[0]["id"] > WORK_ID_BASE
    opened = await _host(client, w, "open")
    assert opened["sessions"]["items"][0]["id"] == sid


async def test_store_is_fixed_at_creation(client, ui, db, world):
    """П2 + П15: беседу рабочего аккаунта перенесли в проект посреди жизни сессии — её строки остаются в
    прежнем файле, и запрос находится."""
    w = world
    sid = await _mk(ui, w)
    await _run(client, w, sid)
    first = (await _ask(client, w, sid))["id"]
    insert_channel(db, w["work_acc"], project_id=w["p3"], ext_id="C2", kind="group", db_name="work")
    second = (await _ask(client, w, sid, tool="Edit"))["id"]
    say = await _say(ui, sid, "после переноса")
    assert max(first, second, say["id"]) < WORK_ID_BASE and _rec(db, sid).store == "main"
    assert (await _get(ui, sid))["session"]["pending_requests"] == 2
    await ui.post(f"{API}/session-requests/{first}/decide", {"decision": "allow"})
    # Новая сессия того же проекта — уже в рабочем файле.
    other = await _mk(ui, w)
    assert _rec(db, other).store == "work"
    # id запроса из чужого файла (строка с таким id в другом файле) — 404.
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO work.held_request (id, session_id, kind, status, created_ts, seq) "
            "VALUES (?, ?, 'permission', 'pending', 0, 0)",
            (WORK_ID_BASE + 500, sid),
        )
    )
    await ui.post(f"{API}/session-requests/{WORK_ID_BASE + 500}/decide", {"decision": "allow"}, expect=404)
    await _host(client, w, f"session-requests/{WORK_ID_BASE + 500}/expire", {}, expect=404)


async def test_session_created_before_migration_gets_store_on_first_write(client, ui, db, world):
    w = world
    sid = await _mk(ui, w, project="pw")
    db.write_sync(lambda conn: conn.execute("UPDATE main.hub_session SET store = NULL WHERE id = ?", (sid,)))
    data = await _get(ui, sid)
    assert data["says"] == [] and data["requests"] == [] and await _events(ui, sid) == []
    say = await _say(ui, sid, "первая запись")
    assert _rec(db, sid).store == "work" and say["id"] > WORK_ID_BASE


# --- ожидание хоста (3.2) -----------------------------------------------------------------------------------


async def test_host_wait_wakes_on_say_and_decision(client, ui, world):
    w = world
    sid = await _mk(ui, w)
    since = (await _host(client, w, "wait", params={"since": 0, "timeout_s": 0}))["max_seq"]
    empty = await _host(client, w, "wait", params={"since": since, "timeout_s": 0})
    assert empty["sessions"] == {"items": [], "requests": [], "says": [], "signals": []}
    assert empty["max_seq"] == since

    waiting = asyncio.create_task(_host(client, w, "wait", params={"since": since, "timeout_s": 10}))
    await asyncio.sleep(0.05)
    say = await _say(ui, sid, "проснись")
    waited = await asyncio.wait_for(waiting, timeout=5)
    assert [s["id"] for s in waited["sessions"]["says"]] == [say["id"]]
    assert waited["sessions"]["items"][0]["id"] == sid and waited["max_seq"] > since
    # Ф78-1: задач по «Го» нет — в снимке ожидания нет и их ключей.
    assert not {"tasks", "requests", "says"} & set(waited)

    await _run(client, w, sid)
    rid = (await _ask(client, w, sid))["id"]
    since = (await _host(client, w, "wait", params={"since": 0, "timeout_s": 0}))["max_seq"]
    waiting = asyncio.create_task(_host(client, w, "wait", params={"since": since, "timeout_s": 10}))
    await asyncio.sleep(0.05)
    await ui.post(f"{API}/session-requests/{rid}/decide", {"decision": "allow"})
    waited = await asyncio.wait_for(waiting, timeout=5)
    assert [(r["id"], r["status"]) for r in waited["sessions"]["requests"]] == [(rid, "allowed")]


# --- `/sync` и SSE (3.3, П12) -------------------------------------------------------------------------------


async def _sync(client, headers=None, *, ui=None, since=0, limit=200) -> dict:
    params = {"since": since, "limit": limit}
    if ui is not None:
        r = await ui.get(f"{API}/sync", params=params)
    else:
        r = await client.get(f"{API}/sync", params=params, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def test_sync_carries_sessions_and_requests_scoped(client, ui, db, world):
    w = world
    sid = await _mk(ui, w)
    other = await _mk(ui, w, project="p4")
    await _host(
        client, w, f"sessions/{sid}/bind", {"engine_session_id": "conv-1", "state": "running", **PROC}
    )
    rid = (await _ask(client, w, sid))["id"]
    await _say(ui, sid, "x")

    mine = await _sync(client, ui=ui)
    rows = {row["id"]: row for row in mine["hub_sessions"]}
    assert set(rows) == {sid, other}
    row = rows[sid]
    assert row["engine_session_id"] == "conv-1"  # кука UI
    assert not {"workdir", "cache", "listening", "proc_pid"} & set(row)
    assert (row["state"], row["pending_requests"], row["queued_says"]) == ("running", 1, 1)
    assert row["seq"] == _rec(db, sid).seq and row["last_event_id"] is not None
    req = mine["session_requests"][0]
    assert (req["id"], req["session_id"], req["project_id"], req["status"]) == (rid, sid, w["p3"], "pending")
    assert req["input"] == {"command": "git push"}

    scoped = await _sync(client, _bearer(w["tok3"]))
    assert [r["id"] for r in scoped["hub_sessions"]] == [sid]
    assert "engine_session_id" not in scoped["hub_sessions"][0]  # П12: только куке UI
    assert [r["id"] for r in scoped["session_requests"]] == [rid]
    foreign = await _sync(client, _bearer(w["tok4"]))
    assert [r["id"] for r in foreign["hub_sessions"]] == [other] and foreign["session_requests"] == []

    # Решение и закрытие приходят изменениями после курсора.
    since = mine["max_seq"]
    await ui.post(f"{API}/session-requests/{rid}/decide", {"decision": "allow"})
    await ui.post(f"{API}/sessions/{other}/close")
    delta = await _sync(client, ui=ui, since=since)
    assert [(r["id"], r["status"]) for r in delta["session_requests"]] == [(rid, "allowed")]
    closed = {r["id"]: r for r in delta["hub_sessions"]}[other]
    assert closed["closed_ts"] is not None


async def test_sync_paging_with_limit_gives_each_change_once(client, ui, db, world):
    """П15: листание `/sync` с новыми видами через `limit` — каждое изменение ровно один раз, по порядку."""
    w = world
    sids = [await _mk(ui, w) for _ in range(3)]
    work_sid = await _mk(ui, w, project="pw")
    rids = []
    for sid in (*sids, work_sid):
        await _run(client, w, sid)
        rids.append((await _ask(client, w, sid))["id"])
    full = await _sync(client, ui=ui)
    expected = sorted(
        [("s", r["id"], r["seq"]) for r in full["hub_sessions"]]
        + [("r", r["id"], r["seq"]) for r in full["session_requests"]],
        key=lambda item: item[2],
    )
    assert {item[1] for item in expected if item[0] == "r"} == set(rids)
    assert len(expected) == 8

    seen, since = [], 0
    for _ in range(50):
        page = await _sync(client, ui=ui, since=since, limit=1)
        seen += [("s", r["id"], r["seq"]) for r in page["hub_sessions"]]
        seen += [("r", r["id"], r["seq"]) for r in page["session_requests"]]
        since = page["max_seq"]
        if not page["more"]:
            break
    assert seen == expected


async def test_stream_frames_for_sessions_and_requests(client, ui, db, world):
    w = world
    notifier = Notifier()
    notifier.attach(asyncio.get_running_loop())
    db.on_commit(notifier.notify)
    with db.reader() as conn:
        start = conn.execute("SELECT value FROM main.seq_counter WHERE id = 1").fetchone()[0]
    principal = Principal(
        token_id=1, kind="interactive", scopes=frozenset({"read"}), project_ids=frozenset({w["p3"]})
    )
    gen = stream(db, principal, last_seq=start, notifier=notifier, heartbeat_s=0.3)
    try:
        hidden = await _mk(ui, w, project="p4", title="секрет")
        await _run(client, w, hidden)
        await _ask(client, w, hidden)
        sid = await _mk(ui, w, title="видимая")
        await _run(client, w, sid)
        rid = (await _ask(client, w, sid))["id"]

        async def _until_heartbeat() -> list[str]:
            got: list[str] = []
            while len(got) < 50:
                frame = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
                if frame.startswith("event: hb"):
                    break
                got.append(frame)
            return got

        frames = await _until_heartbeat()
        # Изменение одной только записи сессии: кадр приходит один раз (курсор потока двигается и по нему).
        await _host(client, w, f"sessions/{sid}/turn", {"state": "idle"})
        only_session = await _until_heartbeat()
        # Чужая запись будит поток, но кадров не даёт — и уже отданный кадр сессии не повторяется.
        await _host(client, w, f"sessions/{hidden}/turn", {"state": "idle"})
        assert await _until_heartbeat() == []
    finally:
        await gen.aclose()
    assert len(only_session) == 1 and "event: hub_session" in only_session[0]
    assert json.loads(only_session[0].split("data: ")[1])["state"] == "idle"
    assert len(frames) == len(set(frames))
    parsed = [
        (frame.split("event: ")[1].split("\n")[0], json.loads(frame.split("data: ")[1])) for frame in frames
    ]
    assert {name for name, _data in parsed} == {"hub_session", "session_request"}
    assert all(data["project_id"] == w["p3"] for _name, data in parsed)  # чужой проект кадров не даёт
    last_session = [data for name, data in parsed if name == "hub_session"][-1]
    assert set(last_session) == {"id", "project_id", "state", "closed", "last_event_id", "seq"}
    assert (last_session["id"], last_session["state"], last_session["closed"]) == (sid, "running", False)
    request = [data for name, data in parsed if name == "session_request"][-1]
    assert request == {
        "id": rid,
        "session_id": sid,
        "project_id": w["p3"],
        "status": "pending",
        "seq": request["seq"],
    }
    assert "видимая" not in "".join(frames) and "git push" not in "".join(frames)


# --- перезапуск ядра ----------------------------------------------------------------------------------------


async def test_queue_and_requests_survive_core_restart(client, ui, db, clock, world):
    w = world
    sid = await _mk(ui, w)
    await _run(client, w, sid)
    say = await _say(ui, sid, "переживёт")
    rid = (await _ask(client, w, sid))["id"]
    held.reset()
    app2 = create_public_app(db, clock=clock, port=PORT)
    async with AsyncClient(transport=ASGITransport(app=app2), base_url=BASE_URL) as c2:
        opened = (await c2.get(f"{API}/work/host/open", headers=_bearer(w["host"]))).json()
        assert [s["id"] for s in opened["sessions"]["says"]] == [say["id"]]
        r = await c2.post(f"{API}/work/host/sessions/recover", json={}, headers=_bearer(w["host"]))
        assert r.json() == {"touched": [sid], "bg": {str(sid): {"items": []}}}  # Ф89-1: процесс оборван
        row = (await c2.get(f"{API}/sessions/{sid}", headers=_bearer(w["tok3"]))).json()
        assert row["session"]["state"] == "asleep" and row["requests"] == []
        assert [s["id"] for s in row["says"]] == [say["id"]]
    with db.reader() as conn:
        assert (
            conn.execute("SELECT status FROM main.held_request WHERE id = ?", (rid,)).fetchone()[0]
            == "expired"
        )


# --- ревью 2б: политика проекта в записи «для хоста» --------------------------------------------------------


async def test_host_record_carries_project_work_policy(client, ui, db, world):
    """Добавки проекта к кнопкам (`work.ask` / `work.self`) доходят до хоста во всех выдачах записи «для
    хоста» — как задаче по «Го»; в публичной записи их нет."""
    w = world
    sid = await _mk(ui, w)
    empty = {"stage": 1, "ask": [], "self": []}
    assert (await _host(client, w, "sessions"))["items"][0]["policy"] == empty

    def _set(policy: dict) -> None:
        db.write_sync(
            lambda conn: conn.execute(
                "UPDATE main.project SET agent_policy_json = ? WHERE id = ?", (json.dumps(policy), w["p3"])
            )
        )

    work = {"stage": 1, "ask": ["mcp__crm__send_invoice"], "self": ["RemoteTrigger"]}
    _set({"subs": True, "work": work})
    assert (await _host(client, w, "sessions"))["items"][0]["policy"] == work
    assert (await _host(client, w, "open"))["sessions"]["items"][0]["policy"] == work
    bind = {"engine_session_id": "conv-1", "state": "idle", **PROC}
    assert (await _host(client, w, f"sessions/{sid}/bind", bind))["session"]["policy"] == work
    assert (await _run(client, w, sid))["session"]["policy"] == work
    assert (await _host(client, w, f"sessions/{sid}/turn", {"state": "idle"}))["session"]["policy"] == work
    waited = await _host(client, w, "wait", params={"since": 0, "timeout_s": 0})
    assert waited["sessions"]["items"][0]["policy"] == work
    assert "policy" not in (await _get(ui, sid))["session"]
    # Негодная политика (этап не 1) задачу по «Го» отвергает; постоянной сессии остаются кнопки по умолчанию.
    _set({"subs": True, "work": {"stage": 2, "ask": ["x"]}})
    assert (await _host(client, w, "sessions"))["items"][0]["policy"] == empty
