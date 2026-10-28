"""Ф30-1 на публичном API: `send_at` у `POST /messages/send` (только bearer, не вместе с `paced`), выдача
`send_at_ts`, подтверждение одним и пачкой — 409 `send_at_passed` и «Отправить сейчас» (`send_now`), прогрев
в `/status` (`docs/architecture.md`, A2–A5, D1, D2, П7, П8)."""

import datetime as dt
import json
import zoneinfo

import pytest

from hub.core.outbox import SEND_AT_GRACE_MS

from .conftest import csrf_headers, insert_channel, insert_project, login_via_code, make_bearer_token

MSK = zoneinfo.ZoneInfo("Europe/Moscow")
NOW_S = dt.datetime(2026, 10, 2, 9, 0, tzinfo=MSK).timestamp()
AT = int(dt.datetime(2026, 10, 5, 10, 0, tzinfo=MSK).timestamp() * 1000)
AT_LOCAL = dt.datetime.fromtimestamp(AT / 1000, MSK).strftime("%H:%M")  # Ф32-2 п.3: пояс аккаунта


def _account(db, config=None) -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, config_json) VALUES ('telegram', 'main', ?)",
                (json.dumps({"idempotent_send": True, **(config or {})}),),
            ).lastrowid
        )
    )


@pytest.fixture
def stand(db, clock):
    clock.value = NOW_S
    project = insert_project(db, name="Umbrella")
    account = _account(db)
    channels = [insert_channel(db, account, project_id=project, ext_id=f"g{i}") for i in range(4)]
    token = make_bearer_token(db, scopes=("read", "draft"), project_ids=[project], name="mcp:iip")
    return {"project": project, "account": account, "channels": channels, "token": token}


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _send(client, headers, channel_id, *, key, send_at="2026-10-05 10:00", paced=None):
    body: dict = {"channel_id": channel_id, "text": "Через час вебинар", "idempotency_key": key}
    if send_at is not None:
        body["send_at"] = send_at
    if paced is not None:
        body["paced"] = paced
    return await client.post("/api/v1/messages/send", json=body, headers=headers)


async def _draft(client, stand, channel_id, *, key=None, send_at="2026-10-05 10:00") -> int:
    r = await _send(client, _bearer(stand["token"]), channel_id, key=key or f"k{channel_id}", send_at=send_at)
    assert r.status_code == 200, r.text
    return r.json()["outbox_id"]


def _row(db, outbox_id) -> dict:
    with db.reader() as conn:
        row = conn.execute(
            "SELECT status, not_before_ts, send_at_ts FROM main.outbox WHERE id = ?", (outbox_id,)
        ).fetchone()
    return dict(zip(("status", "not_before_ts", "send_at_ts"), row, strict=True))


# --- A2 / D1 -----------------------------------------------------------------------------------------------


async def test_a2_bearer_send_at_draft(client, db, stand):
    r = await _send(client, _bearer(stand["token"]), stand["channels"][0], key="a")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["status"], body["paced"], body["send_at_ts"]) == ("pending_approval", False, AT)
    assert _row(db, body["outbox_id"])["send_at_ts"] == AT
    with db.reader() as conn:
        (detail,) = conn.execute(
            "SELECT detail_json FROM main.audit WHERE action = 'message_send' AND outbox_id = ?",
            (body["outbox_id"],),
        ).fetchone()
    assert json.loads(detail)["send_at_ts"] == AT


async def test_a2_plain_send_answer_has_null_send_at(client, stand):
    r = await _send(client, _bearer(stand["token"]), stand["channels"][0], key="p", send_at=None)
    assert r.json()["send_at_ts"] is None


async def test_a2_ui_send_at_is_422(client, db, clock, stand):
    cookie, _csrf = await login_via_code(client, db, clock)
    r = await _send(client, csrf_headers(cookie), stand["channels"][0], key="ui")
    assert r.status_code == 422 and "send_at" in r.text
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.outbox").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("send_at", "paced", "needle"),
    [
        ("2026-10-05 10:00", True, "paced"),
        ("2026-10-01 10:00", None, "прошло"),
        ("2026-12-01 10:00", None, "30 дней"),
        ("в понедельник", None, "send_at"),
    ],
)
async def test_d1_bad_requests_are_422(client, db, stand, send_at, paced, needle):
    r = await _send(
        client, _bearer(stand["token"]), stand["channels"][0], key="b", send_at=send_at, paced=paced
    )
    assert r.status_code == 422, r.text
    assert needle in r.json()["detail"]
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.outbox").fetchone()[0] == 0


async def test_a2_outputs_carry_send_at(client, db, clock, stand):
    oid = await _draft(client, stand, stand["channels"][0])
    cookie, _csrf = await login_via_code(client, db, clock)
    r = await client.get("/api/v1/outbox", params={"status": "pending_approval"})
    (item,) = r.json()
    assert (item["id"], item["send_at_ts"], item["paced"], item["not_before_ts"]) == (oid, AT, False, None)
    r = await client.get(f"/api/v1/outbox/{oid}")
    assert r.json()["send_at_ts"] == AT
    r = await client.get(f"/api/v1/channels/{stand['channels'][0]}/messages")
    (msg,) = [m for m in r.json()["messages"] if m["outbox_id"] == oid]
    assert (msg["outbox_status"], msg["outbox_send_at_ts"]) == ("pending_approval", AT)

    r = await client.post(f"/api/v1/outbox/{oid}/approve", json={}, headers=csrf_headers(cookie))
    r = await client.get("/api/v1/outbox", params={"status": "queued"})
    (item,) = r.json()
    assert (item["send_at_ts"], item["not_before_ts"], item["paced"]) == (AT, AT, False)
    r = await client.get(f"/api/v1/channels/{stand['channels'][0]}/messages")
    (msg,) = [m for m in r.json()["messages"] if m["outbox_id"] == oid]
    assert (msg["outbox_status"], msg["outbox_not_before_ts"], msg["outbox_send_at_ts"]) == ("queued", AT, AT)


# --- D2 / П7 / П8: approve и approve-batch -----------------------------------------------------------------


async def test_d2_approve_before_time(client, db, clock, stand):
    oid = await _draft(client, stand, stand["channels"][0])
    cookie, _csrf = await login_via_code(client, db, clock)
    r = await client.post(f"/api/v1/outbox/{oid}/approve", json={}, headers=csrf_headers(cookie))
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "queued", "paced": False, "not_before_ts": AT, "send_at_ts": AT}
    with db.reader() as conn:
        details = [
            json.loads(d)
            for (d,) in conn.execute(
                "SELECT detail_json FROM main.audit WHERE action = 'outbox_approve' AND outbox_id = ?", (oid,)
            )
        ]
    # Аудит автомата и аудит ручки — оба с временем.
    assert all(d["not_before_ts"] == AT and d["send_at_ts"] == AT for d in details) and len(details) == 2


async def test_p8_approve_after_grace_is_409_send_at_passed(client, db, clock, stand):
    oid = await _draft(client, stand, stand["channels"][0])
    cookie, _csrf = await login_via_code(client, db, clock)
    clock.value = (AT + SEND_AT_GRACE_MS + 1000) / 1000
    r = await client.post(
        f"/api/v1/outbox/{oid}/approve", json={"text": "правка"}, headers=csrf_headers(cookie)
    )
    assert r.status_code == 409, r.text
    assert r.json() == {
        "detail": f"время отправки {AT_LOCAL} прошло",
        "error": "send_at_passed",
        "send_at_ts": AT,
    }
    assert _row(db, oid)["status"] == "pending_approval"

    r = await client.post(
        f"/api/v1/outbox/{oid}/approve", json={"send_now": True}, headers=csrf_headers(cookie)
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "queued", "paced": False, "not_before_ts": None}
    row = _row(db, oid)
    assert (row["status"], row["send_at_ts"]) == ("queued", None)
    assert row["not_before_ts"] <= int(clock() * 1000)


async def test_p8_batch_error_only_for_passed_id(client, db, clock, stand):
    early = await _draft(client, stand, stand["channels"][0], send_at="2026-10-05 10:00")
    later = await _draft(client, stand, stand["channels"][1], send_at="2026-10-05 15:00")
    plain = await _draft(client, stand, stand["channels"][2], send_at=None)
    cookie, _csrf = await login_via_code(client, db, clock)
    clock.value = (AT + SEND_AT_GRACE_MS + 1000) / 1000  # 12:00:01 05.10 — первое опоздало, второе впереди
    r = await client.post(
        "/api/v1/outbox/approve-batch", json={"ids": [early, later, plain]}, headers=csrf_headers(cookie)
    )
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    later_at = AT + 5 * 60 * 60 * 1000
    assert results == [
        {
            "id": early,
            "status": "error",
            "error": "send_at_passed",
            "send_at_ts": AT,
            "message": f"время отправки {AT_LOCAL} прошло",
        },
        {"id": later, "status": "queued", "paced": False, "not_before_ts": later_at, "send_at_ts": later_at},
        {"id": plain, "status": "queued", "paced": False},
    ]
    assert _row(db, early)["status"] == "pending_approval"
    assert _row(db, later)["status"] == "queued" and _row(db, plain)["status"] == "queued"


# --- A5: прогрев в /status ---------------------------------------------------------------------------------


async def test_a5_status_shows_warmup_limit(client, db, clock, stand):
    warm = _account(
        db,
        config={
            "pace_daily": 40,
            "pace_warmup_from": "2026-09-27",
            "pace_warmup_start": 5,
            "pace_warmup_days": 10,
        },
    )
    cookie, _csrf = await login_via_code(client, db, clock)
    r = await client.get("/api/v1/status")
    accounts = {a["id"]: a for a in r.json()["accounts"]}
    assert (accounts[warm]["pace_daily"], accounts[warm]["paced_today"]) == (22, 0)  # пятый день прогрева
    assert accounts[warm]["pace_warmup"] == {"from": "2026-09-27", "start": 5, "days": 10, "target": 40}
    assert "pace_warmup" not in accounts[stand["account"]]
    assert accounts[stand["account"]]["pace_daily"] == 20


# --- Ф32-2 п.3 + П8: «время отправки прошло» — в поясе аккаунта строки, не процесса ядра -------------------


@pytest.fixture
def far_east(db, clock):
    """Аккаунт с `pace_tz`, заведомо не совпадающим с поясом процесса ядра (минуты другие: +05:45)."""
    clock.value = NOW_S
    project = insert_project(db, name="Непал")
    account = _account(db, {"pace_tz": "Asia/Kathmandu"})
    channels = [insert_channel(db, account, project_id=project, ext_id=f"k{i}") for i in range(2)]
    token = make_bearer_token(db, scopes=("read", "draft"), project_ids=[project], name="mcp:nepal")
    at = int(dt.datetime(2026, 10, 5, 10, 7, tzinfo=zoneinfo.ZoneInfo("Asia/Kathmandu")).timestamp() * 1000)
    assert dt.datetime.fromtimestamp(at / 1000).astimezone().strftime("%H:%M") != "10:07"
    return {"project": project, "account": account, "channels": channels, "token": token, "at": at}


async def test_f322_send_at_passed_text_in_account_tz_single(client, db, clock, far_east):
    oid = await _draft(client, far_east, far_east["channels"][0], send_at="2026-10-05 10:07")
    assert _row(db, oid)["send_at_ts"] == far_east["at"]
    cookie, _csrf = await login_via_code(client, db, clock)
    clock.value = (far_east["at"] + SEND_AT_GRACE_MS + 1000) / 1000
    r = await client.post(f"/api/v1/outbox/{oid}/approve", json={}, headers=csrf_headers(cookie))
    assert r.status_code == 409, r.text
    assert r.json() == {
        "detail": "время отправки 10:07 прошло",
        "error": "send_at_passed",
        "send_at_ts": far_east["at"],
    }


async def test_f322_send_at_passed_text_in_account_tz_batch(client, db, clock, stand, far_east):
    """Пачка из двух аккаунтов с разными поясами: у каждой строки — время её аккаунта."""
    nepal = await _draft(client, far_east, far_east["channels"][0], send_at="2026-10-05 10:07")
    moscow = await _draft(client, stand, stand["channels"][0], send_at="2026-10-05 10:00")
    cookie, _csrf = await login_via_code(client, db, clock)
    clock.value = (max(AT, far_east["at"]) + SEND_AT_GRACE_MS + 1000) / 1000
    r = await client.post(
        "/api/v1/outbox/approve-batch", json={"ids": [nepal, moscow]}, headers=csrf_headers(cookie)
    )
    assert r.status_code == 200, r.text
    assert [(x["id"], x["error"], x["message"]) for x in r.json()["results"]] == [
        (nepal, "send_at_passed", "время отправки 10:07 прошло"),
        (moscow, "send_at_passed", "время отправки 10:00 прошло"),
    ]
