"""Ф17-2 (docs/architecture.md, с поправками П1–П12 — они главнее текста спеки):
выход из групп и каналов Telegram — ядро и ручки.

Сессия — bearer `interactive` со скоупом `draft`; владелец — UI-сессия (вход кодом). Коннектор — внутреннее
приложение с секретом аккаунта на той же БД и тех же часах: `wait`/`lease`/`report`; беседы появляются в
хабе событиями коннектора (`/internal/v1/events`)."""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import channels as channels_mod
from hub.core import connector_auth, security
from hub.core import leave as leave_mod
from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.feed import Notifier

from .conftest import ORIGIN, csrf_headers, insert_project, login_via_code

API = "/api/v1"
INTERNAL = "/internal/v1"
ADMIN_SECRET = ADMIN_PREFIX + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHI"
PID, START, SID = 4242, 777_000, "sess-leave-1111"


# --- обвязка ------------------------------------------------------------------------------------------------


@pytest.fixture
def internal_app(db, clock):
    notifier = Notifier()
    db.on_commit(notifier.notify)
    return create_internal_app(db, clock=clock, notifier=notifier, admin_key_hash=hash_secret(ADMIN_SECRET))


@pytest.fixture
async def ic(internal_app):
    async with AsyncClient(transport=ASGITransport(app=internal_app), base_url="http://hub-internal") as c:
        yield c


def _account(db, *, source="telegram", db_name="main") -> tuple[int, dict]:
    account_id = db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.account (source, db) VALUES (?, ?)", (source, db_name)).lastrowid
        )
    )
    secret = connector_auth.generate_connector_secret()
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.connector_secret (account_id, hash, created_ts) VALUES (?, ?, 0)",
            (account_id, connector_auth.hash_secret(secret)),
        )
    )
    return account_id, {"Authorization": f"Bearer {secret}"}


def _token(db, *, name="mcp:all", kind="interactive", scopes=("read", "draft"), project_ids=None):
    return db.write_sync(
        lambda conn: security.create_api_token(
            conn, name=name, kind=kind, scopes=list(scopes), project_ids=project_ids, expires_ts=None
        )
    )


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN}


def _now_ms(clock) -> int:
    return int(clock() * 1000)


async def _ui(client, db, clock) -> dict:
    cookie, _ = await login_via_code(client, db, clock)
    client.cookies.clear()  # bearer и cookie в одном запросе — неоднозначный принципал
    return {"Cookie": f"hub_session={cookie}", **csrf_headers(cookie)}


def _up(ch, ext_id, ts, *, kind="channel", parent=None, is_me=False):
    channel: dict = {"ext_id": ch, "kind": kind}
    if parent is not None:
        channel["parent_ext_id"] = parent
        channel["parent"] = {"ext_id": parent, "kind": "group"}
    return {
        "type": "upsert",
        "channel": channel,
        "ext_id": ext_id,
        "ts": ts,
        "is_me": is_me,
        "kind": "text",
        "text": "t",
        "mentions_me": False,
        "sender": {"ext_id": "u1", "name": "u1", "handle": "u1", "is_me": is_me},
    }


async def _events(ic, h, events):
    r = await ic.post(f"{INTERNAL}/events", headers=h, json={"events": events})
    assert r.status_code == 200 and r.json()["dead"] == 0, r.text


def _chan(db, ext_id) -> dict | None:
    with db.reader() as conn:
        row = conn.execute(
            "SELECT id, project_id, archived, kind, parent_channel_id FROM main.channel WHERE ext_id = ?",
            (ext_id,),
        ).fetchone()
    if row is None:
        return None
    return dict(zip(("id", "project_id", "archived", "kind", "parent"), row, strict=True))


def _set_project(db, channel_id, project_id):
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET project_id = ?, map_source = 'manual' WHERE id = ?",
            (project_id, channel_id),
        )
    )


async def _make_chan(ic, env, clock, ext, *, kind="channel", project=True, parent=None) -> dict:
    await _events(ic, env["h"], [_up(ext, "1", _now_ms(clock), kind=kind, parent=parent)])
    ch = _chan(db_ := env["db"], ext)
    if project:
        _set_project(db_, ch["id"], env["project"])
        if parent is not None:
            _set_project(db_, ch["parent"], env["project"])
    return _chan(db_, ext)


async def _ask(client, token, channel_id, *, status=201, reason=None):
    body: dict = {"channel_id": channel_id}
    if reason is not None:
        body["reason"] = reason
    r = await client.post(f"{API}/leaves", json=body, headers=_bearer(token))
    assert r.status_code == status, r.text
    return r.json()


async def _ui_leave(client, ui, channel_id, *, status=200):
    r = await client.post(f"{API}/channels/{channel_id}/leave", headers=ui)
    assert r.status_code == status, r.text
    return r.json()


async def _decide(client, ui, leave_id, action, *, status=200):
    r = await client.post(f"{API}/leaves/{leave_id}/{action}", headers=ui)
    assert r.status_code == status, r.text
    return r.json()


async def _lease(ic, h, limit=None):
    body = {} if limit is None else {"limit": limit}
    r = await ic.post(f"{INTERNAL}/leave/lease", headers=h, json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def _report(ic, h, item, outcome="done", *, status=200, **extra):
    body = {"id": item["id"], "lease_gen": item["lease_gen"], "outcome": outcome, **extra}
    r = await ic.post(f"{INTERNAL}/leave/report", headers=h, json=body)
    assert r.status_code == status, r.text
    return r.json()


def _req(db, leave_id) -> leave_mod.Req:
    with db.reader() as conn:
        req = leave_mod.get(conn, leave_id)
    assert req is not None
    return req


def _audit(db, action):
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT result, actor, detail_json FROM main.audit WHERE action = ? ORDER BY id", (action,)
        ).fetchall()
    return [(r[0], r[1], json.loads(r[2])) for r in rows]


@pytest.fixture
async def env(db, ic, clock):
    """Проект Umbrella, аккаунт Telegram с секретом коннектора, токен сессии `mcp:all` с `draft`."""
    project = insert_project(db, name="Umbrella")
    acc, h = _account(db)
    created = _token(db)
    return {
        "db": db,
        "project": project,
        "acc": acc,
        "h": h,
        "token": created.token,
        "token_id": created.token_id,
    }


async def _left(client, ic, env, clock, ui, ext="-1001", **kw) -> tuple[dict, dict]:
    """Беседа в проекте -> владелец «Выйти» из шапки -> коннектор `done left`. Возвращает (беседа, заявка)."""
    ch = await _make_chan(ic, env, clock, ext, **kw)
    item = await _ui_leave(client, ui, ch["id"])
    [job] = await _lease(ic, env["h"])
    out = await _report(ic, env["h"], job, result="left")
    assert out == {"ok": True, "result": "left"}
    return ch, item


# --- E1: создание сессией -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, kind, scopes",
    [
        ("mcp:wake-3", "interactive", ("read", "draft")),
        ("mcp:orchestrator", "interactive", ("read", "draft")),
        ("mcp:bg", "background", ("read", "draft")),
        ("mcp:orch2", "interactive", ("read", "draft", "organize", "orchestrate")),
        ("mcp:runs", "interactive", ("read", "draft", "runs")),
    ],
)
async def test_session_check_attach_denied_403(client, db, ic, clock, env, name, kind, scopes):
    ch = await _make_chan(ic, env, clock, "-1001")
    created = _token(db, name=name, kind=kind, scopes=scopes)
    r = await _ask(client, created.token, ch["id"], status=403)
    assert "интерактивной сессии" in r["detail"]
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.leave_request").fetchone()[0] == 0


async def test_session_scope_and_ui_session_refused(client, db, ic, clock, env):
    ch = await _make_chan(ic, env, clock, "-1001")
    read_only = _token(db, name="mcp:ro", scopes=("read",))
    await _ask(client, read_only.token, ch["id"], status=403)
    ui = await _ui(client, db, clock)
    r = await client.post(f"{API}/leaves", json={"channel_id": ch["id"]}, headers=ui)
    assert r.status_code == 403, r.text


async def test_session_creates_pending_and_is_idempotent(client, db, ic, clock, env):
    ch = await _make_chan(ic, env, clock, "-1001")
    item = await _ask(client, env["token"], ch["id"], reason="группа больше не нужна")
    assert (item["status"], item["origin"], item["project_id"], item["channel_id"]) == (
        "pending",
        "session",
        env["project"],
        ch["id"],
    )
    assert item["chat_kind"] == "channel" and item["can_decline"] is True and item["leaving"] is False
    assert "token_id" not in item
    again = await _ask(client, env["token"], ch["id"], status=200)
    assert again["id"] == item["id"] and again["status"] == "pending"
    assert await _lease(ic, env["h"]) == []  # `pending` ждёт владельца — коннектору нечего
    [(result, actor, detail)] = _audit(db, "leave_request_created")
    assert (result, actor) == ("pending", "interactive")
    assert detail == {
        "leave_id": item["id"],
        "project_id": env["project"],
        "account_id": env["acc"],
        "channel_id": ch["id"],
    }


async def test_session_channel_checks(client, db, ic, clock, env):
    # Не видна токену — 404; без проекта — 422.
    ch = await _make_chan(ic, env, clock, "-1001", project=False)
    r = await _ask(client, env["token"], ch["id"], status=422)
    assert r["detail"] == "беседа без проекта — выйти может только владелец из шапки"
    other = insert_project(db, name="Initech")
    scoped = _token(db, name="mcp:initech", project_ids=[other])
    await _ask(client, scoped.token, ch["id"], status=404)
    await _ask(client, env["token"], 999_999, status=404)
    # Личка, Slack, почта, заготовка нового адресата — 422.
    dm = await _make_chan(ic, env, clock, "77", kind="dm")
    r = await _ask(client, env["token"], dm["id"], status=422)
    assert r["detail"] == "выйти можно только из группы или канала Telegram или группы WhatsApp"
    _slack, sh = _account(db, source="slack")
    await _events(ic, sh, [_up("C1", "1.1", _now_ms(clock), kind="group")])
    slack = _chan(db, "C1")
    _set_project(db, slack["id"], env["project"])
    await _ask(client, env["token"], slack["id"], status=422)
    temp_id = db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.channel (account_id, ext_id, kind, project_id, first_seen_ts, seq) "
                "VALUES (?, 'new:abc', 'group', ?, 0, 0)",
                (env["acc"], env["project"]),
            ).lastrowid
        )
    )
    r = await _ask(client, env["token"], temp_id, status=422)
    assert "заготовка" in r["detail"]


async def test_session_topic_goes_to_parent_and_p7(client, db, ic, clock, env):
    topic = await _make_chan(ic, env, clock, "-3000:5", kind="topic", parent="-3000")
    item = await _ask(client, env["token"], topic["id"])
    assert item["channel_id"] == topic["parent"] and item["chat_kind"] == "group"
    assert item["project_id"] == env["project"]
    # П7: тема с проектом, заданным вручную, а форум — в другом проекте (или без проекта).
    other = insert_project(db, name="Initech")
    topic2 = await _make_chan(ic, env, clock, "-3001:5", kind="topic", parent="-3001")
    _set_project(db, topic2["parent"], other)
    scoped = _token(db, name="mcp:iip", project_ids=[env["project"]])
    r = await _ask(client, scoped.token, topic2["id"], status=422)
    assert r["detail"] == "форум в другом проекте — выйти может только владелец из шапки"
    _set_project(db, topic2["parent"], None)
    r = await _ask(client, env["token"], topic2["id"], status=422)
    assert r["detail"] == "форум в другом проекте — выйти может только владелец из шапки"


async def test_session_other_project_409_ui_open_409(client, db, ic, clock, env):
    """П8: открытая заявка той же беседы из другого проекта — 409, не чужая заявка."""
    ch = await _make_chan(ic, env, clock, "-1001")
    item = await _ask(client, env["token"], ch["id"])
    other = insert_project(db, name="Initech")
    _set_project(db, ch["id"], other)
    r = await _ask(client, env["token"], ch["id"], status=409)
    assert r["detail"] == f"выход уже просит проект {env['project']} (заявка #{item['id']})"


async def test_session_declined_repeat_409_for_a_week(client, db, ic, clock, env):
    ch = await _make_chan(ic, env, clock, "-1001")
    ui = await _ui(client, db, clock)
    item = await _ask(client, env["token"], ch["id"])
    out = await _decide(client, ui, item["id"], "decline")
    assert out["status"] == "declined"
    r = await _ask(client, env["token"], ch["id"], status=409)
    assert r["detail"].startswith("владелец оставил беседу ")
    clock.advance_ms(leave_mod.DECLINE_MEMORY_MS + 1)
    again = await _ask(client, env["token"], ch["id"])
    assert again["id"] != item["id"]


async def test_session_limits_open_20_and_day_30(client, db, ic, clock, env):
    chans = [
        await _make_chan(ic, env, clock, f"-{2000 + i}") for i in range(leave_mod.NEW_PER_PROJECT_DAY + 1)
    ]
    for ch in chans[: leave_mod.OPEN_PER_PROJECT]:
        await _ask(client, env["token"], ch["id"])
    r = await _ask(client, env["token"], chans[leave_mod.OPEN_PER_PROJECT]["id"], status=429)
    assert "открытых" in r["detail"]
    db.write_sync(lambda conn: conn.execute("UPDATE main.leave_request SET status = 'failed'"))
    for ch in chans[leave_mod.OPEN_PER_PROJECT : leave_mod.NEW_PER_PROJECT_DAY]:
        await _ask(client, env["token"], ch["id"])
    db.write_sync(lambda conn: conn.execute("UPDATE main.leave_request SET status = 'failed'"))
    r = await _ask(client, env["token"], chans[-1]["id"], status=429)
    assert "за сутки" in r["detail"]
    clock.advance_ms(leave_mod.DAY_MS + 1)
    await _ask(client, env["token"], chans[-1]["id"])


# --- E1: ручка владельца и решение -------------------------------------------------------------------------------


async def test_ui_leave_new_approved_and_pending_to_approved(client, db, ic, clock, env):
    ui = await _ui(client, db, clock)
    ch = await _make_chan(ic, env, clock, "-1001", project=False)
    item = await _ui_leave(client, ui, ch["id"])
    assert (item["status"], item["origin"], item["project_id"]) == ("approved", "ui", None)
    assert item["decided_ts"] == _now_ms(clock) and item["leaving"] is True
    same = await _ui_leave(client, ui, ch["id"])
    assert same["id"] == item["id"] and same["status"] == "approved"
    # Просьба сессии -> кнопка владельца в шапке одобряет её.
    ch2 = await _make_chan(ic, env, clock, "-1002")
    asked = await _ask(client, env["token"], ch2["id"])
    approved = await _ui_leave(client, ui, ch2["id"])
    assert approved["id"] == asked["id"] and approved["status"] == "approved"
    assert [r for r, _a, _d in _audit(db, "leave_request_approved")] == ["approved"]
    # Bearer ручку владельца не зовёт.
    r = await client.post(f"{API}/channels/{ch['id']}/leave", headers=_bearer(env["token"]))
    assert r.status_code == 403
    # Личка — 422, нет беседы — 404.
    dm = await _make_chan(ic, env, clock, "77", kind="dm")
    await _ui_leave(client, ui, dm["id"], status=422)
    await _ui_leave(client, ui, 999_999, status=404)


async def test_decide_bearer_403_approve_only_pending_decline_until_leased(client, db, ic, clock, env):
    ui = await _ui(client, db, clock)
    ch = await _make_chan(ic, env, clock, "-1001")
    item = await _ask(client, env["token"], ch["id"])
    for action in ("approve", "decline"):
        r = await client.post(f"{API}/leaves/{item['id']}/{action}", headers=_bearer(env["token"]))
        assert r.status_code == 403
    await _decide(client, ui, item["id"], "approve")
    await _decide(client, ui, item["id"], "approve", status=409)
    [job] = await _lease(ic, env["h"])
    r = await _decide(client, ui, item["id"], "decline", status=409)
    assert r["detail"] == "уже выхожу — оставить нельзя"
    await _report(ic, env["h"], job, result="left")
    await _decide(client, ui, item["id"], "decline", status=409)
    # Одобренную, но ещё не выданную — оставить можно.
    ch2 = await _make_chan(ic, env, clock, "-1002")
    item2 = await _ui_leave(client, ui, ch2["id"])
    assert item2["can_decline"] is True
    out = await _decide(client, ui, item2["id"], "decline")
    assert out["status"] == "declined" and out["can_decline"] is False
    await _decide(client, ui, 999_999, "approve", status=404)


# --- E1: очередь --------------------------------------------------------------------------------------------


async def test_queue_only_approved_own_account_and_gap(client, db, ic, clock, env):
    ui = await _ui(client, db, clock)
    _acc2, h2 = _account(db)
    c1 = await _make_chan(ic, env, clock, "-1001")
    c2 = await _make_chan(ic, env, clock, "-1002", kind="group")
    await _ask(client, env["token"], c1["id"])  # pending — не выдаётся
    assert await _lease(ic, env["h"]) == []
    r = await ic.get(f"{INTERNAL}/leave/wait", headers=env["h"], params={"timeout_s": 0})
    assert r.json() == {"ready": False}
    a = await _ui_leave(client, ui, c1["id"])
    b = await _ui_leave(client, ui, c2["id"])
    assert await _lease(ic, h2) == []  # чужой аккаунт
    r = await ic.get(f"{INTERNAL}/leave/wait", headers=env["h"], params={"timeout_s": 0})
    assert r.json() == {"ready": True}
    got = await _lease(ic, env["h"], limit=5)
    assert got == [
        {
            "id": a["id"],
            "lease_gen": 1,
            "step": "leave",
            "ext_id": "-1001",
            "chat_kind": "channel",
            "project_id": env["project"],
        }
    ]
    clock.advance_ms(leave_mod.LEAVE_GAP_MS - 1)
    assert await _lease(ic, env["h"]) == []
    clock.advance_ms(1)
    [second] = await _lease(ic, env["h"])
    assert (second["id"], second["chat_kind"]) == (b["id"], "group")


async def test_queue_floodwait_pauses_all_and_is_not_attempt(client, db, ic, clock, env):
    ui = await _ui(client, db, clock)
    c1 = await _make_chan(ic, env, clock, "-1001")
    c2 = await _make_chan(ic, env, clock, "-1002")
    a = await _ui_leave(client, ui, c1["id"])
    b = await _ui_leave(client, ui, c2["id"])
    [job] = await _lease(ic, env["h"])
    out = await _report(ic, env["h"], job, "retry", retry_after_ms=300_000)
    assert out["result"] == "paused"
    assert _req(db, a["id"]).attempts == 0
    assert _req(db, b["id"]).pause_until_ms == _now_ms(clock) + 300_000
    clock.advance_ms(299_000)
    assert await _lease(ic, env["h"]) == []
    # Новая заявка тоже ждёт паузу аккаунта.
    c3 = await _make_chan(ic, env, clock, "-1003")
    c = await _ui_leave(client, ui, c3["id"])
    assert _req(db, c["id"]).not_before_ms >= _req(db, b["id"]).pause_until_ms
    clock.advance_ms(1000)
    [again] = await _lease(ic, env["h"])
    assert again["id"] == a["id"] and again["lease_gen"] == 2


async def test_queue_attempts_gave_up_stale_and_foreign(client, db, ic, clock, env):
    ui = await _ui(client, db, clock)
    ch = await _make_chan(ic, env, clock, "-1001")
    item = await _ui_leave(client, ui, ch["id"])
    [job] = await _lease(ic, env["h"])
    # Устаревший lease_gen — 409, чужой аккаунт — 404, кривой исход — 422.
    await _report(ic, env["h"], {**job, "lease_gen": 99}, result="left", status=409)
    _acc2, h2 = _account(db)
    await _report(ic, h2, job, result="left", status=404)
    await _report(ic, env["h"], job, result="joined", status=422)
    for n in range(1, leave_mod.MAX_ATTEMPTS):
        out = await _report(ic, env["h"], job, "retry")
        assert out["result"] == "paused" and _req(db, item["id"]).attempts == n
        clock.advance_ms(leave_mod.backoff_ms(n) - 1)
        assert await _lease(ic, env["h"]) == []
        clock.advance_ms(1)
        [job] = await _lease(ic, env["h"])
    out = await _report(ic, env["h"], job, "retry")
    assert out["result"] == "failed"
    req = _req(db, item["id"])
    assert (req.status, req.error) == ("failed", "gave_up")
    assert [r for r, _a, d in _audit(db, "leave_request_failed") if d["leave_id"] == item["id"]] == ["failed"]
    assert _chan(db, "-1001")["archived"] == 0


async def test_queue_drop_failed_with_reason_and_blocked_account(client, db, ic, clock, env):
    ui = await _ui(client, db, clock)
    ch = await _make_chan(ic, env, clock, "-1001")
    item = await _ui_leave(client, ui, ch["id"])
    [job] = await _lease(ic, env["h"])
    out = await _report(ic, env["h"], job, "drop", reason="creator")
    assert out["result"] == "failed" and _req(db, item["id"]).error == "creator"
    assert _chan(db, "-1001")["archived"] == 0
    # После неудачи — можно снова (не «уже вышли»).
    again = await _ui_leave(client, ui, ch["id"])
    assert again["id"] != item["id"]
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.account SET status = 'auth_required' WHERE id = ?", (env["acc"],)
        )
    )
    clock.advance_ms(leave_mod.LEAVE_GAP_MS)
    assert await _lease(ic, env["h"]) == []


# --- E1: A5 — после выхода ----------------------------------------------------------------------------------


def _card(db, channel_id):
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.agent_item (channel_id, label, message_ids_json, last_message_id, covered_seq, "
            "status, provider, model, created_ts, updated_ts) "
            "VALUES (?, 'reply', '[]', 1, 1, 'new', 'p', 'm', 0, 0)",
            (channel_id,),
        )
    )


def _cards(db, channel_id):
    with db.reader() as conn:
        return [
            r[0]
            for r in conn.execute("SELECT status FROM main.agent_item WHERE channel_id = ?", (channel_id,))
        ]


def _chan_sub(db, *, channel_id, project_id, token_id):
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.session_chan_sub (channel_id, project_id, api_token_id, session_pid, "
                "session_proc_start, session_id, session_cwd, created_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, '/tmp', 0)",
                (channel_id, project_id, token_id, PID, START, SID),
            ).lastrowid
        )
    )


def _sub(db, *, project_id, token_id):
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.session_sub (project_id, api_token_id, session_pid, session_proc_start, "
                "session_id, session_cwd, created_ts, mode) VALUES (?, ?, ?, ?, ?, '/tmp', 0, 'messages')",
                (project_id, token_id, PID, START, SID),
            ).lastrowid
        )
    )


@pytest.fixture
def procs(monkeypatch):
    from hub.core import subs  # noqa: PLC0415

    table = {PID: START}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


async def test_done_archives_forum_and_topics_closes_cards_ends_subs(client, db, ic, clock, env, procs):
    ui = await _ui(client, db, clock)
    _sub(db, project_id=env["project"], token_id=env["token_id"])
    topic = await _make_chan(ic, env, clock, "-3000:1", kind="topic", parent="-3000")
    await _events(ic, env["h"], [_up("-3000:7", "2", _now_ms(clock), kind="topic", parent="-3000")])
    topic7 = _chan(db, "-3000:7")
    _set_project(db, topic7["id"], env["project"])
    # Сигнал подписке на проект по теме 7 ещё не выдан; карточка разборщика открыта; подписка на тему 1.
    await _events(ic, env["h"], [_up("-3000:7", "3", _now_ms(clock) + 1, kind="topic", parent="-3000")])
    with db.reader() as conn:
        assert conn.execute(
            "SELECT status FROM main.session_chan_signal WHERE channel_id = ?", (topic7["id"],)
        ).fetchall() == [("pending",)]
    _card(db, topic["id"])
    sub_id = _chan_sub(db, channel_id=topic["id"], project_id=env["project"], token_id=env["token_id"])
    other_sub = _chan_sub(db, channel_id=topic7["id"], project_id=env["project"], token_id=env["token_id"])

    item = await _ask(client, env["token"], topic["id"], reason="шум")
    await _decide(client, ui, item["id"], "approve")
    [job] = await _lease(ic, env["h"])
    assert job["ext_id"] == "-3000" and job["chat_kind"] == "group"
    await _report(ic, env["h"], job, result="not_member")
    req = _req(db, item["id"])
    assert (req.status, req.result, req.done_ts) == ("left", "not_member", _now_ms(clock))
    for ext in ("-3000", "-3000:1", "-3000:7"):
        assert _chan(db, ext)["archived"] == 1, ext
    assert _cards(db, topic["id"]) == ["done"]
    with db.reader() as conn:
        ended = dict(
            conn.execute(
                "SELECT id, end_reason FROM main.session_chan_sub WHERE id IN (?, ?)", (sub_id, other_sub)
            ).fetchall()
        )
        signals = conn.execute(
            "SELECT status, reason FROM main.session_chan_signal WHERE channel_id = ? AND project_sub_id "
            "IS NOT NULL",
            (topic7["id"],),
        ).fetchall()
    assert ended == {sub_id: "channel_gone", other_sub: "channel_gone"}
    assert signals == [("skipped", "left")]
    [(result, actor, detail)] = _audit(db, "leave_request_left")
    assert (result, actor, detail["leave_id"]) == ("not_member", "core", item["id"])
    # Уже вышли — обе ручки 409.
    r = await _ui_leave(client, ui, topic["parent"], status=409)
    assert r["detail"] == f"уже вышли из этой беседы (заявка #{item['id']})"
    await _ask(client, env["token"], topic["id"], status=409)


# --- E1: A6 и П2 — возврат из архива ------------------------------------------------------------------------


async def test_a6_new_message_after_leave_returns_channel(client, db, ic, clock, env):
    ui = await _ui(client, db, clock)
    ch, item = await _left(client, ic, env, clock, ui)
    done_ts = _req(db, item["id"]).done_ts
    # Хвост у выхода (служебное «вышел», запоздалый апдейт) — не возврат.
    await _events(ic, env["h"], [_up("-1001", "2", done_ts + leave_mod.RETURN_GRACE_MS)])
    assert _chan(db, "-1001")["archived"] == 1
    # Сообщение со временем ДО выхода, пришедшее позже, — тоже нет.
    clock.advance_ms(10 * 60_000)
    await _events(ic, env["h"], [_up("-1001", "3", done_ts - 1000)])
    assert _chan(db, "-1001")["archived"] == 1
    # Правка старого сообщения — не вставка.
    await _events(ic, env["h"], [{**_up("-1001", "1", _now_ms(clock)), "text": "правка"}])
    assert _chan(db, "-1001")["archived"] == 1
    await _events(ic, env["h"], [_up("-1001", "4", done_ts + leave_mod.RETURN_GRACE_MS + 1)])
    assert _chan(db, "-1001")["archived"] == 0
    [(_r, _a, detail)] = _audit(db, "leave_request_returned")
    assert detail["leave_id"] == item["id"] and detail["why"] == "message"
    # Вернулась — можно выйти снова.
    again = await _ui_leave(client, ui, ch["id"])
    assert again["id"] != item["id"]


async def test_a6_forum_new_topic_returns_parent_and_topics(client, db, ic, clock, env):
    ui = await _ui(client, db, clock)
    topic = await _make_chan(ic, env, clock, "-3000:1", kind="topic", parent="-3000")
    item = await _ui_leave(client, ui, topic["id"])
    assert item["channel_id"] == topic["parent"]
    [job] = await _lease(ic, env["h"])
    await _report(ic, env["h"], job, result="left")
    assert _chan(db, "-3000")["archived"] == 1 and _chan(db, "-3000:1")["archived"] == 1
    clock.advance_ms(3_600_000)
    # П3: новая тема, созданная, пока владельца не было, приходит без архива — форум возвращается целиком.
    await _events(ic, env["h"], [_up("-3000:9", "5", _now_ms(clock), kind="topic", parent="-3000")])
    for ext in ("-3000", "-3000:1", "-3000:9"):
        assert _chan(db, ext)["archived"] == 0, ext


async def test_a6_other_archive_untouched(client, db, ic, clock, env):
    """Архив Ф11-9c без заявки выхода `left` новое сообщение не снимает (приём архив не снимает, как было)."""
    ch = await _make_chan(ic, env, clock, "-1001")
    db.write_sync(
        lambda conn: channels_mod.update_channel(conn, ch["id"], archived=True, now_ms=_now_ms(clock))
    )
    clock.advance_ms(3_600_000)
    await _events(ic, env["h"], [_up("-1001", "9", _now_ms(clock))])
    assert _chan(db, "-1001")["archived"] == 1
    # Заявка выхода не удалась — тоже не возврат.
    ui = await _ui(client, db, clock)
    ch2 = await _make_chan(ic, env, clock, "-1002")
    item = await _ui_leave(client, ui, ch2["id"])
    [job] = await _lease(ic, env["h"])
    await _report(ic, env["h"], job, "drop", reason="forbidden")
    db.write_sync(
        lambda conn: channels_mod.update_channel(conn, ch2["id"], archived=True, now_ms=_now_ms(clock))
    )
    clock.advance_ms(3_600_000)
    await _events(ic, env["h"], [_up("-1002", "9", _now_ms(clock))])
    assert _chan(db, "-1002")["archived"] == 1
    assert _req(db, item["id"]).status == "failed"


async def _left_and_returned(client, db, ic, clock, env, ui, ext="-1001"):
    """Выход -> владелец вернулся в чат сам (в Telegram) -> пост после выхода вернул беседу из архива."""
    ch, item = await _left(client, ic, env, clock, ui, ext=ext)
    assert _chan(db, ext)["archived"] == 1 and _req(db, item["id"]).returned_ts is None
    clock.advance_ms(3_600_000)
    await _events(ic, env["h"], [_up(ext, "50", _now_ms(clock))])
    assert _chan(db, ext)["archived"] == 0
    assert _req(db, item["id"]).returned_ts == _now_ms(clock)
    return ch, item


async def test_a6_manual_archive_after_return_is_not_undone(client, db, ic, clock, env):
    """Ревью 17-2a: после возврата выход не в силе (`returned_ts`) — поздний архив владельца (UI или Ф11-9c)
    следующее сообщение не снимает."""
    ui = await _ui(client, db, clock)
    ch, item = await _left_and_returned(client, db, ic, clock, env, ui)
    clock.advance_ms(60_000)
    r = await client.patch(f"{API}/channels/{ch['id']}", json={"archived": True}, headers=ui)
    assert r.status_code == 200, r.text
    clock.advance_ms(3_600_000)
    await _events(ic, env["h"], [_up("-1001", "51", _now_ms(clock))])
    assert _chan(db, "-1001")["archived"] == 1
    assert len(_audit(db, "leave_request_returned")) == 1
    # UI видит возврат: `returned_ts` в `/leaves` и `/sync`.
    r = await client.get(f"{API}/leaves", params={"channel_id": ch["id"]}, headers=ui)
    [out] = r.json()
    assert out["id"] == item["id"] and out["status"] == "left" and out["returned_ts"] is not None
    r = await client.get(f"{API}/sync", params={"since": 0}, headers=ui)
    assert [x["returned_ts"] for x in r.json()["leaves"]] == [out["returned_ts"]]


async def test_a6_manual_archive_after_return_allows_new_leave(client, db, ic, clock, env):
    """Ревью 17-2a: владелец снова в чате, беседа в его архиве — просьба выйти не получает ложного 409 «уже
    вышли»; без возврата (выход в силе) — 409, как было."""
    ui = await _ui(client, db, clock)
    ch, _item = await _left_and_returned(client, db, ic, clock, env, ui)
    db.write_sync(
        lambda conn: channels_mod.update_channel(conn, ch["id"], archived=True, now_ms=_now_ms(clock))
    )
    again = await _ask(client, env["token"], ch["id"])
    assert again["status"] == "pending"
    still, left = await _left(client, ic, env, clock, ui, ext="-1002")
    r = await client.post(f"{API}/leaves", json={"channel_id": still["id"]}, headers=_bearer(env["token"]))
    assert r.status_code == 409 and f"#{left['id']}" in r.json()["detail"], r.text


async def test_a6_return_keeps_archive_made_before_leave(client, db, ic, clock, env):
    """Ревью 17-2a: возврат снимает архив только с того, что заархивировал выход (`archived_ids_json`), и с
    тем, появившихся после выхода; тема, убранная владельцем в архив до выхода, остаётся в архиве."""
    ui = await _ui(client, db, clock)
    t1 = await _make_chan(ic, env, clock, "-3000:1", kind="topic", parent="-3000")
    await _events(ic, env["h"], [_up("-3000:2", "2", _now_ms(clock), kind="topic", parent="-3000")])
    t2 = _chan(db, "-3000:2")
    _set_project(db, t2["id"], env["project"])
    db.write_sync(
        lambda conn: channels_mod.update_channel(conn, t2["id"], archived=True, now_ms=_now_ms(clock))
    )
    item = await _ui_leave(client, ui, t1["id"])
    [job] = await _lease(ic, env["h"])
    await _report(ic, env["h"], job, result="left")
    assert json.loads(_req(db, item["id"]).archived_ids_json) == [t1["parent"], t1["id"]]
    clock.advance_ms(3_600_000)
    await _events(ic, env["h"], [_up("-3000:9", "9", _now_ms(clock), kind="topic", parent="-3000")])
    assert _chan(db, "-3000")["archived"] == 0 and _chan(db, "-3000:1")["archived"] == 0
    assert _chan(db, "-3000:9")["archived"] == 0
    assert _chan(db, "-3000:2")["archived"] == 1


async def test_a6_return_keeps_chat_archived_before_leave(client, db, ic, clock, env):
    """Беседа была в архиве владельца ещё до выхода: выход её не архивировал — возврат её не снимает, но выход
    больше не в силе (`returned_ts`)."""
    ui = await _ui(client, db, clock)
    ch = await _make_chan(ic, env, clock, "-1001")
    db.write_sync(
        lambda conn: channels_mod.update_channel(conn, ch["id"], archived=True, now_ms=_now_ms(clock))
    )
    item = await _ui_leave(client, ui, ch["id"])
    [job] = await _lease(ic, env["h"])
    await _report(ic, env["h"], job, result="left")
    assert json.loads(_req(db, item["id"]).archived_ids_json) == []
    clock.advance_ms(3_600_000)
    await _events(ic, env["h"], [_up("-1001", "9", _now_ms(clock))])
    assert _chan(db, "-1001")["archived"] == 1
    assert _req(db, item["id"]).returned_ts is not None
    assert _audit(db, "leave_request_returned") == []


async def _join_to_report(client, ic, env, clock, ui, ext_id):
    r = await client.post(
        f"{API}/joins",
        json={"project_id": env["project"], "target": "@some_chan"},
        headers=_bearer(env["token"]),
    )
    assert r.status_code == 201, r.text
    join = r.json()
    r = await ic.post(f"{INTERNAL}/join/lease", headers=env["h"], json={})
    [peek] = r.json()
    return join, peek


async def test_p2_join_again_returns_immediately(client, db, ic, clock, env):
    ui = await _ui(client, db, clock)
    _ch, left = await _left(client, ic, env, clock, ui, ext="-1001")
    clock.advance_ms(60_000)
    join, peek = await _join_to_report(client, ic, env, clock, ui, "-1001")
    body = {
        "id": peek["id"],
        "lease_gen": peek["lease_gen"],
        "outcome": "done",
        "peek": {"title": "К", "kind": "broadcast", "member": False, "ext_id": "-1001"},
    }
    r = await ic.post(f"{INTERNAL}/join/report", headers=env["h"], json=body)
    assert r.json()["result"] == "pending"
    assert _chan(db, "-1001")["archived"] == 1
    r = await client.post(f"{API}/joins/{join['id']}/approve", headers=ui)
    assert r.status_code == 200, r.text
    clock.advance_ms(60_000)
    r = await ic.post(f"{INTERNAL}/join/lease", headers=env["h"], json={})
    [step] = r.json()
    body = {
        "id": step["id"],
        "lease_gen": step["lease_gen"],
        "outcome": "done",
        "result": "joined",
        "ext_id": "-1001",
    }
    r = await ic.post(f"{INTERNAL}/join/report", headers=env["h"], json=body)
    assert r.json()["result"] == "joined"
    assert _chan(db, "-1001")["archived"] == 0
    assert [d["why"] for _r, _a, d in _audit(db, "leave_request_returned")] == ["join"]
    assert _req(db, left["id"]).returned_ts is not None


async def test_p2_join_already_from_peek_returns(client, db, ic, clock, env):
    ui = await _ui(client, db, clock)
    await _left(client, ic, env, clock, ui, ext="-1001")
    _join, peek = await _join_to_report(client, ic, env, clock, ui, "-1001")
    body = {
        "id": peek["id"],
        "lease_gen": peek["lease_gen"],
        "outcome": "done",
        "peek": {"title": "К", "kind": "broadcast", "member": True, "ext_id": "-1001"},
    }
    r = await ic.post(f"{INTERNAL}/join/report", headers=env["h"], json=body)
    assert r.json()["result"] == "already"
    assert _chan(db, "-1001")["archived"] == 0


# --- E1: П6 — гонка вступления и выхода ---------------------------------------------------------------------


async def test_p6_leave_blocked_by_open_join_and_join_approve_blocked_by_leave(client, db, ic, clock, env):
    ui = await _ui(client, db, clock)
    ch = await _make_chan(ic, env, clock, "-1001")
    join, peek = await _join_to_report(client, ic, env, clock, ui, "-1001")
    body = {
        "id": peek["id"],
        "lease_gen": peek["lease_gen"],
        "outcome": "done",
        "peek": {"title": "К", "kind": "broadcast", "member": False, "ext_id": "-1001"},
    }
    r = await ic.post(f"{INTERNAL}/join/report", headers=env["h"], json=body)
    assert r.json()["result"] == "pending"
    r = await _ui_leave(client, ui, ch["id"], status=409)
    assert r["detail"] == f"по этой беседе идёт вступление (заявка #{join['id']})"
    r = await _ask(client, env["token"], ch["id"], status=409)
    assert r["detail"] == f"по этой беседе идёт вступление (заявка #{join['id']})"
    r = await client.post(f"{API}/joins/{join['id']}/decline", headers=ui)
    assert r.status_code == 200
    # Обратное направление: выход открыт — «Вступить» 409.
    leave = await _ask(client, env["token"], ch["id"])
    clock.advance_ms(60_000)
    r = await client.post(
        f"{API}/joins",
        json={"project_id": env["project"], "target": "@other_chan"},
        headers=_bearer(env["token"]),
    )
    join2 = r.json()
    clock.advance_ms(60_000)
    r = await ic.post(f"{INTERNAL}/join/lease", headers=env["h"], json={})
    [peek2] = r.json()
    body = {
        "id": peek2["id"],
        "lease_gen": peek2["lease_gen"],
        "outcome": "done",
        "peek": {"title": "К", "kind": "broadcast", "member": False, "ext_id": "-1001"},
    }
    r = await ic.post(f"{INTERNAL}/join/report", headers=env["h"], json=body)
    assert r.json()["result"] == "pending"
    r = await client.post(f"{API}/joins/{join2['id']}/approve", headers=ui)
    assert r.status_code == 409 and r.json()["detail"] == f"по этой беседе идёт выход (заявка #{leave['id']})"
    # И одобрение выхода при открытом вступлении — тоже 409.
    await _decide(client, ui, leave["id"], "approve", status=409)


async def test_p6_auto_join_goes_to_owner_while_leave_open(client, db, ic, clock, env, procs):
    from hub.core import join as join_mod  # noqa: PLC0415

    db.write_sync(lambda conn: join_mod.set_project_join(conn, env["project"], "auto"))
    _sub(db, project_id=env["project"], token_id=env["token_id"])
    ch = await _make_chan(ic, env, clock, "-1001")
    await _ask(client, env["token"], ch["id"])
    session = {"pid": PID, "proc_start": START, "session_id": SID}
    r = await client.post(
        f"{API}/joins",
        json={"project_id": env["project"], "target": "@some_chan", "session": session},
        headers=_bearer(env["token"]),
    )
    assert r.status_code == 201 and r.json()["status"] == "checking"
    r = await ic.post(f"{INTERNAL}/join/lease", headers=env["h"], json={})
    [peek] = r.json()
    body = {
        "id": peek["id"],
        "lease_gen": peek["lease_gen"],
        "outcome": "done",
        "peek": {"title": "К", "kind": "broadcast", "member": False, "ext_id": "-1001"},
    }
    r = await ic.post(f"{INTERNAL}/join/report", headers=env["h"], json=body)
    assert r.json()["result"] == "pending"


# --- E1: A7 — выдача, `/sync`, кадр SSE ---------------------------------------------------------------------


async def test_list_sync_visibility_and_channel_filter(client, db, ic, clock, env):
    ui = await _ui(client, db, clock)
    ch = await _make_chan(ic, env, clock, "-1001")
    asked = await _ask(client, env["token"], ch["id"], reason="там шум")
    free = await _make_chan(ic, env, clock, "-1002", project=False)
    mine = await _ui_leave(client, ui, free["id"])  # заявка владельца без проекта
    topic = await _make_chan(ic, env, clock, "-3000:1", kind="topic", parent="-3000")
    forum = await _ui_leave(client, ui, topic["id"])

    r = await client.get(f"{API}/sync", params={"since": 0}, headers=ui)
    leaves = r.json()["leaves"]
    assert [x["id"] for x in leaves] == [asked["id"], mine["id"], forum["id"]]
    assert leaves[0]["reason"] == "там шум" and leaves[0]["status"] == "pending"
    since = r.json()["max_seq"]
    await _decide(client, ui, asked["id"], "approve")
    r = await client.get(f"{API}/sync", params={"since": since}, headers=ui)
    assert [(x["id"], x["status"]) for x in r.json()["leaves"]] == [(asked["id"], "approved")]

    # Профиль «на все проекты» (с Неразобранным) видит и заявку без проекта; проектный — только свой проект.
    r = await client.get(f"{API}/leaves", headers=_bearer(env["token"]))
    assert {x["id"] for x in r.json()} == {asked["id"], mine["id"], forum["id"]}
    scoped = _token(db, name="mcp:iip", project_ids=[env["project"]])
    r = await client.get(f"{API}/leaves", headers=_bearer(scoped.token))
    assert {x["id"] for x in r.json()} == {asked["id"], forum["id"]}
    r = await client.get(f"{API}/sync", params={"since": 0}, headers=_bearer(scoped.token))
    assert {x["id"] for x in r.json()["leaves"]} == {asked["id"], forum["id"]}
    other = _token(db, name="mcp:initech", project_ids=[insert_project(db, name="Initech")])
    r = await client.get(f"{API}/leaves", headers=_bearer(other.token))
    assert r.json() == []
    # П12: фильтр по беседе; у темы — её форум.
    r = await client.get(f"{API}/leaves", params={"channel_id": topic["id"]}, headers=ui)
    assert [x["id"] for x in r.json()] == [forum["id"]]
    r = await client.get(f"{API}/leaves", params={"channel_id": ch["id"], "status": "open"}, headers=ui)
    assert [x["id"] for x in r.json()] == [asked["id"]]
    # Закрытые старше 7 дней по умолчанию не выдаются.
    await _decide(client, ui, mine["id"], "decline")
    clock.advance_ms(leave_mod.LIST_RECENT_MS + 1)
    r = await client.get(f"{API}/leaves", headers=ui)
    assert mine["id"] not in {x["id"] for x in r.json()}


def test_leave_frame_has_no_texts():
    from hub.core.feed import _leave_frame  # noqa: PLC0415

    frame = _leave_frame(
        {"id": 3, "project_id": None, "status": "approved", "seq": 77, "title": "секрет", "reason": "причина"}
    )
    assert frame.startswith("id: 77\nevent: leave\n")
    assert "секрет" not in frame and "причина" not in frame
    assert json.loads(frame.split("data: ", 1)[1]) == {
        "id": 3,
        "project_id": None,
        "status": "approved",
        "seq": 77,
    }


async def test_stream_emits_leave_frame(client, db, ic, clock, env):
    from hub.core import feed, queries  # noqa: PLC0415

    ui = await _ui(client, db, clock)
    ch = await _make_chan(ic, env, clock, "-1001")
    with db.reader() as conn:
        start = conn.execute("SELECT value FROM main.seq_counter WHERE id = 1").fetchone()[0]
    item = await _ui_leave(client, ui, ch["id"])
    principal = security.Principal(token_id=0, kind="ui", scopes=frozenset({"read"}), project_ids=None)
    with db.reader() as conn:
        changes = queries.changes_since(conn, principal, since=start, limit=50)
    frames = [f for f in feed._frames(changes) if "event: leave" in f]  # noqa: SLF001
    assert len(frames) == 1 and f'"id": {item["id"]}' in frames[0]
