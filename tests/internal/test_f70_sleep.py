"""Ф70-1 (docs/architecture.md.3–A.4): `GET /internal/v1/account` несёт `sleep`,
long-poll `GET /internal/v1/sleep/wait` отвечает сразу при другом сне и будится записью «Закончить/Начать
день» (тот же `Notifier`, что у `/outbox/wait`)."""

import asyncio

from hub.core import conn_sleep, mapping, workday
from hub.core.routes import internal as internal_routes

from .conftest import connector_headers, make_connector_account

NOW = 1_000
AUDIT = {"actor": "admin", "api_token_id": None, "client_name": None, "request_id": "r"}


def _whole_account(db) -> tuple[int, str, int]:
    account_id, secret = make_connector_account(db, source="slack")

    def _fn(conn):
        project_id = conn.execute("INSERT INTO main.project (name) VALUES ('P')").lastrowid
        mapping.add_rule(
            conn, account_id=account_id, project_id=project_id, match={"all": True}, priority=None, now_ms=NOW
        )
        conn.execute(
            "INSERT INTO main.channel (account_id, ext_id, kind, project_id, map_source) "
            "VALUES (?, 'C1', 'channel', ?, 'auto')",
            (account_id, project_id),
        )
        workday.set_workday(conn, project_id, True, now_ms=NOW)
        return project_id

    return account_id, secret, db.write_sync(_fn)


def _day(db, project_id, open_: bool, now_ms: int) -> None:
    db.write_sync(lambda conn: workday.set_day(conn, project_id, open_, now_ms=now_ms, audit=AUDIT))


async def test_account_carries_sleep(internal_client, db) -> None:
    _account_id, secret, project_id = _whole_account(db)
    r = await internal_client.get("/internal/v1/account", headers=connector_headers(secret))
    assert r.status_code == 200, r.text
    assert r.json()["sleep"] is None
    _day(db, project_id, False, 5_000)
    r = await internal_client.get("/internal/v1/account", headers=connector_headers(secret))
    assert r.json()["sleep"] == {"project_id": project_id, "since_ts": 5_000}


async def test_sleep_wait_answers_at_once_when_state_differs(internal_client, db) -> None:
    _account_id, secret, project_id = _whole_account(db)
    _day(db, project_id, False, 5_000)
    r = await internal_client.get(
        "/internal/v1/sleep/wait",
        params={"asleep": "false", "timeout_s": 30},
        headers=connector_headers(secret),
    )
    assert r.json() == {"sleep": {"project_id": project_id, "since_ts": 5_000}}
    # Тот же сон и `timeout_s=0` — просто прочитать.
    r = await internal_client.get(
        "/internal/v1/sleep/wait",
        params={"asleep": "true", "timeout_s": 0},
        headers=connector_headers(secret),
    )
    assert r.json()["sleep"]["project_id"] == project_id


async def test_sleep_wait_wakes_on_day_change(internal_client, db) -> None:
    _account_id, secret, project_id = _whole_account(db)
    headers = connector_headers(secret)
    task = asyncio.ensure_future(
        internal_client.get(
            "/internal/v1/sleep/wait", params={"asleep": "false", "timeout_s": 20}, headers=headers
        )
    )
    await asyncio.sleep(0.1)
    assert not task.done()
    _day(db, project_id, False, 5_000)
    r = await asyncio.wait_for(task, 5)
    assert r.json()["sleep"] == {"project_id": project_id, "since_ts": 5_000}

    task = asyncio.ensure_future(
        internal_client.get(
            "/internal/v1/sleep/wait", params={"asleep": "true", "timeout_s": 20}, headers=headers
        )
    )
    await asyncio.sleep(0.1)
    assert not task.done()
    _day(db, project_id, True, 6_000)
    r = await asyncio.wait_for(task, 5)
    assert r.json() == {"sleep": None}


async def test_sleep_wait_times_out_with_same_state(internal_client, db) -> None:
    _account_id, secret, _project_id = _whole_account(db)
    r = await internal_client.get(
        "/internal/v1/sleep/wait",
        params={"asleep": "false", "timeout_s": 0.2},
        headers=connector_headers(secret),
    )
    assert r.json() == {"sleep": None}


async def test_sleep_wait_recheck_not_more_often_than_interval(internal_client, db, monkeypatch) -> None:
    """Ревью п.5: `Notifier` будит на каждый коммит — пачка записей за интервал даёт один пересчёт сна."""
    _account_id, secret, project_id = _whole_account(db)
    calls = []
    real = conn_sleep.asleep

    def _counting(conn, account_id, now_ms):
        calls.append(now_ms)
        return real(conn, account_id, now_ms)

    monkeypatch.setattr(conn_sleep, "asleep", _counting)
    monkeypatch.setattr(internal_routes, "_SLEEP_RECHECK_S", 0.5)
    task = asyncio.ensure_future(
        internal_client.get(
            "/internal/v1/sleep/wait",
            params={"asleep": "false", "timeout_s": 20},
            headers=connector_headers(secret),
        )
    )
    await asyncio.sleep(0.05)
    assert len(calls) == 1

    def _touch(conn, i):
        conn.execute("UPDATE main.project SET color = ? WHERE id = ?", (str(i), project_id))

    for i in range(20):  # двадцать коммитов, сон не меняется
        db.write_sync(lambda conn, i=i: _touch(conn, i))
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)
    assert len(calls) <= 2  # пауза ещё идёт: не больше одного пересчёта на интервал
    _day(db, project_id, False, 5_000)
    r = await asyncio.wait_for(task, 5)
    assert r.json()["sleep"] == {"project_id": project_id, "since_ts": 5_000}
    assert len(calls) <= 3
