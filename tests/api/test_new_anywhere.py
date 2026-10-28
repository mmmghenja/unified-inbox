"""Ф14-2a+2b, ядро (`docs/architecture.md`–A6, тесты F 1–8): новые виды адресата
`tg_user`/`slack_users`, `channels/new` без проекта («по правилам»), привязка авто-заготовки, файлы в
заготовку Telegram и Slack, поиск «Кому» (`GET /recipients`), справочники аккаунта порциями.
"""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import draft_channels, ingest, mapping, outbox, queries

from .conftest import (
    BASE_URL,
    csrf_headers,
    insert_channel,
    insert_project,
    login_via_code,
    make_bearer_token,
)
from .test_send_files import PNG
from .test_send_files import _upload as _raw_upload

OWNER = "connector:1"
KIB = 1024


def _now(clock) -> int:
    return int(clock() * 1000)


def _account(db, source, *, schema="main", config=None) -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, config_json) VALUES (?, ?, ?)",
                (source, schema, json.dumps(config) if config is not None else None),
            ).lastrowid
        )
    )


def _contact(db, account_id, ext_id, name, handle=None, *, schema="main", is_me=0):
    db.write_sync(
        lambda conn: conn.execute(
            f"INSERT INTO {schema}.contact (account_id, ext_id, name, handle, is_me) VALUES (?, ?, ?, ?, ?)",
            (account_id, ext_id, name, handle, is_me),
        )
    )


def _meta(db, clock, account_id, events):
    return db.write_sync(
        lambda conn: ingest.apply_batch(
            conn, account_id=account_id, events=events, cursor=None, now_ms=_now(clock)
        )
    )


def _publish(db, clock, account_id, name, parts: list[list[dict]], *, header_parts=None):
    events = [{"type": "account_meta", "key": f"{name}:{i}", "value": part} for i, part in enumerate(parts)]
    count = sum(len(p) for p in parts)
    events.append(
        {
            "type": "account_meta",
            "key": name,
            "value": {"parts": len(parts) if header_parts is None else header_parts, "count": count},
        }
    )
    return _meta(db, clock, account_id, events)


def _row(db, table, row_id, schema="main") -> dict:
    with db.reader() as conn:
        cur = conn.execute(f"SELECT * FROM {schema}.{table} WHERE id = ?", (row_id,))
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, cur.fetchone(), strict=True))


def _audit(db, action) -> list[dict]:
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT result, channel_id, detail_json FROM main.audit WHERE action = ? ORDER BY id", (action,)
        ).fetchall()
    return [{"result": r[0], "channel_id": r[1], "detail": json.loads(r[2] or "null")} for r in rows]


def _lease(db, clock, account_id):
    return db.write_sync(
        lambda conn: outbox.lease(conn, account_id=account_id, owner=OWNER, now_ms=_now(clock))
    )


def _report(db, clock, lease, outcome="sent", **kw):
    db.write_sync(
        lambda conn: outbox.report(
            conn,
            outbox_id=lease.outbox_id,
            owner=OWNER,
            lease_gen=lease.lease_gen,
            outcome=outcome,
            now_ms=_now(clock),
            **kw,
        )
    )


@pytest.fixture
async def bearer(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as c:
        yield c


@pytest.fixture
async def env(client, db, clock):
    project = insert_project(db, name="Umbrella")
    other = insert_project(db, name="Личное")
    tg = _account(db, "telegram", config={"idempotent_send": True})
    slack = _account(db, "slack", schema="work")
    mail = _account(db, "mail", config={"address": "me@gmail.com"})
    cookie, _csrf = await login_via_code(client, db, clock)
    return {"project": project, "other": other, "tg": tg, "slack": slack, "mail": mail, "cookie": cookie}


async def _new(client, env, **body):
    return await client.post("/api/v1/channels/new", json=body, headers=csrf_headers(env["cookie"]))


async def _new_ok(client, env, **body) -> dict:
    r = await _new(client, env, **body)
    assert r.status_code == 200, r.text
    return r.json()


async def _send(client, env, channel_id, *, text="Привет", key="k1", files=None):
    body = {"channel_id": channel_id, "text": text, "idempotency_key": key}
    if files is not None:
        body["files"] = files
    return await client.post("/api/v1/messages/send", json=body, headers=csrf_headers(env["cookie"]))


# --- тест 1: `new_to_of` и лиз ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("new_to", "passes"),
    [
        ({"kind": "tg_username", "username": "petr"}, True),
        ({"kind": "email", "to": "a@b.c", "subject": "s", "from": None}, True),
        ({"kind": "tg_user", "user_id": 5, "name": "Пётр", "username": None}, True),
        ({"kind": "slack_users", "users": ["U1A"], "names": ["Аня"]}, True),
        ({"kind": "tg_phone", "phone": "+79000000001"}, True),  # Ф34-3: вид стал известным
        ({"kind": "wa_phone", "phone": "+7"}, False),
        ("tg_user", False),
    ],
)
def test_1_new_to_of_kinds(new_to, passes):
    meta = json.dumps({"attrs": {"new_to": new_to}})
    assert (draft_channels.new_to_of(meta) == new_to) is passes


async def test_1_lease_carries_full_new_to(client, db, clock, env):
    tg = await _new_ok(
        client,
        env,
        account_id=env["tg"],
        project_id=env["project"],
        to={"kind": "tg_user", "user_id": 555, "name": "Пётр", "username": "Petr_I"},
    )
    sl = await _new_ok(
        client,
        env,
        account_id=env["slack"],
        project_id=env["project"],
        to={"kind": "slack_users", "users": ["U0AAA", "U0BBB"]},
    )
    assert (await _send(client, env, tg["channel"]["id"])).status_code == 200
    assert (await _send(client, env, sl["channel"]["id"], key="k2")).status_code == 200
    (tg_lease,) = _lease(db, clock, env["tg"])
    assert tg_lease.new_to == {"kind": "tg_user", "user_id": 555, "name": "Пётр", "username": "Petr_I"}
    (sl_lease,) = _lease(db, clock, env["slack"])
    assert sl_lease.new_to == {
        "kind": "slack_users",
        "users": ["U0AAA", "U0BBB"],
        "names": ["U0AAA", "U0BBB"],
    }
    assert sl_lease.channel_ext_id.startswith(draft_channels.TEMP_PREFIX)


# --- тест 2: `tg_user` --------------------------------------------------------------------------------------


async def test_2_tg_user_title_from_body_contact_directory(client, db, clock, env):
    body = await _new_ok(
        client,
        env,
        account_id=env["tg"],
        project_id=env["project"],
        to={"kind": "tg_user", "user_id": 101, "name": "Пётр Иванов"},
    )
    ch = body["channel"]
    assert body["created"] is True
    assert (ch["kind"], ch["title"], ch["source"]) == ("dm", "Пётр Иванов", "telegram")
    assert ch["new_to"] == {"kind": "tg_user", "user_id": 101, "name": "Пётр Иванов", "username": None}

    _contact(db, env["tg"], "102", "Мария", "@maria_k")
    ch2 = (
        await _new_ok(
            client,
            env,
            account_id=env["tg"],
            project_id=env["project"],
            to={"kind": "tg_user", "user_id": 102},
        )
    )["channel"]
    assert ch2["title"] == "Мария" and ch2["new_to"]["username"] == "maria_k"

    _publish(
        db,
        clock,
        env["tg"],
        "tg_contacts",
        [[{"id": 103, "first": "Олег", "last": "Петров", "username": "oleg_p"}]],
    )
    ch3 = (
        await _new_ok(
            client,
            env,
            account_id=env["tg"],
            project_id=env["project"],
            to={"kind": "tg_user", "user_id": 103},
        )
    )["channel"]
    assert ch3["title"] == "Олег Петров" and ch3["new_to"]["username"] == "oleg_p"

    ch4 = (
        await _new_ok(
            client,
            env,
            account_id=env["tg"],
            project_id=env["project"],
            to={"kind": "tg_user", "user_id": 104},
        )
    )["channel"]
    assert ch4["title"] == "id 104"

    again = await _new_ok(
        client, env, account_id=env["tg"], project_id=env["project"], to={"kind": "tg_user", "user_id": 101}
    )
    assert again["created"] is False and again["channel"]["id"] == ch["id"]
    audit = _audit(db, "channel_new")
    assert [a["detail"]["to_kind"] for a in audit] == ["tg_user"] * 5
    raw = json.dumps(audit, ensure_ascii=False)
    assert "Пётр" not in raw and "maria" not in raw and "101" not in raw


async def test_2_tg_user_existing_dm_is_returned(client, db, env):
    dm = insert_channel(db, env["tg"], ext_id="555", kind="dm", title="Пётр", project_id=env["other"])
    body = await _new_ok(
        client, env, account_id=env["tg"], project_id=env["project"], to={"kind": "tg_user", "user_id": 555}
    )
    assert body["created"] is False and body["channel"]["id"] == dm
    with db.reader() as conn:
        drafts = conn.execute(
            f"SELECT COUNT(*) FROM main.channel WHERE {draft_channels.temp_ext_sql('')}"
        ).fetchone()[0]
    assert drafts == 0
    assert _audit(db, "channel_new")[0]["result"] == "existing"


@pytest.mark.parametrize(
    ("which", "to"),
    [
        ("mail", {"kind": "tg_user", "user_id": 5}),
        ("slack", {"kind": "tg_user", "user_id": 5}),
        ("tg", {"kind": "tg_user", "user_id": 0}),
        ("tg", {"kind": "tg_user", "user_id": 2**53 + 1}),
        ("tg", {"kind": "tg_user", "user_id": "5"}),
        ("tg", {"kind": "tg_user", "user_id": True}),
        ("tg", {"kind": "tg_user", "user_id": 5, "username": "a b"}),
        ("tg", {"kind": "tg_user", "user_id": 5, "phone": "+7"}),
        ("tg", {"kind": "tg_user", "user_id": 5, "name": "x" * 201}),
        ("tg", {"kind": "tg_phone", "phone": "+7"}),
        ("tg", {"kind": "slack_users", "users": ["U0AAA"]}),
    ],
)
async def test_2_tg_user_invalid_422(client, db, env, which, to):
    r = await _new(client, env, account_id=env[which], project_id=env["project"], to=to)
    assert r.status_code == 422, r.text
    assert _audit(db, "channel_new") == []


# --- тест 3: `slack_users` ----------------------------------------------------------------------------------


async def test_3_slack_users_kinds_and_names(client, db, clock, env):
    _contact(db, env["slack"], "U0AAA", "Аня", "@anya", schema="work")
    _publish(
        db,
        clock,
        env["slack"],
        "slack_people",
        [
            [
                {"id": "U0BBB", "real_name": "Борис Б", "display_name": "boris"},
                {"id": "U0CCC", "real_name": "Вера В", "display_name": ""},
            ]
        ],
    )
    one = (
        await _new_ok(
            client,
            env,
            account_id=env["slack"],
            project_id=env["project"],
            to={"kind": "slack_users", "users": ["U0AAA"]},
        )
    )["channel"]
    assert (one["kind"], one["title"], one["source"]) == ("dm", "Аня", "slack")
    assert one["new_to"] == {"kind": "slack_users", "users": ["U0AAA"], "names": ["Аня"]}
    three = (
        await _new_ok(
            client,
            env,
            account_id=env["slack"],
            project_id=env["project"],
            to={"kind": "slack_users", "users": ["U0CCC", "U0BBB", "W0DDD"]},
        )
    )["channel"]
    assert three["kind"] == "group"
    assert three["new_to"]["names"] == ["Вера В", "boris", "W0DDD"]
    assert three["title"] == "Вера В, boris, W0DDD"
    row = _row(db, "channel", three["id"], schema="work")
    assert json.loads(row["meta_json"])["own_id_space"] is True

    # тот же набор в другом порядке — та же заготовка
    again = await _new_ok(
        client,
        env,
        account_id=env["slack"],
        project_id=env["project"],
        to={"kind": "slack_users", "users": ["W0DDD", "U0BBB", "U0CCC"]},
    )
    assert again["created"] is False and again["channel"]["id"] == three["id"]
    assert [a["detail"]["to_kind"] for a in _audit(db, "channel_new")] == ["slack_users"] * 3


@pytest.mark.parametrize(
    "to",
    [
        {"kind": "slack_users", "users": [f"U0A{i}" for i in range(9)]},
        {"kind": "slack_users", "users": []},
        {"kind": "slack_users", "users": ["U0AAA", "U0AAA"]},
        {"kind": "slack_users", "users": ["u0aaa"]},
        {"kind": "slack_users", "users": ["C0AAA"]},
        {"kind": "slack_users", "users": "U0AAA"},
        {"kind": "slack_users", "users": ["U0AAA"], "extra": 1},
        "@petr_ivanov",
        "anya@example.com",
    ],
)
async def test_3_slack_invalid_422(client, db, env, to):
    r = await _new(client, env, account_id=env["slack"], project_id=env["project"], to=to)
    assert r.status_code == 422, r.text


async def test_3_slack_string_message_and_sessions_unchanged(bearer, client, db, env):
    r = await _new(client, env, account_id=env["slack"], project_id=env["project"], to="@anya")
    assert r.status_code == 422 and "выберите людей" in r.json()["detail"]
    # сессиям Claude Slack по-прежнему нельзя (`drafts/new` не меняется)
    token = make_bearer_token(db, scopes=("read", "draft"), project_ids=[env["project"]], name="mcp:iip")
    r = await bearer.post(
        "/api/v1/drafts/new",
        json={"account_id": env["slack"], "project_id": env["project"], "to": "@anya", "text": "x"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422 and "Telegram, WhatsApp или почты" in r.json()["detail"]


# --- тест 4: без проекта — «по правилам» -------------------------------------------------------------------


async def test_4_null_project_auto_and_rules(client, db, clock, env):
    # ручная заготовка тому же адресату не отдаётся на запрос «по правилам»
    manual_first = await _new_ok(
        client, env, account_id=env["tg"], project_id=env["project"], to="@Petr_Ivanov"
    )
    body = await _new_ok(client, env, account_id=env["tg"], project_id=None, to="@Petr_Ivanov")
    assert body["created"] is True and body["channel"]["id"] != manual_first["channel"]["id"]
    ch = body["channel"]
    row = _row(db, "channel", ch["id"])
    assert (row["map_source"], row["project_id"]) == ("auto", None)
    assert ch["project_id"] is None and row["map_reason"] == "нет правил"
    audit = _audit(db, "channel_new")
    assert audit[-1]["detail"]["project_id"] is None

    # правило «весь аккаунт» перекладывает (и уже заведённую — пересчётом аккаунта)
    db.write_sync(
        lambda conn: mapping.add_rule(
            conn,
            account_id=env["tg"],
            project_id=env["other"],
            match={"all": True},
            priority=None,
            now_ms=_now(clock),
        )
    )
    assert _row(db, "channel", ch["id"])["project_id"] == env["other"]
    # повтор с null — та же, хотя правило её переложило
    again = await _new_ok(client, env, account_id=env["tg"], project_id=None, to="@petr_ivanov")
    assert again["created"] is False and again["channel"]["id"] == ch["id"]
    # с проектом (даже тем же, куда легла по правилу) — отдельная ручная
    manual = await _new_ok(client, env, account_id=env["tg"], project_id=env["other"], to="@Petr_Ivanov")
    assert manual["created"] is True and manual["channel"]["id"] != ch["id"]
    assert _row(db, "channel", manual["channel"]["id"])["map_source"] == "manual"
    # новая заготовка «по правилам» сразу в проекте правила
    fresh = (
        await _new_ok(
            client, env, account_id=env["tg"], project_id=None, to={"kind": "tg_user", "user_id": 77}
        )
    )["channel"]
    assert fresh["project_id"] == env["other"]
    assert _row(db, "channel", fresh["id"])["map_reason"].startswith("правило #")


@pytest.mark.parametrize("project_id", [0, -1, -5])
async def test_4_non_positive_project_422(client, env, project_id):
    r = await _new(client, env, account_id=env["tg"], project_id=project_id, to="@Petr_Ivanov")
    assert r.status_code == 422


# --- тест 5: привязка авто-заготовки -----------------------------------------------------------------------


def _mail_event(thread, msg_id, *, ts, is_me, from_domains):
    return {
        "type": "upsert",
        "channel": {
            "ext_id": thread,
            "kind": "mail_thread",
            "title": "Тема",
            "own_id_space": True,
            "attrs": {"from_domains": from_domains},
        },
        "ext_id": msg_id,
        "ts": ts,
        "is_me": is_me,
        "kind": "text",
        "text": "ответ",
        "mentions_me": False,
    }


async def test_5_rename_auto_draft_then_rules_by_attrs(client, db, clock, env):
    db.write_sync(
        lambda conn: mapping.add_rule(
            conn,
            account_id=env["mail"],
            project_id=env["project"],
            match={"attr": "from_domains", "has": "client.ru"},
            priority=None,
            now_ms=_now(clock),
        )
    )
    ch = (
        await _new_ok(
            client, env, account_id=env["mail"], project_id=None, to="a@client.ru", subject="Договор"
        )
    )["channel"]
    assert ch["project_id"] is None  # домен у заготовки ещё не известен
    await _send(client, env, ch["id"])
    (lease,) = _lease(db, clock, env["mail"])
    _report(db, clock, lease, ext_id="<hub-1@gmail.com>", peer_ext_id="18000000001")
    row = _row(db, "channel", ch["id"])
    assert (row["ext_id"], row["map_source"], row["project_id"]) == ("18000000001", "auto", None)
    # следующее событие беседы приносит домены — правило раскладывает
    clock.advance_ms(1000)
    _meta(
        db,
        clock,
        env["mail"],
        [
            _mail_event(
                "18000000001", "<r1@client.ru>", ts=_now(clock), is_me=False, from_domains=["client.ru"]
            )
        ],
    )
    row = _row(db, "channel", ch["id"])
    assert row["project_id"] == env["project"] and row["map_reason"].startswith("правило #")


async def test_5_rename_auto_draft_applies_ext_id_rule_at_once(client, db, clock, env):
    db.write_sync(
        lambda conn: mapping.add_rule(
            conn,
            account_id=env["tg"],
            project_id=env["project"],
            match={"channel_ext_id": "555"},
            priority=None,
            now_ms=_now(clock),
        )
    )
    ch = (await _new_ok(client, env, account_id=env["tg"], project_id=None, to="@Petr_Ivanov"))["channel"]
    await _send(client, env, ch["id"])
    (lease,) = _lease(db, clock, env["tg"])
    _report(db, clock, lease, ext_id="9001", peer_ext_id="555", peer_title="Пётр")
    row = _row(db, "channel", ch["id"])
    assert (row["ext_id"], row["project_id"], row["map_source"]) == ("555", env["project"], "auto")


async def test_5_merge_auto_draft_keeps_manual_target_project(client, db, clock, env):
    dm = insert_channel(db, env["tg"], ext_id="555", kind="dm", title="Пётр", project_id=env["other"])
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.channel SET map_source = 'manual' WHERE id = ?", (dm,))
    )
    ch = (await _new_ok(client, env, account_id=env["tg"], project_id=None, to="@Petr_Ivanov"))["channel"]
    await _send(client, env, ch["id"])
    (lease,) = _lease(db, clock, env["tg"])
    _report(db, clock, lease, ext_id="9001", peer_ext_id="555", peer_title="Пётр")
    assert (_row(db, "channel", dm)["project_id"], _row(db, "channel", dm)["map_source"]) == (
        env["other"],
        "manual",
    )
    assert _row(db, "channel", ch["id"])["archived"] == 1
    assert _row(db, "outbox", lease.outbox_id)["channel_id"] == dm


async def test_5_merge_auto_draft_leaves_fresh_target_to_rules(client, db, clock, env):
    """Ревью Ф14-2: эхо своей отправки создало настоящую беседу раньше отчёта, её разложило точное правило;
    проект авто-заготовки (от правила «весь аккаунт») не переезжает и не закрепляет беседу руками."""
    for project_id, match in ((env["other"], {"all": True}), (env["project"], {"channel_ext_id": "555"})):
        db.write_sync(
            lambda conn, project_id=project_id, match=match: mapping.add_rule(
                conn,
                account_id=env["tg"],
                project_id=project_id,
                match=match,
                priority=None,
                now_ms=_now(clock),
            )
        )
    ch = (await _new_ok(client, env, account_id=env["tg"], project_id=None, to="@Petr_Ivanov"))["channel"]
    assert ch["project_id"] == env["other"]
    await _send(client, env, ch["id"])
    (lease,) = _lease(db, clock, env["tg"])
    clock.advance_ms(500)
    echo = {
        "type": "upsert",
        "channel": {"ext_id": "555", "kind": "dm", "title": "Пётр"},
        "ext_id": "9001",
        "ts": _now(clock),
        "is_me": True,
        "kind": "text",
        "text": "Привет",
        "mentions_me": False,
    }
    _meta(db, clock, env["tg"], [echo])
    with db.reader() as conn:
        (real_id,) = conn.execute("SELECT id FROM main.channel WHERE ext_id = '555'").fetchone()
    _report(db, clock, lease, ext_id="9001", peer_ext_id="555", peer_title="Пётр")
    row = _row(db, "channel", real_id)
    assert (row["project_id"], row["map_source"]) == (env["project"], "auto")
    assert _row(db, "channel", ch["id"])["archived"] == 1
    assert _audit(db, "draft_merge_other_project") == []


# --- тест 6: файлы в заготовку Telegram и Slack ------------------------------------------------------------


def _png(i: int) -> bytes:
    return PNG + f"-{i}".encode()


async def _upload(client, env, channel_id, body):
    r = await _raw_upload(client, env["cookie"], channel_id, body)
    assert r.status_code == 200, r.text
    return r.json()["upload_id"]


@pytest.mark.parametrize(
    ("which", "to"),
    [
        ("tg", "@Petr_Ivanov"),
        ("tg", {"kind": "tg_user", "user_id": 555, "name": "Пётр"}),
        ("slack", {"kind": "slack_users", "users": ["U0AAA"]}),
    ],
)
async def test_6_files_into_draft_one_row(client, db, clock, env, which, to):
    ch = (await _new_ok(client, env, account_id=env[which], project_id=env["project"], to=to))["channel"]
    ids = [await _upload(client, env, ch["id"], _png(i)) for i in range(3)]
    r = await _send(client, env, ch["id"], text="Фото", files=ids)
    assert r.status_code == 200, r.text
    (lease,) = _lease(db, clock, env[which])
    assert len(lease.files) == 3
    assert lease.new_to["kind"] == (to["kind"] if isinstance(to, dict) else "tg_username")
    schema = "work" if which == "slack" else "main"
    with db.reader() as conn:
        rows = conn.execute(
            f"SELECT COUNT(*) FROM {schema}.outbox WHERE channel_id = ?", (ch["id"],)
        ).fetchone()
    assert rows[0] == 1


async def test_6_telegram_caption_1025_is_422(client, db, env):
    ch = (await _new_ok(client, env, account_id=env["tg"], project_id=env["project"], to="@Petr_Ivanov"))[
        "channel"
    ]
    upload = await _upload(client, env, ch["id"], _png(1))
    r = await _send(client, env, ch["id"], text="я" * 1025, files=[upload])
    assert r.status_code == 422
    r = await _send(client, env, ch["id"], text="я" * 1024, files=[upload], key="k2")
    assert r.status_code == 200, r.text


async def test_6_mail_draft_files_as_before(client, db, clock, env):
    ch = (
        await _new_ok(
            client, env, account_id=env["mail"], project_id=env["project"], to="a@b.ru", subject="Счёт"
        )
    )["channel"]
    upload = await _upload(client, env, ch["id"], _png(1))
    assert (await _send(client, env, ch["id"], text="Во вложении", files=[upload])).status_code == 200
    (lease,) = _lease(db, clock, env["mail"])
    assert len(lease.files) == 1 and lease.new_to["kind"] == "email"


# --- тест 7: `GET /recipients` -----------------------------------------------------------------------------


async def _recipients(client, q):
    r = await client.get("/api/v1/recipients", params={"q": q})
    assert r.status_code == 200, r.text
    return r.json()


async def test_7_recipients_channels_and_people(client, db, clock, env):
    tg, slack, mail = env["tg"], env["slack"], env["mail"]
    live = insert_channel(
        db, tg, ext_id="555", kind="dm", title="Пётр Иванов", last_msg_ts=100, project_id=env["project"]
    )
    newer = insert_channel(db, tg, ext_id="-100", kind="group", title="Группа ПЕТРА", last_msg_ts=200)
    insert_channel(db, tg, ext_id="-101", kind="group", title="Пётр архив", archived=1)
    await _new_ok(client, env, account_id=tg, project_id=env["project"], to="@petr_draft")  # заготовка
    tracker = insert_channel(db, mail, ext_id="jira:x:K-1", kind="issue", title="Пётр задача")
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET meta_json = ? WHERE id = ?",
            (json.dumps({"attrs": {"no_reply": True, "tracker": "jira"}}), tracker),
        )
    )

    _contact(db, tg, "555", "Пётр Иванов", "@petr_i")  # личка уже в «беседах» — не выводится
    _contact(db, tg, "556", "Пётр Сидоров", None)  # без лички
    _contact(db, tg, "1", "владелец", "@me", is_me=1)
    _contact(db, tg, "-1001", "Канал Пётр")  # не пользователь
    _publish(
        db,
        clock,
        tg,
        "tg_contacts",
        [
            [
                {"id": 556, "first": "Петя", "last": None, "username": "petya_s"},  # склейка с contact
                {"id": 557, "first": "Анна", "last": "Петрова", "username": None},
            ]
        ],
    )
    _contact(db, slack, "U0PPP", "Петра", "@petra", schema="work")
    _contact(db, slack, "B0BOT", "Пётр-бот", schema="work")
    _publish(
        db, clock, slack, "slack_people", [[{"id": "U0QQQ", "real_name": "Пётр Слак", "display_name": ""}]]
    )
    _contact(db, mail, "petr@client.ru", "Пётр Почтов", "petr@client.ru")
    _contact(db, mail, "jira:x", "Пётр Jira")

    rows = await _recipients(client, "  пёт ")
    channels = [r for r in rows if r["type"] == "channel"]
    people = [r for r in rows if r["type"] == "person"]
    assert [c["channel_id"] for c in channels] == [live]  # только «Пётр Иванов»: подстрока «пёт»
    assert channels[0] == {
        "type": "channel",
        "channel_id": live,
        "title": "Пётр Иванов",
        "account_id": tg,
        "source": "telegram",
        "kind": "dm",
        "project_id": env["project"],
        "last_msg_ts": 100,
    }
    by_to = {json.dumps(p["to"], sort_keys=True, ensure_ascii=False): p for p in people}
    assert len(by_to) == len(people)
    sid = next(
        p
        for p in people
        if p["to"] == {"kind": "tg_user", "user_id": 556, "name": "Пётр Сидоров", "username": "petya_s"}
    )
    assert (sid["account_id"], sid["source"], sid["handle"], sid["channel_id"]) == (
        tg,
        "telegram",
        "@petya_s",
        None,
    )
    assert {"kind": "slack_users", "users": ["U0QQQ"]} in [p["to"] for p in people]
    assert "petr@client.ru" in [p["to"] for p in people]
    tos = json.dumps([p["to"] for p in people], ensure_ascii=False)
    assert "555" not in tos and "B0BOT" not in tos and "jira" not in tos and "-1001" not in tos

    # «петр» (без ё) — латиница/кириллица без регистра: группа ПЕТРА, Петра (Slack), Петрова
    rows = await _recipients(client, "ПЕТР")
    assert [r["channel_id"] for r in rows if r["type"] == "channel"] == [newer]
    names = [r["name"] for r in rows if r["type"] == "person"]
    assert names[:1] == ["Петра"]  # начинается с запроса — выше
    assert {"kind": "slack_users", "users": ["U0PPP"]} in [r["to"] for r in rows if r["type"] == "person"]
    assert "Анна Петрова" in names
    # у Telegram-человека с личкой, которой нет среди бесед ответа, — `channel_id`
    rows = await _recipients(client, "petr_i")
    (ivanov,) = [r for r in rows if r["type"] == "person"]
    assert ivanov["channel_id"] == live and ivanov["to"]["user_id"] == 555


async def test_7_recipients_limit_and_order(client, db, clock, env):
    for i in range(15):
        insert_channel(db, env["tg"], ext_id=f"-{200 + i}", kind="group", title=f"Команда {i}", last_msg_ts=i)
    for i in range(15):
        _contact(db, env["tg"], str(1000 + i), f"Командир {14 - i:02d}")
    _contact(db, env["tg"], "2000", "Иван Командный")
    rows = await _recipients(client, "ком")
    assert len(rows) == 20
    channels = [r for r in rows if r["type"] == "channel"]
    assert len(channels) == 12
    assert [c["last_msg_ts"] for c in channels] == list(range(14, 2, -1))
    assert [r["name"] for r in rows[12:]] == [f"Командир {i:02d}" for i in range(8)]

    rows = await _recipients(client, "командир 1")
    assert [r["name"] for r in rows] == [f"Командир {i:02d}" for i in range(10, 15)]
    # начинается с запроса — выше, остальное — по имени
    rows = await _recipients(client, "командн")
    assert [r["name"] for r in rows] == ["Иван Командный"]
    _contact(db, env["tg"], "2001", "Командный Олег")
    _contact(db, env["tg"], "2002", "Анна Командная")
    rows = await _recipients(client, "командн")
    assert [r["name"] for r in rows] == ["Командный Олег", "Анна Командная", "Иван Командный"]


@pytest.mark.parametrize("q", ["", "я", "  a  ", "x" * 101])
async def test_7_recipients_bad_query_422(client, env, q):
    r = await client.get("/api/v1/recipients", params={"q": q})
    assert r.status_code == 422


async def test_7_recipients_bearer_forbidden(bearer, db, env):
    token = make_bearer_token(db, scopes=("read", "draft", "send", "organize"), name="mcp:all")
    r = await bearer.get(
        "/api/v1/recipients", params={"q": "пётр"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 403
    denied = _audit(db, "ui_only_denied")
    assert len(denied) == 1 and denied[0]["detail"]["path"] == "/api/v1/recipients"


async def test_7_receive_only_mailbox_people_hidden(client, db, env):
    ro = _account(db, "mail", config={"address": "ro@gmail.com", "send_enabled": False})
    _contact(db, ro, "zina@client.ru", "Зина")
    assert await _recipients(client, "зина") == []


# --- тест 8: справочник порциями ----------------------------------------------------------------------------


def _list(db, account_id, name):
    with db.reader() as conn:
        return queries.account_meta_list(conn, account_id, name)


async def test_8_parts_collected(db, clock, env):
    parts = [[{"id": 1}, {"id": 2}], [{"id": 3}], [{"id": 4}, "мусор"]]
    _publish(db, clock, env["tg"], "tg_contacts", parts)
    assert [i["id"] for i in _list(db, env["tg"], "tg_contacts")] == [1, 2, 3, 4]
    # заголовок с двумя порциями при лежащих трёх — две
    _meta(
        db,
        clock,
        env["tg"],
        [{"type": "account_meta", "key": "tg_contacts", "value": {"parts": 2, "count": 3}}],
    )
    assert [i["id"] for i in _list(db, env["tg"], "tg_contacts")] == [1, 2, 3]
    # нет заголовка — пусто; кривой заголовок — пусто
    assert _list(db, env["tg"], "slack_people") == []
    assert _list(db, 9999, "tg_contacts") == []
    _meta(db, clock, env["tg"], [{"type": "account_meta", "key": "tg_contacts", "value": {"parts": "2"}}])
    assert _list(db, env["tg"], "tg_contacts") == []


async def test_8_part_over_64_kib_rejected(db, clock, env):
    big = [{"id": i, "first": "x" * 100} for i in range(700)]
    assert len(json.dumps(big, ensure_ascii=False).encode()) > 64 * KIB
    result = _meta(db, clock, env["tg"], [{"type": "account_meta", "key": "tg_contacts:0", "value": big}])
    assert result.dead == 1
