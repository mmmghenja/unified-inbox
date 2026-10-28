"""Ф81-1: `GET /internal/v1/admin/sync/busy` — проекты, где у сессии хаба идёт ход или фоновая задача."""

import json

from .conftest import admin_headers


def _insert(db, project_id: int, state: str, bg: dict | None = None, closed: bool = False) -> int:
    def _fn(conn):
        return conn.execute(
            "INSERT INTO main.hub_session (project_id, engine, mode, workdir, state, created_ts, updated_ts, "
            "created_by, bg_json, closed_ts) VALUES (?, 'claude', 'manual', '/tmp', ?, 0, 0, 'ui', ?, ?)",
            (project_id, state, json.dumps(bg) if bg else None, 1 if closed else None),
        ).lastrowid

    return db.write_sync(_fn)


async def test_f81_sync_busy_running_and_bg(internal_client, db) -> None:
    for pid in (1, 2, 3, 4):
        db.write_sync(
            lambda conn, pid=pid: conn.execute("INSERT INTO main.project (id, name) VALUES (?, 'п')", (pid,))
        )
    s1 = _insert(db, 1, "running")
    _insert(db, 2, "idle")
    s3 = _insert(db, 3, "idle", bg={"items": [{"state": "running", "title": "x"}]})
    _insert(db, 4, "running", closed=True)

    r = await internal_client.get("/internal/v1/admin/sync/busy", headers=admin_headers())
    assert r.status_code == 200, r.text
    body = r.json()["projects"]
    assert body == {"1": [f"сессия #{s1}: ход"], "3": [f"сессия #{s3}: фоновая задача"]}


async def test_f81_sync_busy_needs_admin(internal_client) -> None:
    r = await internal_client.get("/internal/v1/admin/sync/busy")
    assert r.status_code in (401, 403)
