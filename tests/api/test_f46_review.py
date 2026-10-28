"""Ф46-1: пробы ревью — большой номер сообщения, предел после отмены, старое сообщение бота."""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from api.conftest import BASE_URL, insert_channel, insert_message, insert_project
from hub.core import outbox, security

AUTHOR = 5550001


def _now(clock):
    return int(clock() * 1000)


def _account(db, db_name="main", config=None):
    base = {"idempotent_send": True, "pace_window": "00:00-24:00", **(config or {})}
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, config_json) VALUES ('telegram', ?, ?)",
                (db_name, json.dumps(base)),
            ).lastrowid
        )
    )


def _token(db):
    return db.write_sync(
        lambda conn: security.create_api_token(
            conn,
            name="mcp:all",
            kind="interactive",
            scopes=["read", "draft", "attach"],
            project_ids=None,
            expires_ts=None,
        )
    ).token


@pytest.fixture
async def bearer(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as c:
        yield c


@pytest.mark.parametrize("db_name", ["main", "work"])
@pytest.mark.parametrize("mid", [2**63, 2**70])
async def test_huge_message_id(bearer, db, clock, db_name, mid):
    project = insert_project(db, name="P")
    tg = _account(db, db_name)
    token = _token(db)
    for extra in ({"text": "x"}, {"hold": True}):
        r = await bearer.post(
            "/api/v1/drafts/new",
            json={
                "account_id": tg,
                "project_id": project,
                "to": {"kind": "tg_msg_author", "message_id": mid},
                **extra,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 422, (r.status_code, r.text)


async def test_limit_after_cancel(bearer, db, clock):
    project = insert_project(db, name="P")
    tg = _account(db, config={"tg_author_daily": 1})
    token = _token(db)
    group = insert_channel(db, tg, project_id=project, ext_id="-1000000004242", kind="group", title="G")
    mids = []
    for i in range(2):
        c = db.write_sync(
            lambda conn, i=i: (
                conn.execute(
                    "INSERT INTO main.contact (account_id, ext_id, name, is_me) VALUES (?, ?, 'A', 0)",
                    (tg, str(AUTHOR + i)),
                ).lastrowid
            )
        )
        mids.append(insert_message(db, group, ext_id=str(800 + i), ts=_now(clock), sender_contact_id=c))

    async def new(mid):
        return await bearer.post(
            "/api/v1/drafts/new",
            json={
                "account_id": tg,
                "project_id": project,
                "to": {"kind": "tg_msg_author", "message_id": mid},
                "text": "x",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    r = await new(mids[0])
    assert r.status_code == 200, r.text
    db.write_sync(lambda conn: outbox.cancel(conn, r.json()["outbox_id"], now_ms=_now(clock)))
    r2 = await new(mids[1])
    assert r2.status_code == 429, (r2.status_code, r2.text)
    r3 = await new(mids[0])
    assert r3.status_code == 429, (r3.status_code, r3.text)


async def test_old_message_of_known_bot_is_refused(bearer, db, clock):
    """У сообщений до миграции 0033 `is_bot = 0`: бот и тот, у кого признак есть на любом другом сообщении."""
    project = insert_project(db, name="P")
    tg = _account(db)
    token = _token(db)
    group = insert_channel(db, tg, project_id=project, ext_id="-1000000004242", kind="group", title="G")
    c = db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.contact (account_id, ext_id, name, is_me) VALUES (?, ?, 'Bot', 0)",
                (tg, str(AUTHOR)),
            ).lastrowid
        )
    )
    old = insert_message(db, group, ext_id="800", ts=_now(clock), sender_contact_id=c)
    new = insert_message(db, group, ext_id="801", ts=_now(clock), sender_contact_id=c)
    db.write_sync(lambda conn: conn.execute("UPDATE main.message SET is_bot = 1 WHERE id = ?", (new,)))
    r = await bearer.post(
        "/api/v1/drafts/new",
        json={
            "account_id": tg,
            "project_id": project,
            "to": {"kind": "tg_msg_author", "message_id": old},
            "text": "x",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    print("СТАТУС:", r.status_code, r.text)
    assert r.status_code == 422, r.text
