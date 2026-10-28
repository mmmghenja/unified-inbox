"""Ф17-1 (docs/architecture.md–7, с поправками П1–П7 — они главнее текста спеки):
заявки сессий на вступление в группы и каналы Telegram — ядро и ручки.

Сессия — bearer `interactive` со скоупом `draft` (и `session` в теле для автоодобрения; живость процесса
подменяется `subs.read_proc_start`). Коннектор — внутреннее приложение с секретом аккаунта на той же БД
и тех же часах: `wait`/`lease`/`report`. Владелец — UI-сессия (вход кодом)."""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import connector_auth, security, subs
from hub.core import join as join_mod
from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.feed import Notifier

from .conftest import ORIGIN, csrf_headers, insert_project, login_via_code

API = "/api/v1"
INTERNAL = "/internal/v1"
ADMIN_SECRET = ADMIN_PREFIX + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHI"
PID, START, SID = 4242, 777_000, "sess-join-1111"
SESSION = {"pid": PID, "proc_start": START, "session_id": SID}


# --- обвязка ------------------------------------------------------------------------------------------------


@pytest.fixture
def procs(monkeypatch):
    table = {PID: START}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


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


async def _create(client, token, project_id, target, *, status=201, **extra):
    body = {"project_id": project_id, "target": target, **extra}
    r = await client.post(f"{API}/joins", json=body, headers=_bearer(token))
    assert r.status_code == status, r.text
    return r.json()


async def _lease(ic, h, limit=None):
    body = {} if limit is None else {"limit": limit}
    r = await ic.post(f"{INTERNAL}/join/lease", headers=h, json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def _report(ic, h, item, outcome="done", *, status=200, **extra):
    body = {"id": item["id"], "lease_gen": item["lease_gen"], "outcome": outcome, **extra}
    r = await ic.post(f"{INTERNAL}/join/report", headers=h, json=body)
    assert r.status_code == status, r.text
    return r.json()


async def _decide(client, ui, join_id, action, *, status=200):
    r = await client.post(f"{API}/joins/{join_id}/{action}", headers=ui)
    assert r.status_code == status, r.text
    return r.json()


def _req(db, join_id) -> join_mod.Req:
    with db.reader() as conn:
        req = join_mod.get(conn, join_id)
    assert req is not None
    return req


def _audit(db, action):
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT result, actor, detail_json FROM main.audit WHERE action = ? ORDER BY id", (action,)
        ).fetchall()
    return [(r[0], r[1], json.loads(r[2])) for r in rows]


def _peek(ext_id=None, *, member=False, forum=False, title="Канал", kind="broadcast"):
    return {
        "title": title,
        "kind": kind,
        "participants": 12_400,
        "about": "описание",
        "request_needed": False,
        "member": member,
        "forum": forum,
        "ext_id": ext_id,
    }


def _up(ch, ext_id, ts, *, kind="channel", parent=None):
    channel: dict = {"ext_id": ch, "kind": kind}
    if parent is not None:
        channel["parent_ext_id"] = parent
        channel["parent"] = {"ext_id": parent, "kind": "group"}
    return {
        "type": "upsert",
        "channel": channel,
        "ext_id": ext_id,
        "ts": ts,
        "is_me": False,
        "kind": "text",
        "text": "t",
        "mentions_me": False,
        "sender": {"ext_id": "u1", "name": "u1", "handle": "u1", "is_me": False},
    }


async def _events(ic, h, events):
    r = await ic.post(f"{INTERNAL}/events", headers=h, json={"events": events})
    assert r.status_code == 200 and r.json()["dead"] == 0, r.text


def _channel(db, ext_id, schema="main") -> dict | None:
    with db.reader() as conn:
        cur = conn.execute(
            f"SELECT id, project_id, map_source, map_reason, muted, kind FROM {schema}.channel "  # noqa: S608
            "WHERE ext_id = ?",
            (ext_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return dict(zip(("id", "project_id", "map_source", "map_reason", "muted", "kind"), row, strict=True))


def _sub(db, *, project_id, token_id, mode="messages"):
    db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.session_sub (project_id, api_token_id, session_pid, session_proc_start, "
                "session_id, session_cwd, created_ts, mode) VALUES (?, ?, ?, ?, ?, '/tmp', 0, ?)",
                (project_id, token_id, PID, START, SID, mode),
            ).lastrowid
        )
    )


def _chan_sub(db, *, channel_id, project_id, token_id):
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.session_chan_sub (channel_id, project_id, api_token_id, session_pid, "
            "session_proc_start, session_id, session_cwd, created_ts) VALUES (?, ?, ?, ?, ?, ?, '/tmp', 0)",
            (channel_id, project_id, token_id, PID, START, SID),
        )
    )


def _signals(db):
    with db.reader() as conn:
        return conn.execute(
            "SELECT chan_sub_id, project_sub_id, channel_id, status, reason FROM main.session_chan_signal "
            "ORDER BY id"
        ).fetchall()


def _policy(db, project_id, mode):
    db.write_sync(lambda conn: join_mod.set_project_join(conn, project_id, mode))


@pytest.fixture
async def env(db, ic, clock):
    """Проект Umbrella, аккаунт Telegram с секретом коннектора, токен сессии `mcp:all` с `draft`."""
    project = insert_project(db, name="Umbrella")
    acc, h = _account(db)
    created = _token(db)
    return {"project": project, "acc": acc, "h": h, "token": created.token, "token_id": created.token_id}


async def _to_pending(client, ic, env, clock, target="@some_channel", ext_id=None, **peek_kw):
    """Создать заявку и пройти шаг `peek` до `pending`. Возвращает заявку (после отчёта)."""
    item = await _create(client, env["token"], env["project"], target)
    leased = await _lease(ic, env["h"])
    assert [x["id"] for x in leased] == [item["id"]] and leased[0]["step"] == "peek"
    await _report(ic, env["h"], leased[0], peek=_peek(ext_id, **peek_kw))
    return item


async def _to_join_leased(client, ic, env, clock, ui, **kw) -> tuple[dict, dict]:
    """Заявка -> `pending` -> «Вступить» -> выдан шаг `join`. Возвращает (заявка, задание)."""
    item = await _to_pending(client, ic, env, clock, **kw)
    await _decide(client, ui, item["id"], "approve")
    leased = await _lease(ic, env["h"])
    assert leased and leased[0]["step"] == "join"
    return item, leased[0]


# --- F2: создание -------------------------------------------------------------------------------------------


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
async def test_f2_check_attach_denied_403(client, db, env, name, kind, scopes):
    created = _token(db, name=name, kind=kind, scopes=scopes)
    r = await _create(client, created.token, env["project"], "@some_channel", status=403)
    assert "интерактивной сессии" in r["detail"]
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.join_request").fetchone()[0] == 0


async def test_f2_ui_session_and_read_only_refused(client, db, clock, env):
    ui = await _ui(client, db, clock)
    body = {"project_id": env["project"], "target": "@some_channel"}
    r = await client.post(f"{API}/joins", json=body, headers=ui)
    assert r.status_code == 403
    reader = _token(db, name="mcp:reader", scopes=("read",))
    r = await client.post(
        f"{API}/joins",
        json={"project_id": env["project"], "target": "@x_channel"},
        headers=_bearer(reader.token),
    )
    assert r.status_code == 403


async def test_f2_foreign_project_404_unassigned_422(client, db, env):
    other = insert_project(db, name="Initech")
    scoped = _token(db, name="mcp:iip", project_ids=[env["project"]])
    r = await _create(client, scoped.token, other, "@some_channel", status=404)
    assert "не виден" in r["detail"]
    await _create(client, scoped.token, 999, "@some_channel", status=404)
    await _create(client, env["token"], 0, "@some_channel", status=422)
    item = await _create(client, scoped.token, env["project"], "@some_channel")
    assert item["status"] == "checking" and item["project_id"] == env["project"]


async def test_f2_accounts_choice(client, db, env):
    second, _ = _account(db)
    slack, _ = _account(db, source="slack", db_name="work")
    r = await _create(client, env["token"], env["project"], "@some_channel", status=422)
    assert f"{env['acc']}, {second}" in r["detail"]
    r = await _create(client, env["token"], env["project"], "@some_channel", status=422, account_id=slack)
    assert "Telegram" in r["detail"]
    await _create(client, env["token"], env["project"], "@some_channel", status=422, account_id=9999)
    item = await _create(client, env["token"], env["project"], "@some_channel", account_id=second)
    assert item["account_id"] == second


async def test_f2_idempotent_other_project_bad_target(client, db, env):
    first = await _create(client, env["token"], env["project"], "@Some_Channel", reason="клиенты тут")
    assert first["target_kind"] == "username" and first["target"] == "some_channel"
    again = await _create(client, env["token"], env["project"], "https://t.me/some_channel/15", status=200)
    assert again["id"] == first["id"] and again["reason"] == "клиенты тут"
    other = insert_project(db, name="Initech")
    r = await _create(client, env["token"], other, "@some_channel", status=409)
    assert f"уже просит проект {env['project']}" in r["detail"]
    r = await _create(client, env["token"], env["project"], "https://t.me/c/123/4", status=422)
    assert "закрытого чата" in r["detail"]
    await _create(client, env["token"], env["project"], "@some_channel2", status=422, reason="x" * 501)
    assert len(_audit(db, "join_create")) == 1


async def test_f2_limits_open_20_and_day_50(client, db, clock, env):
    def _close_all():
        db.write_sync(lambda conn: conn.execute("UPDATE main.join_request SET status = 'failed'"))

    n = 0
    for _ in range(join_mod.OPEN_PER_PROJECT):
        await _create(client, env["token"], env["project"], f"@chan_{n:04d}")
        n += 1
    r = await _create(client, env["token"], env["project"], f"@chan_{n:04d}", status=429)
    assert "открытых заявок" in r["detail"]
    while n < join_mod.NEW_PER_PROJECT_DAY:
        _close_all()
        for _ in range(min(join_mod.OPEN_PER_PROJECT, join_mod.NEW_PER_PROJECT_DAY - n)):
            await _create(client, env["token"], env["project"], f"@chan_{n:04d}")
            n += 1
    _close_all()
    r = await _create(client, env["token"], env["project"], f"@chan_{n:04d}", status=429)
    assert "за сутки" in r["detail"]
    clock.advance_ms(join_mod.DAY_MS + 1)
    await _create(client, env["token"], env["project"], f"@chan_{n:04d}")


async def test_f2_declined_repeat_409_for_a_week(client, ic, db, clock, env):
    ui = await _ui(client, db, clock)
    item = await _to_pending(client, ic, env, clock)
    await _decide(client, ui, item["id"], "decline")
    r = await _create(client, env["token"], env["project"], "t.me/some_channel", status=409)
    assert r["detail"].startswith("владелец отказал ")
    clock.advance_ms(join_mod.DECLINE_MEMORY_MS + 1)
    fresh = await _create(client, env["token"], env["project"], "t.me/some_channel")
    assert fresh["id"] != item["id"] and fresh["status"] == "checking"


# --- F3: очередь --------------------------------------------------------------------------------------------


async def test_f3_peek_to_pending_sync_and_list(client, ic, db, clock, env):
    item = await _create(client, env["token"], env["project"], "@some_channel", reason="там клиенты")
    r = await ic.get(f"{INTERNAL}/join/wait", headers=env["h"], params={"timeout_s": 0})
    assert r.json() == {"ready": True}
    leased = await _lease(ic, env["h"], limit=5)
    assert leased == [
        {
            "id": item["id"],
            "lease_gen": 1,
            "step": "peek",
            "target_kind": "username",
            "target": "some_channel",
            "ext_id": None,
            "project_id": env["project"],
        }
    ]
    assert await _lease(ic, env["h"]) == []
    out = await _report(ic, env["h"], leased[0], peek=_peek("-1001", title="Канал " + "я" * 300))
    assert out == {"ok": True, "result": "pending"}
    req = _req(db, item["id"])
    assert req.status == "pending" and req.ext_id == "-1001" and req.title == "Канал " + "я" * 250
    assert json.loads(req.peek_json)["participants"] == 12_400
    assert await _lease(ic, env["h"]) == []  # `pending` ждёт владельца — коннектору нечего

    ui = await _ui(client, db, clock)
    r = await client.get(f"{API}/sync", params={"since": 0}, headers=ui)
    joins = r.json()["joins"]
    got = [(j["id"], j["status"], j["peek"]["kind"]) for j in joins]
    assert got == [(item["id"], "pending", "broadcast")]
    assert joins[0]["can_decline"] is True and joins[0]["reason"] == "там клиенты"
    assert "session_json" not in joins[0] and "token_id" not in joins[0]
    since = r.json()["max_seq"]
    await _decide(client, ui, item["id"], "approve")
    r = await client.get(f"{API}/sync", params={"since": since}, headers=ui)
    assert [(j["id"], j["status"]) for j in r.json()["joins"]] == [(item["id"], "approved")]

    # Чужой проект bearer-у не виден ни в списке, ни в `/sync`.
    other = insert_project(db, name="Initech")
    scoped = _token(db, name="mcp:initech", project_ids=[other])
    r = await client.get(f"{API}/joins", headers=_bearer(scoped.token))
    assert r.status_code == 200 and r.json() == []
    r = await client.get(f"{API}/sync", params={"since": 0}, headers=_bearer(scoped.token))
    assert r.json()["joins"] == []
    r = await client.get(f"{API}/joins", params={"status": "open"}, headers=_bearer(env["token"]))
    assert [j["id"] for j in r.json()] == [item["id"]]
    assert _audit(db, "join_create")[0][2]["join_id"] == item["id"]


async def test_f3_drop_failed_with_reason(client, ic, db, clock, env):
    item = await _create(client, env["token"], env["project"], "@some_channel")
    leased = await _lease(ic, env["h"])
    assert (await _report(ic, env["h"], leased[0], "drop", reason="not_found"))["result"] == "failed"
    req = _req(db, item["id"])
    assert req.status == "failed" and req.error == "not_found" and req.done_ts == _now_ms(clock)
    done = [(res, detail["error"]) for res, _a, detail in _audit(db, "join_done")]
    assert done == [("failed", "not_found")]
    await _report(ic, env["h"], leased[0], "drop", reason="again", status=409)


async def test_f3_retry_backoff_then_gave_up(client, ic, db, clock, env):
    item = await _create(client, env["token"], env["project"], "@some_channel")
    for attempt in range(1, join_mod.MAX_ATTEMPTS):
        leased = await _lease(ic, env["h"])
        assert leased and leased[0]["id"] == item["id"]
        assert (await _report(ic, env["h"], leased[0], "retry"))["result"] == "paused"
        req = _req(db, item["id"])
        assert req.attempts == attempt and req.status == "checking"
        assert req.not_before_ms == _now_ms(clock) + join_mod.backoff_ms(attempt)
        clock.advance_ms(join_mod.backoff_ms(attempt) - 1)
        assert await _lease(ic, env["h"]) == []
        clock.advance_ms(1)
    leased = await _lease(ic, env["h"])
    assert (await _report(ic, env["h"], leased[0], "retry"))["result"] == "failed"
    req = _req(db, item["id"])
    assert req.status == "failed" and req.error == "gave_up"


async def test_f3_floodwait_pauses_whole_account_not_an_attempt(client, ic, db, clock, env):
    first = await _create(client, env["token"], env["project"], "@first_channel")
    second = await _create(client, env["token"], env["project"], "@second_channel")
    leased = await _lease(ic, env["h"])
    assert leased[0]["id"] == first["id"]
    await _report(ic, env["h"], leased[0], "retry", retry_after_ms=600_000)
    until = _now_ms(clock) + 600_000
    for join_id in (first["id"], second["id"]):
        req = _req(db, join_id)
        assert req.not_before_ms == until and req.pause_until_ms == until and req.attempts == 0
    # Новая заявка пауза аккаунта не обходит.
    third = await _create(client, env["token"], env["project"], "@third_channel")
    assert _req(db, third["id"]).not_before_ms == until
    r = await ic.get(f"{INTERNAL}/join/wait", headers=env["h"], params={"timeout_s": 0})
    assert r.json() == {"ready": False}
    clock.advance_ms(600_000 - 1)
    assert await _lease(ic, env["h"]) == []
    clock.advance_ms(1)
    assert [x["id"] for x in await _lease(ic, env["h"])] == [first["id"]]


async def test_f3_peek_gap_and_join_gap(client, ic, db, clock, env):
    ui = await _ui(client, db, clock)
    a = await _create(client, env["token"], env["project"], "@first_channel")
    b = await _create(client, env["token"], env["project"], "@second_channel")
    assert [x["id"] for x in await _lease(ic, env["h"], limit=5)] == [a["id"]]
    clock.advance_ms(join_mod.PEEK_GAP_MS - 1)
    assert await _lease(ic, env["h"]) == []
    clock.advance_ms(1)
    [lb] = await _lease(ic, env["h"])
    assert lb["id"] == b["id"]
    await _report(ic, env["h"], lb, peek=_peek("-1002"))
    # `a` — лиз истёк (коннектор пропал): выдаётся снова, новым поколением.
    clock.advance_ms(join_mod.LEASE_MS)
    [la] = await _lease(ic, env["h"])
    assert la["id"] == a["id"] and la["lease_gen"] == 2
    await _report(ic, env["h"], la, peek=_peek("-1001"))
    await _decide(client, ui, a["id"], "approve")
    await _decide(client, ui, b["id"], "approve")
    [ja] = await _lease(ic, env["h"], limit=5)
    assert ja["id"] == a["id"] and ja["step"] == "join"  # по одному; раньше одобренная — первой
    await _report(ic, env["h"], ja, result="joined", ext_id="-1001")
    clock.advance_ms(join_mod.JOIN_GAP_MS - 1)
    assert await _lease(ic, env["h"]) == []
    clock.advance_ms(1)
    [jb] = await _lease(ic, env["h"])
    assert jb["id"] == b["id"]


async def test_f3_one_join_step_in_flight_per_account(client, ic, db, clock, env):
    """Лиз шага `join` истёк и выдан снова (`join_leased_ts` — первый, темп уже прошёл): второе одобренное
    вступление ждёт отчёта по первому или конца его нового лиза — два вступления сразу не идут."""
    ui = await _ui(client, db, clock)
    a = await _to_pending(client, ic, env, clock, target="@first_channel")
    clock.advance_ms(join_mod.PEEK_GAP_MS)
    b = await _to_pending(client, ic, env, clock, target="@second_channel")
    await _decide(client, ui, a["id"], "approve")
    await _decide(client, ui, b["id"], "approve")
    [ja] = await _lease(ic, env["h"], limit=5)
    assert ja["id"] == a["id"]
    clock.advance_ms(join_mod.LEASE_MS)
    again = await _lease(ic, env["h"], limit=5)
    assert [(x["id"], x["lease_gen"]) for x in again] == [(a["id"], ja["lease_gen"] + 1)]
    await _report(ic, env["h"], again[0], result="joined")
    assert [x["id"] for x in await _lease(ic, env["h"], limit=5)] == [b["id"]]


async def test_f3_stale_lease_409_foreign_account_404(client, ic, db, clock, env):
    item = await _create(client, env["token"], env["project"], "@some_channel")
    [old] = await _lease(ic, env["h"])
    clock.advance_ms(join_mod.LEASE_MS)
    [new] = await _lease(ic, env["h"])
    assert new["lease_gen"] == old["lease_gen"] + 1
    await _report(ic, env["h"], old, peek=_peek(), status=409)
    _other, h2 = _account(db)
    await _report(ic, h2, new, peek=_peek(), status=404)
    await _report(ic, env["h"], new, "done", status=422)  # `done` шага `peek` без предпросмотра
    await _report(ic, env["h"], new, peek=_peek())
    assert _req(db, item["id"]).status == "pending"


async def test_f3_blocked_account_gets_nothing(client, ic, db, clock, env):
    await _create(client, env["token"], env["project"], "@some_channel")
    for status in ("auth_required", "limited"):
        db.write_sync(lambda conn, s=status: conn.execute("UPDATE main.account SET status = ?", (s,)))
        assert await _lease(ic, env["h"]) == []
        r = await ic.get(f"{INTERNAL}/join/wait", headers=env["h"], params={"timeout_s": 0})
        assert r.json() == {"ready": False}
    db.write_sync(lambda conn: conn.execute("UPDATE main.account SET status = 'ok'"))
    assert len(await _lease(ic, env["h"])) == 1


# --- F4: решение владельца ---------------------------------------------------------------------------------------


async def test_f4_approve_only_from_pending(client, ic, db, clock, env):
    ui = await _ui(client, db, clock)
    item = await _create(client, env["token"], env["project"], "@some_channel")
    r = await _decide(client, ui, item["id"], "approve", status=409)
    assert "ждущую решения" in r["detail"]
    [leased] = await _lease(ic, env["h"])
    await _report(ic, env["h"], leased, peek=_peek())
    approved = await _decide(client, ui, item["id"], "approve")
    assert approved["status"] == "approved" and approved["auto"] is False and approved["can_decline"] is True
    await _decide(client, ui, item["id"], "approve", status=409)
    await _decide(client, ui, 9999, "approve", status=404)
    assert [(res, actor) for res, actor, _d in _audit(db, "join_decide")] == [("approved", "ui")]


async def test_f4_decline_before_join_step_yes_after_409(client, ic, db, clock, env):
    ui = await _ui(client, db, clock)
    first = await _to_pending(client, ic, env, clock, target="@first_channel")
    await _decide(client, ui, first["id"], "approve")
    declined = await _decide(client, ui, first["id"], "decline")
    assert declined["status"] == "declined" and declined["can_decline"] is False
    assert _req(db, first["id"]).done_ts == _now_ms(clock)

    clock.advance_ms(join_mod.PEEK_GAP_MS)
    second, job = await _to_join_leased(client, ic, env, clock, ui, target="@second_channel")
    r = await _decide(client, ui, second["id"], "decline", status=409)
    assert r["detail"] == "уже вступаю — отказать нельзя"
    # П2: и после истечения лиза (коннектор пропал) — отказать уже нельзя.
    clock.advance_ms(join_mod.LEASE_MS)
    await _decide(client, ui, second["id"], "decline", status=409)
    listed = await client.get(f"{API}/joins", params={"status": "approved"}, headers=ui)
    assert listed.json()[0]["joining"] is True and listed.json()[0]["can_decline"] is False
    del job


async def test_f4_bearer_cannot_decide(client, ic, db, clock, env):
    item = await _to_pending(client, ic, env, clock)
    for action in ("approve", "decline"):
        r = await client.post(f"{API}/joins/{item['id']}/{action}", headers=_bearer(env["token"]))
        assert r.status_code == 403
    assert _req(db, item["id"]).status == "pending"


async def test_f4_join_results(client, ic, db, clock, env):
    ui = await _ui(client, db, clock)
    item, job = await _to_join_leased(client, ic, env, clock, ui)
    await _report(ic, env["h"], job, status=422)  # `done` шага `join` без исхода
    out = await _report(ic, env["h"], job, result="requested", title="Группа")
    assert out["result"] == "requested"
    req = _req(db, item["id"])
    assert req.status == "requested" and req.title == "Группа" and req.done_ts == _now_ms(clock)
    assert [res for res, _a, _d in _audit(db, "join_done")] == ["requested"]


# --- F5: автоодобрение (решение 4) --------------------------------------------------------------------------


async def _auto_peek(client, ic, env, clock, target, *, session=SESSION):
    extra = {"session": session} if session is not None else {}
    item = await _create(client, env["token"], env["project"], target, **extra)
    clock.advance_ms(join_mod.PEEK_GAP_MS)
    [leased] = await _lease(ic, env["h"])
    assert leased["id"] == item["id"] and leased["step"] == "peek"
    out = await _report(ic, env["h"], leased, peek=_peek())
    return item, out["result"]


async def test_f5_switch_off_pending(client, ic, db, clock, env, procs):
    _sub(db, project_id=env["project"], token_id=env["token_id"])
    _item, result = await _auto_peek(client, ic, env, clock, "@first_channel")
    assert result == "pending"


async def test_f5_not_subscribed_session_pending(client, ic, db, clock, env, procs):
    _policy(db, env["project"], "auto")
    other = insert_project(db, name="Initech")
    _item, result = await _auto_peek(client, ic, env, clock, "@first_channel", session=None)
    assert result == "pending"
    # Подписка на ДРУГОЙ проект — не в счёт.
    _sub(db, project_id=other, token_id=env["token_id"])
    _item, result = await _auto_peek(client, ic, env, clock, "@second_channel")
    assert result == "pending"
    # Подписана, но процесс мёртв (другой `starttime`).
    _sub(db, project_id=env["project"], token_id=env["token_id"])
    procs[PID] = START + 1
    _item, result = await _auto_peek(client, ic, env, clock, "@third_channel")
    assert result == "pending"


async def test_f5_subscribed_auto_limit_and_gap(client, ic, db, clock, env, procs):
    _policy(db, env["project"], "auto")
    _sub(db, project_id=env["project"], token_id=env["token_id"])
    item, result = await _auto_peek(client, ic, env, clock, "@chan_0000")
    assert result == "approved"
    req = _req(db, item["id"])
    assert req.auto == 1 and req.auto_eligible == 1 and req.decided_ts == _now_ms(clock)
    assert _audit(db, "join_decide")[-1][2]["auto"] is True
    # Автоматический шаг `join` выдаётся сразу (первое вступление аккаунта).
    [job] = await _lease(ic, env["h"])
    assert job["id"] == item["id"] and job["step"] == "join"
    first_join = _now_ms(clock)
    await _report(ic, env["h"], job, result="joined", ext_id="-1000")
    # Вторая: одобрена, но вступление — не раньше AUTO_GAP после первого.
    item2, result = await _auto_peek(client, ic, env, clock, "@chan_0001")
    assert result == "approved"
    assert _req(db, item2["id"]).not_before_ms == first_join + join_mod.AUTO_GAP_MS
    assert await _lease(ic, env["h"]) == []
    for n in range(2, join_mod.AUTO_DAILY):
        _i, result = await _auto_peek(client, ic, env, clock, f"@chan_{n:04d}")
        assert result == "approved"
    # Шестая за сутки — к владельцу.
    item6, result = await _auto_peek(client, ic, env, clock, "@chan_0099")
    assert result == "pending" and _req(db, item6["id"]).auto == 0
    # Выключатель выключили после создания — к владельцу, хоть заявка и от подписанной сессии.
    clock.advance_ms(join_mod.DAY_MS)
    item7 = await _create(client, env["token"], env["project"], "@chan_0100", session=SESSION)
    _policy(db, env["project"], "confirm")
    clock.advance_ms(join_mod.PEEK_GAP_MS)
    leased = [x for x in await _lease(ic, env["h"], limit=10) if x["id"] == item7["id"]]
    assert leased and (await _report(ic, env["h"], leased[0], peek=_peek()))["result"] == "pending"


async def test_f5_project_policy_api_and_cli_views(client, db, clock, env):
    ui = await _ui(client, db, clock)
    r = await client.get(f"{API}/projects", headers=ui)
    mine = [p for p in r.json() if p["id"] == env["project"]][0]
    assert mine["session_join"] == "confirm"
    r = await client.patch(f"{API}/projects/{env['project']}", json={"session_join": "auto"}, headers=ui)
    assert r.status_code == 200, r.text
    with db.reader() as conn:
        assert join_mod.project_auto(conn, env["project"]) is True
    r = await client.patch(
        f"{API}/projects/{env['project']}", json={"session_join": "confirm"}, headers=_bearer(env["token"])
    )
    assert r.status_code == 403
    r = await client.patch(f"{API}/projects/{env['project']}", json={"session_join": "always"}, headers=ui)
    assert r.status_code == 422
    r = await client.get(f"{API}/projects", headers=ui)
    assert [p for p in r.json() if p["id"] == env["project"]][0]["session_join"] == "auto"


async def test_f5_admin_patch_session_join(ic, db, env):
    headers = {"Authorization": f"Bearer {ADMIN_SECRET}"}
    url = f"{INTERNAL}/admin/projects/{env['project']}"
    r = await ic.patch(url, json={"session_join": "auto"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["session_join"] == "auto"
    with db.reader() as conn:
        assert join_mod.project_auto(conn, env["project"]) is True


# --- F6: привязка беседы (A3, П1, П2, П4) -------------------------------------------------------------------


def _assert_bound(db, ext_id, env, join_id):
    ch = _channel(db, ext_id)
    assert ch is not None
    assert (ch["project_id"], ch["map_source"], ch["map_reason"], ch["muted"]) == (
        env["project"],
        "manual",
        f"вступление по заявке #{join_id}",
        1,
    )
    assert _req(db, join_id).channel_id == ch["id"]
    return ch


async def test_f6_report_before_history(client, ic, db, clock, env):
    ui = await _ui(client, db, clock)
    item, job = await _to_join_leased(client, ic, env, clock, ui)  # ext_id из предпросмотра неизвестен
    clock.advance_ms(1000)
    await _report(ic, env["h"], job, result="joined", ext_id="-1005", title="Канал")
    assert _req(db, item["id"]).channel_id is None
    clock.advance_ms(1000)
    await _events(ic, env["h"], [_up("-1005", "1", _now_ms(clock))])
    _assert_bound(db, "-1005", env, item["id"])
    audit = [d for _r, actor, d in _audit(db, "channel_update") if d.get("join_id") == item["id"]]
    assert audit and audit[0]["muted"] is True


async def test_f6_history_before_report_known_ext_id(client, ic, db, clock, env):
    ui = await _ui(client, db, clock)
    item, job = await _to_join_leased(client, ic, env, clock, ui, ext_id="-1006")
    clock.advance_ms(1000)
    # Живой апдейт после вступления пришёл раньше отчёта: заявка ещё `approved`, ext_id известен с
    # `peek` (П4).
    await _events(ic, env["h"], [_up("-1006", "1", _now_ms(clock))])
    _assert_bound(db, "-1006", env, item["id"])
    await _report(ic, env["h"], job, result="joined", ext_id="-1006")
    ch = _assert_bound(db, "-1006", env, item["id"])
    assert _req(db, item["id"]).status == "joined"
    assert len([d for _r, _a, d in _audit(db, "channel_update") if d.get("join_id") == item["id"]]) == 1
    del ch


async def test_f6_history_before_report_invite(client, ic, db, clock, env):
    ui = await _ui(client, db, clock)
    item, job = await _to_join_leased(client, ic, env, clock, ui, target="t.me/+AbCdEfGh123")
    clock.advance_ms(1000)
    await _events(ic, env["h"], [_up("-1007", "1", _now_ms(clock), kind="group")])
    assert _channel(db, "-1007")["project_id"] is None  # ext_id ещё неизвестен — хук не узнал беседу
    await _report(ic, env["h"], job, result="joined", ext_id="-1007")
    _assert_bound(db, "-1007", env, item["id"])


async def test_f6_channel_known_before_join_untouched(client, ic, db, clock, env):
    await _events(ic, env["h"], [_up("-1008", "1", _now_ms(clock))])
    before = _channel(db, "-1008")
    clock.advance_ms(60_000)
    ui = await _ui(client, db, clock)
    item, job = await _to_join_leased(client, ic, env, clock, ui, ext_id="-1008")
    await _report(ic, env["h"], job, result="already", ext_id="-1008")
    assert _channel(db, "-1008") == before
    assert _req(db, item["id"]).channel_id == before["id"]


async def test_f6_already_from_peek_binds_later_channel(client, ic, db, clock, env):
    item = await _to_pending(client, ic, env, clock, ext_id="-1009", member=True)
    assert _req(db, item["id"]).status == "already"
    clock.advance_ms(5000)
    await _events(ic, env["h"], [_up("-1009", "1", _now_ms(clock))])
    _assert_bound(db, "-1009", env, item["id"])


async def test_f6_requested_binds_after_admins_approve(client, ic, db, clock, env):
    ui = await _ui(client, db, clock)
    item, job = await _to_join_leased(client, ic, env, clock, ui, ext_id="-1010")
    await _report(ic, env["h"], job, result="requested")
    assert _req(db, item["id"]).ext_id == "-1010"
    clock.advance_ms(3 * 3_600_000)
    await _events(ic, env["h"], [_up("-1010", "1", _now_ms(clock), kind="group")])
    _assert_bound(db, "-1010", env, item["id"])


async def test_f6_gave_up_after_join_step_still_binds(client, ic, db, clock, env):
    """П2: шаг «вступить» выдавался, коннектор так и не отчитался успехом (`gave_up`), а вступление в
    Telegram на деле прошло — беседа всё равно в проект заявки."""
    ui = await _ui(client, db, clock)
    item, job = await _to_join_leased(client, ic, env, clock, ui, ext_id="-1011")
    for _ in range(join_mod.MAX_ATTEMPTS):
        await _report(ic, env["h"], job, "retry")
        if _req(db, item["id"]).status == "failed":
            break
        clock.advance_ms(join_mod.DAY_MS)
        [job] = await _lease(ic, env["h"])
    assert _req(db, item["id"]).error == "gave_up"
    await _events(ic, env["h"], [_up("-1011", "1", _now_ms(clock))])
    _assert_bound(db, "-1011", env, item["id"])


async def test_f6_manual_channel_not_overridden(client, ic, db, clock, env):
    ui = await _ui(client, db, clock)
    item, job = await _to_join_leased(client, ic, env, clock, ui)
    clock.advance_ms(1000)
    await _events(ic, env["h"], [_up("-1012", "1", _now_ms(clock))])
    ch = _channel(db, "-1012")
    other = insert_project(db, name="Initech")
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET project_id = ?, map_source = 'manual', map_reason = 'manual' "
            "WHERE id = ?",
            (other, ch["id"]),
        )
    )
    await _report(ic, env["h"], job, result="joined", ext_id="-1012")
    after = _channel(db, "-1012")
    assert (after["project_id"], after["muted"]) == (other, 0)
    assert _req(db, item["id"]).channel_id == ch["id"]


# --- F7: сигналы подписке на проект (П1, П4) ----------------------------------------------------------------


async def test_f7_bound_channel_no_project_signal_chan_sub_yes(client, ic, db, clock, env, procs):
    _sub(db, project_id=env["project"], token_id=env["token_id"])
    ui = await _ui(client, db, clock)
    # Контроль: обычная беседа проекта даёт сигнал подписке «каждое сообщение».
    await _events(ic, env["h"], [_up("-2000", "1", _now_ms(clock), kind="group")])
    plain = _channel(db, "-2000")
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET project_id = ? WHERE id = ?", (env["project"], plain["id"])
        )
    )
    await _events(ic, env["h"], [_up("-2000", "2", _now_ms(clock) + 1, kind="group")])
    assert [(s[2], s[3]) for s in _signals(db)] == [(plain["id"], "pending")]

    item, job = await _to_join_leased(client, ic, env, clock, ui, ext_id="-2001")
    await _report(ic, env["h"], job, result="joined", ext_id="-2001")
    clock.advance_ms(1000)
    await _events(ic, env["h"], [_up("-2001", "1", _now_ms(clock), kind="group")])
    bound = _assert_bound(db, "-2001", env, item["id"])
    await _events(ic, env["h"], [_up("-2001", "2", _now_ms(clock) + 1, kind="group")])
    assert [s[2] for s in _signals(db)] == [plain["id"]]
    # Подписка на саму беседу — сигнал даёт.
    _chan_sub(db, channel_id=bound["id"], project_id=env["project"], token_id=env["token_id"])
    await _events(ic, env["h"], [_up("-2001", "3", _now_ms(clock) + 2, kind="group")])
    assert [(s[0] is not None, s[2]) for s in _signals(db)] == [(False, plain["id"]), (True, bound["id"])]


async def test_f7_signal_before_binding_skipped(client, ic, db, clock, env, procs):
    """П4: беседа пришла раньше отчёта с неизвестным ext_id (приглашение) и успела попасть в проект правилом
    — сигнал подписке на проект снимается при привязке."""
    _sub(db, project_id=env["project"], token_id=env["token_id"])
    ui = await _ui(client, db, clock)
    item, job = await _to_join_leased(client, ic, env, clock, ui, target="t.me/+AbCdEfGh123")
    clock.advance_ms(1000)
    await _events(ic, env["h"], [_up("-2002", "1", _now_ms(clock), kind="group")])
    ch = _channel(db, "-2002")
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET project_id = ? WHERE id = ?", (env["project"], ch["id"])
        )
    )
    await _events(ic, env["h"], [_up("-2002", "2", _now_ms(clock) + 1, kind="group")])
    assert [s[3] for s in _signals(db)] == ["pending"]
    await _report(ic, env["h"], job, result="joined", ext_id="-2002")
    _assert_bound(db, "-2002", env, item["id"])
    assert [(s[3], s[4]) for s in _signals(db)] == [("skipped", "join_muted")]


@pytest.mark.parametrize("order", ["report_first", "history_first"])
async def test_f7_forum_topics_muted_no_signal(client, ic, db, clock, env, procs, order):
    """П1: форум — сообщения идут в темы, родитель пуст; темы глушатся явно, проект идёт за родителем."""
    _sub(db, project_id=env["project"], token_id=env["token_id"])
    ui = await _ui(client, db, clock)
    item, job = await _to_join_leased(
        client, ic, env, clock, ui, ext_id="-3000", forum=True, kind="megagroup"
    )
    clock.advance_ms(1000)
    if order == "report_first":
        await _report(ic, env["h"], job, result="joined", ext_id="-3000")
    await _events(ic, env["h"], [_up("-3000:1", "1", _now_ms(clock), kind="topic", parent="-3000")])
    await _events(ic, env["h"], [_up("-3000:7", "2", _now_ms(clock) + 1, kind="topic", parent="-3000")])
    if order == "history_first":
        await _report(ic, env["h"], job, result="joined", ext_id="-3000")
    _assert_bound(db, "-3000", env, item["id"])
    for topic in ("-3000:1", "-3000:7"):
        ch = _channel(db, topic)
        assert (ch["project_id"], ch["muted"], ch["kind"]) == (env["project"], 1, "topic")
    await _events(ic, env["h"], [_up("-3000:7", "3", _now_ms(clock) + 2, kind="topic", parent="-3000")])
    assert [s for s in _signals(db) if s[3] == "pending"] == []


async def test_f7_forum_by_invite_topics_before_report(client, ic, db, clock, env, procs):
    """П1 + П5: форум по приглашению — ext_id узнаётся только из отчёта `join`; темы, пришедшие раньше отчёта,
    глушатся при привязке в `report`, их ещё не выданные сигналы подписке на проект снимаются."""
    _sub(db, project_id=env["project"], token_id=env["token_id"])
    ui = await _ui(client, db, clock)
    item, job = await _to_join_leased(client, ic, env, clock, ui, target="t.me/+AbCdEfGh123", forum=True)
    clock.advance_ms(1000)
    await _events(ic, env["h"], [_up("-3002:1", "1", _now_ms(clock), kind="topic", parent="-3002")])
    parent = _channel(db, "-3002")
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET project_id = ? WHERE id = ?", (env["project"], parent["id"])
        )
    )
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET project_id = ? WHERE parent_channel_id = ?",
            (env["project"], parent["id"]),
        )
    )
    await _events(ic, env["h"], [_up("-3002:1", "2", _now_ms(clock) + 1, kind="topic", parent="-3002")])
    assert [s[3] for s in _signals(db)] == ["pending"]
    await _report(ic, env["h"], job, result="joined", ext_id="-3002")
    _assert_bound(db, "-3002", env, item["id"])
    topic = _channel(db, "-3002:1")
    assert (topic["project_id"], topic["muted"]) == (env["project"], 1)
    assert [(s[3], s[4]) for s in _signals(db)] == [("skipped", "join_muted")]


async def test_f7_forum_topic_created_after_report_before_binding_source(client, ic, db, clock, env, procs):
    """Тема существующего до вступления форума — не трогается (граница по `first_seen_ts`)."""
    await _events(ic, env["h"], [_up("-3001:1", "1", _now_ms(clock), kind="topic", parent="-3001")])
    clock.advance_ms(60_000)
    ui = await _ui(client, db, clock)
    item, job = await _to_join_leased(client, ic, env, clock, ui, ext_id="-3001", forum=True)
    await _report(ic, env["h"], job, result="already", ext_id="-3001")
    ch = _channel(db, "-3001:1")
    assert (ch["project_id"], ch["muted"]) == (None, 0)
    assert _req(db, item["id"]).channel_id == _channel(db, "-3001")["id"]


def test_join_frame_has_no_texts():
    from hub.core.feed import _join_frame  # noqa: PLC0415

    frame = _join_frame({"id": 3, "project_id": 2, "status": "pending", "seq": 77, "title": "секрет"})
    assert frame.startswith("id: 77\nevent: join\n") and "секрет" not in frame


# --- ревью Ф17-1 (`.runs/critic-f17/`, находки 1 и 2) ---


async def test_failed_join_later_manual_join_by_owner_not_bound(client, ic, db, clock, env):
    """Вступление не удалось (группа заполнена, drop на шаге join). Через месяц владелец САМ вступает в эту
    группу из телефона — беседа появляется в хабе и молча уезжает в проект сессии заглушённой."""
    ui = await _ui(client, db, clock)
    item, job = await _to_join_leased(client, ic, env, clock, ui, ext_id="-1777", kind="megagroup")
    await _report(ic, env["h"], job, "drop", reason="group_full")
    assert _req(db, item["id"]).status == "failed"
    clock.advance_ms(30 * 24 * 3_600_000)  # месяц спустя
    await _events(ic, env["h"], [_up("-1777", "1", _now_ms(clock), kind="group")])
    ch = _channel(db, "-1777")
    # Ожидание здравого смысла: беседа, в которую владелец вступил сам, заявкой не трогается.
    assert (ch["project_id"], ch["muted"], ch["map_source"]) == (None, 0, "auto"), ch


async def test_new_forum_topic_keeps_topic_owner_unmuted(client, ic, db, clock, env):
    """Форум после вступления: владелец «Включить звук» у темы A; приходит новая тема B — хук
    `on_channel_created` заново глушит ВСЕ темы после границы, включая A."""
    ui = await _ui(client, db, clock)
    item, job = await _to_join_leased(
        client, ic, env, clock, ui, ext_id="-3100", forum=True, kind="megagroup"
    )
    clock.advance_ms(1000)
    await _report(ic, env["h"], job, result="joined", ext_id="-3100")
    await _events(ic, env["h"], [_up("-3100:1", "1", _now_ms(clock), kind="topic", parent="-3100")])
    topic_a = _channel(db, "-3100:1")
    assert topic_a["muted"] == 1
    r = await client.patch(f"{API}/channels/{topic_a['id']}", json={"muted": False}, headers=ui)
    assert r.status_code == 200, r.text
    assert _channel(db, "-3100:1")["muted"] == 0
    clock.advance_ms(60_000)
    await _events(ic, env["h"], [_up("-3100:9", "2", _now_ms(clock), kind="topic", parent="-3100")])
    assert _channel(db, "-3100:1")["muted"] == 0, "звук темы, включённый владельцем, снова выключен"
