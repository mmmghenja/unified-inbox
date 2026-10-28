"""Ф38-2 (docs/architecture.md — они главнее текста
спеки): заявки сессий на правило раскладки — ядро и ручки.

Сессия — bearer `interactive` со скоупом `draft` (и `session` в теле: по ней ядро находит имя линии подписки;
живость процесса подменяется `subs.read_proc_start`). Владелец — UI-сессия (вход кодом)."""

import asyncio
import json

import pytest

from hub.core import db as db_module
from hub.core import feed, mapping, queries, security, subs
from hub.core import rule_requests as rr_mod
from hub.core.db import Db
from hub.core.feed import HEARTBEAT_FRAME, Notifier

from .conftest import ORIGIN, csrf_headers, insert_project, login_via_code

API = "/api/v1"
PID, START, SID = 4242, 777_000, "sess-rule-1111"
SESSION = {"pid": PID, "proc_start": START, "session_id": SID}
DAY_MS = 24 * 3_600_000


# --- обвязка ------------------------------------------------------------------------------------------------


@pytest.fixture
def procs(monkeypatch):
    table = {PID: START}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


def _account(db, source="mail", *, db_name="main", folders=None, title=None) -> int:
    def _fn(conn):
        account_id = conn.execute(
            "INSERT INTO main.account (source, db, title) VALUES (?, ?, ?)", (source, db_name, title)
        ).lastrowid
        if folders is not None:
            _set_folders(conn, db_name, account_id, folders)
        return account_id

    return db.write_sync(_fn)


def _set_folders(conn, db_name, account_id, folders):
    conn.execute(
        f"INSERT OR REPLACE INTO {db_name}.account_meta (account_id, key, value_json, updated_ts) "  # noqa: S608
        "VALUES (?, 'tg_folders', ?, 0)",
        (account_id, json.dumps(folders, ensure_ascii=False)),
    )


def _channel(db, account_id, ext_id, *, attrs=None, project_id=None, db_name="main", title=None, ts=1000):
    meta = json.dumps({"attrs": attrs}) if attrs is not None else None
    return db.write_sync(
        lambda conn: (
            conn.execute(
                f"INSERT INTO {db_name}.channel (account_id, ext_id, kind, project_id, title, map_source, "  # noqa: S608
                "map_reason, meta_json, last_msg_ts, first_seen_ts) "
                "VALUES (?, ?, 'dm', ?, ?, 'auto', 'нет правил', ?, ?, 0)",
                (account_id, ext_id, project_id, title or ext_id, meta, ts),
            ).lastrowid
        )
    )


def _project_of(db, channel_id, db_name="main"):
    with db.reader() as conn:
        return conn.execute(
            f"SELECT project_id FROM {db_name}.channel WHERE id = ?",  # noqa: S608
            (channel_id,),
        ).fetchone()[0]


def _token(db, *, name="mcp:all", kind="interactive", scopes=("read", "draft"), project_ids=None):
    return db.write_sync(
        lambda conn: security.create_api_token(
            conn, name=name, kind=kind, scopes=list(scopes), project_ids=project_ids, expires_ts=None
        )
    )


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN}


async def _ui(client, db, clock) -> dict:
    cookie, _ = await login_via_code(client, db, clock)
    client.cookies.clear()  # bearer и cookie в одном запросе — неоднозначный принципал
    return {"Cookie": f"hub_session={cookie}", **csrf_headers(cookie)}


async def _create(client, token, project_id, account_id, kind, value=None, *, status=201, **extra):
    body = {"project_id": project_id, "account_id": account_id, "kind": kind, **extra}
    if value is not None:
        body["value"] = value
    r = await client.post(f"{API}/rule-requests", json=body, headers=_bearer(token))
    assert r.status_code == status, r.text
    return r.json()


async def _decide(client, ui, request_id, action, *, status=200):
    r = await client.post(f"{API}/rule-requests/{request_id}/{action}", headers=ui)
    assert r.status_code == status, r.text
    return r.json()


def _req(db, request_id) -> rr_mod.Req:
    with db.reader() as conn:
        req = rr_mod.get(conn, request_id)
    assert req is not None
    return req


def _audit(db, action):
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT result, actor, detail_json FROM main.audit WHERE action = ? ORDER BY id", (action,)
        ).fetchall()
    return [(r[0], r[1], json.loads(r[2])) for r in rows]


def _rules(db) -> list[tuple]:
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT id, account_id, match_json, project_id, priority FROM main.map_rule ORDER BY id"
        ).fetchall()
    return [(r[0], r[1], json.loads(r[2]), r[3], r[4]) for r in rows]


def _count(db) -> int:
    with db.reader() as conn:
        return conn.execute("SELECT COUNT(*) FROM main.rule_request").fetchone()[0]


def _sub(db, *, project_id, token_id, role="маркетолог", role_no=1, sid=SID):
    key = " ".join(role.casefold().split()) if role is not None else None
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.session_sub (project_id, api_token_id, session_pid, session_proc_start, "
            "session_id, session_cwd, created_ts, mode, role, role_key, role_no) "
            "VALUES (?, ?, ?, ?, ?, '/tmp', 0, 'messages', ?, ?, ?)",
            (project_id, token_id, PID, START, sid, role, key, role_no),
        )
    )


@pytest.fixture
def env(db):
    """Проект Umbrella, почтовый аккаунт, токен сессии `mcp:all` с `draft`."""
    project = insert_project(db, name="Umbrella")
    acc = _account(db, "mail", title="mail.ru владельца")
    created = _token(db)
    return {"project": project, "acc": acc, "token": created.token, "token_id": created.token_id}


# --- D1: миграция 0054 --------------------------------------------------------------------------------------


def test_d1_migration_0054(tmp_path, monkeypatch):
    original = db_module._migrations
    monkeypatch.setattr(
        db_module, "_migrations", lambda *, main: {v: f for v, f in original(main=main).items() if v <= 53}
    )
    old = Db(tmp_path)
    old.start()
    try:
        with old.reader() as conn:
            assert conn.execute("PRAGMA main.user_version").fetchone()[0] == 53
            work_version = conn.execute("PRAGMA work.user_version").fetchone()[0]
            tables = {r[0] for r in conn.execute("SELECT name FROM main.sqlite_master")}
            assert "rule_request" not in tables
    finally:
        old.close()
    monkeypatch.setattr(
        db_module, "_migrations", lambda *, main: {v: f for v, f in original(main=main).items() if v <= 54}
    )
    for _ in range(2):  # повторный запуск — без ошибок
        new = Db(tmp_path)
        new.start()
        try:
            with new.reader() as conn:
                assert conn.execute("PRAGMA main.user_version").fetchone()[0] == 54
                assert conn.execute("PRAGMA work.user_version").fetchone()[0] == work_version
                cols = {r[1] for r in conn.execute("PRAGMA main.table_info(rule_request)")}
                assert {"match_key", "value_json", "who", "rule_id", "moved", "seq"} <= cols
                assert "match_json" not in cols  # П2
                work_tables = {r[0] for r in conn.execute("SELECT name FROM work.sqlite_master")}
                assert "rule_request" not in work_tables
        finally:
            new.close()


# --- D2: подача ---------------------------------------------------------------------------------------------


async def test_d2_create_fields_and_repeat(client, db, env):
    item = await _create(
        client, env["token"], env["project"], env["acc"], "domain", " @Forum.Example.COM ", reason="a\nb"
    )
    assert item == {
        "id": item["id"],
        "seq": item["seq"],
        "status": "pending",
        "project_id": env["project"],
        "account_id": env["acc"],
        "account_source": "mail",
        "account_title": "mail.ru владельца",
        "kind": "domain",
        "value": "forum.example.com",
        "description": "отправитель @forum.example.com",
        "reason": "a b",
        "who": None,
        "created_ts": item["created_ts"],
        "decided_ts": None,
        "rule_id": None,
        "moved": 0,
        "examples": [],
    }
    again = await _create(
        client, env["token"], env["project"], env["acc"], "domain", "forum.example.com", status=200
    )
    assert again["id"] == item["id"] and again["reason"] == "a b"
    assert _count(db) == 1 and _rules(db) == []
    assert _audit(db, "rule_request_create") == [
        (
            "pending",
            "interactive",
            {
                "rule_request_id": item["id"],
                "project_id": env["project"],
                "account_id": env["acc"],
                "kind": "domain",
            },
        )
    ]
    whole = await _create(client, env["token"], env["project"], env["acc"], "all")
    assert whole["value"] is None and whole["description"] == "весь аккаунт"


async def test_d2_bad_condition_kind_account_project(client, db, env):
    create = lambda *a, **kw: _create(client, env["token"], *a, **kw)  # noqa: E731
    r = await create(env["project"], env["acc"], "domain", "nodots", status=422)
    assert r["detail"] == "отправитель: нужен домен вида example.com"
    r = await create(env["project"], env["acc"], "folder", 2, status=422)
    assert r["detail"] == "папки есть только у Telegram"
    r = await create(env["project"], env["acc"], "all", "x", status=422)
    assert "значение не нужно" in r["detail"]
    r = await create(env["project"], env["acc"], "channel", "c1", status=422)
    assert "hub_move_channel" in r["detail"]
    await create(env["project"], env["acc"], "nonsense", status=422)
    await create(env["project"], env["acc"], "domain", "a.com", status=422, reason="x" * 301)
    await create(env["project"], env["acc"], "domain", "a.com", status=422, extra_field=1)
    r = await create(env["project"], 9999, "all", status=404)
    assert "account_id 9999" in r["detail"]
    for bad_project in (-1, 0, 999):
        r = await create(bad_project, env["acc"], "all", status=404)
        assert "не виден" in r["detail"]
    other = insert_project(db, name="Initech")
    scoped = _token(db, name="mcp:iip", project_ids=[env["project"]])
    await _create(client, scoped.token, other, env["acc"], "all", status=404)
    assert _count(db) == 0


async def test_d2_rule_already_exists_409(client, db, env):
    rule_id = db.write_sync(
        lambda conn: mapping.add_rule(
            conn,
            account_id=env["acc"],
            project_id=env["project"],
            match={"attr": "from_domains", "has": "a.com", "label": "старое имя"},
            priority=None,
            now_ms=0,
        )[0]
    )
    r = await _create(client, env["token"], env["project"], env["acc"], "domain", "a.com", status=409)
    assert r["detail"] == f"такое правило уже есть (#{rule_id})"
    assert _count(db) == 0


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
async def test_d2_check_attach_denied_403(client, db, env, name, kind, scopes):
    created = _token(db, name=name, kind=kind, scopes=scopes)
    r = await _create(client, created.token, env["project"], env["acc"], "all", status=403)
    assert "интерактивная сессия" in r["detail"]
    assert _count(db) == 0


async def test_d2_ui_session_and_read_only_refused(client, db, clock, env):
    ui = await _ui(client, db, clock)
    body = {"project_id": env["project"], "account_id": env["acc"], "kind": "all"}
    r = await client.post(f"{API}/rule-requests", json=body, headers=ui)
    assert r.status_code == 403
    reader = _token(db, name="mcp:reader", scopes=("read",))
    r = await client.post(f"{API}/rule-requests", json=body, headers=_bearer(reader.token))
    assert r.status_code == 403
    assert _count(db) == 0


async def test_d2_limits_open_10_and_day_30(client, db, env):
    def _close_all():
        db.write_sync(
            lambda conn: conn.execute("UPDATE main.rule_request SET status = 'approved', decided_ts = 0")
        )

    create = lambda n, **kw: _create(  # noqa: E731
        client, env["token"], env["project"], env["acc"], "domain", f"d{n:03d}.com", **kw
    )
    n = 0
    for _ in range(rr_mod.OPEN_PER_PROJECT):
        await create(n)
        n += 1
    r = await create(n, status=429)
    assert "открытых заявок" in r["detail"]
    await create(0, status=200)  # повтор открытой — не новая, под лимит не попадает
    while n < rr_mod.NEW_PER_PROJECT_DAY:
        _close_all()
        for _ in range(min(rr_mod.OPEN_PER_PROJECT, rr_mod.NEW_PER_PROJECT_DAY - n)):
            await create(n)
            n += 1
    _close_all()
    r = await create(n, status=429)
    assert "за сутки" in r["detail"]


async def test_d2_declined_7_days_then_allowed(client, db, clock, env):
    ui = await _ui(client, db, clock)
    item = await _create(client, env["token"], env["project"], env["acc"], "domain", "a.com")
    await _decide(client, ui, item["id"], "decline")
    r = await _create(client, env["token"], env["project"], env["acc"], "domain", "A.com", status=409)
    assert "владелец отказал" in r["detail"]
    # другое условие и другой проект отказом не задеты
    await _create(client, env["token"], env["project"], env["acc"], "domain", "b.com")
    other = insert_project(db, name="Initech")
    elsewhere = await _create(client, env["token"], other, env["acc"], "domain", "a.com")
    await _decide(client, ui, elsewhere["id"], "decline")
    clock.advance_ms(7 * DAY_MS + 1)
    again = await _create(client, env["token"], env["project"], env["acc"], "domain", "a.com")
    assert again["id"] != item["id"] and again["status"] == "pending"


# --- D3: кто просит -----------------------------------------------------------------------------------------


async def test_d3_who_line_name_of_live_role_sub(client, db, env, procs):
    other = insert_project(db, name="Initech")
    _sub(db, project_id=other, token_id=env["token_id"], role="маркетолог", role_no=2)
    item = await _create(client, env["token"], env["project"], env["acc"], "all", session=SESSION)
    assert item["who"] == "маркетолог 2"  # подписка на ЛЮБОЙ проект


async def test_d3_who_null_without_sub_bad_role_dead_process(client, db, env, procs):
    create = lambda value, **kw: _create(  # noqa: E731
        client, env["token"], env["project"], env["acc"], "domain", value, **kw
    )
    assert (await create("a.com"))["who"] is None  # сессия не опознана
    assert (await create("b.com", session=SESSION))["who"] is None  # подписки нет
    _sub(db, project_id=env["project"], token_id=env["token_id"], role=None)
    assert (await create("c.com", session=SESSION))["who"] is None  # подписка без роли
    db.write_sync(lambda conn: conn.execute("UPDATE main.session_sub SET role = 'a--b', role_key = 'a--b'"))
    assert (await create("d.com", session=SESSION))["who"] is None  # роль не строгой формы
    db.write_sync(lambda conn: conn.execute("UPDATE main.session_sub SET role = 'кодер', role_key = 'кодер'"))
    stranger = _token(db, name="mcp:other")
    r = await _create(client, stranger.token, env["project"], env["acc"], "domain", "e.com", session=SESSION)
    assert r["who"] is None  # подписка другого токена
    procs[PID] = START + 1
    assert (await create("f.com", session=SESSION))["who"] is None  # процесс подменён
    procs[PID] = START
    assert (await create("g.com", session=SESSION))["who"] == "кодер"


# --- D4: что переложит --------------------------------------------------------------------------------------


async def test_d4_pending_moved_and_examples_match_dry_run(client, db, clock, env):
    for i in range(5):
        _channel(db, env["acc"], f"f{i}", attrs={"from_domains": ["forum.com"]}, title=f"Форум {i}", ts=i)
    _channel(db, env["acc"], "out", attrs={"from_domains": ["other.com"]})
    _channel(db, env["acc"], "in", attrs={"from_domains": ["forum.com"]}, project_id=env["project"])
    item = await _create(client, env["token"], env["project"], env["acc"], "domain", "forum.com")
    ui = await _ui(client, db, clock)
    body = {"account_id": env["acc"], "project_id": env["project"], "kind": "domain", "value": "forum.com"}
    dry = (await client.post(f"{API}/map-rules", json={**body, "dry_run": True}, headers=ui)).json()
    assert dry["moved"] == 5
    assert item["moved"] == dry["moved"]
    assert item["examples"] == [e["title"] for e in dry["examples"]][:3] == ["Форум 4", "Форум 3", "Форум 2"]
    assert item["description"] == dry["description"]
    # число считается при выдаче: ещё одна беседа — и в списке уже 6
    _channel(db, env["acc"], "f9", attrs={"from_domains": ["forum.com"]}, title="Форум 9", ts=99)
    r = await client.get(f"{API}/rule-requests", headers=ui)
    assert [(x["id"], x["moved"], x["examples"][0]) for x in r.json()] == [(item["id"], 6, "Форум 9")]


# --- D5: «Завести» ------------------------------------------------------------------------------------------


async def test_d5_approve_creates_rule_and_moves(client, db, clock, env):
    moved = [_channel(db, env["acc"], f"f{i}", attrs={"from_domains": ["forum.com"]}) for i in range(2)]
    stay = _channel(db, env["acc"], "out", attrs={"from_domains": ["other.com"]})
    item = await _create(client, env["token"], env["project"], env["acc"], "domain", "forum.com")
    ui = await _ui(client, db, clock)
    clock.advance_ms(5_000)
    done = await _decide(client, ui, item["id"], "approve")
    [(rule_id, account_id, match, project_id, priority)] = _rules(db)
    assert (account_id, project_id, priority) == (env["acc"], env["project"], 0)
    assert match == {"attr": "from_domains", "has": "forum.com", "label": "отправитель @forum.com"}
    assert [_project_of(db, c) for c in moved] == [env["project"]] * 2 and _project_of(db, stay) is None
    assert (done["status"], done["rule_id"], done["moved"]) == ("approved", rule_id, 2)
    assert done["decided_ts"] == item["created_ts"] + 5_000 and done["seq"] > item["seq"]
    req = _req(db, item["id"])
    assert (req.status, req.rule_id, req.moved) == ("approved", rule_id, 2)
    [(result, actor, detail)] = _audit(db, "map_rule_add")
    assert (result, actor) == ("ok", "ui")
    assert detail == {
        "rule_id": rule_id,
        "account_id": env["acc"],
        "project_id": env["project"],
        "match": match,
        "priority": 0,
        "channels_relaid": 2,
    }
    [(result, actor, detail)] = _audit(db, "rule_request_decide")
    assert (result, actor) == ("approved", "ui")
    assert detail["rule_request_id"] == item["id"] and detail["rule_id"] == rule_id and detail["moved"] == 2
    # повторное «Завести» и «Отклонить» решённой — 409, второго правила нет
    for action in ("approve", "decline"):
        r = await _decide(client, ui, item["id"], action, status=409)
        assert "уже решена" in r["detail"]
    assert len(_rules(db)) == 1 and _req(db, item["id"]).status == "approved"
    await _decide(client, ui, 99_999, "approve", status=404)


async def test_d5_bearer_cannot_decide_rule_not_created(client, db, env):
    """П9: bearer со скоупами `organize` и `draft` — 403, правила нет (ловит мутацию «снять `ui_only`»)."""
    _channel(db, env["acc"], "f1", attrs={"from_domains": ["forum.com"]})
    item = await _create(client, env["token"], env["project"], env["acc"], "domain", "forum.com")
    strong = _token(db, name="mcp:organizer", scopes=("read", "draft", "organize"))
    for token in (env["token"], strong.token):
        for action in ("approve", "decline"):
            r = await client.post(f"{API}/rule-requests/{item['id']}/{action}", headers=_bearer(token))
            assert r.status_code == 403, (action, r.text)
    assert _rules(db) == [] and _req(db, item["id"]).status == "pending"
    denied = _audit(db, "ui_only_denied")
    assert len(denied) == 4 and denied[0][2]["path"] == f"{API}/rule-requests/{item['id']}/approve"
    assert _audit(db, "map_rule_add") == [] and _audit(db, "rule_request_decide") == []


async def test_d5_ui_without_csrf_refused(client, db, clock, env):
    """П9: approve и decline из UI-сессии без CSRF-заголовка — отказ, как у `/joins`."""
    item = await _create(client, env["token"], env["project"], env["acc"], "all")
    ui = await _ui(client, db, clock)
    no_csrf = {"Cookie": ui["Cookie"], "Origin": ORIGIN}
    for action in ("approve", "decline"):
        r = await client.post(f"{API}/rule-requests/{item['id']}/{action}", headers=no_csrf)
        assert r.status_code == 403, (action, r.text)
    assert _rules(db) == [] and _req(db, item["id"]).status == "pending"


async def test_d5_duplicate_rule_at_decision_time(client, db, clock, env):
    _channel(db, env["acc"], "f1", attrs={"from_domains": ["forum.com"]})
    item = await _create(client, env["token"], env["project"], env["acc"], "domain", "forum.com")
    ui = await _ui(client, db, clock)
    body = {"account_id": env["acc"], "project_id": env["project"], "kind": "domain", "value": "forum.com"}
    r = await client.post(f"{API}/map-rules", json=body, headers=ui)
    assert r.status_code == 200, r.text
    rule_id = r.json()["rule_id"]
    done = await _decide(client, ui, item["id"], "approve")
    assert (done["status"], done["rule_id"], done["moved"]) == ("approved", rule_id, 0)
    assert len(_rules(db)) == 1 and len(_audit(db, "map_rule_add")) == 1


async def test_d5_folder_removed_409_stays_pending(client, db, clock, env):
    tg = _account(db, "telegram", folders=[{"id": 2, "title": "Initech"}])
    item = await _create(client, env["token"], env["project"], tg, "folder", 2)
    assert item["description"] == "папка «Initech»" and item["value"] == 2
    db.write_sync(lambda conn: _set_folders(conn, "main", tg, [{"id": 7, "title": "Другая"}]))
    ui = await _ui(client, db, clock)
    r = await _decide(client, ui, item["id"], "approve", status=409)
    assert "папка 2 неизвестна" in r["detail"]
    assert _req(db, item["id"]).status == "pending" and _rules(db) == []
    assert _audit(db, "rule_request_decide") == []
    done = await _decide(client, ui, item["id"], "decline")
    assert done["status"] == "declined" and done["description"] == "папка #2"


async def test_p4_account_gone_decide_409_and_listing_survives(client, db, clock, env):
    """П4: аккаунт пропал — `mapping.NotFound` при одобрении даёт 409 (не 500), выдача жива."""
    item = await _create(client, env["token"], env["project"], env["acc"], "domain", "a.com")
    db.write_sync(lambda conn: conn.execute("DELETE FROM main.account WHERE id = ?", (env["acc"],)))
    ui = await _ui(client, db, clock)
    r = await _decide(client, ui, item["id"], "approve", status=409)
    assert f"account_id {env['acc']} не найден" in r["detail"]
    assert _req(db, item["id"]).status == "pending"
    [row] = (await client.get(f"{API}/rule-requests", headers=ui)).json()
    assert (row["account_source"], row["account_title"], row["moved"]) == (None, None, None)
    assert row["description"] == "отправитель @a.com"
    r = await client.get(f"{API}/sync", params={"since": 0}, headers=ui)
    assert r.status_code == 200 and [x["id"] for x in r.json()["rule_requests"]] == [item["id"]]


# --- D6: «Отклонить» ----------------------------------------------------------------------------------------


async def test_d6_decline(client, db, clock, env):
    _channel(db, env["acc"], "f1", attrs={"from_domains": ["forum.com"]})
    item = await _create(client, env["token"], env["project"], env["acc"], "domain", "forum.com")
    ui = await _ui(client, db, clock)
    done = await _decide(client, ui, item["id"], "decline")
    assert (done["status"], done["rule_id"], done["moved"]) == ("declined", None, None)
    assert done["examples"] is None
    assert _rules(db) == [] and _audit(db, "map_rule_add") == []
    [(result, actor, _detail)] = _audit(db, "rule_request_decide")
    assert (result, actor) == ("declined", "ui")
    await _decide(client, ui, item["id"], "approve", status=409)
    assert _rules(db) == []


# --- П1: названия чужих бесед и папок — только тому, кто видит все проекты ---------------------------------


async def test_p1_scoped_token_sees_no_foreign_titles(client, db, clock):
    p1 = insert_project(db, name="Globex")
    p3 = insert_project(db, name="Umbrella")
    acc = _account(db, "telegram", title="Личный Telegram", folders=[{"id": 2, "title": "Тайная папка"}])
    _channel(db, acc, "s1", project_id=p1, title="Секрет", attrs={"tg_folders": [2]})
    scoped = _token(db, name="mcp:iip", project_ids=[p3])
    wake = _token(db, name="mcp:wake-3", scopes=("read",), project_ids=[p3])
    everything = _token(db)
    ui = await _ui(client, db, clock)

    def _leaks(payload) -> bool:
        text = json.dumps(payload, ensure_ascii=False)
        return any(secret in text for secret in ("Секрет", "Тайная папка", "Личный Telegram"))

    item = await _create(client, scoped.token, p3, acc, "all")
    assert not _leaks(item)
    assert (item["moved"], item["examples"], item["account_title"]) == (1, None, None)
    assert item["account_source"] == "telegram"
    # заявка ДРУГОГО токена на тот же проект — этим токеном тоже без названий
    folder = await _create(client, everything.token, p3, acc, "folder", 2)
    assert folder["description"] == "папка «Тайная папка»" and folder["examples"] == ["Секрет"]
    assert folder["account_title"] == "Личный Telegram"

    for token in (scoped.token, wake.token):
        listed = (await client.get(f"{API}/rule-requests", headers=_bearer(token))).json()
        synced = (await client.get(f"{API}/sync", params={"since": 0}, headers=_bearer(token))).json()
        for rows in (listed, synced["rule_requests"]):
            assert not _leaks(rows)
            by_id = {x["id"]: x for x in rows}
            assert set(by_id) == {item["id"], folder["id"]}
            assert by_id[folder["id"]]["description"] == "папка 2"
            assert all(x["moved"] == 1 and x["examples"] is None for x in rows)
    repeat = await _create(client, scoped.token, p3, acc, "folder", 2, status=200)
    assert repeat["id"] == folder["id"] and not _leaks(repeat)

    for headers in (ui, _bearer(everything.token)):
        listed = (await client.get(f"{API}/rule-requests", headers=headers)).json()
        synced = (await client.get(f"{API}/sync", params={"since": 0}, headers=headers)).json()
        for rows in (listed, synced["rule_requests"]):
            by_id = {x["id"]: x for x in rows}
            assert by_id[item["id"]]["examples"] == ["Секрет"]
            assert by_id[item["id"]]["account_title"] == "Личный Telegram"
            assert by_id[folder["id"]]["description"] == "папка «Тайная папка»"

    # кривой номер папки: ограниченному токену — без перечня названий папок
    r = await _create(client, scoped.token, p3, acc, "folder", 99, status=422)
    assert r["detail"] == "папка: у аккаунта нет такой папки" and not _leaks(r)
    r = await _create(client, everything.token, p3, acc, "folder", 99, status=422)
    assert "Тайная папка" in r["detail"]
    r = await _create(client, scoped.token, p3, acc, "folder", "два", status=422)
    assert "нужен номер папки" in r["detail"]
    # проект проверяется ДО условия: чужой проект — 404, а не текст про папки
    await _create(client, scoped.token, p1, acc, "folder", 99, status=404)


# --- П2: ключ заявки — условие без `label` ------------------------------------------------------------------


async def test_p2_folder_rename_keeps_same_request(client, db, clock, env):
    tg = _account(db, "telegram", folders=[{"id": 2, "title": "Initech"}])
    first = await _create(client, env["token"], env["project"], tg, "folder", 2)
    db.write_sync(lambda conn: _set_folders(conn, "main", tg, [{"id": 2, "title": "Initech новое"}]))
    again = await _create(client, env["token"], env["project"], tg, "folder", 2, status=200)
    assert again["id"] == first["id"] and again["description"] == "папка «Initech новое»"
    assert _req(db, first["id"]).match_key == '{"attr": "tg_folders", "has": 2}'
    ui = await _ui(client, db, clock)
    await _decide(client, ui, first["id"], "decline")
    db.write_sync(lambda conn: _set_folders(conn, "main", tg, [{"id": 2, "title": "Третье имя"}]))
    r = await _create(client, env["token"], env["project"], tg, "folder", 2, status=409)
    assert "владелец отказал" in r["detail"]  # переименование не обходит отказ за 7 дней


async def test_p2_work_account_key_and_rule_without_label(client, db, clock, env):
    work = _account(db, "telegram", db_name="work", folders=[{"id": 5, "title": "Корпоративная"}])
    ch = _channel(db, work, "w1", attrs={"tg_folders": [5]}, db_name="work")
    item = await _create(client, env["token"], env["project"], work, "folder", 5)
    assert item["moved"] == 1
    assert "Корпоративная" not in _req(db, item["id"]).match_key
    ui = await _ui(client, db, clock)
    done = await _decide(client, ui, item["id"], "approve")
    [(rule_id, _acc, match, _project, _prio)] = _rules(db)
    assert match == {"attr": "tg_folders", "has": 5} and done["rule_id"] == rule_id
    assert _project_of(db, ch, "work") == env["project"]
    with db.reader() as conn:
        main_text = json.dumps(
            conn.execute("SELECT detail_json FROM main.audit").fetchall()
            + conn.execute("SELECT match_key, value_json FROM main.rule_request").fetchall(),
            ensure_ascii=False,
        )
    assert "Корпоративная" not in main_text


# --- П3: условие уже ведёт в другой проект ------------------------------------------------------------------


async def test_p3_same_condition_to_other_project_refused(client, db, env):
    other = insert_project(db, name="Initech")
    rule_id = db.write_sync(
        lambda conn: mapping.add_rule(
            conn,
            account_id=env["acc"],
            project_id=other,
            match={"attr": "from_domains", "has": "a.com", "label": "как-то названо"},
            priority=None,
            now_ms=0,
        )[0]
    )
    r = await _create(client, env["token"], env["project"], env["acc"], "domain", "a.com", status=409)
    assert r["detail"] == f"это условие уже ведёт в другой проект (правило #{rule_id})"
    first = await _create(client, env["token"], other, env["acc"], "domain", "b.com")
    r = await _create(client, env["token"], env["project"], env["acc"], "domain", "b.com", status=409)
    assert r["detail"] == f"с этим условием уже лежит заявка #{first['id']} в другой проект"
    assert _count(db) == 1


async def test_p3_domain_over_whole_account_rule_accepted(client, db, env):
    """Живой случай: правило «весь аккаунт» (приоритет −100) в другой проект заявке `domain` не мешает."""
    other = insert_project(db, name="Личное")
    chans = [_channel(db, env["acc"], f"f{i}", attrs={"from_domains": ["forum.com"]}) for i in range(2)]
    db.write_sync(
        lambda conn: mapping.add_rule(
            conn, account_id=env["acc"], project_id=other, match={"all": True}, priority=None, now_ms=0
        )
    )
    assert [_project_of(db, c) for c in chans] == [other, other]
    item = await _create(client, env["token"], env["project"], env["acc"], "domain", "forum.com")
    assert item["status"] == "pending" and item["moved"] == 2


# --- D7 + П6: `/sync` и SSE ---------------------------------------------------------------------------------


async def test_d7_sync_on_create_and_decide(client, db, clock, env):
    ui = await _ui(client, db, clock)
    start = (await client.get(f"{API}/sync", params={"since": 0}, headers=ui)).json()["max_seq"]
    item = await _create(client, env["token"], env["project"], env["acc"], "domain", "a.com", reason="форум")
    r = await client.get(f"{API}/sync", params={"since": start}, headers=ui)
    assert r.status_code == 200, r.text
    page = r.json()
    # П6: страница из одной заявки — 200, курсор равен её `seq`, в каналы она не попала
    assert page["max_seq"] == item["seq"] and page["channels"] == [] and page["messages"] == []
    assert page["rule_requests"] == [item]
    assert "token_id" not in item and "match_key" not in item
    done = await _decide(client, ui, item["id"], "decline")
    page = (await client.get(f"{API}/sync", params={"since": item["seq"]}, headers=ui)).json()
    assert [(x["id"], x["status"]) for x in page["rule_requests"]] == [(item["id"], "declined")]
    assert page["max_seq"] == done["seq"]
    # чужой проект токену не виден
    other = insert_project(db, name="Initech")
    scoped = _token(db, name="mcp:initech", project_ids=[other])
    page = (await client.get(f"{API}/sync", params={"since": 0}, headers=_bearer(scoped.token))).json()
    assert page["rule_requests"] == []
    r = await client.get(f"{API}/rule-requests", headers=_bearer(scoped.token))
    assert r.json() == []


async def test_d7_list_filters(client, db, clock, env):
    ui = await _ui(client, db, clock)
    a = await _create(client, env["token"], env["project"], env["acc"], "domain", "a.com")
    b = await _create(client, env["token"], env["project"], env["acc"], "domain", "b.com")
    await _decide(client, ui, a["id"], "decline")

    async def ids(**params):
        r = await client.get(f"{API}/rule-requests", params=params, headers=ui)
        assert r.status_code == 200, r.text
        return [x["id"] for x in r.json()]

    assert await ids() == [b["id"], a["id"]]  # свежие первыми
    assert await ids(status="open") == [b["id"]] == await ids(status="pending")
    assert await ids(status="declined") == [a["id"]] and await ids(status="approved") == []
    assert await ids(project_id=env["project"] + 50) == []
    clock.advance_ms(7 * DAY_MS + 1)
    assert await ids() == [b["id"]]  # решённые — только за 7 дней, открытые — всегда
    r = await client.get(f"{API}/rule-requests", params={"status": "nonsense"}, headers=ui)
    assert r.status_code == 422


def test_d7_rule_request_frame_has_no_texts():
    frame = feed._rule_request_frame(
        {"id": 3, "project_id": 2, "status": "pending", "seq": 77, "reason": "секрет", "examples": ["секрет"]}
    )
    assert frame.startswith("id: 77\nevent: rule_request\n") and "секрет" not in frame
    assert json.loads(frame.split("data: ")[1]) == {"id": 3, "project_id": 2, "status": "pending", "seq": 77}


async def test_p6_stream_does_not_repeat_rule_request_frame(client, db, env):
    item = await _create(client, env["token"], env["project"], env["acc"], "domain", "a.com", reason="секрет")
    seq = item["seq"]
    notifier = Notifier()
    notifier.attach(asyncio.get_running_loop())
    principal = security.Principal(token_id=0, kind="ui", scopes=frozenset({"read"}), project_ids=None)

    async def _two() -> list[str]:
        gen = feed.stream(db, principal, last_seq=seq - 1, notifier=notifier, heartbeat_s=0.05)
        try:
            return [await anext(gen), await anext(gen)]
        finally:
            await gen.aclose()

    first, second = await asyncio.wait_for(_two(), 5)
    assert first.startswith(f"id: {seq}\nevent: rule_request\n") and "секрет" not in first
    assert second == HEARTBEAT_FRAME  # кадр не идёт по кругу
    with db.reader() as conn:
        changes = queries.changes_since(conn, principal, since=seq - 1, limit=50)
    assert [x["id"] for x in changes.rule_requests] == [item["id"]] and changes.max_seq == seq


# --- П9: `POST /map-rules` после выноса `check_rule_target` -------------------------------------------------


async def test_p9_map_rules_error_order_and_dry_run_via_reader(client, db, clock, env, monkeypatch):
    ui = await _ui(client, db, clock)

    async def add(account_id, project_id, **extra):
        body = {"account_id": account_id, "project_id": project_id, "kind": "all", **extra}
        return await client.post(f"{API}/map-rules", json=body, headers=ui)

    r = await add(env["acc"], env["project"])
    assert r.status_code == 200, r.text
    r = await add(9999, 9999)  # нет ни аккаунта, ни проекта — первым аккаунт
    assert r.status_code == 404 and "account_id 9999" in r.json()["detail"]
    r = await add(env["acc"], 9999)
    assert r.status_code == 404 and "project_id 9999" in r.json()["detail"]
    r = await add(env["acc"], env["project"])
    assert r.status_code == 409 and "такое правило уже есть" in r.json()["detail"]
    other = insert_project(db, name="Initech")

    async def _no_write(_fn):
        raise AssertionError("dry_run не должен открывать запись")

    monkeypatch.setattr(db, "write", _no_write)
    r = await add(env["acc"], other, dry_run=True)
    assert r.status_code == 200 and r.json()["dry_run"] is True
    r = await add(env["acc"], env["project"], dry_run=True)
    assert r.status_code == 409
