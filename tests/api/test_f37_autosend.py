"""Ф37-1 (docs/architecture.md): отправка без подтверждения
владельца в выбранные им беседы — ядро.

- E1: список — `PUT /channels/{id}/autosend` только UI (bearer — 403 `ui_only_denied`), 422 для заготовки,
  `no_reply`, архива, «Неразобранного»; поле `autosend` в JSON беседы (список, одна беседа, `/sync`);
  админская ручка CLI.
- E2: сессия с ролью из списка — строка сразу `queued`, `auto_role`, аудит `outbox_autosend`, коннектор её
  получает.
- E3: каждое условие B2 по отдельности не выполнено — обычный черновик.
- E4 + П8: лимит 20 в час на беседу, 60 в час на токен.
- E5: с файлом — уходит сразу (П6 снята владельцем).
- E6 + П7: статистика Ф19-1 авто-строку не считает ни подтверждённой, ни отклонённой.
- П3: брошенная подписка OpenCode. П9: отметка прочтения беседы не двигается.

Стиль — как test_f34_reminders.py: публичное приложение через ASGITransport, `/proc` подменён словарём,
время — `FakeClock`."""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import autosend, outbox, queries, security, subs
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
    login_via_code,
)

ORIGIN = "http://127.0.0.1:8787"
A = (4242, 777_000, "sess-aaaaaaaa-1111")
B = (5151, 888_000, "sess-bbbbbbbb-2222")
OC = (6161, 999_000, "ses_AAAAAAAAAA1")
MIN = 60_000
ROLE = "маркетолог"
ADMIN_SECRET = ADMIN_PREFIX + "autosend-admin-secret-0123456789abcdefghij"
PDF = b"%PDF-1.7\n" + b"x" * 100


@pytest.fixture
def procs(monkeypatch):
    table = {A[0]: A[1], B[0]: B[1], OC[0]: OC[1]}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN, "X-Hub-Sub-Wait": "4"}


def _token(db, *, name="mcp:all", kind="interactive", scopes=("read", "draft", "attach"), project_ids=None):
    created = db.write_sync(
        lambda conn: security.create_api_token(
            conn, name=name, kind=kind, scopes=list(scopes), project_ids=project_ids, expires_ts=None
        )
    )
    return created.token, created.token_id


def _now(clock) -> int:
    return int(clock() * 1000)


def _account(db, source="telegram"):
    config = {"idempotent_send": True, "pace_window": "00:00-24:00"}
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, config_json) VALUES (?, 'main', ?)",
                (source, json.dumps(config)),
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
    acc = _account(db)
    ch = insert_channel(db, acc, project_id=p3, ext_id="team", kind="group", title="Команда")
    ch2 = insert_channel(db, acc, project_id=p3, ext_id="other", kind="group", title="Другая")
    insert_message(db, ch, ext_id="100", ts=_now(clock) - 5 * MIN, text="вопрос маркетологу")
    token, token_id = _token(db)
    return {
        "p3": p3,
        "p4": p4,
        "dir3": str(dir3),
        "dir4": str(dir4),
        "acc": acc,
        "ch": ch,
        "ch2": ch2,
        "tok": token,
        "tok_id": token_id,
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

            async def put(self, path, body):
                return await c.put(path, json=body, headers=headers)

            async def patch(self, path, body):
                return await c.patch(path, json=body, headers=headers)

        yield _Ui()


def _sess(who) -> dict:
    return {"pid": who[0], "proc_start": who[1], "session_id": who[2]}


async def _sub(client, w, who, *, role=ROLE, cwd=None, kind="interactive", token=None, expect=200, **extra):
    session = {**_sess(who), "cwd": cwd or w["dir3"], "kind": kind, "name": None, "name_source": None}
    body: dict = {"session": session, **extra}
    if role is not None:
        body["role"] = role
    r = await client.post("/api/v1/subs", json=body, headers=_h(token or w["tok"]))
    assert r.status_code == expect, r.text
    return r.json()


async def _allow(ui, channel_id, role=ROLE, on=True, expect=200):
    r = await ui.put(f"/api/v1/channels/{channel_id}/autosend", {"role": role, "on": on})
    assert r.status_code == expect, r.text
    return r.json()


async def _send(client, w, who=A, *, channel=None, key="k1", text="ответ команде", token=None, **extra):
    body = {"channel_id": channel or w["ch"], "text": text, "idempotency_key": key, **extra}
    if who is not None:
        body["session"] = _sess(who)
    return await client.post("/api/v1/messages/send", json=body, headers=_h(token or w["tok"]))


def _row(db, outbox_id) -> dict:
    schema = db_of(outbox_id)
    with db.reader() as conn:
        cur = conn.execute(f"SELECT * FROM {schema}.outbox WHERE id = ?", (outbox_id,))  # noqa: S608
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, cur.fetchone(), strict=True))


def _audits(db, action) -> list[tuple]:
    with db.reader() as conn:
        return conn.execute(
            "SELECT actor, result, channel_id, outbox_id, detail_json FROM main.audit WHERE action = ? "
            "ORDER BY id",
            (action,),
        ).fetchall()


def _assert_plain_draft(db, r) -> dict:
    """Обычный черновик без пометок: `pending_approval`, `auto_role` пуст, аудита авто-отправки нет."""
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending_approval"
    assert "auto" not in body and "auto_denied" not in body
    row = _row(db, body["outbox_id"])
    assert row["status"] == "pending_approval" and row["auto_role"] is None
    assert _audits(db, "outbox_autosend") == []
    return body


@pytest.fixture
async def ready(client, ui, world):
    """Сессия A подписана на проект беседы ролью из списка; беседа `ch` разрешена этой роли."""
    sub = await _sub(client, world, A)
    await _allow(ui, world["ch"])
    return {**world, "sub_id": sub["sub"]["id"]}


# --- E1: список ---------------------------------------------------------------------------------------------


async def test_e1_put_ui_on_off_and_channel_json(client, ui, db, world):
    w = world
    body = await _allow(ui, w["ch"], " Маркетолог ")
    assert body == {"autosend": ["Маркетолог"], "changed": True}
    # Та же роль другим написанием — та же строка списка (ключ `subs.role_form`).
    assert (await _allow(ui, w["ch"], "маркетолог"))["changed"] is False
    assert (await _allow(ui, w["ch"], "продажи"))["autosend"] == ["Маркетолог", "продажи"]

    one = (await ui.get(f"/api/v1/channels/{w['ch']}")).json()
    assert one["autosend"] == ["Маркетолог", "продажи"]
    listed = (await ui.get(f"/api/v1/projects/{w['p3']}/channels")).json()
    by_id = {c["id"]: c for c in (listed["channels"] if isinstance(listed, dict) else listed)}
    assert by_id[w["ch"]]["autosend"] == ["Маркетолог", "продажи"]
    assert by_id[w["ch2"]]["autosend"] == []
    sync = (await ui.get("/api/v1/sync", params={"since": 0})).json()
    assert {c["id"]: c["autosend"] for c in sync["channels"]}[w["ch"]] == ["Маркетолог", "продажи"]

    # Выключение двигает `channel.seq`: беседа приходит в `/sync` с новым списком.
    cursor = sync["max_seq"]
    assert (await _allow(ui, w["ch"], "МАРКЕТОЛОГ", on=False)) == {"autosend": ["продажи"], "changed": True}
    sync = (await ui.get("/api/v1/sync", params={"since": cursor})).json()
    assert [(c["id"], c["autosend"]) for c in sync["channels"]] == [(w["ch"], ["продажи"])]

    audits = _audits(db, "channel_autosend")
    assert [(a[0], a[1], a[2]) for a in audits] == [
        ("ui", "changed", w["ch"]),
        ("ui", "unchanged", w["ch"]),
        ("ui", "changed", w["ch"]),
        ("ui", "changed", w["ch"]),
    ]
    assert json.loads(audits[0][4]) == {"role": "Маркетолог", "on": True}
    assert json.loads(audits[3][4]) == {"role": "МАРКЕТОЛОГ", "on": False}


async def test_e1_put_bearer_403_with_audit(client, db, world):
    w = world
    r = await client.put(
        f"/api/v1/channels/{w['ch']}/autosend", json={"role": ROLE, "on": True}, headers=_h(w["tok"])
    )
    assert r.status_code == 403
    organize, _ = _token(db, name="mcp:org", scopes=("read", "draft", "organize"))
    r = await client.put(
        f"/api/v1/channels/{w['ch']}/autosend", json={"role": ROLE, "on": True}, headers=_h(organize)
    )
    assert r.status_code == 403
    assert len(_audits(db, "ui_only_denied")) == 2
    with db.reader() as conn:
        assert autosend.list_all(conn) == []


async def test_e1_put_422_for_unfit_channels(ui, db, world):
    w = world
    unsorted = insert_channel(db, w["acc"], project_id=None, ext_id="u1")
    archived = insert_channel(db, w["acc"], project_id=w["p3"], ext_id="a1", archived=1)
    draft = insert_channel(db, w["acc"], project_id=w["p3"], ext_id="new:abc")
    tracker = insert_channel(db, w["acc"], project_id=w["p3"], ext_id="jira1")
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET meta_json = ? WHERE id = ?",
            (json.dumps({"attrs": {"no_reply": True}}), tracker),
        )
    )
    for channel_id in (unsorted, archived, draft, tracker):
        await _allow(ui, channel_id, expect=422)
    await _allow(ui, 999_999, expect=404)
    await _allow(ui, w["ch"], "a--b", expect=422)  # форма роли — та же, что у подписок
    await _allow(ui, w["ch"], "   ", expect=422)
    with db.reader() as conn:
        assert autosend.list_all(conn) == []
    # A5: выключить можно и у беседы, ушедшей в архив после включения.
    await _allow(ui, w["ch"])
    r = await ui.patch(f"/api/v1/channels/{w['ch']}", {"archived": True})
    assert r.status_code == 200
    assert (await _allow(ui, w["ch"], on=False))["autosend"] == []


async def test_e1_admin_routes_for_cli(db, clock, world):
    w = world
    internal = create_internal_app(
        db, clock=clock, notifier=Notifier(), admin_key_hash=hash_secret(ADMIN_SECRET)
    )
    auth = {"Authorization": f"Bearer {ADMIN_SECRET}"}
    async with AsyncClient(transport=ASGITransport(app=internal), base_url="http://hub-internal") as admin:
        path = f"/internal/v1/admin/channels/{w['ch']}/autosend"
        r = await admin.put(path, json={"role": ROLE, "on": True}, headers=auth)
        assert r.status_code == 200 and r.json() == {"id": w["ch"], "autosend": [ROLE], "changed": True}
        r = await admin.get("/internal/v1/admin/autosend", headers=auth)
        [item] = r.json()["items"]
        assert (item["channel_id"], item["role"], item["title"], item["project_id"]) == (
            w["ch"],
            ROLE,
            "Команда",
            w["p3"],
        )
        r = await admin.put(path, json={"role": ROLE, "on": False}, headers=auth)
        assert r.json()["autosend"] == []
        r = await admin.put(
            "/internal/v1/admin/channels/999999/autosend", json={"role": ROLE, "on": True}, headers=auth
        )
        assert r.status_code == 404
        assert (await admin.put(path, json={"role": ROLE, "on": True})).status_code in (401, 403)
    assert [a[0] for a in _audits(db, "channel_autosend")] == ["admin", "admin"]


# --- E2: сессия с ролью из списка — уходит сразу ------------------------------------------------------------


async def test_e2_session_with_allowed_role_sends_at_once(client, ui, db, clock, ready):
    w = ready
    r = await _send(client, w)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "queued" and body["auto"] == {"role": ROLE} and body["created"] is True
    row = _row(db, body["outbox_id"])
    assert row["status"] == "queued" and row["auto_role"] == ROLE

    [audit] = _audits(db, "outbox_autosend")
    assert audit[1:4] == ("queued", w["ch"], body["outbox_id"])
    assert json.loads(audit[4]) == {"sub_id": w["sub_id"], "role": ROLE, "session_id": A[2]}
    # B3: «подтверждено владельцем» в журнал не пишется.
    assert _audits(db, "outbox_approve") == []

    leases = db.write_sync(
        lambda conn: outbox.lease(conn, account_id=w["acc"], owner="c1", now_ms=_now(clock))
    )
    assert [(lease.outbox_id, lease.text) for lease in leases] == [(body["outbox_id"], "ответ команде")]

    # C1/C2: подпись «агент: роль» — в строке сообщения и в `/outbox`.
    messages = (await ui.get(f"/api/v1/channels/{w['ch']}/messages")).json()["messages"]
    mine = next(m for m in messages if m["outbox_id"] == body["outbox_id"])
    assert mine["outbox_auto_role"] == ROLE
    assert all(m["outbox_auto_role"] is None for m in messages if m["outbox_id"] != body["outbox_id"])
    one = (await ui.get(f"/api/v1/messages/{mine['id']}")).json()
    assert one["outbox_auto_role"] == ROLE
    sync = (await ui.get("/api/v1/sync", params={"since": 0})).json()
    assert next(m for m in sync["messages"] if m["id"] == mine["id"])["outbox_auto_role"] == ROLE
    assert (await ui.get(f"/api/v1/outbox/{body['outbox_id']}")).json()["auto_role"] == ROLE


async def test_e2_outbox_list_carries_auto_role(client, ui, db, clock, ready):
    w = ready
    auto_id = (await _send(client, w)).json()["outbox_id"]
    plain_id = (await _send(client, w, who=None, key="k2")).json()["outbox_id"]
    r = await ui.post(f"/api/v1/outbox/{plain_id}/approve", {})
    assert r.status_code == 200
    queued = (await ui.get("/api/v1/outbox", params={"status": "queued"})).json()
    items = queued["items"] if isinstance(queued, dict) else queued
    assert {item["id"]: item["auto_role"] for item in items} == {auto_id: ROLE, plain_id: None}


async def test_p9_autosend_does_not_move_read_marker(client, ui, db, ready):
    """П9 (и Ф37-2 A4): авто-отправка не читает за владельца — непрочитанное входящее остаётся непрочитанным,
    задания отметки в источник нет."""
    w = ready
    before = (await ui.get(f"/api/v1/channels/{w['ch']}")).json()
    assert before["unread"] == 1
    assert (await _send(client, w)).json()["status"] == "queued"
    after = (await ui.get(f"/api/v1/channels/{w['ch']}")).json()
    assert after["unread"] == 1
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.read_ack").fetchone()[0] == 0


# --- E3: каждое условие B2 по отдельности ------------------------------------------------------------------


async def test_e3_no_session(client, db, ready):
    _assert_plain_draft(db, await _send(client, ready, who=None))


async def test_e3_dead_process(client, db, ready, procs):
    del procs[A[0]]
    _assert_plain_draft(db, await _send(client, ready))


async def test_e3_replaced_process(client, db, ready, procs):
    procs[A[0]] = A[1] + 1  # pid занят другим процессом
    _assert_plain_draft(db, await _send(client, ready))


async def test_e3_sub_ended(client, db, ready):
    w = ready
    r = await client.post(
        f"/api/v1/subs/{w['sub_id']}/end",
        json={"session": _sess(A), "reason": "unsubscribed"},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text
    _assert_plain_draft(db, await _send(client, w))


async def test_e3_sub_without_role(client, ui, db, world):
    w = world
    await _sub(client, w, A, role=None)
    await _allow(ui, w["ch"])
    _assert_plain_draft(db, await _send(client, w))


async def test_e3_other_role(client, ui, db, world):
    w = world
    await _sub(client, w, A, role="аналитик")
    await _allow(ui, w["ch"])
    _assert_plain_draft(db, await _send(client, w))


async def test_e3_other_session_of_same_token(client, db, ready):
    """Сессия B жива и на том же токене, но подписки у неё нет — роль чужой сессии ей не достаётся."""
    _assert_plain_draft(db, await _send(client, ready, who=B))


async def test_e3_sub_of_other_project(client, ui, db, world):
    w = world
    await _sub(client, w, A, cwd=w["dir4"])  # та же роль, но подписка на проект p4
    await _allow(ui, w["ch"])
    _assert_plain_draft(db, await _send(client, w))


async def test_e3_channel_moved_to_other_project(client, ui, db, ready):
    """A5: перенос беседы список не чистит, но проект подписки и беседы больше не совпадают."""
    w = ready
    r = await ui.patch(f"/api/v1/channels/{w['ch']}", {"project_id": w["p4"]})
    assert r.status_code == 200
    assert (await ui.get(f"/api/v1/channels/{w['ch']}")).json()["autosend"] == [ROLE]
    _assert_plain_draft(db, await _send(client, w))


async def test_e3_channel_sub_only(client, ui, db, world):
    w = world
    session = {**_sess(A), "cwd": w["dir3"], "kind": "interactive", "name": None, "name_source": None}
    r = await client.post(
        "/api/v1/chan-subs", json={"channel_id": w["ch"], "session": session}, headers=_h(w["tok"])
    )
    assert r.status_code == 200, r.text
    await _allow(ui, w["ch"])
    _assert_plain_draft(db, await _send(client, w))


@pytest.mark.parametrize(
    ("name", "kind", "scopes"),
    [
        ("mcp:wake-1", "interactive", ("read", "draft")),  # дежурная
        ("mcp:orchestrator", "interactive", ("read", "draft")),  # оркестратор (по имени)
        ("mcp:orch", "interactive", ("read", "draft", "organize", "orchestrate")),  # оркестратор (по скоупу)
        ("mcp:work", "interactive", ("read", "draft", "runs")),  # рабочая сессия
        ("agent:bg", "background", ("read", "draft")),  # фоновый токен
        ("mcp:read", "interactive", ("read", "send")),  # без скоупа draft
    ],
)
async def test_e3_token_fails_check_attach(client, ui, db, clock, world, name, kind, scopes):
    """B2.2: токен дежурной, оркестратора, рабочей сессии, фоновый или без `draft` — черновик, даже если у
    него нашлась бы живая подписка с ролью из списка (строка подписки вставлена напрямую: через ручку такой
    токен подписаться не может, а условие проверяется само по себе)."""
    w = world
    token, token_id = _token(db, name=name, kind=kind, scopes=scopes)
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.session_sub (project_id, api_token_id, session_pid, session_proc_start, "
            "session_id, session_cwd, created_ts, role, role_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (w["p3"], token_id, A[0], A[1], A[2], w["dir3"], _now(clock), ROLE, ROLE),
        )
    )
    await _allow(ui, w["ch"])
    _assert_plain_draft(db, await _send(client, w, token=token))


async def test_e3_same_sub_row_with_plain_token_sends(client, ui, db, clock, world):
    """Контроль к тесту выше: та же напрямую вставленная строка подписки, но обычный токен сессии — уходит
    сразу (значит, в тесте выше отсекает именно `check_attach`)."""
    w = world
    token, token_id = _token(db, name="mcp:plain", scopes=("read", "draft"))
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.session_sub (project_id, api_token_id, session_pid, session_proc_start, "
            "session_id, session_cwd, created_ts, role, role_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (w["p3"], token_id, A[0], A[1], A[2], w["dir3"], _now(clock), ROLE, ROLE),
        )
    )
    await _allow(ui, w["ch"])
    assert (await _send(client, w, token=token)).json()["auto"] == {"role": ROLE}


async def test_e3_paced(client, db, ready):
    body = _assert_plain_draft(db, await _send(client, ready, paced=True))
    assert body["paced"] is True


async def test_e3_send_at(client, db, clock, ready):
    import datetime as dt

    moment = dt.datetime.fromtimestamp(clock() + 3600, tz=dt.UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    body = _assert_plain_draft(db, await _send(client, ready, send_at=moment))
    assert body["send_at_ts"] is not None


async def test_e3_draft_channel_new(client, db, clock, ready):
    """B2.5: беседа-заготовка `new:…` — первое сообщение новому адресату остаётся черновиком, даже если пара
    попала в список (строка списка вставлена напрямую: ручка владельца такую беседу не принимает)."""
    w = ready
    draft = insert_channel(db, w["acc"], project_id=w["p3"], ext_id="new:abc", kind="dm")
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.channel_autosend (channel_id, role_key, role, created_ts) VALUES (?, ?, ?, 0)",
            (draft, ROLE, ROLE),
        )
    )
    _assert_plain_draft(db, await _send(client, w, channel=draft))


async def test_e3_repeat_by_idempotency_key(client, ui, db, ready):
    """Повтор по ключу: вторая строка не создаётся и не подтверждается повторно; черновик, положенный ДО
    включения беседы, повтором по ключу сам не уходит."""
    w = ready
    await _allow(ui, w["ch"], on=False)
    first = _assert_plain_draft(db, await _send(client, w))
    await _allow(ui, w["ch"])
    r = await _send(client, w)
    body = r.json()
    assert body["created"] is False and body["outbox_id"] == first["outbox_id"]
    _assert_plain_draft(db, r)

    sent = (await _send(client, w, key="k2")).json()
    assert sent["status"] == "queued"
    again = (await _send(client, w, key="k2")).json()
    assert again["created"] is False and again["outbox_id"] == sent["outbox_id"]
    assert "auto" not in again and again["status"] == "queued"  # статус строки как есть, без второй отправки
    assert len(_audits(db, "outbox_autosend")) == 1
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.outbox").fetchone()[0] == 2


async def test_e3_channel_switched_off_between_calls(client, ui, db, ready):
    w = ready
    assert (await _send(client, w)).json()["status"] == "queued"
    await _allow(ui, w["ch"], on=False)
    body = (await _send(client, w, key="k2")).json()
    assert body["status"] == "pending_approval" and "auto" not in body and "auto_denied" not in body
    assert _row(db, body["outbox_id"])["auto_role"] is None
    assert len(_audits(db, "outbox_autosend")) == 1


async def test_e3_other_channel_not_in_list(client, db, ready):
    _assert_plain_draft(db, await _send(client, ready, channel=ready["ch2"]))


async def test_e3_ui_send_is_untouched(ui, db, ready):
    """Набранное владельцем уходит как раньше; `session` в теле у UI — 422."""
    w = ready
    r = await ui.post(
        "/api/v1/messages/send", {"channel_id": w["ch"], "text": "сам", "idempotency_key": "u1"}
    )
    assert r.status_code == 200 and r.json()["status"] == "queued" and "auto" not in r.json()
    assert _row(db, r.json()["outbox_id"])["auto_role"] is None
    r = await ui.post(
        "/api/v1/messages/send",
        {"channel_id": w["ch"], "text": "сам", "idempotency_key": "u2", "session": _sess(A)},
    )
    assert r.status_code == 422


async def test_d_bearer_still_cannot_approve(client, db, ready):
    """D: подтверждение чужого (и своего) черновика bearer-токеном — по-прежнему 403."""
    w = ready
    draft = _assert_plain_draft(db, await _send(client, w, who=None))
    r = await client.post(f"/api/v1/outbox/{draft['outbox_id']}/approve", json={}, headers=_h(w["tok"]))
    assert r.status_code == 403
    assert _row(db, draft["outbox_id"])["status"] == "pending_approval"


# --- П3: брошенная подписка OpenCode -----------------------------------------------------------------------


async def test_p3_stale_opencode_sub_is_not_live(client, ui, db, clock, world):
    w = world
    await _sub(client, w, OC, kind="opencode")
    await _allow(ui, w["ch"])
    assert (await _send(client, w, who=OC)).json()["auto"] == {"role": ROLE}
    # Процесс сервера OpenCode жив всегда; подписка старше 10 минут без ожидания — брошена (`subs._sub_dead`).
    clock.advance_ms(subs.OC_LIVE_GRACE_MS + MIN)
    _ = db  # подписка в базе по-прежнему не завершена: cleanup ещё не прошёл
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.session_sub WHERE ended_ts IS NULL").fetchone()[0] == 1
    body = (await _send(client, w, who=OC, key="k2")).json()
    assert body["status"] == "pending_approval" and "auto" not in body and "auto_denied" not in body


# --- E4 + П8: лимиты ----------------------------------------------------------------------------------------


async def test_e4_channel_limit_20_per_hour(client, ui, db, clock, ready):
    w = ready
    # Вторая сессия той же роли: лимит считается по беседе целиком, а не по сессии или написанию роли.
    await _sub(client, w, B, role="Маркетолог")
    for i in range(autosend.AUTOSEND_PER_HOUR):
        who = A if i % 2 else B
        body = (await _send(client, w, who=who, key=f"k{i}")).json()
        assert body["status"] == "queued", i
        clock.advance_ms(MIN)
    body = (await _send(client, w, key="over")).json()
    assert body["status"] == "pending_approval"
    assert body["auto_denied"] == "limit" and body["auto_limit"] == "channel" and "auto" not in body
    assert _row(db, body["outbox_id"])["auto_role"] is None
    assert len(_audits(db, "outbox_autosend")) == autosend.AUTOSEND_PER_HOUR

    # Через час после первой отправки — снова сразу.
    clock.advance_ms(60 * MIN - autosend.AUTOSEND_PER_HOUR * MIN + 1)
    assert (await _send(client, w, key="later")).json()["auto"] == {"role": ROLE}


async def test_e4_token_limit_60_per_hour_across_channels(client, ui, db, clock, ready):
    w = ready
    channels = [w["ch"], w["ch2"]]
    channels += [insert_channel(db, w["acc"], project_id=w["p3"], ext_id=f"x{i}") for i in range(2)]
    for channel_id in channels[1:]:
        await _allow(ui, channel_id)
    sent = 0
    for channel_id in channels:  # 4 беседы по 15 — ни одна не упирается в свой лимит 20
        for _ in range(15):
            body = (await _send(client, w, channel=channel_id, key=f"t{sent}")).json()
            assert body["status"] == "queued", sent
            sent += 1
    assert sent == autosend.AUTOSEND_TOKEN_PER_HOUR
    body = (await _send(client, w, key="over")).json()
    assert body["status"] == "pending_approval"
    assert body["auto_denied"] == "limit" and body["auto_limit"] == "token"


# --- E5: файлы ----------------------------------------------------------------------------------------------


async def test_e5_draft_with_file_goes_at_once(client, db, ready):
    """Владелец 04.10.2026 («должны полно отвечать на разрешённые чаты») — П6 снята: сообщение с файлом в
    разрешённой беседе уходит сразу, как и текст; в неразрешённой — обычный черновик."""
    w = ready
    r = await client.post(
        f"/api/v1/uploads?channel_id={w['ch']}",
        content=PDF,
        headers={"Authorization": f"Bearer {w['tok']}", "X-Filename": "a.pdf"},
    )
    assert r.status_code == 200, r.text
    r = await _send(client, w, text="с файлом", files=[r.json()["upload_id"]])
    body = r.json()
    assert (body["status"], body.get("auto")) == ("queued", {"role": ROLE}), r.text
    row = _row(db, body["outbox_id"])
    assert row["file_json"] is not None and row["auto_role"] == ROLE


async def test_e5_files_without_attach_scope_still_refused(client, ui, db, world):
    w = world
    token, _ = _token(db, name="mcp:noattach", scopes=("read", "draft"))
    await _sub(client, w, A, token=token)
    await _allow(ui, w["ch"])
    r = await _send(client, w, token=token, files=["0" * 32])
    assert r.status_code == 422 and "право прикладывать" in r.json()["detail"]


# --- E6 + П7: статистика Ф19-1 ------------------------------------------------------------------------------


async def test_e6_feedback_ignores_auto_rows(client, ui, db, clock, ready):
    w = ready
    auto_id = (await _send(client, w, text="ушло само")).json()["outbox_id"]
    assert (await ui.get("/api/v1/outbox/feedback")).json() == []

    # П7: владелец отменил авто-строку, пока она в очереди, — это не «отклонённый черновик».
    r = await ui.post(f"/api/v1/outbox/{auto_id}/cancel", {})
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert (await ui.get("/api/v1/outbox/feedback")).json() == []
    with db.reader() as conn:
        assert auto_id not in queries._approved_outbox_ids(conn)

    # Контроль: обычный черновик той же сессии, отменённый владельцем, — отказ.
    plain_id = (await _send(client, w, who=None, key="k2", text="черновик")).json()["outbox_id"]
    r = await ui.post(f"/api/v1/outbox/{plain_id}/cancel", {})
    assert r.status_code == 200
    items = (await ui.get("/api/v1/outbox/feedback")).json()
    assert [(item["id"], item["kind"]) for item in items] == [(plain_id, "rejected")]
