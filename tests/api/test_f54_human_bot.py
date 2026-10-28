"""Ф54-2 (docs/architecture.md–3 и поправки П2, П5, П6): ядро под бота владельца.

- скоуп `human_bot`: только `background`, ровно `{human_bot}`, только все проекты; `attach` не дописывается;
  на прочих ручках ядра — 403;
- бот видит заявки всех видов и проектов при стоящих выключателях, решает любую (`decided_by = bot`, аудит от
  имени бота), не создаёт и не снимает; ответчик `human` по-прежнему пишет `agent`;
- длинный опрос `GET /human-requests/changes`: только боту, контракт страницы, таймаут, пробуждение;
- П2: личка аккаунта Telegram с ботом хаба беседой не становится; заведённая раньше — стёрта, в архиве и
  закрыта bearer-токенам; обычные лички, другие боты и другие источники не задеты.

Мир и помощники — из test_hub_sessions.py и test_f54_human_requests.py."""

import asyncio
import re

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import human_requests as hr
from hub.core import security, service_bot
from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.ingest import apply_batch

from . import test_hub_sessions as base
from .card_helpers import _route
from .conftest import insert_account
from .test_f54_human_requests import (
    API,
    OPEN_ROUTES,
    PNG,
    SECRET,
    _agent,
    _cancel,
    _create,
    _decide,
    _human_token,
    _row,
)
from .test_hub_sessions import ROLE, A, _h, _now, _sub, _token

procs = base.procs
ui = base.ui
world = base.world

ADMIN_SECRET = ADMIN_PREFIX + "f542f542f542f542f542f542f542f542f542f542f54"  # выдуманная строка теста
BOT_ID = 7_000_111_222


def _bot_token(db):
    return _token(db, name="human-bot", kind="background", scopes=("human_bot",))


# --- скоуп (П5) --------------------------------------------------------------------------------------------


def test_human_bot_scope_only_background_exact_and_all_projects(db):
    _bot_token(db)
    with pytest.raises(ValueError, match="human_bot"):
        _token(db, name="human-bot", kind="interactive", scopes=("human_bot",))
    with pytest.raises(ValueError, match="human_bot"):
        _token(db, name="human-bot", kind="background", scopes=("human_bot",), project_ids=[1])
    for scopes in (("human_bot", "read"), ("human_bot", "human"), ("human_bot", "draft")):
        with pytest.raises(ValueError, match="human"):
            _token(db, name="x", kind="background", scopes=scopes)


def test_attach_is_never_appended_to_a_bot_token(db):
    _tok, token_id = _bot_token(db)
    for kind in ("background", "interactive"):
        db.write_sync(
            lambda conn, kind=kind: conn.execute(
                "UPDATE main.api_token SET kind = ? WHERE id = ?", (kind, token_id)
            )
        )
        with pytest.raises(security.AttachError) as err:
            db.write_sync(lambda conn: security.set_attach(conn, token_id=token_id, on=True, now_ms=1))
        assert err.value.status == 409
    with db.reader() as conn:
        assert conn.execute("SELECT scopes FROM main.api_token WHERE id = ?", (token_id,)).fetchone()[0] == (
            "human_bot"
        )


# Ф61-1 (П11): вопросы агента для бота — открыты ТОЛЬКО токену `human_bot` (test_f61_agent_mail_bot.py).
BOT_TALK_ROUTES = (
    "/api/v1/owner_channel/talk/asks",
    "/api/v1/owner_channel/talk/bot-reply",
    "/api/v1/owner_channel/money",  # Ф93-1: денежные карточки — тоже ручки бота
    "/api/v1/owner_channel/card-reply",
    "/api/v1/owner_channel/bot-dictate",  # голосовой ответ бота
    "/api/v1/owner_channel/returned",  # Ф93-1 (F2): возвраты агента — тоже ручка бота
    "/api/v1/owner_channel/talk/tells",  # Ф94-1: сообщения агента владельцу — тоже ручка бота
    "/api/v1/owner_channel/errands/{errand_id}/hands",  # Ф96-1: руки посреди поручения агента — и бот
)


async def test_bot_token_gets_403_on_every_other_route(app, client, db, world):
    bot, _ = _bot_token(db)
    checked = 0
    for template, methods in app.openapi()["paths"].items():
        if template.startswith("/api/v1/human-requests") or template.startswith("/api/v1/auth/"):
            continue
        if template in OPEN_ROUTES or template in BOT_TALK_ROUTES:
            continue
        path = re.sub(r"\{[^}]+\}", "1", template)
        for method in sorted(set(methods) & {"get", "post", "put", "patch", "delete"}):
            kw = {} if method == "get" else {"json": {}}
            r = await asyncio.wait_for(client.request(method, path, headers=_h(bot, 8), **kw), timeout=5)
            assert r.status_code in (401, 403), (method, template, r.status_code, r.text[:200])
            checked += 1
    assert checked > 150
    r = await client.get(f"{API}/human-requests", headers=_h(bot, 8))
    assert r.status_code == 200 and r.json() == []
    r = await client.get(f"{API}/owner_channel/talk/asks?since_ms=0", headers=_h(bot, 8))
    assert r.status_code == 200 and r.json() == {"items": []}


# --- видимость и решение (раздел 2, П5) --------------------------------------------------------------------


async def test_bot_sees_every_kind_and_decides_under_a_standing_switch(client, ui, db, clock, world):  # noqa: F811
    w = world
    await _sub(client, w, A, role=ROLE)
    await _sub(client, w, A, cwd=w["dir4"], role=ROLE)
    bot, bot_id = _bot_token(db)
    agent, agent_id = _human_token(db)
    captcha = (await _create(client, w, project_id=w["p3"], shot=PNG, need=SECRET))["request"]["id"]
    payment = (await _create(client, w, project_id=w["p3"], kind="payment", shot=PNG))["request"]["id"]
    foreign = (await _create(client, w, project_id=w["p4"], kind="login"))["request"]["id"]

    # Выключатели стоят: ответчик не видит ничего, бот — всё, с именем проекта и полем `decided_by`.
    assert (await _agent(client, agent, "GET", "/human-requests")).json() == []
    listed = (await _agent(client, bot, "GET", "/human-requests", params={"status": "pending"})).json()
    assert [item["id"] for item in listed] == [foreign, payment, captcha]
    assert listed[-1]["need"] == SECRET and listed[-1]["project_name"] == "Umbrella"
    assert listed[0]["project_name"] == "Initech" and listed[0]["decided_by"] is None
    one = (await _agent(client, bot, "GET", f"/human-requests/{payment}")).json()
    assert one["kind"] == "payment" and one["project_name"] == "Umbrella"
    r = await _agent(client, bot, "GET", f"/human-requests/{payment}/shot")
    assert r.status_code == 200 and r.content == PNG
    assert (await _agent(client, bot, "GET", "/human-requests/999999")).status_code == 404

    # Создать и снять заявку бот не может.
    r = await client.post(
        f"{API}/human-requests",
        files={"data": (None, '{"kind": "captcha", "need": "x", "session": {}}')},
        headers=_h(bot, 8),
    )
    assert r.status_code == 403, r.text
    await _cancel(client, w, captcha, token=bot, expect=403)

    # Решает вид без ключа при стоящем выключателе: `bot`, свой токен, аудит от имени бота.
    r = await _agent(client, bot, "POST", f"/human-requests/{payment}/failed", json={})
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["decided_by"]) == ("failed", "bot")
    row = _row(db, payment)
    assert (row.decided_by, row.decided_token_id) == ("bot", bot_id)
    with db.reader() as conn:
        actor, token_id = conn.execute(
            "SELECT actor, api_token_id FROM main.audit WHERE action = 'human_decide'"
        ).fetchone()
    assert (actor, token_id) == ("bot", bot_id)
    r = await _agent(client, bot, "POST", f"/human-requests/{payment}/done", json={})
    assert r.status_code == 409 and "уже закрыта" in r.json()["detail"]
    r = await _agent(client, bot, "POST", "/human-requests/999999/done", json={})
    assert r.status_code == 404

    # Ответчик по-прежнему пишет `agent` (и только по маршруту `agent` таблицы, Ф66-1), владелец — `ui`.
    await _route(ui, w, {"human": "agent"})
    r = await _agent(client, agent, "POST", f"/human-requests/{captcha}/done", json={})
    assert r.status_code == 200 and "decided_by" not in r.json()
    assert (_row(db, captcha).decided_by, _row(db, captcha).decided_token_id) == ("agent", agent_id)
    await _decide(ui, foreign, "later")
    assert _row(db, foreign).decided_by == "ui"
    # Боту источник ответа отдаётся: строка «закрыто: в хабе / агентом».
    by = {
        item["id"]: item["decided_by"]
        for item in (await _agent(client, bot, "GET", "/human-requests")).json()
    }
    assert by == {captcha: "agent", payment: "bot", foreign: "ui"}

    # Закрытая больше суток назад боту не видна (ждущие — всегда).
    clock.advance_ms(hr.DAY_MS + 1000)
    assert (await _agent(client, bot, "GET", f"/human-requests/{payment}")).status_code == 404
    assert (await _agent(client, bot, "GET", f"/human-requests/{payment}/shot")).status_code == 404


# --- длинный опрос изменений (П6) --------------------------------------------------------------------------


async def _changes(client, token, **params):
    return await client.get(f"{API}/human-requests/changes", params=params, headers=_h(token, 8))


async def test_changes_only_for_the_bot(client, ui, db, world):  # noqa: F811
    w = world
    agent, _ = _human_token(db)
    bot, _ = _bot_token(db)
    assert (await _changes(client, agent)).status_code == 403
    assert (await _changes(client, w["tok"])).status_code == 403
    assert (await ui.get(f"{API}/human-requests/changes")).status_code == 403
    r = await _changes(client, bot)
    assert r.status_code == 200 and r.json()["items"] == []
    assert (await _changes(client, bot, wait=26)).status_code == 422


async def test_changes_page_contract(client, ui, db, clock, world):  # noqa: F811
    w = world
    await _sub(client, w, A, role=ROLE)
    bot, _ = _bot_token(db)
    first = (await _create(client, w, need=SECRET))["request"]
    second = (await _create(client, w, kind="login"))["request"]
    body = (await _changes(client, bot)).json()
    assert [i["id"] for i in body["items"]] == [first["id"], second["id"]]
    assert [i["seq"] for i in body["items"]] == sorted(i["seq"] for i in body["items"])
    assert body["items"][0]["need"] == SECRET and body["items"][0]["project_name"] == "Umbrella"
    assert "decided_by" in body["items"][0]
    with db.reader() as conn:
        top = conn.execute("SELECT value FROM main.seq_counter WHERE id = 1").fetchone()[0]
    assert body["seq"] == top >= second["seq"]
    # С отметки — только новое; решение двигает seq заявки, и она приходит снова.
    assert (await _changes(client, bot, since=body["seq"])).json() == {"items": [], "seq": top}
    await _decide(ui, first["id"], "done")
    again = (await _changes(client, bot, since=body["seq"])).json()
    assert [(i["id"], i["status"], i["decided_by"]) for i in again["items"]] == [(first["id"], "done", "ui")]
    # Полная страница: отметка — seq её последней заявки, остальное следующим запросом.
    principal = security.Principal(
        token_id=1, kind="background", scopes=frozenset({"human_bot"}), project_ids=None
    )
    with db.reader() as conn:
        page = hr.bot_changes(conn, principal, since=0, now_ms=_now(clock), limit=1)
    assert len(page["items"]) == 1 and page["seq"] == page["items"][0]["seq"] < again["seq"]
    # Отметка клиента из будущего (база восстановлена из копии): пусто сразу и текущий максимум.
    r = await asyncio.wait_for(_changes(client, bot, since=again["seq"] + 10_000, wait=20), timeout=3)
    assert r.json() == {"items": [], "seq": again["seq"]}


async def test_changes_waits_and_wakes(client, ui, db, world):  # noqa: F811
    w = world
    await _sub(client, w, A, role=ROLE)
    bot, _ = _bot_token(db)
    since = (await _changes(client, bot)).json()["seq"]
    # Таймаут: пусто и та же отметка, не раньше срока.
    loop = asyncio.get_running_loop()
    began = loop.time()
    body = (await _changes(client, bot, since=since, wait=0.3)).json()
    assert body["items"] == [] and loop.time() - began >= 0.25
    # Пробуждение: заявка, созданная во время ожидания, приходит сразу, а не через 20 секунд.
    task = asyncio.create_task(_changes(client, bot, since=body["seq"], wait=20))
    await asyncio.sleep(0.2)
    assert not task.done()
    made = (await _create(client, w))["request"]
    woke = (await asyncio.wait_for(task, timeout=5)).json()
    assert [i["id"] for i in woke["items"]] == [made["id"]]


# --- П2: личка с ботом хаба --------------------------------------------------------------------------------


def _dm(ext_id: str, msg: str, ts: int, *, title="Кто-то", is_bot=False, btn=None) -> dict:
    event = {
        "type": "upsert",
        "channel": {"ext_id": ext_id, "kind": "dm", "title": title},
        "ext_id": msg,
        "ts": ts,
        "is_me": False,
        "kind": "text",
        "text": f"текст {SECRET} {msg}",
        "mentions_me": False,
        "sender": {"ext_id": ext_id, "name": title, "is_bot": is_bot},
    }
    if btn is not None:
        event["buttons"] = btn
        event["buttons_ts"] = ts
    return event


BUTTONS = {"kind": "inline", "rows": [[{"t": "Готово", "k": "callback", "d": "aDoxOmQ="}]]}


def _ingest(db, acc, events, now):
    return db.write_sync(
        lambda conn: apply_batch(conn, account_id=acc, events=events, cursor=None, now_ms=now)
    )


def _channels(db) -> dict[str, tuple[int, int]]:
    with db.reader() as conn:
        rows = conn.execute("SELECT ext_id, id, archived FROM main.channel").fetchall()
    return {ext_id: (cid, archived) for ext_id, cid, archived in rows}


def _texts(db, channel_id) -> list[tuple]:
    with db.reader() as conn:
        return conn.execute(
            "SELECT id, hidden, text FROM main.message WHERE channel_id = ? ORDER BY id", (channel_id,)
        ).fetchall()


def test_bot_dm_never_becomes_a_channel_and_others_are_untouched(db, clock):
    now = _now(clock)
    tg = insert_account(db, source="telegram")
    slack = insert_account(db, source="slack")
    db.write_sync(lambda conn: service_bot.set_bot_id(conn, BOT_ID, now_ms=now))
    res = _ingest(
        db,
        tg,
        [
            _dm(str(BOT_ID), "1", now - 5000, title="Бот хаба", is_bot=True, btn=BUTTONS),
            _dm("555", "2", now - 4000, title="Алиса"),
            _dm("777123", "3", now - 3000, title="Другой бот", is_bot=True, btn=BUTTONS),
            {**_dm(str(BOT_ID), "1", now - 2000, is_bot=True), "type": "edit", "edited_ts": now - 2000},
            {"type": "delete", "channel": {"ext_id": str(BOT_ID), "kind": "dm"}, "ext_ids": ["1"]},
            # Группа с тем же числом в ext_id — не личка: не задета.
            {
                **_dm("555", "9", now - 1000),
                "channel": {"ext_id": str(BOT_ID), "kind": "group", "title": "Г"},
            },
        ],
        now,
    )
    assert res.dead == 0 and res.applied == 3 and res.noop == 3
    chans = _channels(db)
    assert sorted(chans) == sorted(["555", "777123", str(BOT_ID)])
    with db.reader() as conn:
        kinds = dict(conn.execute("SELECT ext_id, kind FROM main.channel").fetchall())
        total = conn.execute("SELECT COUNT(*) FROM main.message").fetchone()[0]
        with_buttons = conn.execute(
            "SELECT COUNT(*) FROM main.message WHERE buttons_json IS NOT NULL"
        ).fetchone()[0]
    assert kinds[str(BOT_ID)] == "group" and total == 3 and with_buttons == 1
    # Другой источник с таким же собеседником — не Telegram: принимается как обычно.
    res = _ingest(db, slack, [_dm(str(BOT_ID), "7", now, title="Slack-личка")], now)
    assert (res.applied, res.noop) == (1, 0)
    # Без записанного id бота личка с ним — обычная беседа.
    db.write_sync(lambda conn: conn.execute("DELETE FROM main.meta WHERE key = ?", (service_bot.META_KEY,)))
    res = _ingest(db, tg, [_dm(str(BOT_ID), "8", now, title="Бот хаба", is_bot=True)], now)
    assert (res.applied, res.noop) == (1, 0)


@pytest.fixture
async def admin_client(db, clock, app):
    internal = create_internal_app(
        db,
        clock=clock,
        notifier=app.state.notifier,
        admin_key_hash=hash_secret(ADMIN_SECRET),
        sub_waiters=app.state.sub_waiters,
        chan_sub_waiters=app.state.chan_sub_waiters,
    )
    async with AsyncClient(transport=ASGITransport(app=internal), base_url="http://hub-internal") as c:
        yield c


async def test_existing_bot_dm_is_closed_for_bearer(client, ui, db, clock, world, admin_client):  # noqa: F811
    w = world
    now = _now(clock)
    tg = w["acc"]
    res = _ingest(
        db,
        tg,
        [
            _dm(str(BOT_ID), "1", now - 5000, title="Бот хаба", is_bot=True, btn=BUTTONS),
            _dm("555", "2", now - 4000, title="Алиса"),
        ],
        now,
    )
    assert res.applied == 2
    bot_ch, alisa = _channels(db)[str(BOT_ID)][0], _channels(db)["555"][0]
    bot_msg, alisa_msg = _texts(db, bot_ch)[0][0], _texts(db, alisa)[0][0]
    press = {"row": 1, "col": 1, "text": "Готово", "session": base._sess(A)}  # noqa: SLF001
    await _sub(client, w, A, role=ROLE)

    async def bearer_calls(channel_id, message_id):
        read = await client.get(f"{API}/channels/{channel_id}/messages", headers=_h(w["tok"]))
        one = await client.get(f"{API}/messages/{message_id}", headers=_h(w["tok"]))
        chan = await client.get(f"{API}/channels/{channel_id}", headers=_h(w["tok"]))
        draft = await client.post(
            f"{API}/messages/send",
            json={"channel_id": channel_id, "text": "ответ", "idempotency_key": f"k{channel_id}"},
            headers=_h(w["tok"]),
        )
        pressed = await client.post(
            f"{API}/messages/{message_id}/buttons/press", json=press, headers=_h(w["tok"], 6)
        )
        return read, one, chan, draft, pressed

    # До записи id бота личка с ним — обычная беседа: читается и принимает черновик.
    read, one, chan, draft, _pressed = await bearer_calls(bot_ch, bot_msg)
    assert (read.status_code, one.status_code, chan.status_code, draft.status_code) == (200, 200, 200, 200)

    # Только отметка в базе (как если бы стирание не прошло): беседа уже закрыта самим правилом доступа.
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.meta (key, value) VALUES (?, ?)", (service_bot.META_KEY, str(BOT_ID))
        )
    )
    read, one, chan, draft, pressed = await bearer_calls(bot_ch, bot_msg)
    for r in (read, draft, pressed):
        assert (r.status_code, r.json()["detail"]) == (404, service_bot.CLOSED_TEXT), r.text
    assert (one.status_code, chan.status_code) == (404, 404)
    assert SECRET not in read.text + one.text
    # Обычная личка не задета.
    read, one, chan, draft, _pressed = await bearer_calls(alisa, alisa_msg)
    assert (read.status_code, one.status_code, chan.status_code, draft.status_code) == (200, 200, 200, 200)
    assert SECRET in read.text
    # Владельцу беседа видна.
    assert (await ui.get(f"{API}/channels/{bot_ch}/messages")).status_code == 200

    # `hub auth human-bot`, шаг 1 (до `/start`): админ-ручка пишет id; беседа НЕ стирается и ещё не в архиве.
    r = await admin_client.post("/internal/v1/admin/human-bot", json={"tg_id": BOT_ID})
    assert r.status_code == 401
    head = {"Authorization": f"Bearer {ADMIN_SECRET}"}
    r = await admin_client.post("/internal/v1/admin/human-bot", json={"tg_id": BOT_ID}, headers=head)
    want = {"tg_id": str(BOT_ID), "tg_ids": [str(BOT_ID)], "closed": 1}
    assert r.status_code == 200 and r.json() == want, r.text
    assert (await admin_client.get("/internal/v1/admin/human-bot", headers=head)).json() == {
        "tg_id": str(BOT_ID),
        "tg_ids": [str(BOT_ID)],
    }
    assert _channels(db)[str(BOT_ID)][1] == 0
    # Шаг 2 (привязка подтверждена): беседа уходит в архив; сообщения целы — удаление необратимо.
    r = await admin_client.post(
        "/internal/v1/admin/human-bot", json={"tg_id": BOT_ID, "archive": True}, headers=head
    )
    assert r.status_code == 200 and r.json() == want, r.text
    assert _channels(db)[str(BOT_ID)][1] == 1 and _channels(db)["555"][1] == 0
    rows = _texts(db, bot_ch)
    assert all(hidden == 0 for _id, hidden, _text in rows) and SECRET in rows[0][2]
    assert all(hidden == 0 for _id, hidden, _text in _texts(db, alisa))
    # Владельцу в интерфейсе беседа видна как была — с текстом.
    seen = await ui.get(f"{API}/channels/{bot_ch}/messages")
    assert seen.status_code == 200 and f"{SECRET} 1" in seen.text
    sync = await client.get(f"{API}/sync", params={"since": 0}, headers=_h(w["tok"]))
    assert sync.status_code == 200 and f"{SECRET} 1" not in sync.text and f"{SECRET} 2" in sync.text
    # Новые события лички с ботом после записи id — мимо: беседа не оживает.
    res = _ingest(db, tg, [_dm(str(BOT_ID), "50", now, title="Бот хаба", is_bot=True, btn=BUTTONS)], now)
    assert (res.applied, res.noop) == (0, 1)
    assert len(_texts(db, bot_ch)) == len(rows) and _channels(db)[str(BOT_ID)][1] == 1
    # Мусор вместо id и «снять» — 422: id ботов не забываются.
    for bad in ({"tg_id": 0}, {"tg_id": None}, {"tg_id": BOT_ID, "archive": "да"}, {}):
        r = await admin_client.post("/internal/v1/admin/human-bot", json=bad, headers=head)
        assert r.status_code == 422, bad


# --- правки по ревью ---------------------------------------------------------------------------------------

BOT_B = 7_999_000_111
TITLE = "Бот хаба ЗАГОЛОВОК"


async def _bearer_views(client, w, channel_id) -> dict:
    """Все пути чтения bearer-токена, где могла бы показаться беседа: `(код, текст ответа)`."""
    h = _h(w["tok"])
    calls = {
        "search": client.get(f"{API}/search", params={"q": SECRET}, headers=h),
        "sync": client.get(f"{API}/sync", params={"since": 0}, headers=h),
        "resolve": client.post(f"{API}/channels/resolve", json={"query": "Бот хаба"}, headers=h),
        "wait": client.get(
            f"{API}/channels/{channel_id}/wait", params={"after": 0, "timeout_s": 1}, headers=h
        ),
        "inbox": client.get(f"{API}/inbox", headers=h),
        "home": client.get(f"{API}/home/react", headers=h),
        "outbox": client.get(f"{API}/outbox", params={"status": "pending_approval"}, headers=h),
        "items": client.get(f"{API}/agent/items", params={"status": "any"}, headers=h),
        "people": client.get(f"{API}/people", params={"q": "Бот хаба"}, headers=h),
        "threads": client.get(f"{API}/channels/{channel_id}/threads", headers=h),
        "channel": client.get(f"{API}/channels/{channel_id}", headers=h),
        "messages": client.get(f"{API}/channels/{channel_id}/messages", headers=h),
        "projects": client.get(f"{API}/projects", headers=h),
    }
    out = {}
    for name, call in calls.items():
        r = await call
        out[name] = (r.status_code, r.text)
    with db_reader(w) as conn:
        pids = [row[0] for row in conn.execute("SELECT id FROM main.project").fetchall()]
    for pid in [*pids, -1]:
        r = await client.get(f"{API}/projects/{pid}/channels", headers=h)
        out[f"channels:{pid}"] = (r.status_code, r.text)
    return out


def db_reader(w):
    return w["db"].reader()


async def test_unerased_bot_dm_does_not_leak_on_any_bearer_path(client, ui, db, clock, world):  # noqa: F811
    """Решение головной: беседа с ботом НЕ стирается — значит, закрыта она должна быть общей проверкой на
    всех путях чтения bearer (поиск, `/sync`, поиск беседы, ожидание, входящие, главная, исходящие,
    карточки, списки бесед), а владельцу видна как была."""
    w = {**world, "db": db}
    now = _now(clock)
    await _sub(client, w, A, role=ROLE)
    _ingest(db, w["acc"], [_dm(str(BOT_ID), "1", now - 5000, title=TITLE, is_bot=True, btn=BUTTONS)], now)
    _ingest(db, w["acc"], [_dm("555", "2", now - 4000, title="Алиса")], now)
    ch = _channels(db)[str(BOT_ID)][0]
    # Беседа разложена в проект токена: видна ему и в списке бесед проекта.
    db.write_sync(lambda c: c.execute("UPDATE main.channel SET project_id = ? WHERE id = ?", (w["p3"], ch)))
    r = await client.post(
        f"{API}/messages/send",
        json={"channel_id": ch, "text": f"черновик {SECRET} 1", "idempotency_key": "k1"},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text
    # До привязки бота беседа обычная: проверка действительно смотрит туда, где она видна.
    before = await _bearer_views(client, w, ch)
    shown = sorted(name for name, (_code, text) in before.items() if f"{SECRET} 1" in text or TITLE in text)
    assert {"search", "sync", "resolve", "messages", "channel", "outbox"} <= set(shown), shown
    assert {"wait", "inbox", f"channels:{w['p3']}"} <= set(shown), shown

    db.write_sync(
        lambda c: c.execute("UPDATE main.outbox SET status = 'unknown' WHERE channel_id = ?", (ch,))
    )
    out = db.write_sync(lambda conn: service_bot.set_bot_id(conn, BOT_ID, now_ms=now, archive=True))
    assert out["closed"] == 1 and all(hidden == 0 for _i, hidden, _t in _texts(db, ch))  # ничего не стёрто

    after = await _bearer_views(client, w, ch)
    leaks = sorted(name for name, (_code, text) in after.items() if f"{SECRET} 1" in text or TITLE in text)
    assert leaks == [], leaks
    assert after["sync"][0] == 200 and f"{SECRET} 2" in after["sync"][1]  # обычная личка на месте
    assert after["search"][0] == 200 and f"{SECRET} 2" in after["search"][1]
    for name in ("wait", "threads", "channel", "messages"):
        assert after[name][0] == 404, name
    # Подписка на беседу, реакция, перенос, черновик, отметка — тоже нет такой беседы.
    h = _h(w["tok"])
    msg = _texts(db, ch)[0][0]
    for r in (
        await client.post(
            f"{API}/messages/send", json={"channel_id": ch, "text": "x", "idempotency_key": "k2"}, headers=h
        ),
        await client.get(f"{API}/messages/{msg}", headers=h),
        await client.get(f"{API}/messages/{msg}/edits", headers=h),
    ):
        assert r.status_code in (403, 404), (r.request.url.path, r.status_code)
    with db.reader() as conn:
        from hub.core import chan_subs

        assert chan_subs._channel(conn, ch) is None  # noqa: SLF001 — подписка на беседу её не находит
        assert chan_subs._channel(conn, _channels(db)["555"][0]) is not None  # noqa: SLF001
    # Владельцу беседа видна как была: текст, поиск, список.
    seen = await ui.get(f"{API}/channels/{ch}/messages")
    assert seen.status_code == 200 and f"{SECRET} 1" in seen.text
    found = await ui.get(f"{API}/search", params={"q": SECRET})
    assert found.status_code == 200 and f"{SECRET} 1" in found.text


async def test_bot_change_keeps_the_old_bot_closed(client, db, clock, world):  # noqa: F811
    """Смена бота: id прежнего не забывается — его личка и после сверки коннектора беседой не становится, а
    существующая остаётся закрытой bearer-токенам."""
    w = world
    now = _now(clock)
    await _sub(client, w, A, role=ROLE)
    old_dm = _dm(str(BOT_B), "1", now - 9000, title="Старый бот", is_bot=True)
    assert _ingest(db, w["acc"], [old_dm], now).applied == 1  # прежний бот успел стать беседой
    old_ch = _channels(db)[str(BOT_B)][0]
    db.write_sync(lambda conn: service_bot.set_bot_id(conn, BOT_B, now_ms=now, archive=True))
    msg = _dm(str(BOT_ID), "1", now - 5000, title="Бот хаба", is_bot=True, btn=BUTTONS)
    out = db.write_sync(lambda conn: service_bot.set_bot_id(conn, BOT_ID, now_ms=now))  # владелец завёл нового
    assert out == {"tg_id": str(BOT_ID), "tg_ids": [str(BOT_B), str(BOT_ID)], "closed": 0}
    for event in (msg, {**old_dm, "ext_id": "77"}):
        res = _ingest(db, w["acc"], [event], now)
        assert (res.applied, res.noop) == (0, 1)
    assert str(BOT_ID) not in _channels(db)
    r = await client.get(f"{API}/channels/{old_ch}/messages", headers=_h(w["tok"]))
    assert (r.status_code, r.json()["detail"]) == (404, service_bot.CLOSED_TEXT)
    # Вернулся к прежнему боту — множество то же, нынешний последним; мусор в значении не ломает чтение.
    out = db.write_sync(lambda conn: service_bot.set_bot_id(conn, BOT_B, now_ms=now))
    assert out["tg_ids"] == [str(BOT_ID), str(BOT_B)] and out["closed"] == 1
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.meta SET value = ? WHERE key = ?", (f"x, {BOT_ID},,0,{BOT_ID}", service_bot.META_KEY)
        )
    )
    with db.reader() as conn:
        assert service_bot.bot_id_list(conn) == [str(BOT_ID)]


def test_connector_heads_tell_telegram_to_skip_bot_dms(db, clock):
    """Коннектору Telegram в «головах» бесед — признак `skip` у лички с ботом хаба (есть беседа или нет):
    он её не грузит и не догоняет. Другим источникам и до привязки — ничего лишнего."""
    from hub.core import queries

    now = _now(clock)
    tg = insert_account(db, source="telegram")
    slack = insert_account(db, source="slack")
    _ingest(db, tg, [_dm("555", "2", now, title="Алиса"), _dm(str(BOT_B), "3", now, is_bot=True)], now)
    with db.reader() as conn:
        assert not [h for h in queries.connector_channel_heads(conn, tg) if h.get("skip")]
    db.write_sync(lambda conn: service_bot.set_bot_id(conn, BOT_ID, now_ms=now))
    db.write_sync(lambda conn: service_bot.set_bot_id(conn, BOT_B, now_ms=now))
    with db.reader() as conn:
        heads = queries.connector_channel_heads(conn, tg)
        refill = queries.connector_channel_heads(conn, tg, since_ts=now)
        other = queries.connector_channel_heads(conn, slack)
    skipped = {h["ext_id"]: h for h in heads if h.get("skip")}
    assert sorted(skipped) == sorted([str(BOT_ID), str(BOT_B)])
    assert skipped[str(BOT_ID)]["max_ext_sort"] is None and skipped[str(BOT_ID)]["kind"] == "dm"
    assert skipped[str(BOT_B)]["max_ext_sort"] is not None  # существующая беседа — её же строка
    assert [h["ext_id"] for h in heads if not h.get("skip")] == ["555"] and len(heads) == 3
    assert all("project_id" in h and "refill_lo_ext_sort" in h for h in refill)
    assert other == []


async def test_bot_reads_closed_requests_only_for_a_day(client, ui, db, clock, world):  # noqa: F811
    """Контракт: боту — ждущие и закрытые за последние сутки; и в списке со `status`, и в изменениях."""
    w = world
    await _sub(client, w, A, role=ROLE)
    bot, _ = _bot_token(db)
    old = (await _create(client, w, project_id=w["p3"], need=SECRET))["request"]["id"]
    assert (await _agent(client, bot, "POST", f"/human-requests/{old}/done", json={})).status_code == 200
    waiting = (await _create(client, w, project_id=w["p3"], need="ждёт давно"))["request"]["id"]
    clock.advance_ms(23 * 3600 * 1000)
    for path, params in (("/human-requests", {"status": "done"}), ("/human-requests/changes", {"since": 0})):
        r = await _agent(client, bot, "GET", path, params=params)
        assert r.status_code == 200 and SECRET in r.text, path  # в пределах суток — видно
    clock.advance_ms(2 * 3600 * 1000)
    assert (await _agent(client, bot, "GET", f"/human-requests/{old}")).status_code == 404
    lst = await _agent(client, bot, "GET", "/human-requests", params={"status": "done"})
    plain = await _agent(client, bot, "GET", "/human-requests")
    chg = await _agent(client, bot, "GET", "/human-requests/changes", params={"since": 0})
    assert SECRET not in lst.text + plain.text + chg.text
    # Ждущая старше суток боту видна, отметка `seq` изменений идёт мимо скрытых.
    ids = [item["id"] for item in chg.json()["items"]]
    assert (
        waiting in ids and old not in ids and chg.json()["seq"] >= max(i["seq"] for i in chg.json()["items"])
    )
    # Владельцу список по статусу — без предела давности, как был.
    assert SECRET in (await ui.get(f"{API}/human-requests", params={"status": "done"})).text
