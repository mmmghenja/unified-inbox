"""Ф41-5а (docs/architecture.md): ядро авто-режима — режим сессии,
выключатели автопилота (общие и по проекту), `auto_free` в записи сессии и рост `seq` ровно у затронутых,
ключ правила у запроса сессии, ключ запуска и `session_free`.

Помощники и «мир» — из test_hub_sessions.py; хост поддельный (bearer `work_host`)."""

import hashlib
import json
import sys

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from hub import autopilot as catalog
from hub import cli as hub_cli
from hub import paths
from hub.core import autopilot, handled, held, peer_msgs, subs
from hub.core import remote as remote_mod
from hub.core.app_internal import create_internal_app
from hub.core.app_public import create_public_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.feed import Notifier
from hub.core.remote import RemoteState

from . import test_hub_sessions as base
from .conftest import PORT, csrf_headers, login_via_code
from .test_hub_sessions import ROLE, _audits, _h, _host, _mk

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"
PROC = {"proc_pid": 4242, "proc_start": 777_000}
ADMIN_SECRET = ADMIN_PREFIX + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHI"
REMOTE_PORT = 8790
REMOTE_HOST = "hub.test.example:8787"
REMOTE_ORIGIN = f"http://{REMOTE_HOST}"
PASSWORD = "correct horse battery"
KEY = "k" * 64


def _rec(db, sid) -> held.HubSession:
    with db.reader() as conn:
        return held.get(conn, sid)


async def _get(ui, sid) -> dict:  # noqa: F811
    r = await ui.get(f"{API}/sessions/{sid}")
    assert r.status_code == 200, r.text
    return r.json()["session"]


async def _put(ui, scope, key, hold, *, expect=200) -> dict:  # noqa: F811
    r = await ui.put(f"{API}/autopilot", {"scope": scope, "key": key, "hold": hold})
    assert r.status_code == expect, r.text
    return r.json()


async def _patch(ui, sid, body, *, expect=200) -> dict:  # noqa: F811
    r = await ui.c.patch(f"{API}/sessions/{sid}", json=body, headers=ui.headers)
    assert r.status_code == expect, r.text
    return r.json()


# --- режим (3.1) --------------------------------------------------------------------------------------------


async def test_mode_on_create_and_patch_with_event_and_audit(client, ui, db, clock, world):  # noqa: F811
    w = world
    manual = await _mk(ui, w)
    assert (manual["mode"], manual["auto_free"], manual["autosend_ok"]) == ("manual", [], True)
    assert manual["auto_held"] == len(catalog.SESSION_KEYS)
    auto = await _mk(ui, w, mode="auto")
    assert (auto["mode"], auto["auto_free"], auto["auto_held"]) == ("auto", [], len(catalog.SESSION_KEYS))
    assert [a.get("mode") for a in _audits(db, "hub_session_create")] == [None, "auto"]

    sid = manual["id"]
    seq0 = _rec(db, sid).seq
    row = (await _patch(ui, sid, {"mode": "auto"}))["session"]
    assert row["mode"] == "auto" and _rec(db, sid).seq > seq0
    assert _audits(db, "hub_session_mode") == [{"hub_session_id": sid, "old": "manual", "new": "auto"}]
    events = (await ui.get(f"{API}/sessions/{sid}/events")).json()
    assert [(e["kind"], e["brief"]) for e in events] == [("status", {"type": "mode", "mode": "auto"})]
    # Тот же режим — ничего не пишется.
    seq1 = _rec(db, sid).seq
    await _patch(ui, sid, {"mode": "auto"})
    assert _rec(db, sid).seq == seq1 and len(_audits(db, "hub_session_mode")) == 1
    # Вместе с фильтром подписки; пустое тело и чужое значение — 422; прежний вызов с `want_sub` цел.
    row = (await _patch(ui, sid, {"mode": "manual", "want_sub": {"mode": "direct"}}))["session"]
    assert row["mode"] == "manual" and row["want_sub"]["mode"] == "direct"
    assert (await _patch(ui, sid, {"want_sub": None}))["session"]["want_sub"] is None
    await _patch(ui, sid, {}, expect=422)
    await _patch(ui, sid, {"mode": "turbo"}, expect=422)
    # Не интерфейс режим не меняет; закрытая — 409.
    r = await client.patch(f"{API}/sessions/{sid}", json={"mode": "auto"}, headers=_h(w["tok"]))
    assert r.status_code == 403
    await ui.post(f"{API}/sessions/{sid}/close")
    await _patch(ui, sid, {"mode": "auto"}, expect=409)
    # Режим доходит в `/sync` и в форму «для хоста».
    r = await ui.get(f"{API}/sync", params={"since": 0})
    (item,) = [s for s in r.json()["hub_sessions"] if s["id"] == auto["id"]]
    assert (item["mode"], item["auto_free"], item["auto_held"]) == ("auto", [], len(catalog.SESSION_KEYS))
    (host_row,) = [s for s in (await _host(client, w, "sessions"))["items"] if s["id"] == auto["id"]]
    assert (host_row["mode"], host_row["auto_free"]) == ("auto", []) and "auto_held" not in host_row


# --- выключатели (3.2, 3.3) ---------------------------------------------------------------------------------


async def test_switches_effective_value_and_seq_of_affected_sessions_only(client, ui, db, clock, world):  # noqa: F811
    w = world
    state = (await ui.get(f"{API}/autopilot")).json()
    # Ф66-1: делегата карточек нет — полей `delegates` и `delegate_wait_ms` в выдаче тоже.
    keys = {"switches", "global", "projects", "local"}
    assert set(state) == keys and state["local"] is True
    assert [s["key"] for s in state["switches"]] == [r["key"] for r in catalog.public()]
    assert state["global"] == dict.fromkeys(catalog.KEYS, True) and state["projects"] == {}

    a3 = (await _mk(ui, w, mode="auto"))["id"]  # авто, проект 3
    m3 = (await _mk(ui, w))["id"]  # ручная, проект 3
    a4 = (await _mk(ui, w, project="p4", mode="auto"))["id"]  # авто, проект 4

    def _seqs():
        return {sid: _rec(db, sid).seq for sid in (a3, m3, a4)}

    async def _free():
        return {sid: (await _get(ui, sid))["auto_free"] for sid in (a3, m3, a4)}

    # Общий выключатель снят: обе авто-сессии, ручная — пусто и не тронута.
    before = _seqs()
    state = await _put(ui, "global", "push", False)
    assert state["global"]["push"] is False and state["projects"] == {}
    assert await _free() == {a3: ["push"], m3: [], a4: ["push"]}
    after = _seqs()
    assert after[a3] > before[a3] and after[a4] > before[a4] and after[m3] == before[m3]
    assert (await _get(ui, a3))["auto_held"] == len(catalog.SESSION_KEYS) - 1
    # Тот же вызов ещё раз — никто не затронут.
    before = _seqs()
    await _put(ui, "global", "push", False)
    assert _seqs() == before

    # Проект главнее общего: в проекте 4 `push` стоит, `server` снят только в нём.
    before = _seqs()
    state = await _put(ui, w["p4"], "push", True)
    assert state["projects"] == {str(w["p4"]): {"push": True}}
    after = _seqs()
    assert after[a4] > before[a4] and after[a3] == before[a3] and after[m3] == before[m3]
    await _put(ui, w["p4"], "server", False)
    assert await _free() == {a3: ["push"], m3: [], a4: ["server"]}
    # `null` — снять переопределение проекта: снова как общее.
    state = await _put(ui, w["p4"], "push", None)
    assert state["projects"] == {str(w["p4"]): {"server": False}}
    assert (await _free())[a4] == ["push", "server"]

    # Режим: ручная стала авто — получила набор проекта; обратно — пусто.
    row = (await _patch(ui, m3, {"mode": "auto"}))["session"]
    assert row["auto_free"] == ["push"]
    assert (await _patch(ui, m3, {"mode": "manual"}))["session"]["auto_free"] == []

    audits = _audits(db, "autopilot_set")
    assert audits[0] == {"scope": "global", "key": "push", "old": True, "new": False, "sessions": [a3, a4]}
    assert audits[2] == {"scope": w["p4"], "key": "push", "old": None, "new": True, "sessions": [a4]}

    # «Поставить все»: общие стоят, переопределения сняты, затронуты только авто-сессии.
    before = _seqs()
    r = await ui.post(f"{API}/autopilot/hold-all")
    assert r.status_code == 200 and r.json()["global"] == dict.fromkeys(catalog.KEYS, True)
    assert r.json()["projects"] == {}
    after = _seqs()
    assert after[a3] > before[a3] and after[a4] > before[a4] and after[m3] == before[m3]
    assert await _free() == {a3: [], m3: [], a4: []}
    (all_audit,) = _audits(db, "autopilot_hold_all")
    assert all_audit == {"projects": [w["p4"]], "sessions": [a3, a4]}
    # Закрытая сессия набора не имеет.
    await _put(ui, "global", "push", False)
    await ui.post(f"{API}/sessions/{a3}/close")
    assert (await _get(ui, a3))["auto_free"] == []


async def test_switch_refusals_and_storage_is_owned_by_the_module(client, ui, db, clock, app, world):  # noqa: F811
    w = world
    await _put(ui, "global", "нет такого", False, expect=422)
    await _put(ui, "global", "push", None, expect=422)  # `null` — только у проекта
    await _put(ui, 987_654, "push", False, expect=404)
    await _put(ui, "global", "push", "no", expect=422)
    r = await ui.put(f"{API}/autopilot", {"scope": "global", "key": "push", "hold": False, "extra": 1})
    assert r.status_code == 422
    # Bearer любого скоупа — 403 (и чтение тоже: таблица — только интерфейсу).
    for token in (w["tok"], w["host"]):
        body = {"scope": "global", "key": "push", "hold": False}
        assert (await client.put(f"{API}/autopilot", json=body, headers=_h(token))).status_code == 403
        assert (await client.post(f"{API}/autopilot/hold-all", json={}, headers=_h(token))).status_code == 403
        assert (await client.get(f"{API}/autopilot", headers=_h(token))).status_code == 403
    assert _audits(db, "autopilot_set") == []

    # П14: ключ политики проекта пишет только модуль автопилота; прежние записи политики его не теряют и
    # не принимают.
    await _put(ui, w["p3"], "push", False)
    await _put(ui, w["p3"], "hub_send", False)
    db.write_sync(lambda conn: subs.set_project_subs(conn, w["p3"], True, now_ms=0))
    db.write_sync(lambda conn: handled.set_project_enabled(conn, w["p3"], False))
    db.write_sync(lambda conn: peer_msgs.set_project_enabled(conn, w["p3"], True))
    with db.reader() as conn:
        assert autopilot.project_hold(conn, w["p3"]) == {"push": False, "hub_send": False}
        assert autopilot.free_for(conn, w["p3"]) == ["hub_send", "push"]
        raw = conn.execute("SELECT agent_policy_json FROM main.project WHERE id = ?", (w["p3"],)).fetchone()
    assert json.loads(raw[0])["subs"] is True
    internal = create_internal_app(
        db,
        clock=clock,
        notifier=app.state.notifier,
        admin_key_hash=hash_secret(ADMIN_SECRET),
        sub_waiters=app.state.sub_waiters,
        chan_sub_waiters=app.state.chan_sub_waiters,
    )
    admin = {"Authorization": f"Bearer {ADMIN_SECRET}"}
    async with AsyncClient(transport=ASGITransport(app=internal), base_url="http://hub-internal") as c:
        r = await c.patch(
            f"/internal/v1/admin/projects/{w['p3']}", json={"autopilot_hold": {"push": True}}, headers=admin
        )
        assert r.status_code == 422
        # Админ-сокет (CLI): показать и «поставить все»; без ключа — отказ.
        r = await c.get("/internal/v1/admin/autopilot", headers=admin)
        assert r.status_code == 200 and r.json()["projects"] == {
            str(w["p3"]): {"hub_send": False, "push": False}
        }
        assert (await c.post("/internal/v1/admin/autopilot/hold-all")).status_code in (401, 403)
        r = await c.post("/internal/v1/admin/autopilot/hold-all", headers=admin)
        assert r.status_code == 200 and r.json()["projects"] == {}
    with db.reader() as conn:
        assert autopilot.project_hold(conn, w["p3"]) == {}
        # Мусор в хранилище читается как «стоит».
        conn.execute("SELECT 1")
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.meta SET value = ? WHERE key = ?",
            ('{"push": "no", "нет": false}', autopilot.META_KEY),
        )
    )
    assert (await ui.get(f"{API}/autopilot")).json()["global"]["push"] is True


async def test_put_autopilot_from_remote_listener_allowed(db, clock, monkeypatch):
    """Слова владельца: с телефона выключатели меняются (прежний запрет П14 снят): `PUT` и «поставить все»
    удалённому слушателю — 200, в ответе честный `local: false`."""
    monkeypatch.setattr(remote_mod, "SCRYPT_LOG_N", 10)
    notifier = Notifier()
    db.on_commit(notifier.notify)
    state = RemoteState(port=REMOTE_PORT)
    with db.reader() as conn:
        state.load(conn)
    rapp = create_public_app(
        db, clock=clock, port=PORT, remote_port=REMOTE_PORT, remote=state, notifier=notifier
    )
    origin = f"http://127.0.0.1:{PORT}"
    async with AsyncClient(transport=ASGITransport(app=rapp), base_url=origin) as local:
        cookie, _csrf = await login_via_code(local, db, clock)
        headers = {"Cookie": f"hub_session={cookie}", **csrf_headers(cookie)}
        local.cookies.clear()
        r = await local.put(f"{API}/remote/password", json={"password": PASSWORD}, headers=headers)
        assert r.status_code == 200, r.text
        r = await local.put(f"{API}/remote", json={"enabled": True, "hosts": [REMOTE_HOST]}, headers=headers)
        assert r.status_code == 200, r.text
        body = {"scope": "global", "key": "push", "hold": False}
        r = await local.put(f"{API}/autopilot", json=body, headers=headers)
        assert r.status_code == 200 and r.json()["local"] is True
        async with AsyncClient(
            transport=ASGITransport(app=rapp),
            base_url=f"http://127.0.0.1:{REMOTE_PORT}",
            headers={"Host": REMOTE_HOST},
        ) as phone:
            r = await phone.post(
                f"{API}/auth/password", json={"password": PASSWORD}, headers={"Origin": REMOTE_ORIGIN}
            )
            assert r.status_code == 200, r.text
            phone_headers = {
                "Cookie": f"hub_session={phone.cookies.get('hub_session')}",
                "Origin": REMOTE_ORIGIN,
                "X-Hub-CSRF": r.json()["csrf"],
            }
            phone.cookies.clear()
            r = await phone.get(f"{API}/autopilot", headers=phone_headers)
            assert r.status_code == 200 and r.json()["local"] is False and r.json()["global"]["push"] is False
            body = {"scope": "global", "key": "server", "hold": False}
            r = await phone.put(f"{API}/autopilot", json=body, headers=phone_headers)
            assert r.status_code == 200, r.text
            assert r.json()["local"] is False and r.json()["global"]["server"] is False
            with db.reader() as conn:
                assert autopilot.global_hold(conn) == {"push": False, "server": False}
            r = await phone.post(f"{API}/autopilot/hold-all", json={}, headers=phone_headers)
            assert r.status_code == 200 and r.json()["local"] is False
            assert r.json()["global"]["push"] is True and r.json()["global"]["server"] is True


def test_cli_autopilot_show_and_hold_all(monkeypatch, tmp_path, capsys):
    seen: list[httpx.Request] = []
    body = {
        "switches": catalog.public(),
        "global": {**dict.fromkeys(catalog.KEYS, True), "push": False},
        "projects": {"3": {"server": False, "push": True}},
        "local": True,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body)

    monkeypatch.setattr(
        hub_cli,
        "_admin_client",
        lambda *, timeout=None: httpx.Client(
            transport=httpx.MockTransport(handler), base_url="http://hub-internal/internal/v1"
        ),
    )
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    for argv in (["autopilot"], ["autopilot", "hold-all"]):
        monkeypatch.setattr(sys, "argv", ["hub", *argv])
        hub_cli.main()
    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", "/internal/v1/admin/autopilot"),
        ("POST", "/internal/v1/admin/autopilot/hold-all"),
    ]
    out = capsys.readouterr().out
    total = len(catalog.KEYS)
    assert f"стоит {total - 1} из {total}" in out and "снят: push" in out
    assert "проект 3: push=стоит, server=снят" in out


# --- карточка (3.4) -----------------------------------------------------------------------------------------


async def test_request_carries_rule_and_title(client, ui, db, world):  # noqa: F811
    w = world
    sid = (await _mk(ui, w))["id"]
    await _host(client, w, f"sessions/{sid}/state", {"state": "running", **PROC})
    body = {"kind": "permission", "tool": "Bash", "input": {"command": "git push"}, "reason": "гард"}
    plain = await _host(client, w, f"sessions/{sid}/requests", body)
    ruled = await _host(client, w, f"sessions/{sid}/requests", {**body, "rule": "push"})
    await _host(client, w, f"sessions/{sid}/requests", {**body, "rule": "нет такого"}, expect=422)
    rows = {r["id"]: r for r in (await ui.get(f"{API}/sessions/{sid}")).json()["requests"]}
    assert (rows[plain["id"]]["rule"], rows[plain["id"]]["rule_title"]) == (None, None)
    assert rows[ruled["id"]]["rule"] == "push"
    assert rows[ruled["id"]]["rule_title"] == catalog.BY_KEY["push"].title
    r = await ui.get(f"{API}/sync", params={"since": 0})
    synced = {x["id"]: x for x in r.json()["session_requests"]}
    assert synced[ruled["id"]]["rule"] == "push" and synced[ruled["id"]]["rule_title"]


# --- ключ запуска и личность авто-сессии (П1, П12) ----------------------------------------------------------


async def test_launch_key_hash_lives_with_the_process_and_gates_session_free(client, ui, db, world):  # noqa: F811
    w = world
    sid = (await _mk(ui, w, role=ROLE, mode="auto"))["id"]
    await _put(ui, "global", "hub_send", False)
    digest = hashlib.sha256(KEY.encode()).hexdigest()
    me = {"pid": -sid, "proc_start": 0, "session_id": f"hub:{sid}", "key": KEY}

    def _free(session=me, key="hub_send", project="p3"):  # noqa: B006
        with db.reader() as conn:
            rec = autopilot.session_free(conn, session, key, w[project] if project else None)
        return rec.id if rec is not None else None

    assert _free() is None  # процесс не запущен, ключа нет
    await _host(
        client, w, f"sessions/{sid}/state", {"state": "idle", **PROC, "launch_key_hash": "zz"}, expect=422
    )
    bad = {"state": "idle", **PROC, "launch_key_hash": "Z" * 64}
    await _host(client, w, f"sessions/{sid}/state", bad, expect=422)
    row = await _host(
        client, w, f"sessions/{sid}/state", {"state": "idle", **PROC, "launch_key_hash": digest}
    )
    assert "launch_key_hash" not in row["session"]  # хеш наружу не отдаётся
    assert "launch_key_hash" not in await _get(ui, sid)
    assert _rec(db, sid).launch_key_hash == digest
    assert _free() == sid

    # Каждое условие по отдельности.
    assert _free({**me, "key": "x" * 64}) is None  # чужой ключ
    assert _free({k: v for k, v in me.items() if k != "key"}) is None  # тройка `hub:N` без ключа
    assert _free({**me, "pid": 4242, "proc_start": 777_000, "session_id": "sess-aaaa"}) is None  # внешняя
    assert _free({**me, "session_id": f"hub:{sid + 1}"}) is None  # тройка не сходится
    assert _free(project="p4") is None and _free(project=None) is None  # П12: только свой проект
    assert _free(key="hub_send_new") is None  # этот выключатель стоит
    assert _free(key="нет такого") is None
    await _put(ui, w["p3"], "hub_send", True)  # проект придержал
    assert _free() is None
    await _put(ui, w["p3"], "hub_send", None)
    assert _free() == sid
    await _patch(ui, sid, {"mode": "manual"})  # ручная
    assert _free() is None
    await _patch(ui, sid, {"mode": "auto"})
    assert _free() == sid

    # Ключ живёт, пока идёт процесс: сон стирает; новый запуск — новый ключ.
    await _host(client, w, f"sessions/{sid}/turn", {"state": "asleep"})
    assert _rec(db, sid).launch_key_hash is None and _free() is None
    other = hashlib.sha256(b"other").hexdigest()
    bind = {"engine_session_id": "conv-1", "state": "idle", **PROC, "launch_key_hash": other}
    await _host(client, w, f"sessions/{sid}/bind", bind)
    assert _rec(db, sid).launch_key_hash == other and _free() is None  # прежний ключ больше не годится
    assert _free({**me, "key": "other"}) == sid
    # Хост замолчал — ядро само усыпляет и стирает ключ; закрытие — тоже.
    await _host(client, w, "sessions/recover", {})
    assert _rec(db, sid).launch_key_hash is None
    await _host(client, w, f"sessions/{sid}/state", {"state": "running", **PROC, "launch_key_hash": digest})
    assert _free() == sid
    await ui.post(f"{API}/sessions/{sid}/close")
    assert _rec(db, sid).launch_key_hash is None and _free() is None


@pytest.mark.parametrize("value", ["adopt", "request", "agent", "handoff"])
def test_created_by_values(value):
    assert value in held.CREATED_BY
