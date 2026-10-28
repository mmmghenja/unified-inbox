"""Ф12-2 на публичном API: черновик по графику (`paced`), подтверждение одним и пачкой, выдача наружу слота и
остатка лимита, миграция 0020 (`docs/architecture.md`, A2–A5, тесты 6, 9 + П3)."""

import datetime as dt
import json
import zoneinfo

import pytest

from hub.core import db as db_module
from hub.core import pace
from hub.core.db import Db
from hub.core.routes.write import APPROVE_BATCH_MAX

from .conftest import (
    csrf_headers,
    insert_account,
    insert_channel,
    insert_project,
    login_via_code,
    make_bearer_token,
)

MSK = zoneinfo.ZoneInfo("Europe/Moscow")
NOON_S = dt.datetime(2026, 9, 28, 12, 0, tzinfo=MSK).timestamp()


def _account(db, source="telegram", config=None) -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, config_json) VALUES (?, 'main', ?)",
                (source, json.dumps(config or {"idempotent_send": True})),
            ).lastrowid
        )
    )


@pytest.fixture
def stand(db, clock):
    clock.value = NOON_S
    project = insert_project(db, name="Umbrella")
    account = _account(db)
    channels = [insert_channel(db, account, project_id=project, ext_id=f"u{i}") for i in range(8)]
    token = make_bearer_token(db, scopes=("read", "draft"), project_ids=[project], name="mcp:iip")
    return {"project": project, "account": account, "channels": channels, "token": token}


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _draft(client, stand, channel_id, *, paced=True, key=None):
    body = {
        "channel_id": channel_id,
        "text": "Напоминаю о подписи",
        "idempotency_key": key or f"k{channel_id}",
    }
    if paced is not None:
        body["paced"] = paced
    r = await client.post("/api/v1/messages/send", json=body, headers=_bearer(stand["token"]))
    assert r.status_code == 200, r.text
    return r.json()


def _row(db, outbox_id) -> dict:
    with db.reader() as conn:
        row = conn.execute(
            "SELECT status, paced, not_before_ts, pace_slot_ts FROM main.outbox WHERE id = ?", (outbox_id,)
        ).fetchone()
    return dict(zip(("status", "paced", "not_before_ts", "pace_slot_ts"), row, strict=True))


# --- A2 / П3: paced только от bearer --------------------------------------------------------------------


async def test_a2_bearer_paced_draft_and_ui_paced_422(client, db, clock, stand):
    body = await _draft(client, stand, stand["channels"][0])
    assert (body["status"], body["paced"]) == ("pending_approval", True)
    assert _row(db, body["outbox_id"])["paced"] == 1
    plain = await _draft(client, stand, stand["channels"][1], paced=None)
    assert plain["paced"] is False and _row(db, plain["outbox_id"])["paced"] == 0

    cookie, _csrf = await login_via_code(client, db, clock)
    r = await client.post(
        "/api/v1/messages/send",
        json={"channel_id": stand["channels"][2], "text": "x", "idempotency_key": "ui-1", "paced": True},
        headers=csrf_headers(cookie),
    )
    assert r.status_code == 422, r.text
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.outbox").fetchone()[0] == 2


async def test_a2_drafts_new_is_paced(client, db, stand):
    r = await client.post(
        "/api/v1/drafts/new",
        json={
            "account_id": stand["account"],
            "project_id": stand["project"],
            "to": "@Petr_Ivanov",
            "text": "Hi",
        },
        headers=_bearer(stand["token"]),
    )
    assert r.status_code == 200, r.text
    assert r.json()["paced"] is True
    assert _row(db, r.json()["outbox_id"])["paced"] == 1


# --- A3: одиночный approve --------------------------------------------------------------------------------


async def test_a3_single_approve_returns_slot_and_audits(client, db, clock, stand):
    paced = await _draft(client, stand, stand["channels"][0])
    plain = await _draft(client, stand, stand["channels"][1], paced=False)
    cookie, _csrf = await login_via_code(client, db, clock)
    r = await client.post(
        f"/api/v1/outbox/{paced['outbox_id']}/approve", json={}, headers=csrf_headers(cookie)
    )
    assert r.status_code == 200, r.text
    now_ms = int(clock() * 1000)
    slot = r.json()["not_before_ts"]
    # Слова владельца: первая строка графика — через случайную паузу `[0, gap_min]` от «сейчас».
    assert now_ms <= slot <= now_ms + pace.DEFAULT_GAP_MIN_S * 1000
    assert r.json() == {"status": "queued", "paced": True, "not_before_ts": slot}
    r = await client.post(
        f"/api/v1/outbox/{plain['outbox_id']}/approve", json={}, headers=csrf_headers(cookie)
    )
    assert r.json() == {"status": "queued", "paced": False, "not_before_ts": None}
    with db.reader() as conn:
        details = [
            json.loads(d) if d else None
            for (d,) in conn.execute(
                "SELECT detail_json FROM main.audit WHERE action = 'outbox_approve' AND outbox_id = ?",
                (paced["outbox_id"],),
            )
        ]
    assert details and all(d == {"not_before_ts": slot} for d in details)


# --- 6 / A4: approve-batch --------------------------------------------------------------------------------


async def test_6_batch_five_paced_get_increasing_slots_in_given_order(client, db, clock, stand):
    ids = [(await _draft(client, stand, stand["channels"][i]))["outbox_id"] for i in range(5)]
    cookie, _csrf = await login_via_code(client, db, clock)
    order = list(reversed(ids))
    r = await client.post("/api/v1/outbox/approve-batch", json={"ids": order}, headers=csrf_headers(cookie))
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert [x["id"] for x in results] == order
    assert all(x["status"] == "queued" and x["paced"] is True for x in results)
    slots = [x["not_before_ts"] for x in results]
    assert len(set(slots)) == 5 and slots == sorted(slots)
    assert all(b - a >= pace.DEFAULT_GAP_MIN_S * 1000 for a, b in zip(slots, slots[1:], strict=False))
    assert [_row(db, i)["not_before_ts"] for i in order] == slots


async def test_6_batch_error_only_for_bad_ids(client, db, clock, stand):
    ids = [(await _draft(client, stand, stand["channels"][i]))["outbox_id"] for i in range(3)]
    cookie, _csrf = await login_via_code(client, db, clock)
    r = await client.post(f"/api/v1/outbox/{ids[1]}/approve", json={}, headers=csrf_headers(cookie))
    assert r.status_code == 200
    r = await client.post(
        "/api/v1/outbox/approve-batch",
        json={"ids": [ids[0], ids[1], 999_999, ids[2]]},
        headers=csrf_headers(cookie),
    )
    assert r.status_code == 200, r.text
    results = {x["id"]: x for x in r.json()["results"]}
    assert results[ids[0]]["status"] == "queued" and results[ids[2]]["status"] == "queued"
    assert results[ids[1]] == {"id": ids[1], "status": "error", "error": "invalid transition"}
    assert results[999_999] == {"id": 999_999, "status": "error", "error": "not found"}
    assert _row(db, ids[2])["status"] == "queued"


async def test_6_batch_failure_after_write_rolls_back_only_that_id(client, db, clock, stand, monkeypatch):
    """Точка сохранения: сбой одного id уже ПОСЛЕ записи (здесь — в аудите маршрута) откатывает его запись,
    остальные подтверждены, их слоты не сдвинуты откатом."""
    from hub.core import outbox as outbox_mod
    from hub.core.routes import write as write_route

    ids = [(await _draft(client, stand, stand["channels"][i]))["outbox_id"] for i in range(3)]
    cookie, _csrf = await login_via_code(client, db, clock)
    real_audit = write_route.write_audit

    def _flaky_audit(conn, **kw):
        if kw.get("action") == "outbox_approve" and kw.get("outbox_id") == ids[1]:
            raise outbox_mod.OutboxError("сбой записи")
        return real_audit(conn, **kw)

    monkeypatch.setattr(write_route, "write_audit", _flaky_audit)
    r = await client.post("/api/v1/outbox/approve-batch", json={"ids": ids}, headers=csrf_headers(cookie))
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert [x["status"] for x in results] == ["queued", "error", "queued"]
    assert results[1]["error"] == "сбой записи"
    failed = _row(db, ids[1])
    assert (failed["status"], failed["pace_slot_ts"]) == ("pending_approval", None)
    expected = [results[0]["not_before_ts"], results[2]["not_before_ts"]]
    assert [_row(db, i)["not_before_ts"] for i in (ids[0], ids[2])] == expected
    with db.reader() as conn:
        audited = conn.execute(
            "SELECT COUNT(*) FROM main.audit WHERE action = 'outbox_approve' AND outbox_id = ?", (ids[1],)
        ).fetchone()[0]
    assert audited == 0


async def test_6_batch_cancelled_draft_is_invalid_transition(client, db, clock, stand):
    body = await _draft(client, stand, stand["channels"][0])
    cookie, _csrf = await login_via_code(client, db, clock)
    r = await client.post(f"/api/v1/outbox/{body['outbox_id']}/cancel", headers=csrf_headers(cookie))
    assert r.status_code == 200
    r = await client.post(
        "/api/v1/outbox/approve-batch", json={"ids": [body["outbox_id"]]}, headers=csrf_headers(cookie)
    )
    assert r.json()["results"] == [
        {"id": body["outbox_id"], "status": "error", "error": "invalid transition"}
    ]


async def test_6_batch_limits_and_bearer(client, db, clock, stand):
    body = await _draft(client, stand, stand["channels"][0])
    r = await client.post(
        "/api/v1/outbox/approve-batch", json={"ids": [body["outbox_id"]]}, headers=_bearer(stand["token"])
    )
    assert r.status_code == 403
    assert _row(db, body["outbox_id"])["status"] == "pending_approval"
    cookie, _csrf = await login_via_code(client, db, clock)
    too_many = list(range(1, APPROVE_BATCH_MAX + 2))
    r = await client.post(
        "/api/v1/outbox/approve-batch", json={"ids": too_many}, headers=csrf_headers(cookie)
    )
    assert r.status_code == 422
    r = await client.post("/api/v1/outbox/approve-batch", json={"ids": []}, headers=csrf_headers(cookie))
    assert r.status_code == 422
    assert _row(db, body["outbox_id"])["status"] == "pending_approval"


# --- A2/A5: выдача слота и остатка лимита ------------------------------------------------------------------


async def test_a2_outputs_carry_paced_and_slot(client, db, clock, stand):
    first = await _draft(client, stand, stand["channels"][0])
    second = await _draft(client, stand, stand["channels"][1])
    cookie, _csrf = await login_via_code(client, db, clock)

    r = await client.get("/api/v1/outbox", params={"status": "pending_approval"})
    assert {(x["id"], x["paced"], x["not_before_ts"]) for x in r.json()} == {
        (first["outbox_id"], True, None),
        (second["outbox_id"], True, None),
    }
    assert all(x["account_id"] == stand["account"] for x in r.json())

    r = await client.post(
        "/api/v1/outbox/approve-batch",
        json={"ids": [first["outbox_id"], second["outbox_id"]]},
        headers=csrf_headers(cookie),
    )
    slots = {x["id"]: x["not_before_ts"] for x in r.json()["results"]}

    r = await client.get("/api/v1/outbox", params={"status": "queued"})
    assert r.status_code == 200, r.text
    assert {x["id"]: x["not_before_ts"] for x in r.json()} == slots
    r = await client.get(f"/api/v1/outbox/{second['outbox_id']}")
    assert (r.json()["paced"], r.json()["not_before_ts"]) == (True, slots[second["outbox_id"]])

    r = await client.get(f"/api/v1/channels/{stand['channels'][1]}/messages")
    (msg,) = [m for m in r.json()["messages"] if m["outbox_id"] == second["outbox_id"]]
    assert (msg["outbox_status"], msg["outbox_paced"], msg["outbox_not_before_ts"]) == (
        "queued",
        True,
        slots[second["outbox_id"]],
    )

    r = await client.get("/api/v1/status")
    (acc,) = [a for a in r.json()["accounts"] if a["id"] == stand["account"]]
    assert (acc["paced_today"], acc["pace_daily"]) == (2, 20)


async def test_a5_status_for_project_token(client, db, clock, stand):
    await _draft(client, stand, stand["channels"][0])
    r = await client.get("/api/v1/status", headers=_bearer(stand["token"]))
    (acc,) = r.json()["accounts"]
    assert (acc["paced_today"], acc["pace_daily"]) == (0, 20)


# --- 9: миграция 0020 на копии БД предыдущей версии, обе схемы ----------------------------------------------


def test_9_migration_0020_on_previous_version_both_files(tmp_path, monkeypatch):
    original = db_module._migrations

    def _up_to_19(*, main: bool):
        return {v: files for v, files in original(main=main).items() if v <= 19}

    monkeypatch.setattr(db_module, "_migrations", _up_to_19)
    old = Db(tmp_path)
    old.start()
    try:
        token_id = old.write_sync(
            lambda conn: (
                conn.execute(
                    "INSERT INTO main.api_token (name, kind, hash) VALUES ('t', 'ui', 'x')"
                ).lastrowid
            )
        )
        for schema, source in (("main", "telegram"), ("work", "slack")):
            acc = insert_account(old, source=source, db_name=schema)

            def _fn(conn, schema=schema, acc=acc):
                cid = conn.execute(
                    f"INSERT INTO {schema}.channel (account_id, ext_id, kind, title, first_seen_ts) "  # noqa: S608
                    "VALUES (?, 'c1', 'dm', 'Вася', 0)",
                    (acc,),
                ).lastrowid
                for status in ("sent", "queued"):
                    conn.execute(
                        f"INSERT INTO {schema}.outbox (channel_id, account_id, text, origin, api_token_id, "  # noqa: S608
                        "send_token, status, not_before_ts, created_ts) "
                        "VALUES (?, ?, 'старое', 'ui', ?, ?, ?, 5, 0)",
                        (cid, acc, token_id, f"tok-{status}", status),
                    )

            old.write_sync(_fn)
        with old.reader() as conn:
            for schema in ("main", "work"):
                # 0019 — только main: у work последняя версия до 0020 — 18.
                assert conn.execute(f"PRAGMA {schema}.user_version").fetchone()[0] == (
                    19 if schema == "main" else 18
                )
                cols = {r[1] for r in conn.execute(f"PRAGMA {schema}.table_info(outbox)")}
                assert "paced" not in cols and "pace_slot_ts" not in cols
    finally:
        old.close()

    monkeypatch.setattr(db_module, "_migrations", original)
    new = Db(tmp_path)
    new.start()
    try:
        with new.reader() as conn:
            for schema in ("main", "work"):
                assert conn.execute(f"PRAGMA {schema}.user_version").fetchone()[0] >= 20
                idx = {r[1] for r in conn.execute(f"PRAGMA {schema}.index_list(outbox)")}
                assert "idx_outbox_paced" in idx
                rows = conn.execute(
                    f"SELECT status, paced, pace_slot_ts, not_before_ts, text FROM {schema}.outbox "  # noqa: S608
                    "ORDER BY id"
                ).fetchall()
                assert rows == [("sent", 0, None, 5, "старое"), ("queued", 0, None, 5, "старое")]
    finally:
        new.close()
