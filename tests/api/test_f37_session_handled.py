"""Ф37-3 (docs/architecture.md): «Разобрано сессией» — ядро.

- E1: выключатель проекта — по умолчанию выкл (403 с командой), `PATCH /projects/{id}` и админская ручка CLI.
- E2: отметка — беседа уходит из `GET /home/react`, строка `channel_handled` с ролью и пометкой;
  `read_ack` НЕ ставится; открытая карточка беседы — `done`.
- E3: входящее после `up_to` остаётся непрочитанным.
- E4: каждое условие B1 по отдельности — отказ без записи.
- E5: ответ в тред — маркер треда; повторная отметка — одна строка; 61-я в час — 429.

Стиль — как test_f37_autosend.py."""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import handled, security, subs
from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.db import db_of
from hub.core.feed import Notifier

from .conftest import (
    BASE_URL,
    csrf_headers,
    insert_channel,
    insert_message,
    insert_project,
    insert_thread,
    login_via_code,
)

ORIGIN = "http://127.0.0.1:8787"
A = (4242, 777_000, "sess-aaaaaaaa-1111")
B = (5151, 888_000, "sess-bbbbbbbb-2222")
MIN = 60_000
ROLE = "маркетолог"
NOTE = "автоответ об отпуске, ответа не требует"
ADMIN_SECRET = ADMIN_PREFIX + "handled-admin-secret-0123456789abcdefghijk"


@pytest.fixture
def procs(monkeypatch):
    table = {A[0]: A[1], B[0]: B[1]}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN, "X-Hub-Sub-Wait": "4"}


def _token(db, *, name="mcp:all", kind="interactive", scopes=("read", "draft"), project_ids=None):
    created = db.write_sync(
        lambda conn: security.create_api_token(
            conn, name=name, kind=kind, scopes=list(scopes), project_ids=project_ids, expires_ts=None
        )
    )
    return created.token, created.token_id


def _now(clock) -> int:
    return int(clock() * 1000)


def _account(db, source="telegram"):
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, config_json) VALUES (?, 'main', '{}')", (source,)
            ).lastrowid
        )
    )


@pytest.fixture
def world(db, tmp_path, clock, procs):
    dir3, dir4 = tmp_path / "p3", tmp_path / "p4"
    dir3.mkdir()
    dir4.mkdir()
    p3 = insert_project(db, name="Umbrella", workdir=str(dir3))
    p4 = insert_project(db, name="Initech", workdir=str(dir4))
    for pid in (p3, p4):
        db.write_sync(lambda conn, pid=pid: subs.set_project_subs(conn, pid, True, now_ms=0))
        db.write_sync(lambda conn, pid=pid: handled.set_project_enabled(conn, pid, True))
    acc = _account(db)
    ch = insert_channel(db, acc, project_id=p3, ext_id="dm1", kind="dm", title="Партнёр")
    first = insert_message(db, ch, ext_id="100", ts=_now(clock) - 10 * MIN, text="автоответ")
    last = insert_message(db, ch, ext_id="101", ts=_now(clock) - 5 * MIN, text="ещё автоответ")
    token, token_id = _token(db)
    return {
        "p3": p3,
        "p4": p4,
        "dir3": str(dir3),
        "dir4": str(dir4),
        "acc": acc,
        "ch": ch,
        "first": first,
        "last": last,
        "tok": token,
        "tok_id": token_id,
    }


@pytest.fixture
async def ui(app, db, clock):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as c:
        cookie, _csrf = await login_via_code(c, db, clock)
        headers = csrf_headers(cookie)

        class _Ui:
            async def get(self, path, **kw):
                return await c.get(path, **kw)

            async def post(self, path, body):
                return await c.post(path, json=body, headers=headers)

            async def patch(self, path, body):
                return await c.patch(path, json=body, headers=headers)

        yield _Ui()


def _sess(who) -> dict:
    return {"pid": who[0], "proc_start": who[1], "session_id": who[2]}


async def _sub(client, w, who, *, role=ROLE, cwd=None, token=None):
    session = {
        **_sess(who),
        "cwd": cwd or w["dir3"],
        "kind": "interactive",
        "name": None,
        "name_source": None,
    }
    body: dict = {"session": session}
    if role is not None:
        body["role"] = role
    r = await client.post("/api/v1/subs", json=body, headers=_h(token or w["tok"]))
    assert r.status_code == 200, r.text
    return r.json()


async def _mark(client, w, who=A, *, channel=None, up_to=None, note=NOTE, token=None):
    body: dict = {"up_to_message_id": up_to if up_to is not None else w["last"], "note": note}
    if who is not None:
        body["session"] = _sess(who)
    return await client.post(
        f"/api/v1/channels/{channel or w['ch']}/handled", json=body, headers=_h(token or w["tok"])
    )


@pytest.fixture
async def ready(client, world):
    sub = await _sub(client, world, A)
    return {**world, "sub_id": sub["sub"]["id"]}


def _rows(db) -> list[tuple]:
    with db.reader() as conn:
        return conn.execute(
            "SELECT channel_id, project_id, message_id, role, note, dismissed_ts FROM main.channel_handled "
            "ORDER BY id"
        ).fetchall()


def _audits(db, action="channel_handled") -> list[tuple]:
    with db.reader() as conn:
        return conn.execute(
            "SELECT actor, result, channel_id, detail_json FROM main.audit WHERE action = ? ORDER BY id",
            (action,),
        ).fetchall()


async def _channel(ui, channel_id) -> dict:
    return (await ui.get(f"/api/v1/channels/{channel_id}")).json()


async def _react_ids(ui) -> set[int]:
    items = (await ui.get("/api/v1/home/react")).json()
    return {item["channel"]["id"] for item in items if item.get("channel")}


async def _assert_refused(db, ui, r, status, words=None, channel=None):
    """Отказ без записи: строки нет, аудита отметки нет, беседа по-прежнему непрочитана."""
    assert r.status_code == status, r.text
    if words is not None:
        assert words in r.json()["detail"], r.text
    assert _rows(db) == [] and _audits(db) == []
    if channel is not None:
        assert (await _channel(ui, channel))["unread"] == 2


def _card(db, channel_id, message_id, status="new"):
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.agent_item (channel_id, label, summary, message_ids_json, "
                "last_message_id, covered_seq, status, provider, model, created_ts, updated_ts) "
                "VALUES (?, 'reply', 'сводка', ?, ?, 1, ?, 'p', 'm', 1, 1)",
                (channel_id, json.dumps([message_id]), message_id, status),
            ).lastrowid
        )
    )


# --- E1: выключатель проекта --------------------------------------------------------------------------------


async def test_e1_switch_off_by_default_then_on(client, ui, db, clock, ready):
    w = ready
    db.write_sync(lambda conn: handled.set_project_enabled(conn, w["p3"], False))
    projects = {p["id"]: p for p in (await ui.get("/api/v1/projects")).json()}
    assert projects[w["p3"]]["session_handled"] is False
    r = await _mark(client, w)
    await _assert_refused(db, ui, r, 403, f"hub project set {w['p3']} --session-handled on", w["ch"])
    assert (await _channel(ui, w["ch"]))["session_handled"] is False

    r = await ui.patch(f"/api/v1/projects/{w['p3']}", {"session_handled": True})
    assert r.status_code == 200, r.text
    projects = {p["id"]: p for p in (await ui.get("/api/v1/projects")).json()}
    assert projects[w["p3"]]["session_handled"] is True and projects[w["p4"]]["session_handled"] is True
    assert (await _channel(ui, w["ch"]))["session_handled"] is True
    assert (await _mark(client, w)).status_code == 200

    r = await ui.patch(f"/api/v1/projects/{w['p3']}", {"session_handled": None})
    assert r.status_code == 422
    # Bearer проект не правит.
    r = await client.patch(
        f"/api/v1/projects/{w['p3']}", json={"session_handled": False}, headers=_h(w["tok"])
    )
    assert r.status_code == 403


async def test_e1_new_project_is_off_and_admin_route(db, clock, world):
    fresh = insert_project(db, name="Личное")
    with db.reader() as conn:
        assert handled.project_enabled(conn, fresh) is False
    internal = create_internal_app(
        db, clock=clock, notifier=Notifier(), admin_key_hash=hash_secret(ADMIN_SECRET)
    )
    auth = {"Authorization": f"Bearer {ADMIN_SECRET}"}
    async with AsyncClient(transport=ASGITransport(app=internal), base_url="http://hub-internal") as admin:
        r = await admin.patch(
            f"/internal/v1/admin/projects/{fresh}", json={"session_handled": True}, headers=auth
        )
        assert r.status_code == 200 and r.json()["session_handled"] is True
        listed = {p["id"]: p for p in (await admin.get("/internal/v1/admin/projects", headers=auth)).json()}
        assert listed[fresh]["session_handled"] is True
        r = await admin.patch(
            f"/internal/v1/admin/projects/{fresh}", json={"session_handled": False}, headers=auth
        )
        assert r.json()["session_handled"] is False
        r = await admin.patch(
            "/internal/v1/admin/projects/999999", json={"session_handled": True}, headers=auth
        )
        assert r.status_code == 404
    assert len(_audits(db, "project_session_handled")) == 2


# --- E2: отметка --------------------------------------------------------------------------------------------


async def test_e2_mark_moves_channel_from_react_to_handled(client, ui, db, clock, ready):
    w = ready
    card = _card(db, w["ch"], w["last"])
    closed_before = _card(db, w["ch"], w["first"], "dismissed")
    assert w["ch"] in await _react_ids(ui)
    assert _rows(db) == []

    r = await _mark(client, w, note=f"  {NOTE}  ")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["channel_id"], body["message_id"], body["role"], body["note"]) == (
        w["ch"],
        w["last"],
        ROLE,
        NOTE,
    )
    assert body["changed"] is True and body["closed_cards"] == 1

    channel = await _channel(ui, w["ch"])
    assert channel["unread"] == 0 and channel["unread_react"] == 0
    assert w["ch"] not in await _react_ids(ui)
    assert _rows(db) == [(w["ch"], w["p3"], w["last"], ROLE, NOTE, None)]
    with db.reader() as conn:
        # Отметка в источник НЕ ставится: «прочитано» собеседнику ставит только сам владелец.
        assert conn.execute("SELECT COUNT(*) FROM main.read_ack").fetchone()[0] == 0
        statuses = dict(conn.execute("SELECT id, status FROM main.agent_item").fetchall())
    assert statuses == {card: "done", closed_before: "dismissed"}

    [audit] = _audits(db)
    assert audit[:3] == ("interactive", "changed", w["ch"])
    detail = json.loads(audit[3])
    assert detail == {
        "handled_id": body["id"],
        "message_id": w["last"],
        "sub_id": w["sub_id"],
        "role": ROLE,
        "note_len": len(NOTE),
        "closed_cards": 1,
    }
    assert NOTE not in audit[3]


# --- E3: входящее после up_to остаётся ---------------------------------------------------------------------


async def test_e3_later_incoming_stays_unread(client, ui, db, clock, ready):
    w = ready
    r = await _mark(client, w, up_to=w["first"])
    assert r.status_code == 200
    channel = await _channel(ui, w["ch"])
    assert channel["unread"] == 1 and w["ch"] in await _react_ids(ui)

    assert (await _mark(client, w)).status_code == 200
    clock.advance_ms(MIN)
    insert_message(db, w["ch"], ext_id="102", ts=_now(clock), text="а это уже владельцу")
    assert (await _channel(ui, w["ch"]))["unread"] == 1
    assert w["ch"] in await _react_ids(ui)

    # Только вперёд: отметка назад маркер не двигает.
    r = await _mark(client, w, up_to=w["first"])
    assert r.status_code == 200 and r.json()["changed"] is False
    assert (await _channel(ui, w["ch"]))["unread"] == 1


# --- E4: каждое условие B1 по отдельности ------------------------------------------------------------------


async def test_e4_no_session(client, ui, db, ready):
    await _assert_refused(db, ui, await _mark(client, ready, who=None), 403, "session", ready["ch"])


async def test_e4_dead_process(client, ui, db, ready, procs):
    del procs[A[0]]
    await _assert_refused(db, ui, await _mark(client, ready), 403, "сессия не найдена", ready["ch"])


async def test_e4_sub_without_role(client, ui, db, world):
    await _sub(client, world, A, role=None)
    await _assert_refused(db, ui, await _mark(client, world), 403, "с ролью", world["ch"])


async def test_e4_no_sub_at_all(client, ui, db, ready):
    """Сессия B жива и на том же токене, но не подписана."""
    await _assert_refused(db, ui, await _mark(client, ready, who=B), 403, "с ролью", ready["ch"])


async def test_e4_sub_of_other_project(client, ui, db, world):
    await _sub(client, world, A, cwd=world["dir4"])
    await _assert_refused(db, ui, await _mark(client, world), 403, "на проект беседы", world["ch"])


async def test_e4_channel_sub_only(client, ui, db, world):
    w = world
    session = {**_sess(A), "cwd": w["dir3"], "kind": "interactive", "name": None, "name_source": None}
    r = await client.post(
        "/api/v1/chan-subs", json={"channel_id": w["ch"], "session": session}, headers=_h(w["tok"])
    )
    assert r.status_code == 200, r.text
    await _assert_refused(db, ui, await _mark(client, w), 403, "на проект беседы", w["ch"])


@pytest.mark.parametrize(
    ("name", "kind", "scopes"),
    [
        ("mcp:wake-1", "interactive", ("read", "draft")),  # дежурная
        ("mcp:orchestrator", "interactive", ("read", "draft")),
        ("agent:bg", "background", ("read", "draft")),
        ("mcp:read", "interactive", ("read",)),  # без скоупа draft
    ],
)
async def test_e4_token_fails_check_attach(client, ui, db, clock, world, name, kind, scopes):
    """Строка подписки вставлена напрямую (через ручку такой токен не подпишется): отсекает сам токен."""
    w = world
    token, token_id = _token(db, name=name, kind=kind, scopes=scopes)
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.session_sub (project_id, api_token_id, session_pid, session_proc_start, "
            "session_id, session_cwd, created_ts, role, role_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (w["p3"], token_id, A[0], A[1], A[2], w["dir3"], _now(clock), ROLE, ROLE),
        )
    )
    await _assert_refused(db, ui, await _mark(client, w, token=token), 403, None, w["ch"])


async def test_e4_ui_session_cannot_mark(ui, db, ready):
    w = ready
    r = await ui.post(
        f"/api/v1/channels/{w['ch']}/handled",
        {"up_to_message_id": w["last"], "note": NOTE, "session": _sess(A)},
    )
    await _assert_refused(db, ui, r, 403, None, w["ch"])


async def test_e4_foreign_message_in_up_to(client, ui, db, clock, ready):
    w = ready
    other = insert_channel(db, w["acc"], project_id=w["p3"], ext_id="dm2", kind="dm")
    foreign = insert_message(db, other, ext_id="900", ts=_now(clock) - MIN)
    await _assert_refused(db, ui, await _mark(client, w, up_to=foreign), 422, "не из этой беседы", w["ch"])
    await _assert_refused(db, ui, await _mark(client, w, up_to=999_999), 422, "не из этой беседы", w["ch"])


@pytest.mark.parametrize("note", ["", "   ", "ок", "x" * 201])
async def test_e4_bad_note(client, ui, db, ready, note):
    await _assert_refused(db, ui, await _mark(client, ready, note=note), 422, "note", ready["ch"])


async def test_e4_archived_channel(client, ui, db, ready):
    w = ready
    db.write_sync(lambda conn: conn.execute("UPDATE main.channel SET archived = 1 WHERE id = ?", (w["ch"],)))
    await _assert_refused(db, ui, await _mark(client, w), 422, "архив")


async def test_e4_draft_channel(client, ui, db, clock, ready):
    w = ready
    draft = insert_channel(db, w["acc"], project_id=w["p3"], ext_id="new:abc", kind="dm")
    message = insert_message(db, draft, ext_id="1", ts=_now(clock) - MIN)
    await _assert_refused(db, ui, await _mark(client, w, channel=draft, up_to=message), 422, "новая беседа")


async def test_e4_invisible_channel_404(client, ui, db, ready):
    w = ready
    narrow, _ = _token(db, name="mcp:initech", project_ids=[w["p4"]])
    await _assert_refused(db, ui, await _mark(client, w, token=narrow), 404, None, w["ch"])


# --- E5: тред, повтор, лимит --------------------------------------------------------------------------------


async def test_e5_thread_reply_moves_thread_marker(client, ui, db, clock, ready):
    w = ready
    acc = _account(db, "slack")
    ch = insert_channel(db, acc, project_id=w["p3"], ext_id="C1", kind="group")
    now = _now(clock)
    parent = "1700000000.000100"
    insert_message(db, ch, ext_id=parent, ts=now - 20 * MIN, is_me=1)
    insert_message(db, ch, ext_id="1700000000.000200", ts=now - 15 * MIN, text="в ленте")
    insert_thread(db, ch, ext_id=parent, subscribed=1, first_seen_ts=now - 20 * MIN, last_reply_ts=now)
    reply = insert_message(db, ch, ext_id="1700000000.000300", ts=now - 10 * MIN, thread_ext_id=parent)
    insert_message(db, ch, ext_id="1700000000.000400", ts=now - 5 * MIN, thread_ext_id=parent)
    before = await _channel(ui, ch)
    assert (before["unread_top"], before["unread_threads"]) == (1, 2)

    r = await _mark(client, w, channel=ch, up_to=reply)
    assert r.status_code == 200, r.text
    after = await _channel(ui, ch)
    assert (after["unread_top"], after["unread_threads"]) == (1, 1)  # маркер беседы цел


async def test_e5_repeat_updates_single_row(client, ui, db, clock, ready):
    w = ready
    first = (await _mark(client, w, up_to=w["first"], note="первая пометка")).json()
    clock.advance_ms(MIN)
    second = (await _mark(client, w, note="вторая пометка")).json()
    assert second["id"] == first["id"]
    assert _rows(db) == [(w["ch"], w["p3"], w["last"], ROLE, "вторая пометка", None)]


async def test_e5_limit_per_hour(client, ui, db, clock, ready, monkeypatch):
    # Лимит 1000 в час (23.10) — гоняем на уменьшенном, проверяем механику окна.
    monkeypatch.setattr(handled, "HANDLED_PER_HOUR", 5)
    w = ready
    for i in range(handled.HANDLED_PER_HOUR):
        r = await _mark(client, w, note=f"пометка {i}")
        assert r.status_code == 200, (i, r.text)
    r = await _mark(client, w, note="лишняя")
    assert r.status_code == 429 and "5 в час" in r.json()["detail"]
    assert len(_audits(db)) == handled.HANDLED_PER_HOUR
    assert _rows(db)[0][4] == f"пометка {handled.HANDLED_PER_HOUR - 1}"
    clock.advance_ms(60 * MIN + 1)
    assert (await _mark(client, w, note="через час")).status_code == 200


async def test_agent_items_status_open(client, db, clock, world):
    """Ф37-2 B2: `GET /agent/items?status=open` — `new` и `seen` одним запросом."""
    w = world
    new, seen = _card(db, w["ch"], w["last"]), _card(db, w["ch"], w["last"], "seen")
    _card(db, w["ch"], w["last"], "done")
    r = await client.get(
        "/api/v1/agent/items", params={"status": "open", "channel": w["ch"]}, headers=_h(w["tok"])
    )
    assert r.status_code == 200, r.text
    assert sorted(item["id"] for item in r.json()) == [new, seen]


# --- 12.10.2026: закрытие карточки сессией снимает беседу с «Нужна реакция» ---------------------------


async def test_close_card_by_role_session_clears_react(client, ui, db, ready):
    w = ready
    older = _card(db, w["ch"], w["first"], "seen")
    card = _card(db, w["ch"], w["last"])
    assert w["ch"] in await _react_ids(ui)

    r = await client.post(
        f"/api/v1/agent/items/{card}/status",
        json={"status": "done", "session": _sess(A)},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text
    assert r.json()["handled_up_to"] == w["last"]
    channel = await _channel(ui, w["ch"])
    assert channel["unread"] == 0 and channel["unread_react"] == 0
    assert w["ch"] not in await _react_ids(ui)
    assert _rows(db) == [(w["ch"], w["p3"], w["last"], ROLE, handled.CARD_NOTE, None)]
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.read_ack").fetchone()[0] == 0
        statuses = dict(conn.execute("SELECT id, status FROM main.agent_item").fetchall())
    assert statuses == {older: "done", card: "done"}


async def test_close_cards_by_channel_clears_react(client, ui, db, ready):
    w = ready
    _card(db, w["ch"], w["first"], "seen")
    _card(db, w["ch"], w["last"])
    r = await client.post(
        "/api/v1/agent/items/channel-status",
        json={"channel_id": w["ch"], "status": "done", "session": _sess(A)},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text
    assert r.json()["handled_up_to"] == w["last"]
    assert w["ch"] not in await _react_ids(ui)


@pytest.mark.parametrize("case", ["no_session", "seen", "switch_off", "no_role"])
async def test_close_card_keeps_react_when_not_allowed(client, ui, db, world, case):
    w = world
    await _sub(client, w, A, role=None if case == "no_role" else ROLE)
    if case == "switch_off":
        db.write_sync(lambda conn: handled.set_project_enabled(conn, w["p3"], False))
    card = _card(db, w["ch"], w["last"])
    body: dict = {"status": "seen" if case == "seen" else "done"}
    if case != "no_session":
        body["session"] = _sess(A)
    r = await client.post(f"/api/v1/agent/items/{card}/status", json=body, headers=_h(w["tok"]))
    assert r.status_code == 200, r.text
    assert r.json()["handled_up_to"] is None
    assert (await _channel(ui, w["ch"]))["unread"] == 2
    assert w["ch"] in await _react_ids(ui)
    assert _rows(db) == [] and _audits(db) == []


# --- Ф66-3 D: пачка отметок ---------------------------------------------------------------------------------


def _extra_channel(db, clock, w, ext, *, project=None):
    ch = insert_channel(db, w["acc"], project_id=project or w["p3"], ext_id=ext, kind="dm", title=ext)
    msg = insert_message(db, ch, ext_id="1", ts=_now(clock) - 5 * MIN, text="входящее")
    return ch, msg


async def _batch(client, w, items, *, who=A, token=None):
    body: dict = {
        "items": [{"channel_id": c, "up_to_message_id": m, "note": NOTE} for c, m in items],
    }
    if who is not None:
        body["session"] = _sess(who)
    return await client.post("/api/v1/handled/batch", json=body, headers=_h(token or w["tok"]))


async def test_f66_batch_two_marked_one_refused(client, ui, db, clock, ready):
    w = ready
    ch2, msg2 = _extra_channel(db, clock, w, "dm2")
    foreign, foreign_msg = _extra_channel(db, clock, w, "dm-initech", project=w["p4"])
    r = await _batch(client, w, [(w["ch"], w["last"]), (foreign, foreign_msg), (ch2, msg2)])
    assert r.status_code == 200, r.text
    first, second, third = r.json()["results"]
    assert (first["ok"], first["channel_id"], first["message_id"]) == (True, w["ch"], w["last"])
    assert (second["ok"], second["channel_id"]) == (False, foreign)
    assert second["error"]
    assert (third["ok"], third["channel_id"]) == (True, ch2)
    assert {row[0] for row in _rows(db)} == {w["ch"], ch2}
    assert (await _channel(ui, w["ch"]))["unread"] == 0
    assert (await _channel(ui, foreign))["unread"] == 1  # чужая цела


async def test_f66_batch_over_50_is_422(client, db, ready):
    w = ready
    r = await _batch(client, w, [(w["ch"], w["last"])] * 51)
    assert r.status_code == 422
    assert _rows(db) == []
    r = await _batch(client, w, [])
    assert r.status_code == 422


async def test_f66_batch_rate_limit_in_the_middle(client, db, clock, ready, monkeypatch):
    w = ready
    monkeypatch.setattr(handled, "HANDLED_PER_HOUR", 2)
    chans = [_extra_channel(db, clock, w, f"dmx{i}") for i in range(3)]
    r = await _batch(client, w, chans)
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert [x["ok"] for x in results] == [True, True, False]
    assert results[2]["error"] == "rate_limited"
    assert {row[0] for row in _rows(db)} == {chans[0][0], chans[1][0]}


async def test_f66_batch_without_session_refuses_each(client, db, ready):
    w = ready
    r = await _batch(client, w, [(w["ch"], w["last"])], who=None)
    assert r.status_code == 200
    [item] = r.json()["results"]
    assert item["ok"] is False and item["error"]
    assert _rows(db) == []


# --- up_to="last": до последнего чужого живого сообщения ---------------------------------------------------


async def test_last_takes_latest_foreign_live_message(client, ui, db, clock, ready):
    w = ready
    now = _now(clock)
    mine = insert_message(db, w["ch"], ext_id="m1", ts=now - 3 * MIN, is_me=1)
    deleted = insert_message(db, w["ch"], ext_id="m2", ts=now - 2 * MIN, deleted=1)
    hidden = insert_message(db, w["ch"], ext_id="m3", ts=now - MIN)
    schema = db_of(w["ch"])
    sql = f"UPDATE {schema}.message SET hidden = 1 WHERE id = ?"
    db.write_sync(lambda conn: conn.execute(sql, (hidden,)))
    assert w["last"] < mine < deleted < hidden

    r = await _mark(client, w, up_to="last")
    assert r.status_code == 200, r.text
    assert r.json()["message_id"] == w["last"]
    assert _rows(db)[0][2] == w["last"]


async def test_last_refuses_when_nothing_to_mark(client, ui, db, clock, ready):
    w = ready
    ch = insert_channel(db, w["acc"], project_id=w["p3"], ext_id="dm-own", kind="dm", title="own")
    insert_message(db, ch, ext_id="1", ts=_now(clock) - MIN, is_me=1)
    r = await _mark(client, w, channel=ch, up_to="last")
    assert r.status_code == 422 and "нет чужого живого сообщения" in r.json()["detail"], r.text
    assert _rows(db) == [] and _audits(db) == []


async def test_last_in_batch_and_garbage_word_refused(client, db, clock, ready):
    w = ready
    r = await client.post(
        "/api/v1/handled/batch",
        json={
            "items": [{"channel_id": w["ch"], "up_to_message_id": "last", "note": NOTE}],
            "session": _sess(A),
        },
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text
    [row] = r.json()["results"]
    assert row["ok"] and row["message_id"] == w["last"]
    bad = await _mark(client, w, up_to="first")
    assert bad.status_code == 422


async def test_last_skips_thread_reply_and_backfilled_older(client, db, clock, ready):
    # «last» — лента беседы по порядку источника: ответ в треде (двигал бы маркер треда) и догнанное старое
    # сообщение с бо́льшим id не выбираются.
    w = ready
    now = _now(clock)
    older = insert_message(db, w["ch"], ext_id="old-backfill", ts=now - 30 * 24 * 60 * MIN)
    reply = insert_message(db, w["ch"], ext_id="r1", ts=now - MIN, thread_ext_id="root-x")
    assert w["last"] < older < reply

    r = await _mark(client, w, up_to="last")
    assert r.status_code == 200, r.text
    assert r.json()["message_id"] == w["last"]
