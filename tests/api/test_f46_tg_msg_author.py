"""Ф46-1 (`docs/architecture.md`–3 и поправки П2–П6, П10), ядро: личка автору
сообщения группы Telegram — вид адресата `tg_msg_author`. Клиент называет только id сообщения хаба, человека
и чат выводит ядро; отказы; видимость и проект; одна заготовка на человека; существующая личка; предел
bearer; отсечка объектных видов; повтор по ключу. Имена и id — выдуманные."""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import new_recipient, outbox, security

from .conftest import BASE_URL, csrf_headers, insert_channel, insert_message, insert_project, login_via_code

OWNER = "connector:1"
AUTHOR = 5550001
GROUP_EXT = "-1000000004242"
NAME = "Пенопласт"
NOT_FOUND = "to: сообщение не найдено"


def _now(clock) -> int:
    return int(clock() * 1000)


def _account(db, *, source="telegram", config=None) -> int:
    base = {"idempotent_send": True, "pace_window": "00:00-24:00"}
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, config_json) VALUES (?, 'main', ?)",
                (source, json.dumps({**base, **(config or {})})),
            ).lastrowid
        )
    )


def _token(db, *, scopes=("read", "draft", "attach"), name="mcp:all", project_ids=None) -> str:
    created = db.write_sync(
        lambda conn: security.create_api_token(
            conn, name=name, kind="interactive", scopes=list(scopes), project_ids=project_ids, expires_ts=None
        )
    )
    return created.token


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _contact(db, account_id, ext_id, *, name=NAME, is_me=0) -> int:
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.contact (account_id, ext_id, name, is_me) VALUES (?, ?, ?, ?)",
                (account_id, str(ext_id), name, is_me),
            ).lastrowid
        )
    )


def _sql(db, query, params=()):
    return db.write_sync(lambda conn: conn.execute(query, params).fetchall())


def _msg(db, clock, channel_id, *, ext_id="812", sender=None, **kw) -> int:
    return insert_message(db, channel_id, ext_id=ext_id, ts=_now(clock), sender_contact_id=sender, **kw)


@pytest.fixture
async def bearer(app):
    """Отдельный клиент без cookie (у `client` после входа кука — bearer с ней был бы 400)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as c:
        yield c


@pytest.fixture
async def env(client, db, clock):
    project = insert_project(db, name="Umbrella")
    other_project = insert_project(db, name="Initech")
    tg = _account(db)
    group = insert_channel(db, tg, project_id=project, ext_id=GROUP_EXT, kind="group", title="Продавцы")
    author = _contact(db, tg, AUTHOR)
    message = _msg(db, clock, group, sender=author)
    token = _token(db)
    cookie, _csrf = await login_via_code(client, db, clock)
    return {
        "project": project,
        "other_project": other_project,
        "tg": tg,
        "group": group,
        "author": author,
        "message": message,
        "token": token,
        "cookie": cookie,
    }


def _to(message_id) -> dict:
    return {"kind": "tg_msg_author", "message_id": message_id}


async def _draft(bearer, env, message_id=None, *, to=None, text="Здравствуйте!", token=None, **kw):
    body = {
        "account_id": env["tg"],
        "project_id": env["project"],
        "to": to if to is not None else _to(message_id or env["message"]),
        "text": text,
        **kw,
    }
    return await bearer.post("/api/v1/drafts/new", json=body, headers=_auth(token or env["token"]))


async def _ok(bearer, env, message_id=None, **kw) -> dict:
    r = await _draft(bearer, env, message_id, **kw)
    assert r.status_code == 200, r.text
    return r.json()


async def _ui_new(client, env, message_id=None, *, to=None, project="project", account=None):
    body = {
        "account_id": account or env["tg"],
        "project_id": env[project] if project else None,
        "to": to if to is not None else _to(message_id or env["message"]),
    }
    return await client.post("/api/v1/channels/new", json=body, headers=csrf_headers(env["cookie"]))


def _channel(db, channel_id) -> dict:
    with db.reader() as conn:
        row = conn.execute(
            "SELECT meta_json, title, kind, project_id, ext_id, archived FROM main.channel WHERE id = ?",
            (channel_id,),
        ).fetchone()
    return {
        "meta": json.loads(row[0] or "{}"),
        "title": row[1],
        "kind": row[2],
        "project": row[3],
        "ext_id": row[4],
        "archived": row[5],
    }


def _stubs(db) -> int:
    with db.reader() as conn:
        return conn.execute("SELECT COUNT(*) FROM main.channel WHERE ext_id LIKE 'new:%'").fetchone()[0]


def _approve_and_lease(db, clock, outbox_id, account_id):
    slot = db.write_sync(lambda conn: outbox.approve(conn, outbox_id, now_ms=_now(clock)))
    if slot is not None and slot > _now(clock):
        clock.advance_ms(slot - _now(clock))
    leases = db.write_sync(
        lambda conn: outbox.lease(conn, account_id=account_id, owner=OWNER, now_ms=_now(clock))
    )
    return next(item for item in leases if item.outbox_id == outbox_id)


def _report(db, clock, lease, outcome="sent", **kw):
    db.write_sync(
        lambda conn: outbox.report(
            conn, outbox_id=lease.outbox_id, owner=OWNER, lease_gen=lease.lease_gen, outcome=outcome,
            now_ms=_now(clock), **kw,
        )
    )  # fmt: skip


# --- создание -----------------------------------------------------------------------------------------------


async def test_bearer_creates_stub_with_derived_recipient(client, bearer, db, clock, env):
    created = await _ok(bearer, env)
    assert created["status"] == "pending_approval"
    ch = _channel(db, created["channel_id"])
    assert (ch["title"], ch["kind"], ch["project"]) == (NAME, "dm", env["project"])
    assert ch["ext_id"].startswith("new:")
    new_to = {
        "kind": "tg_msg_author",
        "user_id": AUTHOR,
        "name": NAME,
        "chat_ext_id": GROUP_EXT,
        "msg_ext_id": "812",
    }
    assert ch["meta"]["attrs"]["new_to"] == new_to
    draft = ch["meta"]["new_draft"]
    assert draft["to"] == new_to
    assert (draft["from_title"], draft["from_channel_id"], draft["from_message_id"]) == (
        "Продавцы",
        env["group"],
        env["message"],
    )
    # Наружу — без адреса чата и номера сообщения у источника.
    r = await client.get(f"/api/v1/channels/{created['channel_id']}")
    assert r.status_code == 200, r.text
    assert r.json()["new_to"] == {
        "kind": "tg_msg_author",
        "user_id": AUTHOR,
        "name": NAME,
        "from_title": "Продавцы",
        "from_channel_id": env["group"],
        "from_message_id": env["message"],
    }
    # Коннектору — всё, что нужно для доступа через сообщение.
    lease = _approve_and_lease(db, clock, created["outbox_id"], env["tg"])
    assert lease.new_to == new_to
    with db.reader() as conn:
        detail = conn.execute(
            "SELECT detail_json FROM main.audit WHERE action = 'draft_new' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert json.loads(detail)["to_kind"] == "tg_msg_author"
    assert NAME not in detail and str(AUTHOR) not in detail


async def test_ui_creates_stub_and_reuses_it(client, db, env):
    r = await _ui_new(client, env)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True
    assert body["channel"]["new_to"]["kind"] == "tg_msg_author"
    assert body["channel"]["new_to"]["from_message_id"] == env["message"]
    assert _channel(db, body["channel"]["id"])["meta"]["new_draft"]["origin"] == "ui"
    # Тот же человек из другого проекта и «по правилам» — та же заготовка (П6: без учёта проекта).
    for project in ("other_project", None):
        again = await _ui_new(client, env, project=project)
        assert again.status_code == 200, again.text
        assert (again.json()["created"], again.json()["channel"]["id"]) == (False, body["channel"]["id"])
    assert _stubs(db) == 1


async def test_ui_source_in_unsorted_goes_by_rules(client, db, clock, env):
    loose = insert_channel(db, env["tg"], project_id=None, ext_id="-1000000009999", kind="group", title="Чат")
    message = _msg(db, clock, loose, ext_id="5", sender=env["author"])
    r = await _ui_new(client, env, message, project=None)
    assert r.status_code == 200, r.text
    assert _channel(db, r.json()["channel"]["id"])["project"] is None


async def test_topic_message_gives_parent_chat(bearer, db, clock, env):
    forum = insert_channel(db, env["tg"], project_id=env["project"], ext_id="-1000000007777", kind="group",
                           title="Форум")  # fmt: skip
    topic = insert_channel(db, env["tg"], project_id=env["project"], ext_id="-1000000007777:15", kind="topic",
                           title="Тема", parent_channel_id=forum)  # fmt: skip
    message = _msg(db, clock, topic, ext_id="901", sender=env["author"])
    created = await _ok(bearer, env, message)
    meta = _channel(db, created["channel_id"])["meta"]
    assert meta["attrs"]["new_to"]["chat_ext_id"] == "-1000000007777"
    assert meta["attrs"]["new_to"]["msg_ext_id"] == "901"
    assert (meta["new_draft"]["from_title"], meta["new_draft"]["from_channel_id"]) == ("Форум", topic)


async def test_basic_group_accepted_by_core(bearer, db, clock, env):
    """П8: обычная группа (без префикса -100) — ядро заготовку принимает, решает коннектор."""
    basic = insert_channel(db, env["tg"], project_id=env["project"], ext_id="-300", kind="group", title="Мал")
    message = _msg(db, clock, basic, ext_id="7", sender=env["author"])
    created = await _ok(bearer, env, message)
    assert _channel(db, created["channel_id"])["meta"]["attrs"]["new_to"]["chat_ext_id"] == "-300"


# --- отказы раздела 1 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spoil", "detail"),
    [
        ("UPDATE main.message SET deleted = 1 WHERE id = :m", "to: сообщение удалено"),
        ("UPDATE main.message SET purged = 1 WHERE id = :m", "to: сообщение удалено"),
        ("UPDATE main.message SET hidden = 1 WHERE id = :m", NOT_FOUND),
        (
            "UPDATE main.message SET kind = 'event' WHERE id = :m",
            "to: служебное сообщение — у него нет автора, которому можно написать",
        ),
        (
            "UPDATE main.channel SET kind = 'dm' WHERE id = :c",
            "to: сообщение не из группы Telegram (личка или канал)",
        ),
        (
            "UPDATE main.channel SET kind = 'channel' WHERE id = :c",
            "to: сообщение не из группы Telegram (личка или канал)",
        ),
        (
            "UPDATE main.message SET ext_id = NULL WHERE id = :m",
            "to: у сообщения нет номера в Telegram (ещё не отправлено)",
        ),
        ("UPDATE main.message SET is_me = 1 WHERE id = :m", "to: это твоё сообщение"),
        ("UPDATE main.contact SET is_me = 1 WHERE id = :a", "to: это твоё сообщение"),
        (
            "UPDATE main.message SET sender_contact_id = NULL WHERE id = :m",
            "to: у сообщения нет автора-человека (анонимный админ или канал)",
        ),
        (
            "UPDATE main.contact SET ext_id = '-1000000004242' WHERE id = :a",
            "to: у сообщения нет автора-человека (анонимный админ или канал)",
        ),
        ("UPDATE main.message SET is_bot = 1 WHERE id = :m", "to: автор — бот"),
        ("UPDATE main.channel SET project_id = NULL WHERE id = :c", "to: сообщение из другого проекта"),
    ],
)
async def test_refusals(bearer, db, env, spoil, detail):
    sql = spoil.replace(":m", str(env["message"])).replace(":c", str(env["group"]))
    _sql(db, sql.replace(":a", str(env["author"])))
    r = await _draft(bearer, env)
    assert (r.status_code, r.json()["detail"]) == (422, detail), r.text
    assert NAME not in r.text and str(AUTHOR) not in r.text
    assert _stubs(db) == 0


@pytest.mark.parametrize(
    ("to", "detail"),
    [
        ({"kind": "tg_msg_author", "message_id": 1, "user_id": 5}, "объектом — только автор сообщения"),
        ({"kind": "tg_msg_author"}, "объектом — только автор сообщения"),
        ({"kind": "tg_msg_author", "message_id": "12"}, "to.message_id"),
        ({"kind": "tg_msg_author", "message_id": 0}, "to.message_id"),
        ({"kind": "tg_msg_author", "message_id": True}, "to.message_id"),
        ({"kind": "tg_msg_author", "message_id": 999_999}, NOT_FOUND),
        # П5: прочие объектные виды остаются путём UI.
        ({"kind": "tg_user", "user_id": AUTHOR, "name": "x", "username": None}, "объектом — только автор"),
        ({"kind": "slack_users", "users": ["U12345"]}, "объектом — только автор"),
        ({"kind": "mm_users", "users": ["a" * 26]}, "объектом — только автор"),
        ({"kind": "tg_user", "message_id": 1}, "объектом — только автор"),
    ],
)
async def test_bad_object_from_bearer(bearer, db, env, to, detail):
    for extra in ({}, {"hold": True, "text": None}):
        r = await _draft(bearer, env, to=to, **extra)
        assert r.status_code == 422 and detail in r.json()["detail"], r.text
    assert _stubs(db) == 0


async def test_extra_keys_refused_for_ui(client, db, env):
    r = await _ui_new(client, env, to={**_to(env["message"]), "user_id": 5})
    assert (r.status_code, r.json()["detail"]) == (422, "to: у автора сообщения — ровно kind и message_id")
    assert _stubs(db) == 0


async def test_subject_and_from_refused(bearer, env):
    for extra in ({"subject": "Тема"}, {"from": "a@b.ru"}):
        r = await _draft(bearer, env, **extra)
        assert r.status_code == 422 and "только для почты" in r.json()["detail"], r.text


async def test_wrong_account(bearer, db, env):
    second = _account(db)
    r = await _draft(bearer, {**env, "tg": second})
    assert r.status_code == 422
    assert r.json()["detail"] == f"to: сообщение из аккаунта {env['tg']} — передай account_id={env['tg']}"
    slack = _account(db, source="slack")
    r = await _draft(bearer, {**env, "tg": slack})
    assert r.status_code == 422 and _stubs(db) == 0


async def test_source_must_be_telegram(bearer, db, clock, env):
    slack = _account(db, source="slack")
    channel = insert_channel(db, slack, project_id=env["project"], ext_id="C1", kind="group", title="dev")
    author = _contact(db, slack, "77")
    message = _msg(db, clock, channel, ext_id="5", sender=author)
    r = await _draft(bearer, {**env, "tg": slack}, message)
    assert r.status_code == 422, r.text


# --- видимость и проект (П3) --------------------------------------------------------------------------------


async def test_foreign_project_is_not_found_for_scoped_token(bearer, db, env):
    scoped = _token(db, name="mcp:initech", project_ids=[env["other_project"]])
    r = await _draft(bearer, {**env, "project": env["other_project"]}, token=scoped)
    assert (r.status_code, r.json()["detail"]) == (422, NOT_FOUND)
    r = await _draft(bearer, {**env, "project": env["other_project"]}, token=scoped, hold=True, text=None)
    assert (r.status_code, r.json()["detail"]) == (422, NOT_FOUND)
    assert _stubs(db) == 0


async def test_token_all_and_source_from_another_project(bearer, db, env):
    """Токен `all` проекты не делит: сообщение видно, но проект источника ≠ `project_id` запроса — отказ."""
    for extra in ({}, {"hold": True, "text": None}):
        r = await _draft(bearer, {**env, "project": env["other_project"]}, **extra)
        assert (r.status_code, r.json()["detail"]) == (422, "to: сообщение из другого проекта"), r.text
    assert _stubs(db) == 0


async def test_message_of_other_schema_is_not_found(bearer, db, env):
    r = await _draft(bearer, env, 2**40 + 5)
    assert (r.status_code, r.json()["detail"]) == (422, NOT_FOUND)


# --- одна заготовка на человека (П6) ------------------------------------------------------------------------


async def test_one_stub_per_person_together_with_tg_user(client, bearer, db, env):
    ui = await _ui_new(client, env, to={"kind": "tg_user", "user_id": AUTHOR, "name": NAME, "username": None})
    assert ui.status_code == 200, ui.text
    stub = ui.json()["channel"]["id"]
    same = await _ui_new(client, env)
    assert (same.json()["created"], same.json()["channel"]["id"]) == (False, stub)
    r = await _draft(bearer, env)
    assert r.status_code == 409 and f"#{stub}" in r.json()["detail"], r.text
    assert _stubs(db) == 1


async def test_two_authors_two_stubs(bearer, db, clock, env):
    """П6: у нового вида свой ключ адресата — разные авторы не склеиваются в одну заготовку."""
    second = _contact(db, env["tg"], AUTHOR + 1, name="Другой")
    other_msg = _msg(db, clock, env["group"], ext_id="813", sender=second)
    first = await _ok(bearer, env)
    another = await _ok(bearer, env, other_msg)
    assert first["channel_id"] != another["channel_id"] and _stubs(db) == 2
    again = await _draft(bearer, env)
    assert again.status_code == 409 and f"#{first['outbox_id']}" in again.json()["detail"]


async def test_one_author_in_two_projects_one_stub(bearer, db, clock, env):
    """П6: открытая заготовка автору ищется БЕЗ учёта проекта; чужой проект токену не виден — без номеров."""
    await _ok(bearer, env)
    twin = insert_channel(db, env["tg"], project_id=env["other_project"], ext_id="-1000000005555",
                          kind="group", title="Вторая")  # fmt: skip
    message = _msg(db, clock, twin, ext_id="3", sender=env["author"])
    there = {**env, "project": env["other_project"]}
    r = await _draft(bearer, there, message)
    assert r.status_code == 409 and "уже есть черновик" in r.json()["detail"], r.text
    scoped = _token(db, name="mcp:initech", project_ids=[env["other_project"]])
    r = await _draft(bearer, there, message, token=scoped)
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "этому адресату уже готовится сообщение в другом проекте"
    assert _stubs(db) == 1


# --- существующая личка (П2) --------------------------------------------------------------------------------


async def test_existing_dm_ui_gets_it_bearer_gets_409(client, bearer, db, env):
    dm = insert_channel(db, env["tg"], project_id=env["project"], ext_id=str(AUTHOR), kind="dm", title=NAME)
    r = await _ui_new(client, env)
    assert r.status_code == 200, r.text
    assert (r.json()["created"], r.json()["channel"]["id"]) == (False, dm)
    want = f"с этим человеком уже есть беседа #{dm} — пиши `hub_draft(channel_id={dm}, …)`"
    for extra in ({}, {"hold": True, "text": None}):
        r = await _draft(bearer, env, **extra)
        assert (r.status_code, r.json()["detail"]) == (409, want), r.text
    assert _stubs(db) == 0
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.outbox").fetchone()[0] == 0


@pytest.mark.parametrize("dm_project", ["other_project", None])
async def test_existing_dm_invisible_to_token_has_no_number(bearer, db, env, dm_project):
    dm = insert_channel(
        db,
        env["tg"],
        project_id=env[dm_project] if dm_project else None,
        ext_id=str(AUTHOR),
        kind="dm",
        title=NAME,
    )
    scoped = _token(db, name="mcp:ii", project_ids=[env["project"]])
    for extra in ({}, {"hold": True, "text": None}):
        r = await _draft(bearer, env, token=scoped, **extra)
        assert r.status_code == 409, r.text
        assert r.json()["detail"] == "с этим человеком уже есть беседа в другом проекте — скажи владельцу"
        assert str(dm) not in r.text
    assert _stubs(db) == 0


# --- предел (раздел 3) --------------------------------------------------------------------------------------


def _authors(db, clock, env, n, *, start=1) -> list[int]:
    out = []
    for i in range(start, start + n):
        contact = _contact(db, env["tg"], AUTHOR + i, name=f"Автор {i}")
        out.append(_msg(db, clock, env["group"], ext_id=str(1000 + i), sender=contact))
    return out


async def test_daily_limit_for_bearer_not_for_ui(client, bearer, db, clock, env):
    messages = _authors(db, clock, env, 12)
    for message in messages[:10]:
        await _ok(bearer, env, message)
    r = await _draft(bearer, env, messages[10])
    assert r.status_code == 429, r.text
    assert r.json()["detail"] == (
        "предел первых сообщений авторам из групп на сегодня (10) — остальное завтра или попроси владельца "
        "поднять"
    )
    r = await _draft(bearer, env, messages[10], hold=True, text=None)
    assert r.status_code == 429
    # Повтор уже открытой заготовки — 409, а не 429 (предел — после поиска открытой).
    assert (await _draft(bearer, env, messages[0])).status_code == 409
    # Клик владельца не ограничен и в счёт не идёт; `@username` предел не задевает.
    assert (await _ui_new(client, env, messages[10])).json()["created"] is True
    assert (await _draft(bearer, env, to="@someone_else")).status_code == 200
    # Через сутки счётчик свободен.
    clock.advance_ms(new_recipient.TG_PHONE_WINDOW_MS + 1000)
    assert (await _draft(bearer, env, messages[11])).status_code == 200


@pytest.mark.parametrize(
    ("value", "limit"),
    [(2, 2), (0, 0), (99, 30), (-1, 10), ("7", 10), (True, 10), (None, 10)],
)
def test_tg_author_daily_setting(value, limit):
    config = json.dumps({"tg_author_daily": value}) if value is not None else None
    assert new_recipient.tg_author_daily(config) == limit


async def test_account_setting_changes_limit(bearer, db, clock, env):
    _sql(
        db,
        "UPDATE main.account SET config_json = json_set(config_json, '$.tg_author_daily', 1) WHERE id = ?",
        (env["tg"],),
    )
    messages = _authors(db, clock, env, 2)
    await _ok(bearer, env, messages[0])
    r = await _draft(bearer, env, messages[1])
    assert r.status_code == 429 and "(1)" in r.json()["detail"]


# --- повтор по ключу (П10) ----------------------------------------------------------------------------------


async def test_idempotent_repeat_before_and_after_send(bearer, db, clock, env):
    first = await _ok(bearer, env, idempotency_key="key-1")
    again = await _ok(bearer, env, idempotency_key="key-1")
    assert (again["outbox_id"], again["channel_id"]) == (first["outbox_id"], first["channel_id"])
    lease = _approve_and_lease(db, clock, first["outbox_id"], env["tg"])
    _report(db, clock, lease, ext_id="9001", peer_ext_id=str(AUTHOR), peer_title="Пено Пласт")
    assert _channel(db, first["channel_id"])["ext_id"] == str(AUTHOR)
    # Заготовка стала настоящей личкой: повтор отдаёт прежнюю строку, а не 409 «уже есть беседа».
    after = await _ok(bearer, env, idempotency_key="key-1")
    assert (after["outbox_id"], after["channel_id"], after["status"]) == (
        first["outbox_id"],
        first["channel_id"],
        "sent",
    )
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.outbox").fetchone()[0] == 1
    assert (await _draft(bearer, env, text="другой", idempotency_key="key-1")).status_code == 409
    # Без ключа — уже обычный отказ П2.
    r = await _draft(bearer, env)
    assert r.status_code == 409 and f"#{first['channel_id']}" in r.json()["detail"]
