"""Ф27-1 B (`docs/architecture.md`): ядро под упоминания Slack — `mention_names` у своих
строк Slack (лента, тред, `/sync`, `/messages/{id}`, `/outbox`, `/outbox/{id}`), ручка
`GET /accounts/{id}/mention_people`, Slack-id у людей (`/people`, `/people/{id}`)."""

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import mention_names, queries

from .conftest import (
    BASE_URL,
    csrf_headers,
    insert_channel,
    insert_message,
    insert_project,
    login_via_code,
    make_bearer_token,
)
from .test_new_anywhere import _account, _contact, _publish
from .test_people_api import world  # noqa: F401 — фикстура людей Ф21-1 (Кира — Slack U0KIRA)

API = "/api/v1"
PEOPLE = [
    {"id": "U0AAA", "real_name": "Анна Смирнова", "display_name": "Аня"},
    {"id": "U0BBB", "real_name": "Иван Петров", "display_name": None},
    {"id": "U0CCC", "real_name": "Ёжиков Игорь", "display_name": ""},
    {"id": "W0DDD", "real_name": None, "display_name": None},
]


@pytest.fixture
async def bearer(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as c:
        yield c


@pytest.fixture
async def env(client, db, clock):
    project = insert_project(db, name="Globex")
    other = insert_project(db, name="Личное")
    slack = _account(db, "slack", schema="work")
    slack2 = _account(db, "slack", schema="work")
    tg = _account(db, "telegram", config={"idempotent_send": True})
    _publish(db, clock, slack, "slack_people", [PEOPLE])
    ch = insert_channel(
        db, slack, project_id=project, ext_id="C1", kind="group", db_name="work", title="general"
    )
    ch_other = insert_channel(
        db, slack, project_id=other, ext_id="C2", kind="group", db_name="work", title="личное"
    )
    ch2 = insert_channel(db, slack2, project_id=project, ext_id="C9", kind="group", db_name="work")
    tg_ch = insert_channel(db, tg, project_id=project, ext_id="100", title="Пётр")
    cookie, _csrf = await login_via_code(client, db, clock)
    token = make_bearer_token(db, scopes=("read", "draft"), project_ids=[project], name="mcp:p")
    return {
        "project": project,
        "other": other,
        "slack": slack,
        "slack2": slack2,
        "tg": tg,
        "ch": ch,
        "ch_other": ch_other,
        "ch2": ch2,
        "tg_ch": tg_ch,
        "cookie": cookie,
        "ui": csrf_headers(cookie),
        "auth": {"Authorization": f"Bearer {token}"},
    }


async def _draft(bearer, env, channel_id, text, *, key, thread=None) -> int:
    body = {"channel_id": channel_id, "text": text, "idempotency_key": key}
    if thread is not None:
        body["thread"] = thread
    r = await bearer.post(f"{API}/messages/send", json=body, headers=env["auth"])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending_approval"
    return r.json()["outbox_id"]


def _by_outbox(messages: list[dict], outbox_id: int) -> dict:
    return next(m for m in messages if m.get("outbox_id") == outbox_id)


# --- mention_names ----------


async def test_draft_row_in_feed_outbox_and_sync(client, bearer, env):
    oid = await _draft(bearer, env, env["ch"], "<@U0AAA>, <@U0BBB> и <@U0ZZZ>, <@W0DDD>", key="d1")
    expected = {"U0AAA": "Аня", "U0BBB": "Иван Петров", "W0DDD": "W0DDD"}

    r = await client.get(f"{API}/channels/{env['ch']}/messages", headers=env["ui"])
    assert r.status_code == 200, r.text
    row = _by_outbox(r.json()["messages"], oid)
    assert row["mention_names"] == expected

    r = await client.get(f"{API}/outbox", params={"status": "pending_approval"}, headers=env["ui"])
    (item,) = [i for i in r.json() if i["id"] == oid]
    assert item["mention_names"] == expected

    r = await client.get(f"{API}/outbox/{oid}", headers=env["ui"])
    assert r.json()["mention_names"] == expected

    r = await client.get(f"{API}/sync", params={"since": 0}, headers=env["ui"])
    assert _by_outbox(r.json()["messages"], oid)["mention_names"] == expected

    r = await client.get(f"{API}/messages/{row['id']}", headers=env["ui"])
    assert r.json()["mention_names"] == expected


async def test_thread_draft_has_mention_names(client, bearer, env):
    parent = "1790000000.000100"
    oid = await _draft(bearer, env, env["ch"], "в тред <@U0CCC>", key="t1", thread=parent)
    r = await client.get(f"{API}/channels/{env['ch']}/threads/{parent}/messages", headers=env["ui"])
    assert r.status_code == 200, r.text
    assert _by_outbox(r.json()["messages"], oid)["mention_names"] == {"U0CCC": "Ёжиков Игорь"}


async def test_no_field_without_known_ids_and_outside_slack(client, bearer, env):
    plain = await _draft(bearer, env, env["ch"], "без упоминаний", key="p1")
    unknown = await _draft(bearer, env, env["ch"], "<@U0ZZZ> <!channel> <@U0AAA|x>", key="p2")
    tg = await _draft(bearer, env, env["tg_ch"], "<@U0AAA>", key="p3")
    other_ws = await _draft(bearer, env, env["ch2"], "<@U0AAA>", key="p4")  # другой воркспейс

    r = await client.get(f"{API}/outbox", params={"status": "pending_approval"}, headers=env["ui"])
    items = {i["id"]: i for i in r.json()}
    for oid in (plain, unknown, tg, other_ws):
        assert "mention_names" not in items[oid]
    r = await client.get(f"{API}/channels/{env['tg_ch']}/messages", headers=env["ui"])
    assert "mention_names" not in _by_outbox(r.json()["messages"], tg)


async def test_incoming_message_untouched(client, db, env):
    insert_message(db, env["ch"], ext_id="1790000001.000100", ts=1, text="<@U0AAA> буквально")
    r = await client.get(f"{API}/channels/{env['ch']}/messages", headers=env["ui"])
    (msg,) = r.json()["messages"]
    assert "mention_names" not in msg


async def test_directory_read_once_per_response(client, bearer, env, monkeypatch):
    for i in range(3):
        await _draft(bearer, env, env["ch"], f"<@U0AAA> {i}", key=f"m{i}")
    calls: list[int] = []
    real = queries.account_meta_list

    def counting(conn, account_id, name):
        calls.append(account_id)
        return real(conn, account_id, name)

    monkeypatch.setattr(queries, "account_meta_list", counting)
    r = await client.get(f"{API}/outbox", params={"status": "pending_approval"}, headers=env["ui"])
    assert all(i["mention_names"] == {"U0AAA": "Аня"} for i in r.json())
    assert calls == [env["slack"]]
    calls.clear()
    await client.get(f"{API}/channels/{env['ch']}/messages", headers=env["ui"])
    assert calls == [env["slack"]]
    calls.clear()
    await client.get(f"{API}/sync", params={"since": 0}, headers=env["ui"])
    assert calls == [env["slack"]]


# --- GET /accounts/{id}/mention_people ----------


async def _mp(client, env, account_id, headers=None, **params):
    return await client.get(
        f"{API}/accounts/{account_id}/mention_people", params=params, headers=headers or env["ui"]
    )


async def test_mention_people_participants_first_then_directory(client, db, env):
    _contact(db, env["slack"], "U0BBB", "Иван Петров", "@ivan", schema="work")
    _contact(db, env["slack"], "U0BOT", "Бот", "@bot", schema="work")
    with db.reader() as conn:
        ids = dict(conn.execute("SELECT ext_id, id FROM work.contact").fetchall())
    insert_message(db, env["ch"], ext_id="1790000001.000100", ts=10, sender_contact_id=ids["U0BOT"])
    insert_message(db, env["ch"], ext_id="1790000002.000100", ts=20, sender_contact_id=ids["U0BBB"])

    r = await _mp(client, env, env["slack"], channel_id=env["ch"])
    assert r.status_code == 200, r.text
    rows = r.json()
    # участник беседы первым; бот — не человек справочника; дальше справочник по имени (ё = е), без имени —
    # в конце
    assert [p["id"] for p in rows] == ["U0BBB", "U0AAA", "U0CCC", "W0DDD"]
    assert rows[0] == {"id": "U0BBB", "name": "Иван Петров", "handle": "@ivan"}
    assert rows[1] == {"id": "U0AAA", "name": "Аня", "handle": None}

    r = await _mp(client, env, env["slack"])  # без беседы — только справочник
    assert [p["id"] for p in r.json()] == ["U0AAA", "U0CCC", "U0BBB", "W0DDD"]


@pytest.mark.parametrize(
    ("q", "ids"),
    [
        ("ан", ["U0AAA"]),  # начало имени display_name / real_name
        ("Смир", ["U0AAA"]),  # начало второго слова
        ("петр", ["U0BBB"]),
        ("етр", []),  # не с начала слова
        ("еж", ["U0CCC"]),  # ё/е
        ("ЁЖИК", ["U0CCC"]),
        ("@iv", ["U0BBB"]),  # ник
        ("iv", ["U0BBB"]),
        ("", ["U0AAA", "U0BBB", "W0DDD", "U0CCC"]),
    ],
)
async def test_mention_people_match(client, db, env, q, ids):
    _contact(db, env["slack"], "U0BBB", "Иван Петров", "@ivan", schema="work")
    r = await _mp(client, env, env["slack"], q=q)
    assert r.status_code == 200, r.text
    assert sorted(p["id"] for p in r.json()) == sorted(ids)


async def test_mention_people_limit_8(client, db, clock, env):
    many = [{"id": f"U0X{i:02d}", "real_name": f"Человек {i}", "display_name": None} for i in range(12)]
    _publish(db, clock, env["slack"], "slack_people", [many])
    r = await _mp(client, env, env["slack"], q="чел")
    assert len(r.json()) == mention_names.MENTION_PEOPLE_MAX == 8


async def test_mention_people_not_found_cases(client, env):
    assert (await _mp(client, env, env["tg"])).status_code == 404
    assert (await _mp(client, env, 999)).status_code == 404
    # беседа другого аккаунта
    assert (await _mp(client, env, env["slack"], channel_id=env["ch2"])).status_code == 404
    assert (await _mp(client, env, env["slack"], channel_id=10**6)).status_code == 404
    assert (await _mp(client, env, env["slack"], q="x" * 101)).status_code == 422


async def test_mention_people_empty_directory(client, env):
    r = await _mp(client, env, env["slack2"], q="ан")
    assert r.status_code == 200 and r.json() == []


async def test_mention_people_bearer_scope(bearer, db, env):
    r = await _mp(bearer, env, env["slack"], headers=env["auth"], q="ан")
    assert r.status_code == 200 and [p["id"] for p in r.json()] == ["U0AAA"]
    r = await _mp(bearer, env, env["slack"], headers=env["auth"], channel_id=env["ch"])
    assert r.status_code == 200
    # беседа чужого проекта — 404
    r = await _mp(bearer, env, env["slack"], headers=env["auth"], channel_id=env["ch_other"])
    assert r.status_code == 404
    # токен проекта, где у аккаунта нет бесед, — 404
    token = make_bearer_token(db, scopes=("read",), project_ids=[env["other"] + 100], name="mcp:x")
    r = await _mp(bearer, env, env["slack"], headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 404
    # без скоупа read — 403
    token = make_bearer_token(db, scopes=("draft",), project_ids=[env["project"]], name="mcp:y")
    r = await _mp(bearer, env, env["slack"], headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


# --- Slack-id у людей ----------------------------------------------------------


async def test_people_slack_id_in_sources_and_card(client, db, clock, world):  # noqa: F811
    cookie, _ = await login_via_code(client, db, clock)
    ui = csrf_headers(cookie)
    r = await client.get(f"{API}/people", params={"q": "кира"}, headers=ui)
    (brief,) = r.json()["items"]
    assert brief["sources"] == [{"source": "slack", "handle": "@kira", "email": None, "slack_id": "U0KIRA"}]
    r = await client.get(f"{API}/people/{world['kira']}", headers=ui)
    (contact,) = r.json()["contacts"]
    assert contact["slack_id"] == "U0KIRA"
    # не Slack — поля нет
    r = await client.get(f"{API}/people/{world['petr']}", headers=ui)
    assert all("slack_id" not in c for c in r.json()["contacts"])
    r = await client.get(f"{API}/people", params={"q": "CLIENT.ru"}, headers=ui)
    assert all("slack_id" not in s for s in r.json()["items"][0]["sources"])
