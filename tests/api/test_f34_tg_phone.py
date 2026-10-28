"""Ф34-3 (`docs/architecture.md` и «Тесты сверх раздела B» поправок П1–П7), ядро:
первое сообщение в Telegram по номеру телефона — разбор номера, вид адресата `tg_phone`, маска в названии,
уникальность по (аккаунт, номер), лимит заготовок от bearer, номер только UI-сессии, стирание номера из
`meta_json` на всех путях, срок жизни неподтверждённого черновика. Номера в тестах — выдуманные."""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import draft_channels, ingest, new_recipient, outbox, runner, security

from .conftest import BASE_URL, csrf_headers, insert_project, login_via_code

OWNER = "connector:1"
PHONE = "+79000000001"
DIGITS = "9000000001"  # без кода страны — ловит номер в любом написании
MASK = "+7 ••• ••• 00 01"
HOUR_MS = 60 * 60 * 1000
DAY_MS = 24 * HOUR_MS


def _now(clock) -> int:
    return int(clock() * 1000)


def _account(db, *, config=None) -> int:
    base = {"idempotent_send": True, "pace_window": "00:00-24:00"}
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, config_json) VALUES ('telegram', 'main', ?)",
                (json.dumps({**base, **(config or {})}),),
            ).lastrowid
        )
    )


def _token(db, *, scopes=("read", "draft", "attach"), name="mcp:all", project_ids=None):
    created = db.write_sync(
        lambda conn: security.create_api_token(
            conn, name=name, kind="interactive", scopes=list(scopes), project_ids=project_ids, expires_ts=None
        )
    )
    return created.token


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


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
    token = _token(db)
    cookie, _csrf = await login_via_code(client, db, clock)
    return {"project": project, "other_project": other_project, "tg": tg, "token": token, "cookie": cookie}


async def _draft(bearer, env, to=PHONE, text="Здравствуйте! Пишу по поводу подписи.", **kw):
    body = {"account_id": env["tg"], "project_id": env["project"], "to": to, "text": text, **kw}
    return await bearer.post("/api/v1/drafts/new", json=body, headers=_auth(env["token"]))


async def _ok_draft(bearer, env, to=PHONE, **kw) -> dict:
    r = await _draft(bearer, env, to=to, **kw)
    assert r.status_code == 200, r.text
    return r.json()


async def _ui_new(client, env, to=PHONE, project="project"):
    return await client.post(
        "/api/v1/channels/new",
        json={"account_id": env["tg"], "project_id": env[project] if project else None, "to": to},
        headers=csrf_headers(env["cookie"]),
    )


def _meta(db, channel_id) -> dict:
    with db.reader() as conn:
        row = conn.execute(
            "SELECT meta_json, title, archived, ext_id FROM main.channel WHERE id = ?", (channel_id,)
        ).fetchone()
    return {"meta": json.loads(row[0] or "{}"), "title": row[1], "archived": row[2], "ext_id": row[3]}


def _where_number(db) -> list[str]:
    """Все места БД (обе схемы, все таблицы и столбцы), где встречается номер, — `схема.таблица.столбец`."""
    found = []
    with db.reader() as conn:
        for schema in ("main", "work"):
            tables = [
                r[0]
                for r in conn.execute(
                    f"SELECT name FROM {schema}.sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%' AND sql NOT LIKE 'CREATE VIRTUAL%' "
                    "AND name NOT LIKE '%_fts%'"
                ).fetchall()
            ]
            for table in tables:
                cur = conn.execute(f'SELECT * FROM {schema}."{table}"')
                cols = [c[0] for c in cur.description]
                for row in cur.fetchall():
                    for col, value in zip(cols, row, strict=True):
                        if isinstance(value, bytes):
                            value = value.decode("utf-8", "replace")
                        if isinstance(value, str) and DIGITS in value:
                            found.append(f"{schema}.{table}.{col}")
    return sorted(set(found))


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


def _dm_event(ext_id, chat, *, ts, text="hi"):
    return {
        "type": "upsert",
        "channel": {"ext_id": chat, "kind": "dm", "title": "Пётр"},
        "ext_id": ext_id,
        "ts": ts,
        "is_me": True,
        "kind": "text",
        "text": text,
        "mentions_me": False,
    }


# --- B1: разбор номера -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "to",
    [
        "+79000000001",
        "+7 900 000-00-01",
        "+7 (900) 000 00 01",
        "8 (900) 000-00-01",
        "89000000001",
        " +7 900 000 00 01 ",
        "+7‑900‑000‑00‑01",
    ],
)
def test_parse_phone_forms(to):
    assert new_recipient.looks_like_phone(to)
    assert new_recipient.parse_tg_phone(to) == PHONE


def test_parse_phone_other_country_and_mask():
    assert new_recipient.parse_tg_phone("+44 20 7946 0958") == "+442079460958"
    assert new_recipient.mask_phone(PHONE) == MASK
    # ревью Ф34-3: у номера короче 10 цифр — код страны и две последние
    assert new_recipient.mask_phone("+12345678") == "+1 ••• ••• 78"
    assert new_recipient.mask_phone("+123456789") == "+1 ••• ••• 89"
    assert new_recipient.mask_phone("+1234567890") == "+1 ••• ••• 78 90"
    assert not new_recipient.looks_like_phone("@Petr_Ivanov")
    assert not new_recipient.looks_like_phone("t.me/petr_ivanov")


@pytest.mark.parametrize(
    "to",
    ["9000000001", "79000000001", "+7900", "+7 900 000 00 01 доб. 5", "+7900000000100000", "8900000000", "+"],
)
async def test_bad_phone_422_without_number(bearer, db, env, to):
    r = await _draft(bearer, env, to=to)
    assert r.status_code == 422
    assert "международном формате" in r.json()["detail"]
    digits = "".join(ch for ch in to if ch.isdigit())
    assert not digits or digits not in r.text  # текст ошибки — без введённого номера
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.channel").fetchone()[0] == 0


# --- решение 1 и 4, П2: заготовка, маска, ответ и аудит без номера ------------------------------------------


async def test_draft_by_phone_mask_and_no_number_outside_meta(bearer, db, env):
    r = await _draft(bearer, env, to="8 (900) 000-00-01")
    assert r.status_code == 200, r.text
    body = r.json()
    assert DIGITS not in r.text  # П2: номера в ответе `hub_draft_new` нет
    ch = _meta(db, body["channel_id"])
    assert ch["title"] == MASK and draft_channels.is_temp_ext_id(ch["ext_id"])
    assert ch["meta"]["attrs"]["new_to"] == {"kind": "tg_phone", "phone": PHONE}
    assert ch["meta"]["new_draft"]["to"] == {"kind": "tg_phone", "phone": PHONE}
    # лиз отдаёт адресат коннектору (`tg_phone` — в `NEW_TO_KINDS`)
    with db.reader() as conn:
        assert draft_channels.lease_new_to(conn, "main", body["channel_id"]) == {
            "kind": "tg_phone",
            "phone": PHONE,
        }
        audit = conn.execute("SELECT detail_json FROM main.audit WHERE action = 'draft_new'").fetchone()
    assert json.loads(audit[0])["to_kind"] == "tg_phone"
    # номер — только в `meta_json` заготовки: ни в аудите, ни в названии, ни в `contact`, ни в outbox
    assert _where_number(db) == ["main.channel.meta_json"]


# --- П6: уникальность по (аккаунт, номер) без проекта -------------------------------------------------------


async def test_same_number_other_writing_and_other_project_409(bearer, client, db, env):
    body = await _ok_draft(bearer, env)
    again = await _draft(bearer, env, to="+7 (900) 000-00-01", text="другой текст")
    assert again.status_code == 409 and DIGITS not in again.text
    assert f"#{body['outbox_id']}" in again.json()["detail"]
    other = await _draft(bearer, env, project_id=env["other_project"])
    assert other.status_code == 409
    # Владелец из формы тем же номером (любой проект, «по правилам») получает ту же заготовку
    for project in ("project", "other_project", None):
        r = await _ui_new(client, env, to="8 900 000 00 01", project=project)
        assert r.status_code == 200, r.text
        assert (r.json()["created"], r.json()["channel"]["id"]) == (False, body["channel_id"])
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.channel").fetchone()[0] == 1
    # другой номер — другая заготовка
    assert (await _ok_draft(bearer, env, to="+79000000002"))["channel_id"] != body["channel_id"]


# --- B2 + П3б: лимит заготовок от bearer --------------------------------------------------------------------


async def test_limit_five_per_day_cancelled_count_ui_free_second_account_own(bearer, client, db, clock, env):
    first = await _ok_draft(bearer, env, to="+79000000010")
    r = await bearer.post(f"/api/v1/outbox/{first['outbox_id']}/cancel", headers=_auth(env["token"]))
    assert r.status_code == 200 and _meta(db, first["channel_id"])["archived"] == 1
    for i in range(1, 5):
        clock.advance_ms(HOUR_MS)
        await _ok_draft(bearer, env, to=f"+7900000001{i}")
    sixth = await _draft(bearer, env, to="+79000000015")
    assert sixth.status_code == 429
    detail = sixth.json()["detail"]
    assert "5 в сутки" in detail and "через 20 ч" in detail and "900" not in detail
    # повтор уже открытому номеру — 409 (до лимита), а не 429
    assert (await _draft(bearer, env, to="+79000000011")).status_code == 409
    # `hold` (путь с файлами) — тот же лимит
    held = await _draft(bearer, env, to="+79000000015", hold=True, text=None)
    assert held.status_code == 429
    # Владелец из формы лимитом ядра не ограничен, и его заготовки в счёт не идут
    assert (await _ui_new(client, env, to="+79000000015")).json()["created"] is True
    # второй аккаунт — свой счёт
    second = _account(db)
    r = await _draft(bearer, {**env, "tg": second}, to="+79000000015")
    assert r.status_code == 200, r.text
    # окно скользящее: через сутки после первой — снова можно (счёт — из строк БД, рестарт его не трогает)
    clock.advance_ms(20 * HOUR_MS + 1000)
    assert (await _draft(bearer, env, to="+79000000016")).status_code == 200
    assert (await _draft(bearer, env, to="+79000000017")).status_code == 429


async def test_limit_from_account_setting(bearer, client, db, env):
    for value, expected in ((2, 2), (0, 0), (50, 20), ("3", 5), (True, 5), (None, 5), (-1, 5)):
        assert new_recipient.tg_phone_daily(json.dumps({"tg_phone_daily": value})) == expected
    assert new_recipient.tg_phone_daily(None) == 5 and new_recipient.tg_phone_daily("{") == 5
    small = _account(db, config={"tg_phone_daily": 1})
    assert (await _draft(bearer, {**env, "tg": small}, to="+79000000021")).status_code == 200
    r = await _draft(bearer, {**env, "tg": small}, to="+79000000022")
    assert r.status_code == 429 and "1 в сутки" in r.json()["detail"]
    off = _account(db, config={"tg_phone_daily": 0})
    # 0 — номер не принимается вовсе: ни от сессии (и с `hold`), ни из формы владельца
    r = await _draft(bearer, {**env, "tg": off}, to="+79000000023")
    assert r.status_code == 422 and "выключено" in r.json()["detail"]
    r = await _draft(bearer, {**env, "tg": off}, to="+79000000023", hold=True, text=None)
    assert r.status_code == 422
    r = await _ui_new(client, {**env, "tg": off}, to="+79000000023")
    assert r.status_code == 422 and "выключено" in r.json()["detail"]
    assert (
        await _draft(bearer, {**env, "tg": off}, to="@Petr_Ivanov")
    ).status_code == 200  # @username — можно


async def test_limit_counts_bound_drafts(bearer, db, clock, env):
    """Вид адресата переживает стирание номера: привязанная (уже настоящая) беседа остаётся в счёте."""
    small = _account(db, config={"tg_phone_daily": 1})
    body = await _ok_draft(bearer, {**env, "tg": small})
    lease = _approve_and_lease(db, clock, body["outbox_id"], small)
    _report(db, clock, lease, ext_id="9001", peer_ext_id="555", peer_title="Пётр Иванов")
    assert _meta(db, body["channel_id"])["ext_id"] == "555"
    assert (await _draft(bearer, {**env, "tg": small}, to="+79000000031")).status_code == 429


async def test_status_gives_limit_to_ui_only(bearer, client, db, env):
    small = _account(db, config={"tg_phone_daily": 7})
    accounts = {a["id"]: a for a in (await client.get("/api/v1/status")).json()["accounts"]}
    assert accounts[env["tg"]]["tg_phone_daily"] == 5 and accounts[small]["tg_phone_daily"] == 7
    got = (await bearer.get("/api/v1/status", headers=_auth(env["token"]))).json()["accounts"]
    assert got and all("tg_phone_daily" not in a for a in got)


# --- П2: сырой API — номер только UI-сессии -----------------------------------------------------------------


async def test_raw_api_number_only_for_ui(bearer, client, db, env):
    body = await _ok_draft(bearer, env)
    cid = body["channel_id"]
    paths = [
        "/api/v1/sync?since=0",
        f"/api/v1/projects/{env['project']}/channels",
        f"/api/v1/channels/{cid}",
        "/api/v1/outbox?status=pending_approval",
        f"/api/v1/outbox/{body['outbox_id']}",
        "/api/v1/home/react",
    ]
    for path in paths:
        r = await bearer.get(path, headers=_auth(env["token"]))
        if r.status_code == 404:
            continue
        assert r.status_code == 200, (path, r.text)
        assert DIGITS not in r.text, path
    as_bearer = (await bearer.get(f"/api/v1/channels/{cid}", headers=_auth(env["token"]))).json()
    assert as_bearer["new_to"] == {"kind": "tg_phone"} and as_bearer["title"] == MASK
    drafts = (await bearer.get(paths[3], headers=_auth(env["token"]))).json()
    assert [d["new_to"] for d in drafts] == [{"kind": "tg_phone"}]

    # Владелец (UI) видит полный номер: в беседе, списке, `/sync` и на карточке черновика
    full = {"kind": "tg_phone", "phone": PHONE}
    assert (await client.get(f"/api/v1/channels/{cid}")).json()["new_to"] == full
    listed = (await client.get(f"/api/v1/projects/{env['project']}/channels")).json()
    assert [c["new_to"] for c in listed if c["id"] == cid] == [full]
    synced = (await client.get("/api/v1/sync?since=0")).json()["channels"]
    assert [c["new_to"] for c in synced if c["id"] == cid] == [full]
    assert [d["new_to"] for d in (await client.get(paths[3])).json()] == [full]


# --- П1: номера нет нигде в `meta_json` после привязки и архива ---------------------------------------------


async def test_erased_after_rename(bearer, db, clock, env):
    body = await _ok_draft(bearer, env)
    lease = _approve_and_lease(db, clock, body["outbox_id"], env["tg"])
    assert lease.new_to == {"kind": "tg_phone", "phone": PHONE}
    _report(db, clock, lease, ext_id="9001", peer_ext_id="555", peer_title="Пётр Иванов")
    ch = _meta(db, body["channel_id"])
    assert (ch["ext_id"], ch["title"], ch["archived"]) == ("555", "Пётр Иванов", 0)
    assert "attrs" not in ch["meta"]
    assert ch["meta"]["new_draft"]["to"] == {"kind": "tg_phone"}
    assert ch["meta"]["new_draft"]["bound"] == "rename"
    assert _where_number(db) == []


async def test_erased_after_merge_echo_before_report(bearer, db, clock, env):
    body = await _ok_draft(bearer, env)
    lease = _approve_and_lease(db, clock, body["outbox_id"], env["tg"])
    clock.advance_ms(1000)
    db.write_sync(
        lambda conn: ingest.apply_batch(
            conn,
            account_id=env["tg"],
            events=[_dm_event("9001", "555", ts=_now(clock))],
            cursor=None,
            now_ms=_now(clock),
        )
    )
    _report(db, clock, lease, ext_id="9001", peer_ext_id="555", peer_title="Пётр")
    draft = _meta(db, body["channel_id"])
    assert draft["archived"] == 1 and draft["meta"]["merged_into"] == "555"
    assert draft["meta"]["new_draft"]["to"] == {"kind": "tg_phone"} and "attrs" not in draft["meta"]
    assert _where_number(db) == []


async def test_erased_after_failed(bearer, db, clock, env):
    body = await _ok_draft(bearer, env)
    lease = _approve_and_lease(db, clock, body["outbox_id"], env["tg"])
    _report(db, clock, lease, outcome="permanent", error="номер не найден в Telegram или скрыт настройками")
    ch = _meta(db, body["channel_id"])
    assert ch["archived"] == 1 and ch["title"] == MASK  # причина видна, название — маска
    assert _where_number(db) == []
    # архивная заготовка не мешает новому черновику тому же номеру
    assert (await _ok_draft(bearer, env))["channel_id"] != body["channel_id"]


async def test_erased_after_cancelled(bearer, db, env):
    body = await _ok_draft(bearer, env)
    r = await bearer.post(f"/api/v1/outbox/{body['outbox_id']}/cancel", headers=_auth(env["token"]))
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert _meta(db, body["channel_id"])["archived"] == 1
    assert _where_number(db) == []


async def test_erased_after_sent_unbound(bearer, db, clock, env):
    body = await _ok_draft(bearer, env)
    lease = _approve_and_lease(db, clock, body["outbox_id"], env["tg"])
    _report(db, clock, lease, outcome="unknown")
    assert _where_number(db) == ["main.channel.meta_json"]  # исход не ясен — заготовка ещё живая
    db.write_sync(
        lambda conn: outbox.resolve_unknown(conn, body["outbox_id"], decision="sent", now_ms=_now(clock))
    )
    assert _meta(db, body["channel_id"])["archived"] == 1
    assert _where_number(db) == []


async def test_erased_after_idle_and_hidden_ui_drafts(client, db, clock, env):
    idle = (await _ui_new(client, env)).json()["channel"]
    assert idle["title"] == MASK and idle["new_to"] == {"kind": "tg_phone", "phone": PHONE}
    assert _meta(db, idle["id"])["meta"]["new_draft"]["origin"] == "ui"
    hidden = (await _ui_new(client, env, to="+79000000002")).json()["channel"]
    r = await client.delete(f"/api/v1/channels/{hidden['id']}", headers=csrf_headers(env["cookie"]))
    assert r.status_code == 204, r.text
    assert _meta(db, hidden["id"])["archived"] == 1
    assert "9000000002" not in json.dumps(_meta(db, hidden["id"])["meta"])

    clock.advance_ms(DAY_MS + HOUR_MS)
    assert db.write_sync(lambda conn: draft_channels.cleanup_idle(conn, now_ms=_now(clock))) == 1
    ch = _meta(db, idle["id"])
    assert ch["archived"] == 1 and ch["meta"]["new_draft"]["to"] == {"kind": "tg_phone"}
    assert _where_number(db) == []


async def test_ui_draft_failed_keeps_number_until_idle(client, db, clock, env):
    """UI-заготовка при неудаче остаётся открытой (владелец пишет снова) — номер живёт до уборки `idle`."""
    channel = (await _ui_new(client, env)).json()["channel"]
    r = await client.post(
        "/api/v1/messages/send",
        json={"channel_id": channel["id"], "text": "Здравствуйте!", "idempotency_key": "k1"},
        headers=csrf_headers(env["cookie"]),
    )
    assert r.status_code == 200, r.text
    (lease,) = db.write_sync(
        lambda conn: outbox.lease(conn, account_id=env["tg"], owner=OWNER, now_ms=_now(clock))
    )
    _report(db, clock, lease, outcome="permanent", error="номер не найден в Telegram или скрыт настройками")
    assert _meta(db, channel["id"])["archived"] == 0
    assert _where_number(db) == ["main.channel.meta_json"]
    clock.advance_ms(DAY_MS + HOUR_MS)
    db.write_sync(lambda conn: runner._hourly_cleanup(conn, _now(clock)))  # noqa: SLF001
    assert _meta(db, channel["id"])["archived"] == 1 and _where_number(db) == []


# --- путь `hold` с файлом: лимит и стирание -----------------------------------------------------------------


async def test_hold_with_file_limit_and_erase(bearer, db, clock, env):
    small = _account(db, config={"tg_phone_daily": 1})
    env = {**env, "tg": small}
    r = await _draft(bearer, env, hold=True, text=None)
    assert r.status_code == 200, r.text
    assert DIGITS not in r.text
    channel_id = r.json()["channel_id"]
    assert _meta(db, channel_id)["meta"]["new_draft"] == {
        "to": {"kind": "tg_phone", "phone": PHONE},
        "origin": "api",
        "hold": True,
    }
    assert (await _draft(bearer, env, to="+79000000002", hold=True, text=None)).status_code == 429
    up = await bearer.post(
        f"/api/v1/uploads?channel_id={channel_id}",
        content=b"%PDF-1.7\n" + b"x" * 100,
        headers={**_auth(env["token"]), "X-Filename": "a.pdf"},
    )
    assert up.status_code == 200, up.text
    sent = await bearer.post(
        "/api/v1/messages/send",
        json={"channel_id": channel_id, "text": "Договор", "idempotency_key": "k1",
              "files": [up.json()["upload_id"]]},
        headers=_auth(env["token"]),
    )  # fmt: skip
    assert sent.status_code == 200, sent.text
    assert sent.json()["status"] == "pending_approval" and DIGITS not in sent.text
    lease = _approve_and_lease(db, clock, sent.json()["outbox_id"], small)
    assert lease.new_to == {"kind": "tg_phone", "phone": PHONE}
    _report(db, clock, lease, ext_id="9001", peer_ext_id="555", peer_title="Пётр Иванов")
    assert _meta(db, channel_id)["ext_id"] == "555"
    assert _where_number(db) == []


async def test_hold_without_message_erased_by_idle_cleanup(bearer, db, clock, env):
    channel_id = (await _draft(bearer, env, hold=True, text=None)).json()["channel_id"]
    clock.advance_ms(DAY_MS + HOUR_MS)
    assert db.write_sync(lambda conn: draft_channels.cleanup_idle(conn, now_ms=_now(clock))) == 1
    assert _meta(db, channel_id)["archived"] == 1 and _where_number(db) == []


# --- П1: повтор по ключу после стирания ---------------------------------------------------------------------


async def test_idempotent_repeat_after_erase(bearer, db, clock, env):
    first = await _ok_draft(bearer, env, idempotency_key="key-1")
    lease = _approve_and_lease(db, clock, first["outbox_id"], env["tg"])
    _report(db, clock, lease, ext_id="9001", peer_ext_id="555", peer_title="Пётр Иванов")
    assert _where_number(db) == []
    again = await _draft(bearer, env, to="8 900 000 00 01", idempotency_key="key-1")
    assert again.status_code == 200, again.text
    assert (again.json()["outbox_id"], again.json()["channel_id"]) == (
        first["outbox_id"],
        first["channel_id"],
    )
    assert again.json()["status"] == "sent"
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.outbox").fetchone()[0] == 1
    assert _where_number(db) == []
    # тот же ключ, другой текст или другой вид адресата — конфликт
    assert (await _draft(bearer, env, text="другой", idempotency_key="key-1")).status_code == 409
    assert (await _draft(bearer, env, to="@Petr_Ivanov", idempotency_key="key-1")).status_code == 409


# --- П5: срок жизни неподтверждённого черновика -------------------------------------------------------------


async def test_pending_phone_draft_cancelled_after_seven_days(bearer, db, clock, env):
    old = await _ok_draft(bearer, env)
    username = await _ok_draft(bearer, env, to="@Petr_Ivanov")
    clock.advance_ms(6 * DAY_MS)
    fresh = await _ok_draft(bearer, env, to="+79000000002")
    db.write_sync(lambda conn: runner._hourly_cleanup(conn, _now(clock)))  # noqa: SLF001
    assert _meta(db, old["channel_id"])["archived"] == 0  # 6 дней — ещё ждёт

    clock.advance_ms(DAY_MS + HOUR_MS)
    db.write_sync(lambda conn: runner._hourly_cleanup(conn, _now(clock)))  # noqa: SLF001
    with db.reader() as conn:
        statuses = dict(conn.execute("SELECT id, status FROM main.outbox").fetchall())
    assert statuses[old["outbox_id"]] == "cancelled"
    assert statuses[fresh["outbox_id"]] == "pending_approval"
    assert statuses[username["outbox_id"]] == "pending_approval"  # @username срок не трогает
    assert _meta(db, old["channel_id"])["archived"] == 1
    assert _meta(db, fresh["channel_id"])["archived"] == 0
    assert _where_number(db) == []


async def test_approved_phone_draft_not_cancelled_by_ttl(bearer, db, clock, env):
    body = await _ok_draft(bearer, env)
    db.write_sync(lambda conn: outbox.approve(conn, body["outbox_id"], now_ms=_now(clock)))
    clock.advance_ms(8 * DAY_MS)
    assert db.write_sync(lambda conn: draft_channels.expired_phone_drafts(conn, now_ms=_now(clock))) == []


# --- ревью Ф34-3: ручной архив заготовки по номеру ----------------------------------------------------------


async def _archive(client, env, channel_id):
    r = await client.patch(
        f"/api/v1/channels/{channel_id}", json={"archived": True}, headers=csrf_headers(env["cookie"])
    )
    assert r.status_code == 200, r.text


def _statuses(db) -> dict:
    with db.reader() as conn:
        return dict(conn.execute("SELECT id, status FROM main.outbox").fetchall())


async def test_manual_archive_cancels_pending_and_erases(bearer, client, db, env):
    body = await _ok_draft(bearer, env)
    other = await _ok_draft(bearer, env, to="@Petr_Ivanov")
    await _archive(client, env, body["channel_id"])
    await _archive(client, env, other["channel_id"])
    statuses = _statuses(db)
    assert statuses[body["outbox_id"]] == "cancelled"
    assert statuses[other["outbox_id"]] == "pending_approval"  # заготовку по @username архив не трогает
    assert _where_number(db) == []
    # архивная без номера и без живого черновика — новая заготовка тому же номеру законна
    again = await _ok_draft(bearer, env, text="ещё раз")
    assert again["channel_id"] != body["channel_id"]


async def test_manual_archive_cancels_approved_not_yet_leased(bearer, client, db, clock, env):
    body = await _ok_draft(bearer, env)
    db.write_sync(lambda conn: outbox.approve(conn, body["outbox_id"], now_ms=_now(clock)))
    await _archive(client, env, body["channel_id"])
    assert _statuses(db)[body["outbox_id"]] == "cancelled"
    assert _where_number(db) == []


async def test_manual_archive_keeps_number_for_leased_row_until_outcome(bearer, client, db, clock, env):
    body = await _ok_draft(bearer, env)
    lease = _approve_and_lease(db, clock, body["outbox_id"], env["tg"])
    await _archive(client, env, body["channel_id"])
    assert _statuses(db)[body["outbox_id"]] == "sending"
    assert _where_number(db) == ["main.channel.meta_json"]  # строка у коннектора — номер ей ещё нужен
    _report(db, clock, lease, outcome="permanent", error="номер не найден в Telegram или скрыт настройками")
    assert _where_number(db) == []


async def test_manual_archive_of_ui_draft_erases_now_or_after_outcome(client, db, clock, env):
    empty = (await _ui_new(client, env)).json()["channel"]
    await _archive(client, env, empty["id"])
    assert _where_number(db) == []

    channel = (await _ui_new(client, env)).json()["channel"]
    r = await client.post(
        "/api/v1/messages/send",
        json={"channel_id": channel["id"], "text": "Здравствуйте!", "idempotency_key": "k1"},
        headers=csrf_headers(env["cookie"]),
    )
    assert r.status_code == 200, r.text
    (lease,) = db.write_sync(
        lambda conn: outbox.lease(conn, account_id=env["tg"], owner=OWNER, now_ms=_now(clock))
    )
    await _archive(client, env, channel["id"])
    assert _where_number(db) == ["main.channel.meta_json"]
    _report(db, clock, lease, outcome="permanent", error="это ваш номер")
    assert _where_number(db) == []  # UI-заготовку `failed` в архив не ведёт, но она уже в архиве


async def test_ttl_cancels_pending_draft_in_archived_channel(bearer, db, clock, env):
    """Заготовка оказалась в архиве мимо `update_channel` — срок 7 дней всё равно отменяет и стирает."""
    body = await _ok_draft(bearer, env)
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.channel SET archived = 1 WHERE id = ?", (body["channel_id"],))
    )
    clock.advance_ms(8 * DAY_MS)
    db.write_sync(lambda conn: runner._hourly_cleanup(conn, _now(clock)))  # noqa: SLF001
    assert _statuses(db)[body["outbox_id"]] == "cancelled" and _where_number(db) == []


# --- ревью Ф34-3: заготовка номера в невидимом токену проекте -----------------------------------------------


async def test_other_project_token_gets_409_without_ids(bearer, db, env):
    body = await _ok_draft(bearer, env)
    other = _token(db, name="mcp:initech", project_ids=[env["other_project"]])
    for extra in ({"text": "x"}, {"hold": True}):
        r = await bearer.post(
            "/api/v1/drafts/new",
            json={"account_id": env["tg"], "project_id": env["other_project"], "to": PHONE, **extra},
            headers=_auth(other),
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"] == "этому адресату уже готовится сообщение в другом проекте"
        assert f"#{body['outbox_id']}" not in r.text and f"#{body['channel_id']}" not in r.text
    # токен, которому проект виден, — прежний ответ с номерами строки и беседы
    seen = await _draft(bearer, env, project_id=env["other_project"])
    assert seen.status_code == 409 and f"#{body['outbox_id']}" in seen.json()["detail"]


# --- ревью Ф34-3: «отложено до запроса» (`postponed`) — без попытки и без остановки аккаунта ----------------


def _outbox_row(db, outbox_id) -> dict:
    with db.reader() as conn:
        row = conn.execute(
            "SELECT o.status, o.attempts, o.not_before_ts, o.send_started_ts, a.flood_until "
            "FROM main.outbox o JOIN main.account a ON a.id = o.account_id WHERE o.id = ?",
            (outbox_id,),
        ).fetchone()
    return dict(zip(("status", "attempts", "not_before", "started", "flood_until"), row, strict=True))


def _lease_again(db, clock, outbox_id, account_id):
    leases = db.write_sync(
        lambda conn: outbox.lease(conn, account_id=account_id, owner=OWNER, now_ms=_now(clock))
    )
    return next(item for item in leases if item.outbox_id == outbox_id)


async def test_postponed_report_spends_no_attempt_and_cancel_is_cancelled(bearer, db, clock, env):
    body = await _ok_draft(bearer, env)
    lease = _approve_and_lease(db, clock, body["outbox_id"], env["tg"])
    for _ in range(8):  # больше пяти ожиданий — строка не уходит в `failed`
        _report(db, clock, lease, outcome="temporary", error="лимит", retry_after_ms=HOUR_MS, postponed=True)
        row = _outbox_row(db, body["outbox_id"])
        assert (row["status"], row["attempts"], row["started"]) == ("queued", 0, None)
        assert row["not_before"] == _now(clock) + HOUR_MS and row["flood_until"] is None
        clock.advance_ms(HOUR_MS)
        lease = _lease_again(db, clock, body["outbox_id"], env["tg"])
    _report(db, clock, lease, outcome="temporary", error="лимит", retry_after_ms=HOUR_MS, postponed=True)
    # отправка не начиналась — отмена честная `cancelled`, заготовка в архив, номер стёрт
    status = db.write_sync(lambda conn: outbox.cancel(conn, body["outbox_id"], now_ms=_now(clock)))
    assert status == "cancelled" and _where_number(db) == []


async def test_postponed_after_real_attempt_keeps_send_started(bearer, db, clock, env):
    body = await _ok_draft(bearer, env)
    lease = _approve_and_lease(db, clock, body["outbox_id"], env["tg"])
    _report(db, clock, lease, outcome="temporary", error="network", retry_after_ms=1000)
    clock.advance_ms(1000)
    lease = _lease_again(db, clock, body["outbox_id"], env["tg"])
    _report(db, clock, lease, outcome="temporary", error="лимит", retry_after_ms=HOUR_MS, postponed=True)
    row = _outbox_row(db, body["outbox_id"])
    assert (row["status"], row["attempts"]) == ("queued", 1) and row["started"] is not None
    status = db.write_sync(lambda conn: outbox.cancel(conn, body["outbox_id"], now_ms=_now(clock)))
    assert status == "unknown"  # была настоящая попытка — как раньше


async def test_postponed_without_retry_after_is_plain_temporary(bearer, db, clock, env):
    body = await _ok_draft(bearer, env)
    lease = _approve_and_lease(db, clock, body["outbox_id"], env["tg"])
    _report(db, clock, lease, outcome="temporary", error="x", postponed=True)
    row = _outbox_row(db, body["outbox_id"])
    assert (row["status"], row["attempts"]) == ("queued", 1) and row["started"] is not None
