"""Ф77-1 (docs/architecture.md, A5, E): машина у подписки и роли — ручки ядра.

Машина ядра — `HUB_HOST_ID=CORE`; сессия «ноутбука» шлёт `host=LAPTOP` и поле `machine`. `/proc` подменён:
процессов «ноутбука» на машине ядра нет — живость по реестру удалённых личностей (Ф33-1)."""

import pytest
from httpx import ASGITransport, AsyncClient

from hub import hostid
from hub.core import machines, security, subs
from hub.core.app_internal import create_internal_app
from hub.core.app_public import create_public_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.feed import Notifier

from .conftest import BASE_URL, PORT, csrf_headers, insert_project, login_via_code

CORE = "c0" * 8
LAPTOP = "1a" * 8
ORIGIN = BASE_URL
PID, START, SID = 4242, 777_000, "sess-aaaaaaaa-1111"
LOCAL_PID, LOCAL_START, LOCAL_SID = 5151, 888_000, "sess-bbbbbbbb-2222"
MACHINE = {"hostname": "my-laptop", "route": "s1", "version": "abcdef12"}


@pytest.fixture(autouse=True)
def core_host(monkeypatch):
    monkeypatch.setenv(hostid.ENV, CORE)


@pytest.fixture
def procs(monkeypatch):
    table = {LOCAL_PID: LOCAL_START}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


@pytest.fixture
def app(db, clock):
    return create_public_app(db, clock=clock, port=PORT)


def _token(db):
    return db.write_sync(
        lambda conn: security.create_api_token(
            conn,
            name="mcp:all",
            kind="interactive",
            scopes=["read", "draft"],
            project_ids=None,
            expires_ts=None,
        )
    ).token


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN, "X-Hub-Sub-Wait": "9"}


def _session(cwd, *, pid=PID, start=START, sid=SID, host=LAPTOP):
    body = {"pid": pid, "proc_start": start, "session_id": sid, "cwd": cwd, "kind": "interactive"}
    body.update({"name": None, "name_source": None})
    if host is not None:
        body["host"] = host
    return body


@pytest.fixture
def world(db, tmp_path):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    pid = insert_project(db, name="Umbrella", workdir=str(workdir))
    db.write_sync(lambda conn: subs.set_project_subs(conn, pid, True, now_ms=0))
    return {"pid": pid, "workdir": str(workdir), "tok": _token(db)}


async def _subscribe(client, w, *, machine=MACHINE, role="маркетолог", **kw):
    body = {"project_id": None, "session": _session(w["workdir"], **kw), "role": role}
    if machine is not None:
        body["machine"] = machine
    r = await client.post("/api/v1/subs", json=body, headers=_h(w["tok"]))
    assert r.status_code == 200, r.text
    return r.json()


def _machine_row(db, host):
    with db.reader() as conn:
        return conn.execute(
            "SELECT hostname, route, version FROM main.machine WHERE host = ?", (host,)
        ).fetchone()


async def test_subscribe_registers_machine_and_answers_both(client, db, world, procs):
    body = await _subscribe(client, world)
    assert _machine_row(db, LAPTOP) == ("my-laptop", "s1", "abcdef12")
    assert body["machine"]["host"] == LAPTOP and body["machine"]["display"] == "my-laptop"
    assert body["machine"]["route"] == "s1" and body["machine"]["core"] is False
    assert body["core_machine"]["host"] == CORE and body["core_machine"]["core"] is True
    assert body["sub"]["machine"]["host"] == LAPTOP  # строка подписки — тоже


async def test_old_client_without_machine(client, db, world, procs):
    """Старый клиент: подписка как раньше, строка машины — без имени хоста, показ «машина 1a1a1a»."""
    body = await _subscribe(client, world, machine=None)
    assert _machine_row(db, LAPTOP) == (None, None, None)
    assert body["machine"]["display"] == "машина 1a1a1a"


async def test_session_without_host_is_core_machine(client, db, world, procs):
    body = await _subscribe(client, world, pid=LOCAL_PID, start=LOCAL_START, sid=LOCAL_SID, host=None)
    assert body["machine"]["host"] == CORE and body["machine"]["core"] is True
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.machine WHERE host = ?", (LAPTOP,)).fetchone()[0] == 0


async def test_core_host_from_client_written_as_core(client, db, world, procs):
    await _subscribe(
        client,
        world,
        pid=LOCAL_PID,
        start=LOCAL_START,
        sid=LOCAL_SID,
        host=CORE,
        machine={"hostname": "x", "route": "lan", "version": "1234567"},
    )
    assert _machine_row(db, CORE)[1] == "core"


async def test_bad_machine_field_422(client, world, procs):
    for bad in ({"hostname": "a\nb"}, {"route": "core"}, {"version": "nothex!"}, {"extra": 1}):
        r = await client.post(
            "/api/v1/subs",
            json={"project_id": None, "session": _session(world["workdir"]), "machine": bad},
            headers=_h(world["tok"]),
        )
        assert r.status_code == 422, bad


async def test_get_subs_and_agent_states_carry_machine(client, world, procs):
    await _subscribe(client, world)
    r = await client.get("/api/v1/subs", headers=_h(world["tok"]))
    [row] = r.json()["items"]
    assert row["machine"]["host"] == LAPTOP and row["machine"]["display"] == "my-laptop"
    params = {"pid": PID, "proc_start": START, "session_id": SID, "host": LAPTOP}
    r = await client.get("/api/v1/agents/states", params=params, headers=_h(world["tok"]))
    assert r.status_code == 200, r.text
    [item] = r.json()["items"]
    assert item["machine"]["host"] == LAPTOP
    r = await client.get(
        "/api/v1/agents/state",
        params={**params, "project_id": world["pid"], "role": "маркетолог"},
        headers=_h(world["tok"]),
    )
    assert r.status_code == 200, r.text
    assert r.json()["agent"]["machine"]["host"] == LAPTOP


async def test_beat_refreshes_machine(client, db, clock, world, procs):
    await _subscribe(client, world, machine={"hostname": "laptop", "route": "lan", "version": None})
    clock.advance_ms(1000)
    r = await client.post(
        "/api/v1/subs/beat",
        json={
            "session": {"pid": PID, "proc_start": START, "session_id": SID, "host": LAPTOP},
            "machine": MACHINE,
        },
        headers=_h(world["tok"]),
    )
    assert r.status_code == 200, r.text
    assert _machine_row(db, LAPTOP) == ("my-laptop", "s1", "abcdef12")


async def test_chan_sub_answers_machine(client, db, world, procs):
    from .conftest import insert_account, insert_channel

    acc = insert_account(db, source="tg")
    cid = insert_channel(db, acc, project_id=world["pid"], ext_id="c1", title="Клиент")
    r = await client.post(
        "/api/v1/chan-subs",
        json={"channel_id": cid, "session": _session(world["workdir"]), "machine": MACHINE},
        headers=_h(world["tok"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["machine"]["host"] == LAPTOP and body["core_machine"]["host"] == CORE
    assert body["sub"]["machine"]["host"] == LAPTOP
    assert _machine_row(db, LAPTOP)[0] == "my-laptop"


async def test_machines_list_and_rename(client, db, clock, world, procs):
    await _subscribe(client, world)
    r = await client.get("/api/v1/machines", headers=_h(world["tok"]))
    assert r.status_code == 200, r.text
    assert r.json()["core"] == CORE
    assert [m["host"] for m in r.json()["items"]] == [CORE, LAPTOP]
    # bearer — 403
    r = await client.patch(f"/api/v1/machines/{LAPTOP}", json={"name": "ноутбук"}, headers=_h(world["tok"]))
    assert r.status_code == 403
    cookie, _csrf = await login_via_code(client, db, clock)
    r = await client.patch(
        f"/api/v1/machines/{LAPTOP}", json={"name": "ноутбук"}, headers=csrf_headers(cookie)
    )
    assert r.status_code == 200, r.text
    assert r.json()["machine"]["display"] == "ноутбук"
    r = await client.patch(
        f"/api/v1/machines/{LAPTOP}", json={"name": "x" * 33}, headers=csrf_headers(cookie)
    )
    assert r.status_code == 422
    r = await client.patch(f"/api/v1/machines/{'0e' * 8}", json={"name": "a"}, headers=csrf_headers(cookie))
    assert r.status_code == 404
    r = await client.get("/api/v1/machines")
    assert r.status_code == 200 and r.json()["items"][1]["name"] == "ноутбук"


async def test_internal_app_machines(db, clock):
    """`hub machine list|name` — через внутренний сокет."""
    secret = ADMIN_PREFIX + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHI"
    notifier = Notifier()
    app = create_internal_app(db, clock=clock, notifier=notifier, admin_key_hash=hash_secret(secret))
    db.write_sync(lambda conn: machines.register(conn, LAPTOP, hostname="laptop", now_ms=1))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://hub-internal") as c:
        h = {"Authorization": f"Bearer {secret}"}
        r = await c.get("/internal/v1/admin/machines", headers=h)
        assert r.status_code == 200, r.text
        assert {m["host"] for m in r.json()["items"]} == {CORE, LAPTOP}
        r = await c.patch(f"/internal/v1/admin/machines/{LAPTOP}", json={"name": "ноут"}, headers=h)
        assert r.status_code == 200 and r.json()["machine"]["name"] == "ноут"
        r = await c.get("/internal/v1/admin/machines")
        assert r.status_code == 401


def test_cli_machine_list_and_name(db, clock, monkeypatch, capsys):
    """`hub machine list` и `hub machine name <hostname> <имя>` — через внутреннее приложение."""
    import argparse

    import httpx

    from hub import cli

    db.write_sync(
        lambda conn: machines.register(conn, LAPTOP, hostname="my-laptop", route="s1", now_ms=1)
    )
    seen = []

    class _Client:
        """Синхронный клиент админ-сокета: ответы — те же функции ядра, что у ручек `/admin/machines`."""

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, path):
            seen.append(("GET", path))
            with db.reader() as conn:
                body = {"items": machines.list_all(conn, now_ms=1), "core": CORE}
            return httpx.Response(200, json=body, request=httpx.Request("GET", "http://x" + path))

        def patch(self, path, json):
            seen.append(("PATCH", path, json))
            host = path.rsplit("/", 1)[1]
            out = db.write_sync(lambda conn: machines.rename(conn, host, json["name"], now_ms=1))
            return httpx.Response(
                200, json={"machine": out}, request=httpx.Request("PATCH", "http://x" + path)
            )

    monkeypatch.setattr(cli, "_admin_client", _Client)
    cli._cmd_machine(argparse.Namespace(machine_command="list", json=False))
    out = capsys.readouterr().out.splitlines()
    assert "машина ядра" in out[0] and "my-laptop" in out[1] and "путь s1" in out[1]
    cli._cmd_machine(argparse.Namespace(machine_command="name", machine="my-laptop", name="ноутбук"))
    assert capsys.readouterr().out.startswith("ноутбук · " + LAPTOP)
    with pytest.raises(SystemExit, match="нет"):
        cli._cmd_machine(argparse.Namespace(machine_command="name", machine="сервер", name="x"))
