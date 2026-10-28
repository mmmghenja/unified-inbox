"""Ф41-2а (docs/architecture.md и дополнительные тесты 3а): сессия хаба — запись
ядра, подписка без живого процесса.

Стиль — как test_f37_autosend.py: публичное приложение через ASGITransport, `/proc` подменён словарём (для
внешних сессий), часы — `FakeClock`. «Перезапуск ядра» — новое приложение над той же базой после сброса
реестра в памяти (`held.reset`)."""

import asyncio
import datetime as dt
import json

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import chan_subs, handled, held, ingest, join, runner, security, subs, transcript
from hub.core import reactions as reactions_mod
from hub.core import rule_requests as rr_mod
from hub.core.app_public import create_public_app
from hub.core.db import db_of

from .conftest import (
    BASE_URL,
    PORT,
    csrf_headers,
    insert_channel,
    insert_message,
    insert_project,
    login_via_code,
)

ORIGIN = "http://127.0.0.1:8787"
A = (4242, 777_000, "sess-aaaaaaaa-1111")
B = (5151, 888_000, "sess-bbbbbbbb-2222")
EXTRA = [(6000 + i, 100_000 + i, f"sess-extra-{i:04d}") for i in range(8)]
MIN = 60_000
ROLE = "маркетолог"
CODER = "программист"


@pytest.fixture
def procs(monkeypatch):
    table = {who[0]: who[1] for who in (A, B, *EXTRA)}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


def _h(token: str, proto: int = 5) -> dict:
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN, "X-Hub-Sub-Wait": str(proto)}


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
    dir3, dir4, bare = tmp_path / "p3", tmp_path / "p4", tmp_path / "bare"
    for path in (dir3, dir4, bare):
        path.mkdir()
    p3 = insert_project(db, name="Umbrella", workdir=str(dir3))
    p4 = insert_project(db, name="Initech", workdir=str(dir4))
    off = insert_project(db, name="Выключен", workdir=str(bare))
    nodir = insert_project(db, name="Личное")
    for pid in (p3, p4):
        db.write_sync(lambda conn, pid=pid: subs.set_project_subs(conn, pid, True, now_ms=0))
        db.write_sync(lambda conn, pid=pid: handled.set_project_enabled(conn, pid, True))
    db.write_sync(lambda conn: subs.set_project_subs(conn, nodir, True, now_ms=0))
    acc = _account(db)
    ch = insert_channel(db, acc, project_id=p3, ext_id="team", kind="group", title="Команда")
    ch2 = insert_channel(db, acc, project_id=p3, ext_id="other", kind="group", title="Другая")
    last = insert_message(db, ch, ext_id="100", ts=_now(clock) - 5 * MIN, text="вопрос маркетологу")
    token, token_id = _token(db)
    host, _ = _token(db, name="work:host", kind="background", scopes=("work_host", "read"))
    return {
        "p3": p3,
        "p4": p4,
        "off": off,
        "nodir": nodir,
        "dir3": str(dir3),
        "dir4": str(dir4),
        "acc": acc,
        "ch": ch,
        "ch2": ch2,
        "last": last,
        "tok": token,
        "tok_id": token_id,
        "host": host,
    }


class _Ui:
    def __init__(self, client, headers):
        self.c, self.headers = client, headers

    async def get(self, path, **kw):
        return await self.c.get(path, **kw)

    async def post(self, path, body=None):
        return await self.c.post(path, json=body if body is not None else {}, headers=self.headers)

    async def put(self, path, body):
        return await self.c.put(path, json=body, headers=self.headers)


@pytest.fixture
async def ui(app, db, clock):
    """UI-сессия владельца — своим клиентом (bearer и cookie в одном запросе ядро не принимает)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as c:
        cookie, _csrf = await login_via_code(c, db, clock)
        yield _Ui(c, csrf_headers(cookie))


async def _mk(ui, w, *, project="p3", expect=200, **body) -> dict:
    body.setdefault("engine", "claude")
    r = await ui.post("/api/v1/sessions", {"project_id": w[project], **body})
    assert r.status_code == expect, r.text
    return r.json().get("session") or r.json()


def _hs(hub_id: int, *, cwd=None, kind="hub") -> dict:
    pid, start, sid = held.triple(hub_id)
    body = {"pid": pid, "proc_start": start, "session_id": sid}
    if cwd is not None:
        body.update({"cwd": cwd, "kind": kind, "name": None, "name_source": None})
    return body


def _sess(who, *, cwd=None) -> dict:
    body = {"pid": who[0], "proc_start": who[1], "session_id": who[2]}
    if cwd is not None:
        body.update({"cwd": cwd, "kind": "interactive", "name": None, "name_source": None})
    return body


async def _hsub(client, w, hub_id, *, expect=200, token=None, cwd=None, **extra) -> dict:
    """`hub_subscribe` сессии хаба: её MCP-сервер шлёт условную тройку и `kind="hub"`."""
    body = {"session": _hs(hub_id, cwd=cwd or w["dir3"]), **extra}
    r = await client.post("/api/v1/subs", json=body, headers=_h(token or w["tok"]))
    assert r.status_code == expect, r.text
    return r.json()


async def _sub(client, w, who, *, expect=200, token=None, cwd=None, **extra) -> dict:
    body = {"session": _sess(who, cwd=cwd or w["dir3"]), **extra}
    r = await client.post("/api/v1/subs", json=body, headers=_h(token or w["tok"]))
    assert r.status_code == expect, r.text
    return r.json()


async def _wait(client, w, sub_id, session, *, token=None, expect=200, timeout_s=1):
    r = await client.post(
        f"/api/v1/subs/{sub_id}/wait",
        json={"session": session, "waiter": "0123456789abcdef", "timeout_s": timeout_s},
        headers=_h(token or w["tok"]),
    )
    assert r.status_code == expect, r.text
    return r.json().get("signals") if r.status_code == 200 else r.json()


async def _host(client, w, path, body=None, *, token=None, expect=200):
    headers = _h(token or w["host"])
    if body is None:
        r = await client.get(f"/api/v1/work/host/{path}", headers=headers)
    else:
        r = await client.post(f"/api/v1/work/host/{path}", json=body, headers=headers)
    assert r.status_code == expect, r.text
    return r.json()


def _ev(chat, ext, ts, *, kind="group", text="x"):
    return {
        "type": "upsert",
        "channel": {"ext_id": chat, "kind": kind},
        "ext_id": ext,
        "ts": ts,
        "sender": {"ext_id": "U2", "name": "Пётр"},
        "is_me": False,
        "kind": "text",
        "text": text,
        "mentions_me": False,
    }


def _ingest(db, w, events, clock):
    now = _now(clock)
    result = db.write_sync(
        lambda conn: ingest.apply_batch(conn, account_id=w["acc"], events=events, cursor=None, now_ms=now)
    )
    assert result.dead == 0, result


def _sub_rec(db, sub_id) -> subs.SubRec:
    with db.reader() as conn:
        return subs.get_sub(conn, sub_id)


def _pending(db, sub_id) -> int:
    with db.reader() as conn:
        return subs._pending_counts(conn, [sub_id]).get(sub_id, 0)


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


def _restart(db, clock):
    """«Перезапуск ядра»: память процесса пуста, новое приложение над той же базой перечитывает реестр."""
    held.reset()
    return create_public_app(db, clock=clock, port=PORT)


# --- 3.1: создание, чтение, закрытие — права и проверки -----------------------------------------------------


async def test_create_only_ui_bearers_denied(client, ui, db, world):
    w = world
    body = {"project_id": w["p3"], "engine": "claude"}
    for token in (w["tok"], w["host"]):
        r = await client.post("/api/v1/sessions", json=body, headers=_h(token))
        assert r.status_code == 403, r.text
        r = await client.post("/api/v1/sessions/1/close", json={}, headers=_h(token))
        assert r.status_code == 403, r.text
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.hub_session").fetchone()[0] == 0
    assert len(_audits(db, "ui_only_denied")) == 4

    made = await _mk(ui, w, role=f" {ROLE} ", title="Маркетолог Umbrella", model="opus", effort="high")
    assert (made["project_id"], made["role"], made["role_no"]) == (w["p3"], ROLE, 1)
    assert (made["engine"], made["mode"], made["state"]) == ("claude", "manual", "asleep")
    assert (made["model"], made["effort"], made["title"]) == ("opus", "high", "Маркетолог Umbrella")
    assert (made["engine_session_id"], made["sub_id"], made["listening"]) == (None, None, False)
    assert made["workdir"] == w["dir3"] and made["created_by"] == "ui"
    assert "proc_pid" not in made and "proc_start" not in made
    assert _audits(db, "hub_session_create") == [
        {"hub_session_id": made["id"], "project_id": w["p3"], "engine": "claude", "role": True, "role_no": 1}
    ]
    assert subs.is_alive(-made["id"], 0)  # реестр пополнен после коммита


async def test_create_refusals(ui, db, world):
    w = world
    assert (await _mk(ui, w, project="nodir", expect=409))["detail"].startswith("у проекта нет рабочей")
    assert "выключены" in (await _mk(ui, w, project="off", expect=409))["detail"]
    assert "занято" in (await _mk(ui, w, role="владелец", expect=422))["detail"]
    assert "number" in (await _mk(ui, w, number=2, expect=422))["detail"]
    await _mk(ui, w, engine="gpt", expect=422)
    r = await ui.post("/api/v1/sessions", {"project_id": 999, "engine": "claude"})
    assert r.status_code == 404
    r = await ui.post("/api/v1/sessions", {"project_id": w["p3"], "engine": "claude", "mode": "turbo"})
    assert r.status_code == 422  # Ф41-5: режим — только `manual` или `auto`
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.hub_session").fetchone()[0] == 0
    assert len(held.current()) == 0


async def test_create_lines_free_number_and_ceiling(client, ui, world):
    w = world
    await _sub(client, w, A, role=ROLE)  # живая внешняя подписка держит «маркетолог» №1
    r = await _mk(ui, w, role=ROLE, number=1, expect=409)
    assert "уже слушает сессия" in r["detail"]
    one = await _mk(ui, w, role=ROLE)
    assert one["role_no"] == 2  # наименьший свободный — с учётом живых подписок
    two = await _mk(ui, w, role="МАРКЕТОЛОГ")
    assert two["role_no"] == 3  # и других сессий хаба (у `one` подписки ещё нет)
    r = await _mk(ui, w, role=ROLE, number=2, expect=409)
    assert "линию держит сессия хаба" in r["detail"]
    for _ in range(held.MAX_HUB_SESSIONS_PER_PROJECT - 2):
        await _mk(ui, w)
    r = await _mk(ui, w, expect=409)
    assert "уже 8 сессий хаба" in r["detail"]
    await _mk(ui, w, project="p4")  # потолок — на проект


async def test_get_sessions_visibility(client, ui, db, world):
    w = world
    mine = await _mk(ui, w, role=ROLE)
    other = await _mk(ui, w, project="p4")
    listed = (await ui.get("/api/v1/sessions")).json()["items"]
    assert [row["id"] for row in listed] == [mine["id"], other["id"]]
    assert listed[0]["workdir"] == w["dir3"]
    only = (await ui.get("/api/v1/sessions", params={"project_id": w["p4"]})).json()["items"]
    assert [row["id"] for row in only] == [other["id"]]

    r = await client.get("/api/v1/sessions", headers=_h(w["tok"]))
    rows = r.json()["items"]
    assert [row["id"] for row in rows] == [mine["id"], other["id"]]
    assert all("workdir" not in row and "proc_pid" not in row for row in rows)  # `workdir` — только куке UI
    # id разговора движка — тоже только владельцу и хосту (ревью, мелкое 1).
    await _host(
        client,
        w,
        f"sessions/{mine['id']}/bind",
        {"engine_session_id": "sess-движка", "proc_pid": 4242, "proc_start": 777_000, "state": "idle"},
    )
    rows = (await client.get("/api/v1/sessions", headers=_h(w["tok"]))).json()["items"]
    assert all("engine_session_id" not in row for row in rows)
    assert (await ui.get("/api/v1/sessions")).json()["items"][0]["engine_session_id"] == "sess-движка"
    assert (await _host(client, w, "sessions"))["items"][0]["engine_session_id"] == "sess-движка"
    narrow, _ = _token(db, name="mcp:iip", project_ids=[w["p3"]])
    r = await client.get("/api/v1/sessions", headers=_h(narrow))
    assert [row["id"] for row in r.json()["items"]] == [mine["id"]]  # П10: только проекты токена
    r = await client.get("/api/v1/sessions", headers=_h(w["host"]))
    assert r.status_code == 200  # у токена хоста есть `read`
    nothing, _ = _token(db, name="mcp:nothing", scopes=("draft",))
    assert (await client.get("/api/v1/sessions", headers=_h(nothing))).status_code == 403

    await ui.post(f"/api/v1/sessions/{other['id']}/close")
    assert [row["id"] for row in (await ui.get("/api/v1/sessions")).json()["items"]] == [mine["id"]]
    closed = (await ui.get("/api/v1/sessions", params={"closed": 1})).json()["items"]
    assert [(row["id"], row["close_reason"]) for row in closed] == [(other["id"], "closed_by_owner")]


# --- 3.2: подписка `kind == "hub"` --------------------------------------------------------------------------


async def test_subscribe_hub_success_and_shape(client, ui, db, world):
    w = world
    made = await _mk(ui, w, role=ROLE, title="М")
    # Папка запроса — чужая: проект берётся из записи, не поиском по `cwd` (П5).
    body = await _hsub(client, w, made["id"], cwd=w["dir4"])
    row = body["sub"]
    assert body["created"] is True
    assert (row["project_id"], row["role"], row["role_no"], row["mode"]) == (w["p3"], ROLE, 1, "cards")
    assert (row["held"], row["hub_session_id"], row["state"]) == (True, made["id"], "asleep")
    assert row["live"] is True and row["listening"] is False  # ждать будет хост (41-4) — честно `false`
    assert row["session_pid"] == -made["id"] and row["engine"] == "claude"
    rec = _sub_rec(db, row["id"])
    assert (rec.session_pid, rec.session_proc_start, rec.session_id) == held.triple(made["id"])

    # Роль и номер, совпадающие с записью, — можно; повтор — на месте.
    again = await _hsub(client, w, made["id"], role=ROLE, number=1, mode="messages")
    assert again["created"] is False and again["sub"]["id"] == row["id"]
    assert again["sub"]["mode"] == "messages"

    listed = (await ui.get("/api/v1/sessions")).json()["items"][0]
    assert (listed["sub_id"], listed["listening"]) == (row["id"], False)


async def test_subscribe_hub_refusals(client, ui, db, world):
    w = world
    made = await _mk(ui, w, role=ROLE)
    hub_id = made["id"]
    assert "задаёт владелец" in (await _hsub(client, w, hub_id, role=CODER, expect=422))["detail"]
    assert "задаёт владелец" in (await _hsub(client, w, hub_id, number=2, expect=422))["detail"]
    assert "задаёт владелец" in (await _hsub(client, w, hub_id, role="", expect=422))["detail"]
    r = await _hsub(client, w, hub_id, project_id=w["p4"], expect=403)
    assert "проектом сессии хаба" in r["detail"]
    narrow, _ = _token(db, name="mcp:initech", project_ids=[w["p4"]])
    assert "вне скоупа" in (await _hsub(client, w, hub_id, token=narrow, expect=403))["detail"]

    # П4: несогласованная тройка и вид — 422 ещё на разборе запроса.
    full = _hs(hub_id, cwd=w["dir3"])
    for bad in (
        {**full, "kind": "interactive"},  # условная тройка без `kind == "hub"`
        {**_sess(A, cwd=w["dir3"]), "kind": "hub"},  # `kind == "hub"` с настоящим pid
        {**full, "proc_start": 5},
        {**full, "session_id": "sess-движка"},  # id разговора движка в тройку не входит (П1)
        {**full, "session_id": f"hub:{hub_id + 1}"},
        {**full, "pid": 0},
        {**_sess(A, cwd=w["dir3"]), "proc_start": 0},  # ноль — только у условной тройки
    ):
        r = await client.post("/api/v1/subs", json={"session": bad}, headers=_h(w["tok"]))
        assert r.status_code == 422, (bad, r.text)
    # Неизвестная и закрытая сессия — 409 по базе.
    assert "не найдена" in (await _hsub(client, w, hub_id + 50, expect=409))["detail"]
    await ui.post(f"/api/v1/sessions/{hub_id}/close")
    assert "закрыта" in (await _hsub(client, w, hub_id, expect=409))["detail"]
    # Выключенные подписки проекта — 403, как у внешней.
    other = await _mk(ui, w, project="p4")
    db.write_sync(lambda conn: subs.set_project_subs(conn, w["p4"], False, now_ms=0))
    assert "выключены" in (await _hsub(client, w, other["id"], expect=403))["detail"]
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.session_sub").fetchone()[0] == 0


async def test_roleless_hub_session_does_not_inherit_role(client, ui, db, world):
    w = world
    made = await _mk(ui, w)
    pid, start, sid = held.triple(made["id"])
    # Прежняя подписка той же тройки с ролью (такой быть не должно — роль задаёт запись): не наследуется.
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.session_sub (project_id, api_token_id, session_pid, session_proc_start, "
            "session_id, session_cwd, created_ts, ended_ts, end_reason, role, role_key, mode, msg_filter) "
            "VALUES (?, ?, ?, ?, ?, '/w', 1, 2, 'session_gone', ?, ?, 'messages', 'personal')",
            (w["p3"], w["tok_id"], pid, start, sid, ROLE, ROLE),
        )
    )
    body = await _hsub(client, w, made["id"])
    assert body["sub"]["role"] is None
    assert body["sub"]["mode"] == "messages"  # режим своей линии наследуется как обычно
    assert "задаёт владелец" in (await _hsub(client, w, made["id"], number=3, expect=422))["detail"]
    assert (await _hsub(client, w, made["id"], role=""))["sub"]["role"] is None  # «без роли» = как в записи


async def test_ceilings_are_separate(client, ui, world):
    w = world
    hubs = [await _mk(ui, w) for _ in range(3)]
    for made in hubs:
        await _hsub(client, w, made["id"])
    for who in EXTRA[: subs.MAX_PROJECT_SUBS]:
        await _sub(client, w, who)  # пятая внешняя проходит при трёх сессиях хаба
    r = await _sub(client, w, EXTRA[5], expect=409)
    assert "уже 5 подписок" in r["detail"]
    extra = await _mk(ui, w)
    await _hsub(client, w, extra["id"])  # подписка сессии хаба — сверх пяти внешних


# --- П3: линию роли держит сама запись ----------------------------------------------------------------------


async def test_external_cannot_take_line_of_hub_session_without_live_sub(client, ui, db, clock, app, world):
    w = world
    made = await _mk(ui, w, role=ROLE, title="Маркетолог")

    async def _external_blocked():
        r = await _sub(client, w, A, role=ROLE, number=1, expect=409)
        assert "линию держит сессия хаба «Маркетолог»" in r["detail"]

    await _external_blocked()  # до первой подписки сессии хаба
    got = await _sub(client, w, A, role=ROLE)
    assert got["sub"]["role_no"] == 2  # номер внешней — с учётом линий сессий хаба
    # Смена роли на месте — тоже: заданный номер 409, без номера — мимо линии сессии хаба.
    r = await _sub(client, w, B, role=CODER)
    assert "линию держит" in (await _sub(client, w, B, role=ROLE, number=1, expect=409))["detail"]
    assert (await _sub(client, w, B, role=ROLE))["sub"]["role_no"] == 3
    await client.post(
        f"/api/v1/subs/{r['sub']['id']}/end",
        json={"session": _sess(B), "reason": "unsubscribed"},
        headers=_h(w["tok"]),
    )
    await client.post(
        f"/api/v1/subs/{got['sub']['id']}/end",
        json={"session": _sess(A), "reason": "unsubscribed"},
        headers=_h(w["tok"]),
    )

    sub_id = (await _hsub(client, w, made["id"]))["sub"]["id"]
    r = await _sub(client, w, A, role=ROLE, number=1, expect=409)
    assert f"уже слушает сессия #{sub_id}" in r["detail"]  # живая подписка — прежний текст отказа
    # После отписки сессии хаба.
    r = await client.post(
        f"/api/v1/subs/{sub_id}/end",
        json={"session": _hs(made["id"]), "reason": "unsubscribed"},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200 and r.json()["sub"]["end_reason"] == "unsubscribed"
    await _external_blocked()
    # После отзыва токена.
    other_tok, other_id = _token(db, name="mcp:second")
    sub2 = (await _hsub(client, w, made["id"], token=other_tok))["sub"]["id"]
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.api_token SET revoked = 1 WHERE id = ?", (other_id,))
    )
    assert (str(sub2), "token_revoked") in _cleanup(db, clock, app)
    await _external_blocked()
    # После `subs off` и снова `on`.
    sub3 = (await _hsub(client, w, made["id"]))["sub"]["id"]
    db.write_sync(lambda conn: subs.set_project_subs(conn, w["p3"], False, now_ms=_now(clock)))
    assert _sub_rec(db, sub3).end_reason == "subs_off"
    db.write_sync(lambda conn: subs.set_project_subs(conn, w["p3"], True, now_ms=_now(clock)))
    await _external_blocked()
    assert (await _hsub(client, w, made["id"]))["sub"]["role_no"] == 1  # сессия хаба свою линию получает


# --- главное: подписка переживает сон, чистку и перезапуск ядра ---------------------------------------------


async def test_pending_survives_asleep_cleanup_and_core_restart(client, ui, db, clock, app, world):
    w = world
    made = await _mk(ui, w, role=ROLE)
    hub_id = made["id"]
    session = _hs(hub_id)
    await _host(
        client,
        w,
        f"sessions/{hub_id}/bind",
        {"engine_session_id": "sess-движка-1", "proc_pid": 4242, "proc_start": 777_000, "state": "running"},
    )
    sub_id = (await _hsub(client, w, hub_id))["sub"]["id"]  # сначала `cards`
    body = await _hsub(
        client, w, hub_id, mode="messages", msg_filter="personal", keywords=["договор"], batch_min=0
    )
    assert body["sub"]["id"] == sub_id and body["sub"]["keywords"]["keys"] == ["договор"]
    assert body["sub"]["state"] == "running"
    # Подписка на беседу и её сигнал.
    r = await client.post(
        "/api/v1/chan-subs",
        json={"channel_id": w["ch2"], "session": _hs(hub_id, cwd=w["dir3"])},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text
    chan_sub_id = r.json()["sub"]["id"]
    assert r.json()["sub"]["held"] is True
    clock.advance_ms(MIN)
    _ingest(db, w, [_ev("team", "201", _now(clock), text="пришлите договор")], clock)  # «по ключу»
    _ingest(db, w, [_ev("other", "301", _now(clock), text="привет")], clock)  # беседа под подпиской
    # Ждущее сообщение агента по линии роли — от внешней сессии другого проекта.
    await _sub(client, w, B, cwd=w["dir4"], role=CODER)
    r = await client.post(
        "/api/v1/peer-msgs",
        json={"text": "передай клиенту", "to": {"project_id": w["p3"], "role": ROLE}, "session": _sess(B)},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 201, r.text
    msg_id = r.json()["msg"]["id"]
    assert r.json()["recipient"]["live"] is True  # спящая или нет — линия жива

    def _state():
        with db.reader() as conn:
            chan = conn.execute(
                "SELECT status FROM main.session_chan_signal WHERE chan_sub_id = ?", (chan_sub_id,)
            ).fetchall()
            peer = conn.execute("SELECT status FROM main.peer_msg WHERE id = ?", (msg_id,)).fetchone()[0]
        return _pending(db, sub_id), chan, peer

    before = _state()
    assert before == (1, [("pending",)], "pending")

    await _host(client, w, f"sessions/{hub_id}/state", {"state": "asleep"})
    clock.advance_ms(30 * MIN)  # дольше окна «брошенной подписки» OpenCode и без единого ожидания
    assert _cleanup(db, clock, app) == []
    assert _state() == before

    app2 = _restart(db, clock)
    assert subs.is_alive(-hub_id, 0)  # реестр перечитан из базы
    async with AsyncClient(transport=ASGITransport(app=app2), base_url=BASE_URL) as c2:
        assert _cleanup(db, clock, app2) == []
        assert _state() == before
        rows = (await c2.get("/api/v1/subs", headers=_h(w["tok"]), params=session)).json()
        mine = [row for row in rows["items"] if row["mine"]]
        assert [(row["id"], row["live"], row["state"], row["pending"]) for row in mine] == [
            (sub_id, True, "asleep", 1)
        ]
        assert [row["id"] for row in rows["channels"] if row["mine"]] == [chan_sub_id]
        # Выдача после перезапуска — то же: сигнал сообщения «по ключу» и сигнал сообщения агента.
        signals = await _wait(c2, w, sub_id, session)
        kinds = sorted(sig.get("kind", "card") for sig in signals)
        assert kinds == ["messages", "peer"], signals
        assert next(sig for sig in signals if sig["kind"] == "messages")["channel_id"] == w["ch"]
        r = await c2.post(
            "/api/v1/chan-subs/wait",
            json={"session": session, "waiter": "0123456789abcdef", "timeout_s": 1},
            headers=_h(w["tok"]),
        )
        assert r.status_code == 200, r.text
        assert [sig["channel_id"] for sig in r.json()["signals"]] == [w["ch2"]]
        inbox = await c2.get("/api/v1/peer-msgs", params=session, headers=_h(w["tok"]))
        assert [row["id"] for row in inbox.json()["items"]] == [msg_id]


async def test_roleless_hub_peer_msg_not_lost(client, ui, db, clock, app, world):
    """Сообщение безрольному агенту привязано к подписке (`to_sub_id`): у спящей сессии хаба не `lost`."""
    w = world
    made = await _mk(ui, w)
    sub_id = (await _hsub(client, w, made["id"]))["sub"]["id"]
    r = await ui.post(
        "/api/v1/peer-msgs", {"text": "от владельца", "to": {"project_id": w["p3"], "sub_id": sub_id}}
    )
    assert r.status_code == 201, r.text
    msg_id = r.json()["msg"]["id"]
    await _host(client, w, f"sessions/{made['id']}/state", {"state": "asleep"})
    clock.advance_ms(30 * MIN)
    held.reset()  # даже с пустым реестром чистка ничего не завершает
    assert _cleanup(db, clock, app) == []
    app2 = _restart(db, clock)
    assert _cleanup(db, clock, app2) == []
    with db.reader() as conn:
        assert conn.execute("SELECT status FROM main.peer_msg WHERE id = ?", (msg_id,)).fetchone()[0] == (
            "pending"
        )
    async with AsyncClient(transport=ASGITransport(app=app2), base_url=BASE_URL) as c2:
        inbox = await c2.get("/api/v1/peer-msgs", params=_hs(made["id"]), headers=_h(w["tok"]))
        assert [row["id"] for row in inbox.json()["items"]] == [msg_id]


async def test_resubscribe_same_triple_in_place_keeps_pending(client, ui, db, clock, world):
    w = world
    made = await _mk(ui, w, role=ROLE)
    sub_id = (await _hsub(client, w, made["id"], mode="messages"))["sub"]["id"]
    clock.advance_ms(MIN)
    _ingest(db, w, [_ev("team", "201", _now(clock))], clock)
    assert _pending(db, sub_id) == 1
    again = await _hsub(client, w, made["id"])
    assert (again["created"], again["sub"]["id"], again["sub"]["pending"]) == (False, sub_id, 1)


# --- 3.3: хост — привязка разговора и состояние -------------------------------------------------------------


async def test_host_bind_changes_conversation_without_touching_subs(
    client, ui, db, clock, world, monkeypatch
):
    w = world
    made = await _mk(ui, w, role=ROLE)
    other = await _mk(ui, w, role=CODER)
    hub_id = made["id"]
    seen: list[str] = []
    monkeypatch.setattr(transcript, "state_for", lambda sid: seen.append(sid) or {"ttl_s": 300})
    listed = await _host(client, w, "sessions")
    assert [(row["id"], row["workdir"], row["proc_pid"]) for row in listed["items"]] == [
        (hub_id, w["dir3"], None),
        (other["id"], w["dir3"], None),
    ]
    bind = {"engine_session_id": "sess-1", "proc_pid": 4242, "proc_start": 777_000, "state": "idle"}
    row = (await _host(client, w, f"sessions/{hub_id}/bind", bind))["session"]
    assert (row["engine_session_id"], row["state"], row["proc_pid"]) == ("sess-1", "idle", 4242)
    assert row["host_seen_ts"] == _now(clock)

    sub_id = (await _hsub(client, w, hub_id, mode="messages"))["sub"]["id"]
    clock.advance_ms(MIN)
    _ingest(db, w, [_ev("team", "201", _now(clock))], clock)
    key_before = subs.skey(_sub_rec(db, sub_id))

    # Смена разговора движка: подписка на месте, ждущее цело, тройка и `skey` прежние (П1).
    await _host(
        client, w, f"sessions/{hub_id}/bind", {**bind, "engine_session_id": "sess-2", "state": "running"}
    )
    rec = _sub_rec(db, sub_id)
    assert rec.ended_ts is None and rec.session_id == f"hub:{hub_id}"
    assert subs.skey(rec) == key_before and _pending(db, sub_id) == 1
    assert _audits(db, "hub_session_bind") == [
        {"hub_session_id": hub_id, "old": None, "new": "sess-1"},
        {"hub_session_id": hub_id, "old": "sess-1", "new": "sess-2"},
    ]
    rows = (await ui.get("/api/v1/subs")).json()["items"]
    mine = next(row for row in rows if row["id"] == sub_id)
    assert (mine["state"], mine["engine"], mine["cache"]) == ("running", "claude", {"ttl_s": 300})
    assert seen[-1] == "sess-2"  # срок кэша — по разговору движка из записи, не по метке `hub:<id>`
    # Прежний id разговора личностью не был и не стал: опознаётся только условная тройка.
    r = await client.post(
        f"/api/v1/subs/{sub_id}/wait",
        json={"session": {**_hs(hub_id), "session_id": "sess-1"}, "waiter": "0" * 16, "timeout_s": 1},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 422
    assert len(await _wait(client, w, sub_id, _hs(hub_id))) == 1

    # Занятый разговор — 409; `asleep` обнуляет настоящий процесс; чужие токены — 403; закрытая — 410.
    r = await _host(
        client, w, f"sessions/{other['id']}/bind", {**bind, "engine_session_id": "sess-2"}, expect=409
    )
    assert f"#{hub_id}" in r["detail"]
    row = (await _host(client, w, f"sessions/{hub_id}/state", {"state": "asleep"}))["session"]
    assert (row["state"], row["proc_pid"], row["proc_start"], row["engine_session_id"]) == (
        "asleep",
        None,
        None,
        "sess-2",
    )
    wake, _ = _token(db, name="mcp:wake-p3", scopes=("read", "draft", "runs"), project_ids=[w["p3"]])
    for token in (w["tok"], wake):
        await _host(client, w, "sessions", token=token, expect=403)
        await _host(client, w, f"sessions/{hub_id}/bind", bind, token=token, expect=403)
        await _host(client, w, f"sessions/{hub_id}/state", {"state": "idle"}, token=token, expect=403)
    r = await ui.post(f"/api/v1/work/host/sessions/{hub_id}/state", {"state": "idle"})
    assert r.status_code == 403  # кука UI — не хост
    await _host(client, w, "sessions/999/state", {"state": "idle"}, expect=404)
    # Настоящий процесс: `starttime` нулём не бывает (ревью, мелкое 3).
    await _host(client, w, f"sessions/{hub_id}/bind", {**bind, "proc_start": 0}, expect=422)
    await _host(
        client,
        w,
        f"sessions/{hub_id}/state",
        {"state": "idle", "proc_pid": 4242, "proc_start": 0},
        expect=422,
    )
    await ui.post(f"/api/v1/sessions/{other['id']}/close")
    await _host(client, w, f"sessions/{other['id']}/state", {"state": "idle"}, expect=410)
    await _host(client, w, f"sessions/{other['id']}/bind", bind, expect=410)


async def test_host_stale_goes_asleep_subs_untouched(client, ui, db, clock, app, world):
    w = world
    made = await _mk(ui, w, role=ROLE)
    sub_id = (await _hsub(client, w, made["id"]))["sub"]["id"]
    await _host(
        client,
        w,
        f"sessions/{made['id']}/state",
        {"state": "running", "proc_pid": 4242, "proc_start": 777_000},
    )
    clock.advance_ms(held.HOST_STALE_MS + 1000)
    assert _cleanup(db, clock, app) == []
    row = (await ui.get("/api/v1/sessions")).json()["items"][0]
    assert (row["state"], row["sub_id"]) == ("asleep", sub_id)
    assert _sub_rec(db, sub_id).ended_ts is None


# --- закрытие -----------------------------------------------------------------------------------------------


async def test_close_ends_subs_for_good_and_frees_line(client, ui, db, clock, world):
    w = world
    made = await _mk(ui, w, role=ROLE)
    hub_id = made["id"]
    sub_id = (await _hsub(client, w, hub_id, mode="messages"))["sub"]["id"]
    r = await client.post(
        "/api/v1/chan-subs",
        json={"channel_id": w["ch2"], "session": _hs(hub_id, cwd=w["dir3"])},
        headers=_h(w["tok"]),
    )
    chan_sub_id = r.json()["sub"]["id"]
    clock.advance_ms(MIN)
    _ingest(db, w, [_ev("team", "201", _now(clock))], clock)
    assert _pending(db, sub_id) == 1

    r = await ui.post(f"/api/v1/sessions/{hub_id}/close")
    assert r.status_code == 200 and r.json()["closed"] is True
    assert r.json()["session"]["close_reason"] == "closed_by_owner"
    assert _sub_rec(db, sub_id).end_reason == "ended_by_owner"
    with db.reader() as conn:
        assert chan_subs.get(conn, chan_sub_id).end_reason == "ended_by_owner"
    assert not subs.is_alive(-hub_id, 0)  # реестр — после коммита
    assert _audits(db, "hub_session_close") == [
        {"hub_session_id": hub_id, "project_id": w["p3"], "subs": [sub_id], "chan_subs": [chan_sub_id]}
    ]
    again = await ui.post(f"/api/v1/sessions/{hub_id}/close")
    assert again.status_code == 200 and again.json()["closed"] is False  # идемпотентно
    assert len(_audits(db, "hub_session_close")) == 1
    assert (await ui.post("/api/v1/sessions/999/close")).status_code == 404

    # Линия свободна: внешняя сессия той же роли подписывается и НЕ получает ждущее закрытой.
    got = await _sub(client, w, A, role=ROLE, mode="messages")
    assert got["sub"]["role_no"] == 1 and got["sub"]["pending"] == 0
    assert "restored_channels" not in got  # причина `ended_by_owner` — не из причин возврата
    with db.reader() as conn:
        assert chan_subs.session_subs(conn, w["tok_id"], *A) == []
    # Новая сессия хаба получает новую тройку — ничего чужого.
    fresh = await _mk(ui, w, role=CODER)
    assert fresh["id"] > hub_id


async def test_close_during_wait_answers_410(client, ui, world):
    w = world
    made = await _mk(ui, w, role=ROLE)
    sub_id = (await _hsub(client, w, made["id"]))["sub"]["id"]
    waiting = asyncio.create_task(
        client.post(
            f"/api/v1/subs/{sub_id}/wait",
            json={"session": _hs(made["id"]), "waiter": "0123456789abcdef", "timeout_s": 30},
            headers=_h(w["tok"]),
        )
    )
    await asyncio.sleep(0.2)
    assert not waiting.done()
    assert (await ui.post(f"/api/v1/sessions/{made['id']}/close")).status_code == 200
    r = await asyncio.wait_for(waiting, timeout=5)
    assert r.status_code == 410 and r.json()["detail"] == {"end_reason": "ended_by_owner"}


# --- П4: тройка сессии хаба во всех ручках ------------------------------------------------------------------


async def test_get_handlers_accept_negative_pid(client, ui, world):
    w = world
    made = await _mk(ui, w, role=ROLE)
    sub_id = (await _hsub(client, w, made["id"]))["sub"]["id"]
    me = _hs(made["id"])
    r = await client.get("/api/v1/subs", params=me, headers=_h(w["tok"]))
    assert r.status_code == 200 and [row["mine"] for row in r.json()["items"]] == [True]
    r = await client.get("/api/v1/reminders", params=me, headers=_h(w["tok"]))
    assert r.status_code == 200, r.text
    r = await client.get("/api/v1/peer-msgs", params=me, headers=_h(w["tok"]))
    assert r.status_code == 200 and r.json()["sub"]["id"] == sub_id
    for path in ("subs", "reminders", "peer-msgs"):
        for bad in ({**me, "proc_start": 3}, {**me, "session_id": "sess-x"}, {**me, "pid": 0}):
            r = await client.get(f"/api/v1/{path}", params=bad, headers=_h(w["tok"]))
            assert r.status_code == 422, (path, bad, r.text)


async def test_chan_sub_bad_triple_422_without_side_effects(client, ui, db, world):
    w = world
    made = await _mk(ui, w, role=ROLE)
    await _sub(client, w, A)
    r = await client.post(
        "/api/v1/chan-subs",
        json={"channel_id": w["ch2"], "session": _sess(A, cwd=w["dir3"])},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200
    counts = lambda: _counts(db)  # noqa: E731
    before = counts()
    full = _hs(made["id"], cwd=w["dir3"])
    for bad in ({**full, "kind": "interactive"}, {**full, "proc_start": 1}, {**full, "session_id": A[2]}):
        r = await client.post(
            "/api/v1/chan-subs", json={"channel_id": w["ch2"], "session": bad}, headers=_h(w["tok"])
        )
        assert r.status_code == 422, r.text
    r = await client.post(
        "/api/v1/chan-subs",
        json={"channel_id": w["ch2"], "session": _hs(made["id"] + 7, cwd=w["dir3"])},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 409 and "не найдена" in r.json()["detail"]
    await ui.post(f"/api/v1/sessions/{made['id']}/close")
    r = await client.post(
        "/api/v1/chan-subs", json={"channel_id": w["ch2"], "session": full}, headers=_h(w["tok"])
    )
    assert r.status_code == 409
    after = counts()
    assert (after["chan"], after["chan_audit"]) == (before["chan"], before["chan_audit"])


def _counts(db) -> dict:
    with db.reader() as conn:
        return {
            "chan": conn.execute("SELECT COUNT(*) FROM main.session_chan_sub").fetchone()[0],
            "chan_audit": conn.execute(
                "SELECT COUNT(*) FROM main.audit WHERE action LIKE 'chan_sub%'"
            ).fetchone()[0],
        }


# --- П7: отзыв токена профиля -------------------------------------------------------------------------------


async def test_new_token_after_revoke_returns_pending_and_channels(client, ui, db, clock, app, world):
    w = world
    made = await _mk(ui, w, role=ROLE)
    hub_id = made["id"]
    old_tok, old_id = _token(db, name="mcp:old")
    sub_id = (await _hsub(client, w, hub_id, token=old_tok, mode="messages"))["sub"]["id"]
    r = await client.post(
        "/api/v1/chan-subs",
        json={"channel_id": w["ch2"], "session": _hs(hub_id, cwd=w["dir3"]), "keywords": ["счёт"]},
        headers=_h(old_tok),
    )
    assert r.status_code == 200, r.text
    clock.advance_ms(MIN)
    _ingest(db, w, [_ev("team", "201", _now(clock))], clock)
    assert _pending(db, sub_id) == 1

    db.write_sync(lambda conn: conn.execute("UPDATE main.api_token SET revoked = 1 WHERE id = ?", (old_id,)))
    ended = dict(_cleanup(db, clock, app))
    assert ended[str(sub_id)] == "token_revoked" and "token_revoked" in ended.values()
    assert _pending(db, sub_id) == 0

    # Та же сессия хаба новым токеном: владелец тот же — ждущее и беседы возвращаются.
    body = await _hsub(client, w, hub_id)
    assert body["created"] is True and body["sub"]["id"] != sub_id
    assert (body["sub"]["mode"], body["sub"]["pending"]) == ("messages", 1)
    assert [row["channel_id"] for row in body["restored_channels"]] == [w["ch2"]]
    assert body["restored_channels"][0]["keys"] == 1

    # Внешняя сессия после отзыва токена — как было: насовсем.
    ext_tok, ext_id = _token(db, name="mcp:ext")
    ext_sub = (await _sub(client, w, A, token=ext_tok, role=CODER, mode="messages"))["sub"]["id"]
    _ingest(db, w, [_ev("team", "202", _now(clock))], clock)
    db.write_sync(lambda conn: conn.execute("UPDATE main.api_token SET revoked = 1 WHERE id = ?", (ext_id,)))
    assert (str(ext_sub), "token_revoked") in _cleanup(db, clock, app)
    again = await _sub(client, w, A, role=CODER)
    assert again["sub"]["pending"] == 0


# --- девять мест личности: из сессии хаба работает, из закрытой — отказ -------------------------------------


def _at(clock, minutes: float) -> str:
    moment = dt.datetime.fromtimestamp(clock() + minutes * 60, tz=dt.UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


async def _react(client, w, message_id, session, *, expect=200):
    r = await client.post(
        f"/api/v1/messages/{message_id}/reactions",
        json={"emoji": "👍", "on": True, "session": session},
        headers=_h(w["tok"]),
    )
    assert r.status_code == expect, r.text
    return r.json()


async def test_identity_places_work_from_hub_session(client, ui, db, clock, world):
    w = world
    made = await _mk(ui, w, role=ROLE)
    hub_id = made["id"]
    me = _hs(hub_id)
    sub_id = (await _hsub(client, w, hub_id, mode="messages"))["sub"]["id"]
    token_id = w["tok_id"]
    db.write_sync(
        lambda conn: reactions_mod.set_project_reactions(conn, w["p3"], sessions=True, delay_s=None)
    )

    # 1. `hub_react` и ключ лимита реакций (тройка постоянна: сон, подъём и смена разговора его не меняют).
    await _react(client, w, w["last"], me)

    def _rate():
        with db.reader() as conn:
            return reactions_mod.session_rate_count(
                conn, token_id=token_id, pid=-hub_id, proc_start=0, now_ms=_now(clock)
            )

    assert _rate() == 1
    for body in (
        {"engine_session_id": "sess-1", "proc_pid": 4242, "proc_start": 777_000, "state": "running"},
        {"engine_session_id": "sess-2", "proc_pid": 5151, "proc_start": 888_000, "state": "running"},
    ):
        await _host(client, w, f"sessions/{hub_id}/bind", body)
        await _host(client, w, f"sessions/{hub_id}/state", {"state": "asleep"})
        assert _rate() == 1

    # 2. `hub_remind`.
    r = await client.post(
        "/api/v1/reminders",
        json={"channel_id": w["ch"], "at": _at(clock, 30), "note": "спросить", "session": me},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 201 and r.json()["role"] == ROLE, r.text

    # 3. `hub_agent_send` / `hub_agent_inbox`.
    await _sub(client, w, B, cwd=w["dir4"], role=CODER)
    r = await client.post(
        "/api/v1/peer-msgs",
        json={"text": "привет", "to": {"project_id": w["p4"], "role": CODER}, "session": me},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 201 and r.json()["msg"]["from"]["sub_id"] == sub_id, r.text
    assert r.json()["msg"]["from"]["engine"] == "claude"  # движок адреса — из записи сессии хаба
    r = await client.get("/api/v1/peer-msgs", params={**me, "all": 1}, headers=_h(w["tok"]))
    assert r.status_code == 200 and len(r.json()["items"]) == 1

    # 4. `hub_join` и 5. `hub_propose_rule` — опознание сессии и имя просителя.
    with db.reader() as conn:
        assert join._session_eligible(conn, token_id, w["p3"], me)
        assert rr_mod._who(conn, token_id, w["p3"], me) == ROLE

    # 6. `hub_handled`.
    r = await client.post(
        f"/api/v1/channels/{w['ch']}/handled",
        json={"up_to_message_id": w["last"], "note": "автоответ", "session": me},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200 and r.json()["role"] == ROLE, r.text

    # 7–8. Автор черновика и исход автору.
    r = await client.post(
        "/api/v1/messages/send",
        json={"channel_id": w["ch"], "text": "черновик", "idempotency_key": "k1", "session": me},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200 and r.json()["status"] == "pending_approval", r.text
    draft_id = r.json()["outbox_id"]
    assert r.json()["heard"] == "project"
    with db.reader() as conn:
        row = conn.execute(
            f"SELECT author_role, author_sub_id FROM {db_of(draft_id)}.outbox WHERE id = ?",  # noqa: S608
            (draft_id,),
        ).fetchone()
    assert row == (ROLE, sub_id)
    r = await ui.post(f"/api/v1/outbox/{draft_id}/approve")
    assert r.status_code == 200, r.text
    with db.reader() as conn:
        outcomes = conn.execute(
            "SELECT author_sub_id, outcome FROM main.draft_outcome WHERE outbox_id = ?", (draft_id,)
        ).fetchall()
    assert outcomes == [(sub_id, "approved")]

    # 9. Автоотправка в беседу «без подтверждения» для роли сессии хаба.
    r = await ui.put(f"/api/v1/channels/{w['ch']}/autosend", {"role": ROLE, "on": True})
    assert r.status_code == 200, r.text
    r = await client.post(
        "/api/v1/messages/send",
        json={"channel_id": w["ch"], "text": "сразу", "idempotency_key": "k2", "session": me},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["auto"]) == ("queued", {"role": ROLE})
    # …и только токеном той же подписки: другой профиль права автоотправки не получает (3.5).
    other_tok, _ = _token(db, name="mcp:other")
    r = await client.post(
        "/api/v1/messages/send",
        json={"channel_id": w["ch"], "text": "чужим токеном", "idempotency_key": "k3", "session": me},
        headers=_h(other_tok),
    )
    assert r.status_code == 200 and r.json()["status"] == "pending_approval" and "auto" not in r.json()

    # Из закрытой сессии хаба — отказ во всех местах.
    assert (await ui.post(f"/api/v1/sessions/{hub_id}/close")).status_code == 200
    await _react(client, w, w["last"], me, expect=403)
    r = await client.post(
        "/api/v1/reminders",
        json={"channel_id": w["ch"], "at": _at(clock, 40), "note": "ещё", "session": me},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 409, r.text
    r = await client.post(
        "/api/v1/peer-msgs",
        json={"text": "после закрытия", "to": {"project_id": w["p4"], "role": CODER}, "session": me},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 409, r.text
    assert (await client.get("/api/v1/peer-msgs", params=me, headers=_h(w["tok"]))).status_code == 409
    with db.reader() as conn:
        assert not join._session_eligible(conn, token_id, w["p3"], me)
        assert rr_mod._who(conn, token_id, w["p3"], me) is None
    r = await client.post(
        f"/api/v1/channels/{w['ch']}/handled",
        json={"up_to_message_id": w["last"], "note": "после", "session": me},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 403, r.text
    r = await client.post(
        "/api/v1/messages/send",
        json={"channel_id": w["ch"], "text": "после закрытия", "idempotency_key": "k4", "session": me},
        headers=_h(w["tok"]),
    )
    # Черновик класть можно всегда, но ни автора, ни автоотправки у закрытой сессии нет.
    assert r.status_code == 200 and r.json()["status"] == "pending_approval" and "auto" not in r.json()
    with db.reader() as conn:
        row = conn.execute(
            f"SELECT author_role, author_sub_id FROM {db_of(r.json()['outbox_id'])}.outbox WHERE id = ?",  # noqa: S608
            (r.json()["outbox_id"],),
        ).fetchone()
    assert row == (None, None)


# --- правки по ревью: Н1 (безрольная сессия хаба и смена токена), Н2 (наследование «единственная») ----------


def _peer_row(db, msg_id) -> tuple:
    with db.reader() as conn:
        return conn.execute(
            "SELECT status, to_sub_id, expires_ts FROM main.peer_msg WHERE id = ?", (msg_id,)
        ).fetchone()


async def _peer_to_sub(ui, w, sub_id) -> int:
    r = await ui.post(
        "/api/v1/peer-msgs", {"text": "от владельца", "to": {"project_id": w["p3"], "sub_id": sub_id}}
    )
    assert r.status_code == 201, r.text
    return r.json()["msg"]["id"]


def _revoke(db, token_id) -> None:
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.api_token SET revoked = 1 WHERE id = ?", (token_id,))
    )


async def test_roleless_hub_peer_msg_survives_token_revoke(client, ui, db, clock, app, world):
    """Сессия НЕ закрыта, сменился только токен профиля: сообщение агенту безрольной сессии хаба (привязано к
    подписке) возвращается новой подписке той же тройки; срок сообщения не продлевается (П7)."""
    w = world
    made = await _mk(ui, w)
    old_tok, old_id = _token(db, name="mcp:old")
    sub_id = (await _hsub(client, w, made["id"], token=old_tok))["sub"]["id"]
    msg_id = await _peer_to_sub(ui, w, sub_id)
    expires = _peer_row(db, msg_id)[2]
    _revoke(db, old_id)
    assert (str(sub_id), "token_revoked") in _cleanup(db, clock, app)
    assert _peer_row(db, msg_id)[0] == "lost"
    body = await _hsub(client, w, made["id"])  # та же сессия хаба новым токеном
    assert body["created"] is True and body["peer_unread"] == 1
    assert _peer_row(db, msg_id) == ("pending", body["sub"]["id"], expires)
    inbox = await client.get("/api/v1/peer-msgs", params=_hs(made["id"]), headers=_h(w["tok"]))
    assert [row["id"] for row in inbox.json()["items"]] == [msg_id]


async def test_roleless_hub_channels_return_after_token_revoke(client, ui, db, clock, app, world):
    """П7: подписки на беседы той же сессии хаба после `token_revoked` возвращаются и у безрольной — по своей
    тройке, без источника роли; ждущий сигнал беседы — с ними."""
    w = world
    made = await _mk(ui, w)
    old_tok, old_id = _token(db, name="mcp:old")
    await _hsub(client, w, made["id"], token=old_tok, mode="messages")
    r = await client.post(
        "/api/v1/chan-subs",
        json={"channel_id": w["ch2"], "session": _hs(made["id"], cwd=w["dir3"]), "keywords": ["счёт"]},
        headers=_h(old_tok),
    )
    assert r.status_code == 200, r.text
    clock.advance_ms(MIN)
    _ingest(db, w, [_ev("other", "301", _now(clock), text="пришлите счёт")], clock)
    _revoke(db, old_id)
    _cleanup(db, clock, app)
    body = await _hsub(client, w, made["id"])
    assert [(row["channel_id"], row["keys"]) for row in body["restored_channels"]] == [(w["ch2"], 1)]
    with db.reader() as conn:
        mine = chan_subs.session_subs(conn, w["tok_id"], *held.triple(made["id"]))
        assert [rec.channel_id for rec in mine] == [w["ch2"]]
        assert chan_subs.pending_counts(conn, [mine[0].id]) == {mine[0].id: 1}
    # Повторный вызов ничего не двоит.
    again = await _hsub(client, w, made["id"])
    assert again["created"] is False and "restored_channels" not in again


async def test_roleless_hub_peer_msg_survives_second_token(client, ui, db, world):
    """Та же сессия хаба подписалась вторым действующим токеном: прежняя подписка `replaced`, привязанное к
    ней сообщение агенту переходит новой."""
    w = world
    made = await _mk(ui, w)
    other_tok, _ = _token(db, name="mcp:proj")
    sub_id = (await _hsub(client, w, made["id"], token=other_tok))["sub"]["id"]
    msg_id = await _peer_to_sub(ui, w, sub_id)
    body = await _hsub(client, w, made["id"])
    assert body["sub"]["id"] != sub_id and _sub_rec(db, sub_id).end_reason == "replaced"
    assert _peer_row(db, msg_id)[:2] == ("pending", body["sub"]["id"])


async def test_token_change_returns_nothing_to_other_sessions(client, ui, db, clock, app, world):
    """Возврат — только той же условной тройке: другой сессии хаба, внешней сессии и после явной отписки не
    достаётся ничего; истёкшее по сроку сообщение не оживает."""
    w = world
    made = await _mk(ui, w)
    old_tok, old_id = _token(db, name="mcp:old")
    sub_id = (await _hsub(client, w, made["id"], token=old_tok, mode="messages"))["sub"]["id"]
    r = await client.post(
        "/api/v1/chan-subs",
        json={"channel_id": w["ch2"], "session": _hs(made["id"], cwd=w["dir3"])},
        headers=_h(old_tok),
    )
    assert r.status_code == 200, r.text
    msg_id = await _peer_to_sub(ui, w, sub_id)
    _revoke(db, old_id)
    _cleanup(db, clock, app)

    stranger = await _mk(ui, w)  # другая сессия хаба — другая тройка
    for body in (await _hsub(client, w, stranger["id"]), await _sub(client, w, A)):
        assert "restored_channels" not in body and body["peer_unread"] == 0
    assert _peer_row(db, msg_id)[:2] == ("lost", sub_id)
    with db.reader() as conn:
        assert chan_subs.session_subs(conn, w["tok_id"], *held.triple(stranger["id"])) == []
        assert chan_subs.session_subs(conn, w["tok_id"], *A) == []

    # Срок сообщения вышел, пока сессия была без подписки, — оно остаётся потерянным.
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.peer_msg SET expires_ts = ? WHERE id = ?", (_now(clock) - 1, msg_id)
        )
    )
    body = await _hsub(client, w, made["id"])
    assert body["peer_unread"] == 0 and _peer_row(db, msg_id)[0] == "lost"

    # Явная отписка — насовсем: следующей подписке той же сессии не возвращается ни беседа, ни сообщение.
    late = await _peer_to_sub(ui, w, body["sub"]["id"])
    r = await client.post(
        "/api/v1/chan-subs/end",
        json={"session": _hs(made["id"]), "reason": "unsubscribed"},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text
    r = await client.post(
        f"/api/v1/subs/{body['sub']['id']}/end",
        json={"session": _hs(made["id"]), "reason": "unsubscribed"},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text
    again = await _hsub(client, w, made["id"])
    assert again["created"] is True and "restored_channels" not in again and again["peer_unread"] == 0
    assert _peer_row(db, late)[0] == "lost"


async def test_roleless_hub_does_not_inherit_alien_line_with_live_neighbour(
    client, ui, db, clock, app, world, procs
):
    """Шаг наследования «единственная в проекте» видит всех живых соседей: при живой внешней сессии новая
    безрольная сессия хаба не берёт режим и ждущий сигнал умершей внешней безрольной (П6 — только про
    потолок мест)."""
    w = world
    await _sub(client, w, A)  # живой сосед
    b_sub = (await _sub(client, w, B, mode="messages"))["sub"]["id"]
    clock.advance_ms(MIN)
    _ingest(db, w, [_ev("team", "201", _now(clock))], clock)
    assert _pending(db, b_sub) == 1
    del procs[B[0]]
    assert (str(b_sub), "session_gone") in _cleanup(db, clock, app)
    ctl = await _sub(client, w, EXTRA[0])  # контроль: внешняя при живом соседе ничего не наследует
    assert "inherited_from" not in ctl and ctl["sub"]["mode"] == "cards" and ctl["sub"]["pending"] == 0
    made = await _mk(ui, w)
    body = await _hsub(client, w, made["id"])
    assert "inherited_from" not in body, body.get("inherited_why")
    assert (body["sub"]["mode"], body["sub"]["pending"]) == ("cards", 0)


async def test_external_roleless_does_not_inherit_with_live_hub_neighbour(
    client, ui, db, clock, app, world, procs
):
    """Зеркало: живая подписанная сессия хаба — тоже сосед для новой внешней безрольной сессии."""
    w = world
    made = await _mk(ui, w)
    await _hsub(client, w, made["id"])
    b_sub = (await _sub(client, w, B, mode="messages"))["sub"]["id"]
    clock.advance_ms(MIN)
    _ingest(db, w, [_ev("team", "201", _now(clock))], clock)
    del procs[B[0]]
    assert (str(b_sub), "session_gone") in _cleanup(db, clock, app)
    body = await _sub(client, w, A)
    assert "inherited_from" not in body, body.get("inherited_why")
    assert (body["sub"]["mode"], body["sub"]["pending"]) == ("cards", 0)
