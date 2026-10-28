"""Ф14-1: раскладка и проекты из интерфейса — ручки `routes/organize.py`, `build_match`, предпросмотр правил,
перенос пачки бесед, `map_source` в выдаче (`docs/architecture.md`, тесты D 1–12).
"""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core.db import next_seq

from .conftest import BASE_URL, csrf_headers, insert_project, login_via_code, make_bearer_token

NEW_ENDPOINTS = [
    ("GET", "/api/v1/map-rules", None),
    ("POST", "/api/v1/map-rules", {"account_id": 1, "project_id": 1, "kind": "all"}),
    ("DELETE", "/api/v1/map-rules/1", None),
    ("GET", "/api/v1/accounts/1/map-options", None),
    ("POST", "/api/v1/projects", {"name": "X"}),
    ("PATCH", "/api/v1/projects/1", {"name": "X"}),
    ("PUT", "/api/v1/projects/order", {"ids": [1]}),
    ("POST", "/api/v1/channels/move", {"channel_ids": [1], "project_id": None}),
]


# --- помощники ----------------------------------------------------------------------------------------------


def _account(db, source, *, db_name="main", folders=None) -> int:
    def _fn(conn):
        account_id = conn.execute(
            "INSERT INTO main.account (source, db) VALUES (?, ?)", (source, db_name)
        ).lastrowid
        if folders is not None:
            conn.execute(
                f"INSERT INTO {db_name}.account_meta (account_id, key, value_json, updated_ts) "  # noqa: S608
                "VALUES (?, 'tg_folders', ?, 0)",
                (account_id, json.dumps(folders, ensure_ascii=False)),
            )
        return account_id

    return db.write_sync(_fn)


def _channel(
    db,
    account_id,
    ext_id,
    *,
    attrs=None,
    project_id=None,
    map_source="auto",
    kind="dm",
    parent=None,
    archived=0,
    ts=1000,
    db_name="main",
    title=None,
) -> int:
    meta = json.dumps({"attrs": attrs}) if attrs is not None else None
    reason = "manual" if map_source == "manual" else "нет правил"
    return db.write_sync(
        lambda conn: (
            conn.execute(
                f"INSERT INTO {db_name}.channel (account_id, ext_id, kind, project_id, title, archived, "  # noqa: S608
                "map_source, map_reason, meta_json, last_msg_ts, parent_channel_id, first_seen_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    account_id,
                    ext_id,
                    kind,
                    project_id,
                    title or ext_id,
                    archived,
                    map_source,
                    reason,
                    meta,
                    ts,
                    parent,
                ),
            ).lastrowid
        )
    )


def _state(db, db_name="main") -> dict[int, tuple]:
    with db.reader() as conn:
        rows = conn.execute(
            f"SELECT id, project_id, map_source, map_reason, seq FROM {db_name}.channel"  # noqa: S608
        ).fetchall()
    return {r[0]: r[1:] for r in rows}


def _audit(db, action) -> list[dict]:
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT actor, api_token_id, detail_json FROM main.audit WHERE action = ? ORDER BY id", (action,)
        ).fetchall()
    return [{"actor": r[0], "token": r[1], "detail": json.loads(r[2] or "null")} for r in rows]


def _audit_count(db) -> int:
    with db.reader() as conn:
        return conn.execute("SELECT COUNT(*) FROM main.audit").fetchone()[0]


def _rules_count(db) -> int:
    with db.reader() as conn:
        return conn.execute("SELECT COUNT(*) FROM main.map_rule").fetchone()[0]


@pytest.fixture
async def ui(client, db, clock):
    cookie, _csrf = await login_via_code(client, db, clock)
    headers = csrf_headers(cookie)

    class _Ui:
        async def get(self, path, **kw):
            return await client.get(path, **kw)

        async def post(self, path, body):
            return await client.post(path, json=body, headers=headers)

        async def patch(self, path, body):
            return await client.patch(path, json=body, headers=headers)

        async def put(self, path, body):
            return await client.put(path, json=body, headers=headers)

        async def delete(self, path, **kw):
            return await client.delete(path, headers=headers, **kw)

    return _Ui()


@pytest.fixture
async def bearer(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as c:
        yield c


# --- тест 1: только UI-сессия ----------------------------------------------------------------------


@pytest.mark.parametrize("scope", ["read", "organize", "draft"])
async def test_1_bearer_any_scope_is_forbidden_with_audit(bearer, db, scope):
    token = make_bearer_token(db, scopes=(scope,), name=f"t-{scope}")
    for method, path, body in NEW_ENDPOINTS:
        before = len(_audit(db, "ui_only_denied"))
        r = await bearer.request(method, path, json=body, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403, (method, path, r.text)
        denied = _audit(db, "ui_only_denied")
        assert len(denied) == before + 1
        assert denied[-1]["detail"]["path"] == path.split("?")[0]
    assert _audit(db, "channels_move") == [] and _audit(db, "project_add") == []


async def test_1_ui_without_csrf_refused(client, db, clock):
    await login_via_code(client, db, clock)
    r = await client.post("/api/v1/projects", json={"name": "Без CSRF"})
    assert r.status_code == 403
    assert _audit(db, "project_add") == []


# --- тест 2: build_match через ручку -----------------------------------------------------------------------


async def test_2_build_match_validation(ui, db):
    project = insert_project(db, name="Initech")
    tg = _account(db, "telegram", folders=[{"id": 2, "title": "Initech"}, {"id": 3, "title": "Globex"}])
    tg_nofolders = _account(db, "telegram")
    mail = _account(db, "mail")

    async def add(account, kind, value=None, **extra):
        body = {"account_id": account, "project_id": project, "kind": kind, "value": value, "dry_run": True}
        return await ui.post("/api/v1/map-rules", {**body, **extra})

    r = await add(tg, "folder", 2)
    assert r.status_code == 200 and r.json()["description"] == "папка «Initech»"
    r = await add(tg, "folder", 9)
    assert (
        r.status_code == 422
        and r.json()["detail"] == "папка 9 неизвестна. Известные папки: 2=Initech, 3=Globex"
    )
    r = await add(tg_nofolders, "folder", 2)
    assert r.status_code == 422 and r.json()["detail"] == "папки ещё не пришли от коннектора"
    r = await add(mail, "folder", 2)
    assert r.status_code == 422 and r.json()["detail"] == "папки есть только у Telegram"

    r = await add(mail, "domain", " @Example.COM ")
    assert r.status_code == 200 and r.json()["description"] == "отправитель @example.com"
    for bad in ("a@b.c", "", "   ", "localhost"):
        r = await add(mail, "domain", bad)
        assert r.status_code == 422, bad
        assert "домен" in r.json()["detail"]

    r = await add(tg, "label", "Работа")
    assert r.status_code == 422 and "почты" in r.json()["detail"]
    r = await add(mail, "label", "  Работа ")
    assert r.status_code == 200 and r.json()["description"] == "метка «Работа»"
    r = await add(tg, "all", 1)
    assert r.status_code == 422
    r = await add(tg, "all")
    assert r.status_code == 200 and r.json()["description"] == "весь аккаунт"
    r = await add(tg, "nonsense", 1)
    assert r.status_code == 422 and "неизвестный вид" in r.json()["detail"]
    r = await add(tg, "folder", True)
    assert r.status_code == 422

    # 404: аккаунт, проект (в т.ч. псевдо-проект)
    r = await add(999, "all")
    assert r.status_code == 404
    for bad_project in (-1, 0, 999):
        r = await ui.post(
            "/api/v1/map-rules", {"account_id": tg, "project_id": bad_project, "kind": "all", "dry_run": True}
        )
        assert r.status_code == 404, bad_project
    # лишнее поле — 422
    r = await ui.post(
        "/api/v1/map-rules", {"account_id": tg, "project_id": project, "kind": "all", "match": {}}
    )
    assert r.status_code == 422
    assert _rules_count(db) == 0


# --- тест 3: dry_run == настоящая запись ------------------------------------------------------------


async def test_3_dry_run_writes_nothing_and_matches_real_add(ui, db):
    initech = insert_project(db, name="Initech")
    other = insert_project(db, name="Другое")
    tg = _account(db, "telegram", folders=[{"id": 2, "title": "Initech"}])
    in_folder = [_channel(db, tg, f"f{i}", attrs={"tg_folders": [2, 7]}, ts=1000 + i) for i in range(3)]
    archived = _channel(db, tg, "arch", attrs={"tg_folders": [2]}, archived=1, ts=5000)
    _channel(db, tg, "out", attrs={"tg_folders": [7]})
    manual = _channel(db, tg, "man", attrs={"tg_folders": [2]}, project_id=other, map_source="manual")
    already = _channel(db, tg, "already", attrs={"tg_folders": [2]}, project_id=initech)

    before_state, before_audit = _state(db), _audit_count(db)
    body = {"account_id": tg, "project_id": initech, "kind": "folder", "value": 2}
    r = await ui.post("/api/v1/map-rules", {**body, "dry_run": True})
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview["dry_run"] is True
    assert _state(db) == before_state and _audit_count(db) == before_audit and _rules_count(db) == 0

    expected = {*in_folder, archived}
    assert preview["moved"] == len(expected)
    assert {e["channel_id"] for e in preview["examples"]} == expected
    assert manual not in expected and already not in expected
    # сначала неархивные, по last_msg_ts DESC
    assert [e["channel_id"] for e in preview["examples"]] == [
        in_folder[2],
        in_folder[1],
        in_folder[0],
        archived,
    ]
    assert all(e["from_project_id"] is None and e["to_project_id"] == initech for e in preview["examples"])

    r = await ui.post("/api/v1/map-rules", body)
    assert r.status_code == 200, r.text
    added = r.json()
    assert added["priority"] == 0 and added["description"] == "папка «Initech»"
    after = _state(db)
    moved = {cid for cid, row in after.items() if row[0] != before_state[cid][0]}
    assert moved == expected
    assert after[in_folder[0]][2] == f"правило #{added['rule_id']}: папка «Initech»"
    audit = _audit(db, "map_rule_add")
    assert len(audit) == 1 and audit[0]["actor"] == "ui" and audit[0]["token"] is not None
    assert audit[0]["detail"]["match"] == {"attr": "tg_folders", "has": 2, "label": "папка «Initech»"}


# --- тест 4: порядок правил ----------------------------------------------------------------------


async def test_4_candidate_with_equal_priority_loses_higher_wins(ui, db):
    first = insert_project(db, name="Первый")
    second = insert_project(db, name="Второй")
    mail = _account(db, "mail")
    _channel(db, mail, "t1", attrs={"from_domains": ["a.com"], "labels": ["L"]})
    _channel(db, mail, "t2", attrs={"from_domains": ["a.com"]}, project_id=first, map_source="manual")
    r = await ui.post(
        "/api/v1/map-rules", {"account_id": mail, "project_id": first, "kind": "domain", "value": "a.com"}
    )
    assert r.status_code == 200 and r.json()["channels_relaid"] == 1

    body = {"account_id": mail, "project_id": second, "kind": "label", "value": "L", "dry_run": True}
    r = await ui.post("/api/v1/map-rules", body)
    assert r.json()["moved"] == 0
    r = await ui.post("/api/v1/map-rules", {**body, "priority": 1})
    assert r.json()["moved"] == 1
    r = await ui.post("/api/v1/map-rules", {**body, "priority": 1, "dry_run": False})
    assert r.json()["channels_relaid"] == 1
    # «весь аккаунт» по умолчанию весит -100 — ручную не трогает никогда, домен не перебивает
    r = await ui.post(
        "/api/v1/map-rules", {"account_id": mail, "project_id": second, "kind": "all", "dry_run": True}
    )
    assert r.json()["moved"] == 0


# --- тест 5: топики -----------------------------------------------------------------------------------------


async def test_5_topics_follow_forum_in_preview(ui, db):
    project = insert_project(db, name="Форумы")
    other = insert_project(db, name="Другое")
    tg = _account(db, "telegram", folders=[{"id": 4, "title": "Форумы"}])
    forum = _channel(db, tg, "forum", kind="group", attrs={"tg_folders": [4]})
    auto_topic = _channel(db, tg, "forum:1", kind="topic", parent=forum)
    manual_topic = _channel(
        db, tg, "forum:2", kind="topic", parent=forum, project_id=other, map_source="manual"
    )

    body = {"account_id": tg, "project_id": project, "kind": "folder", "value": 4}
    r = await ui.post("/api/v1/map-rules", {**body, "dry_run": True})
    preview = r.json()
    targets = {e["channel_id"]: e["to_project_id"] for e in preview["examples"]}
    assert targets == {forum: project, auto_topic: project}
    assert manual_topic not in targets and preview["moved"] == 2

    before = _state(db)
    r = await ui.post("/api/v1/map-rules", body)
    after = _state(db)
    assert {cid for cid in after if after[cid][0] != before[cid][0]} == {forum, auto_topic}


# --- тест 6: DELETE с предпросмотром ------------------------------------------------------------------------


async def test_6_delete_dry_run_then_real(ui, db):
    first = insert_project(db, name="Первый")
    fallback = insert_project(db, name="Запасной")
    mail = _account(db, "mail")
    a = _channel(db, mail, "a", attrs={"from_domains": ["a.com"]})
    b = _channel(db, mail, "b", attrs={"from_domains": ["b.com"]})
    r = await ui.post(
        "/api/v1/map-rules", {"account_id": mail, "project_id": first, "kind": "domain", "value": "a.com"}
    )
    rule_a = r.json()["rule_id"]
    r = await ui.post(
        "/api/v1/map-rules", {"account_id": mail, "project_id": first, "kind": "domain", "value": "b.com"}
    )
    rule_b = r.json()["rule_id"]
    await ui.post("/api/v1/map-rules", {"account_id": mail, "project_id": fallback, "kind": "all"})
    await ui.post(
        "/api/v1/map-rules",
        {"account_id": mail, "project_id": fallback, "kind": "label", "value": "x", "priority": -5},
    )
    assert _state(db)[a][0] == first and _state(db)[b][0] == first

    before_state, before_audit, before_rules = _state(db), _audit_count(db), _rules_count(db)
    r = await ui.delete(f"/api/v1/map-rules/{rule_a}", params={"dry_run": "true"})
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview["dry_run"] is True and preview["description"] == "отправитель @a.com"
    assert preview["moved"] == 1
    assert preview["examples"] == [
        {"channel_id": a, "title": "a", "from_project_id": first, "to_project_id": fallback}
    ]
    assert (_state(db), _audit_count(db), _rules_count(db)) == (before_state, before_audit, before_rules)

    r = await ui.delete(f"/api/v1/map-rules/{rule_a}")
    assert r.status_code == 200 and r.json()["ok"] is True
    after = _state(db)
    assert {cid for cid in after if after[cid][0] != before_state[cid][0]} == {a}
    assert _audit(db, "map_rule_rm")[-1]["detail"]["rule_id"] == rule_a

    # в «Неразобранное», когда правил больше нет (удаляем «весь аккаунт» и b)
    rules = (await ui.get("/api/v1/map-rules")).json()
    for rule in rules:
        if rule["kind"] == "all":
            await ui.delete(f"/api/v1/map-rules/{rule['id']}")
    r = await ui.delete(f"/api/v1/map-rules/{rule_b}", params={"dry_run": "true"})
    assert r.json()["examples"][0]["to_project_id"] is None
    assert (await ui.delete("/api/v1/map-rules/99999", params={"dry_run": "true"})).status_code == 404
    assert (await ui.delete("/api/v1/map-rules/99999")).status_code == 404


# --- тест 7: дубли ------------------------------------------------------------------------------------------


async def test_7_duplicate_rule_conflict_ui_and_admin(ui, db, app):
    first = insert_project(db, name="Первый")
    second = insert_project(db, name="Второй")
    tg = _account(db, "telegram", folders=[{"id": 2, "title": "Initech"}])
    body = {"account_id": tg, "project_id": first, "kind": "folder", "value": 2}
    rule_id = (await ui.post("/api/v1/map-rules", body)).json()["rule_id"]
    r = await ui.post("/api/v1/map-rules", body)
    assert r.status_code == 409 and r.json()["detail"] == f"такое правило уже есть (#{rule_id})"
    r = await ui.post("/api/v1/map-rules", {**body, "dry_run": True})
    assert r.status_code == 409
    r = await ui.post("/api/v1/map-rules", {**body, "project_id": second})
    assert r.status_code == 200

    # админ-ручка (CLI) — тот же отказ: tests/internal/test_map_rules.py::test_f141_admin_duplicate_is_409


# --- тест 8: список правил ---------------------------------------------------------------------------------


async def test_8_rules_list_descriptions_counts_order(ui, db):
    p1 = insert_project(db, name="Первый")
    p2 = insert_project(db, name="Второй")
    tg = _account(db, "telegram", folders=[{"id": 2, "title": "Initech"}])
    work = _account(db, "slack", db_name="work", folders=[{"id": 5, "title": "Рабочая"}])
    mail = _account(db, "mail")
    _channel(db, tg, "c1", attrs={"tg_folders": [2]})
    _channel(db, tg, "c2", attrs={"tg_folders": [2]})
    _channel(db, tg, "named", title="Петя")
    _channel(db, work, "w1", attrs={"tg_folders": [5]}, db_name="work")
    _channel(db, mail, "m1", attrs={"labels": ["Счета"], "from_domains": ["bank.ru"]})

    async def add(**body):
        r = await ui.post("/api/v1/map-rules", body)
        assert r.status_code == 200, r.text
        return r.json()["rule_id"]

    r_folder = await add(account_id=tg, project_id=p1, kind="folder", value=2)
    r_all = await add(account_id=tg, project_id=p2, kind="all")
    from hub.core import mapping

    # правило-беседа и правило work-аккаунта (без label в main) — админским путём ядра
    def _admin_rules(conn):
        ch = mapping.add_rule(
            conn, account_id=tg, project_id=p1, match={"channel_ext_id": "named"}, priority=10, now_ms=1
        )[0]
        gone = mapping.add_rule(
            conn, account_id=tg, project_id=p1, match={"channel_ext_id": "nope"}, priority=10, now_ms=1
        )[0]
        wk = mapping.add_rule(
            conn,
            account_id=work,
            project_id=p1,
            match={"attr": "tg_folders", "has": 5, "label": "папка «Рабочая»"},
            priority=None,
            now_ms=1,
        )[0]
        return ch, gone, wk

    r_channel, r_gone, r_work = db.write_sync(_admin_rules)
    r_label = await add(account_id=mail, project_id=p1, kind="label", value="Счета")
    r_domain = await add(account_id=mail, project_id=p2, kind="domain", value="bank.ru", priority=3)

    rules = (await ui.get("/api/v1/map-rules")).json()
    assert [r["id"] for r in rules] == [r_channel, r_gone, r_folder, r_all, r_work, r_domain, r_label]
    by_id = {r["id"]: r for r in rules}
    assert by_id[r_folder] | {} == {
        "id": r_folder,
        "account_id": tg,
        "project_id": p1,
        "priority": 0,
        "kind": "folder",
        "value": 2,
        "description": "папка «Initech»",
        "channels": 2,
    }
    assert (by_id[r_all]["kind"], by_id[r_all]["value"], by_id[r_all]["description"]) == (
        "all",
        None,
        "весь аккаунт",
    )
    assert by_id[r_channel]["description"] == "беседа «Петя»" and by_id[r_channel]["channels"] == 1
    assert by_id[r_gone]["description"] == "беседа nope" and by_id[r_gone]["channels"] == 0
    assert by_id[r_work]["description"] == "папка «Рабочая»" and by_id[r_work]["channels"] == 1
    with db.reader() as conn:
        stored = conn.execute("SELECT match_json FROM main.map_rule WHERE id = ?", (r_work,)).fetchone()[0]
    assert "Рабочая" not in stored
    assert by_id[r_label]["description"] == "метка «Счета»" and by_id[r_label]["channels"] == 0
    assert by_id[r_domain]["description"] == "отправитель @bank.ru" and by_id[r_domain]["channels"] == 1


# --- тест 9: варианты условий ------------------------------------------------------------------------------


async def test_9_map_options(ui, db):
    p = insert_project(db, name="P")
    tg = _account(db, "telegram", folders=[{"id": 2, "title": "Initech"}, {"id": 3, "title": "Globex"}])
    _channel(db, tg, "a", attrs={"tg_folders": [2]})
    _channel(db, tg, "b", attrs={"tg_folders": [2, 3]}, project_id=p, map_source="manual")
    _channel(db, tg, "c", attrs={"tg_folders": [3]}, archived=1)
    r = await ui.get(f"/api/v1/accounts/{tg}/map-options")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "telegram" and body["kinds"] == ["folder", "all"]
    assert body["folders"] == [
        {"id": 2, "title": "Initech", "channels": 2, "unsorted": 1},
        {"id": 3, "title": "Globex", "channels": 1, "unsorted": 0},
    ]
    assert body["labels"] == [] and body["domains"] == []

    tg_empty = _account(db, "telegram")
    assert (await ui.get(f"/api/v1/accounts/{tg_empty}/map-options")).json()["kinds"] == ["all"]
    slack = _account(db, "slack", db_name="work")
    assert (await ui.get(f"/api/v1/accounts/{slack}/map-options")).json()["kinds"] == ["all"]

    mail = _account(db, "mail")
    for i in range(105):
        _channel(
            db, mail, f"m{i}", attrs={"from_domains": [f"d{i:03}.com"]}, project_id=p, map_source="manual"
        )
    _channel(db, mail, "x1", attrs={"from_domains": ["z.com"], "labels": ["B"]})
    _channel(db, mail, "x2", attrs={"from_domains": ["z.com", "y.com"], "labels": ["A", "B"]}, project_id=p)
    _channel(db, mail, "x3", attrs={"from_domains": ["y.com"]}, project_id=p)
    body = (await ui.get(f"/api/v1/accounts/{mail}/map-options")).json()
    assert body["kinds"] == ["domain", "label", "all"]
    assert body["labels"] == [
        {"name": "B", "channels": 2, "unsorted": 1},
        {"name": "A", "channels": 1, "unsorted": 0},
    ]
    assert len(body["domains"]) == 100
    assert body["domains"][:3] == [
        {"domain": "z.com", "channels": 2, "unsorted": 1},
        {"domain": "y.com", "channels": 2, "unsorted": 0},
        {"domain": "d000.com", "channels": 1, "unsorted": 0},
    ]

    mail_nolabels = _account(db, "mail")
    assert (await ui.get(f"/api/v1/accounts/{mail_nolabels}/map-options")).json()["kinds"] == [
        "domain",
        "all",
    ]
    assert (await ui.get("/api/v1/accounts/999/map-options")).status_code == 404


# --- тест 10: проекты ---------------------------------------------------------------------------------------


async def test_10_projects_create_and_patch(ui, db):
    r = await ui.post("/api/v1/projects", {"name": "  Личное ", "color": "#aabbcc"})
    assert r.status_code == 200, r.text
    project_id = r.json()["project_id"]
    listed = {p["id"]: p for p in (await ui.get("/api/v1/projects")).json()}
    assert listed[project_id]["name"] == "Личное" and listed[project_id]["color"] == "#aabbcc"
    assert _audit(db, "project_add")[-1]["detail"] == {"project_id": project_id, "name": "Личное"}

    for bad in ("", "   ", "x" * 41, "a\x07b", "все", "НЕРАЗОБРАННОЕ", "Главная"):
        r = await ui.post("/api/v1/projects", {"name": bad})
        assert r.status_code == 422, bad
    assert (await ui.post("/api/v1/projects", {"name": "x" * 40})).status_code == 200
    r = await ui.post("/api/v1/projects", {"name": "личное"})
    assert r.status_code == 409 and "Личное" in r.json()["detail"]
    assert (await ui.post("/api/v1/projects", {"name": "Цвет", "color": "red"})).status_code == 422
    assert (await ui.post("/api/v1/projects", {"name": "Воркдир", "workdir": "/tmp"})).status_code == 422

    r = await ui.patch(f"/api/v1/projects/{project_id}", {"name": "Личное 2", "color": "#000000"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    listed = {p["id"]: p for p in (await ui.get("/api/v1/projects")).json()}
    assert (listed[project_id]["name"], listed[project_id]["color"]) == ("Личное 2", "#000000")
    assert _audit(db, "project_update")[-1]["detail"] == {"project_id": project_id, "keys": ["color", "name"]}
    # своё же имя в другом регистре — можно; цвет снимается null
    assert (
        await ui.patch(f"/api/v1/projects/{project_id}", {"name": "ЛИЧНОЕ 2", "color": None})
    ).status_code == 200
    listed = {p["id"]: p for p in (await ui.get("/api/v1/projects")).json()}
    assert listed[project_id]["color"] is None and listed[project_id]["name"] == "ЛИЧНОЕ 2"
    assert (await ui.patch(f"/api/v1/projects/{project_id}", {"name": "x" * 40})).status_code == 409
    assert (await ui.patch(f"/api/v1/projects/{project_id}", {"color": "#12345"})).status_code == 422
    assert (await ui.patch(f"/api/v1/projects/{project_id}", {"name": None})).status_code == 422
    for bad_id in (0, -1, 99999):
        assert (await ui.patch(f"/api/v1/projects/{bad_id}", {"name": "Новое"})).status_code == 404


async def test_99_8_project_order(ui, db):
    a = insert_project(db, name="A")
    b = insert_project(db, name="B")
    c = insert_project(db, name="C")
    ids = [c, a, b]
    r = await ui.put("/api/v1/projects/order", {"ids": ids})
    assert r.status_code == 200 and r.json() == {"ok": True}
    listed = [p["id"] for p in (await ui.get("/api/v1/projects")).json()]
    assert [i for i in listed if i in ids] == ids
    assert _audit(db, "project_order")[-1]["detail"] == {"ids": ids}
    # неполный, лишний, повтор — 422, порядок не меняется
    for bad in ([a, b], [a, b, c, 99999], [a, a, b, c], [c, a, b, b]):
        assert (await ui.put("/api/v1/projects/order", {"ids": bad})).status_code == 422, bad
    listed = [p["id"] for p in (await ui.get("/api/v1/projects")).json()]
    assert [i for i in listed if i in ids] == ids
    # новый проект идёт в конец
    new = (await ui.post("/api/v1/projects", {"name": "Новый"})).json()["project_id"]
    assert [p["id"] for p in (await ui.get("/api/v1/projects")).json() if p["id"] > 0][-1] == new


# --- тест 11: перенос пачки ---------------------------------------------------------------------------------


async def test_11_channels_move(ui, db):
    target = insert_project(db, name="Цель")
    ruled = insert_project(db, name="По правилу")
    tg = _account(db, "telegram", folders=[{"id": 2, "title": "Initech"}])
    a = _channel(db, tg, "a", attrs={"tg_folders": [2]})
    b = _channel(db, tg, "b")
    forum = _channel(db, tg, "forum", kind="group")
    topic = _channel(db, tg, "forum:1", kind="topic", parent=forum)
    await ui.post("/api/v1/map-rules", {"account_id": tg, "project_id": ruled, "kind": "folder", "value": 2})

    r = await ui.post("/api/v1/channels/move", {"channel_ids": [a, b, forum], "project_id": target})
    assert r.status_code == 200 and r.json() == {"moved": 3}
    state = _state(db)
    assert all(state[c][:3] == (target, "manual", "manual") for c in (a, b, forum))
    assert state[topic][0] == target and state[topic][1] == "auto"
    audit = _audit(db, "channels_move")
    assert len(audit) == 1 and audit[0]["detail"] == {"channel_ids": [a, b, forum], "project_id": target}

    r = await ui.post("/api/v1/channels/move", {"channel_ids": [a, forum], "project_id": None})
    assert r.status_code == 200
    state = _state(db)
    assert (
        state[a][:2] == (None, "manual") and state[forum][:2] == (None, "manual") and state[topic][0] is None
    )

    r = await ui.post("/api/v1/channels/move", {"channel_ids": [a, b], "project_id": "auto"})
    assert r.status_code == 200
    state = _state(db)
    assert state[a][:2] == (ruled, "auto") and state[b][:2] == (None, "auto")
    assert len(_audit(db, "channels_move")) == 3

    for bad in (
        {"channel_ids": list(range(1, 202)), "project_id": None},
        {"channel_ids": [], "project_id": None},
        {"channel_ids": [a, a], "project_id": None},
        {"channel_ids": [a]},
        {"channel_ids": [a], "project_id": "rules"},
    ):
        assert (await ui.post("/api/v1/channels/move", bad)).status_code == 422, bad

    before = _state(db)
    r = await ui.post("/api/v1/channels/move", {"channel_ids": [a, 99999, b], "project_id": target})
    assert r.status_code == 404
    r = await ui.post("/api/v1/channels/move", {"channel_ids": [a, b], "project_id": 99999})
    assert r.status_code == 404
    r = await ui.post("/api/v1/channels/move", {"channel_ids": [a, b], "project_id": -1})
    assert r.status_code == 404
    assert _state(db) == before and len(_audit(db, "channels_move")) == 3


# --- тест 12: map_source в выдаче --------------------------------------------------------------------------


async def test_12_map_source_in_channel_payloads(ui, db):
    project = insert_project(db, name="P")
    tg = _account(db, "telegram")
    auto = _channel(db, tg, "auto", project_id=project)
    manual = _channel(db, tg, "manual", project_id=project, map_source="manual")
    for channel_id in (auto, manual):
        db.write_sync(
            lambda conn, cid=channel_id: conn.execute(
                "UPDATE main.channel SET seq = ? WHERE id = ?", (next_seq(conn), cid)
            )
        )

    one = (await ui.get(f"/api/v1/channels/{auto}")).json()
    assert one["map_source"] == "auto"
    assert (await ui.get(f"/api/v1/channels/{manual}")).json()["map_source"] == "manual"
    listed = {c["id"]: c for c in (await ui.get(f"/api/v1/projects/{project}/channels")).json()}
    assert listed[auto]["map_source"] == "auto" and listed[manual]["map_source"] == "manual"
    synced = (await ui.get("/api/v1/sync", params={"since": 0})).json()
    by_id = {c["id"]: c for c in synced["channels"]}
    assert by_id[auto]["map_source"] == "auto" and by_id[manual]["map_source"] == "manual"
