"""Ф63-2 (docs/architecture.md и поправки П1–П10 — они главнее): сессии хаба в
авто переписываются с Telegram-ботами без черновика при снятом выключателе `bot_chat`, со страховкой.

Мир и помощники — из test_hub_sessions.py / test_f41_autopilot_5b.py / test_f44_presses.py; коннектор —
внутреннее приложение на `/internal/v1`."""

import base64
import json

from hub.core import ingest, pace, presses

from . import test_hub_sessions as base
from .conftest import insert_message
from .test_f41_autopilot import _put
from .test_f41_autopilot_5b import _auto, _iso, _me
from .test_f44_presses import _conn_headers, _lease, _press, _row, ic  # noqa: F401 — фикстура `ic`
from .test_hub_sessions import ROLE, A, _audits, _h, _now, _sess, _sub, _token

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"
INTERNAL = "/internal/v1"
MIN = 60_000
PDF = b"%PDF-1.7\n" + b"x" * 100


# --- помощники ----------------------------------------------------------------------------------------------


def _set_config(db, account_id, **values) -> None:
    def _fn(conn):
        row = conn.execute("SELECT config_json FROM main.account WHERE id = ?", (account_id,)).fetchone()
        config = json.loads(row[0] or "{}")
        config.update(values)
        conn.execute("UPDATE main.account SET config_json = ? WHERE id = ?", (json.dumps(config), account_id))

    db.write_sync(_fn)


def _chat(
    db, w, clock, *, ext="9001", text="Чем помочь?", msg_ext="700", tg_type="bot", project="p3", title="Бот",
    kb=None, etype="upsert", bump=0,
) -> tuple[int, int]:  # fmt: skip
    """Личка с ботом (или человеком — `tg_type="user"`) приёмом, как от коннектора, и сообщение собеседника
    в ней; беседа — в проекте мира. Возвращает `(channel_id, message_id)`."""
    now = _now(clock)
    event = {
        "type": etype,
        "channel": {"ext_id": ext, "kind": "dm", "title": title, "attrs": {"tg_type": tg_type}},
        "ext_id": msg_ext,
        "ts": now - MIN,
        "sender": {"ext_id": ext, "name": title, "is_bot": tg_type == "bot"},
        "is_me": False,
        "kind": "text",
        "text": text,
        "mentions_me": False,
    }
    if kb is not None:
        event["buttons"] = kb
        event["buttons_ts"] = now + bump
    result = db.write_sync(
        lambda conn: ingest.apply_batch(conn, account_id=w["acc"], events=[event], cursor=None, now_ms=now)
    )
    assert result.dead == 0, result

    def _fn(conn):
        conn.execute(
            "UPDATE main.channel SET project_id = ?, map_source = 'manual' "
            "WHERE account_id = ? AND ext_id = ?",
            (w[project] if project else None, w["acc"], ext),
        )
        return conn.execute(
            "SELECT c.id, m.id FROM main.channel c JOIN main.message m ON m.channel_id = c.id "
            "WHERE c.account_id = ? AND c.ext_id = ? AND m.ext_id = ?",
            (w["acc"], ext, msg_ext),
        ).fetchone()

    channel_id, message_id = db.write_sync(_fn)
    return channel_id, message_id


async def _draft(client, w, channel, ident, text="Хочу узнать тарифы", *, expect=200, **extra) -> dict:
    body = {"channel_id": channel, "text": text, "idempotency_key": f"b-{_draft.n}", **ident, **extra}
    _draft.n += 1
    r = await client.post(f"{API}/messages/send", json=body, headers=_h(w["tok"]))
    assert r.status_code == expect, r.text
    return r.json()


_draft.n = 0


async def _new(client, w, to, ident, text="/start", *, expect=200, token=None, project="p3") -> dict:
    body = {"account_id": w["acc"], "project_id": w[project], "to": to, "text": text, **ident}
    r = await client.post(f"{API}/drafts/new", json=body, headers=_h(token or w["tok"]))
    assert r.status_code == expect, r.text
    return r.json()


def _outbox(db, outbox_id) -> dict:
    with db.reader() as conn:
        row = conn.execute(
            "SELECT status, paced, not_before_ts, auto_role, auto_session, channel_id FROM main.outbox "
            "WHERE id = ?",
            (outbox_id,),
        ).fetchone()
    return dict(
        zip(("status", "paced", "not_before_ts", "auto_role", "auto_session", "channel_id"), row, strict=True)
    )


def _channel(db, channel_id) -> dict:
    with db.reader() as conn:
        row = conn.execute(
            "SELECT ext_id, project_id, meta_json, archived FROM main.channel WHERE id = ?", (channel_id,)
        ).fetchone()
    return {"ext_id": row[0], "project_id": row[1], "meta": json.loads(row[2] or "{}"), "archived": row[3]}


async def _report(ic, h, item, outcome="sent", **extra):  # noqa: F811 — фикстура `ic`
    body = {"outbox_id": item["outbox_id"], "lease_gen": item["lease_gen"], "outcome": outcome, **extra}
    r = await ic.post(f"{INTERNAL}/outbox/report", headers=h, json=body)
    assert r.status_code == 200, r.text


async def _lease_outbox(ic, h) -> list[dict]:  # noqa: F811
    r = await ic.post(f"{INTERNAL}/outbox/lease", headers=h, json={})
    assert r.status_code == 200, r.text
    return r.json()


def _guard_audits(db) -> list[dict]:
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT result, detail_json, outbox_id FROM main.audit WHERE action = 'bot_guard' ORDER BY id"
        ).fetchall()
    return [{"result": r[0], "detail": json.loads(r[1]), "outbox_id": r[2]} for r in rows]


# --- первое сообщение боту (1.2, П1, П9) --------------------------------------------------------------------


async def test_start_to_bot_at_night_goes_at_once_without_pace(client, ui, ic, db, clock, world):  # noqa: F811
    """П1: `/start` боту в нерабочее время (окно графика 10–19, сейчас ночь по времени хаба) уходит сразу, без
    слота графика и не занимая `pace_daily`; счёт — `bot_new`, не `new_account`."""
    w = world
    _set_config(db, w["acc"], pace_window="10:00-19:00", pace_tz="Europe/Moscow")
    sid = await _auto(client, ui, w)
    await _put(ui, "global", "hub_send_new", False)
    # Для сравнения: человеку по `hub_send_new` — по графику (утром).
    human = await _new(client, w, "@petr_ivanov", _me(sid), text="Здравствуйте")
    assert (human["status"], human["auto"]["by"], human["auto"]["mode"]) == ("queued", "autopilot", "paced")

    # `bot_chat` стоит — бот идёт прежним путём `hub_send_new` (по графику) и через страховку.
    held = await _new(client, w, "@first_bot", _me(sid))
    assert held["auto"]["by"] == "autopilot" and held["auto"]["mode"] == "paced"
    with db.reader() as conn:
        paced_before = pace.today(conn, w["acc"], _now(clock))[0]

    await _put(ui, "global", "bot_chat", False)
    sent = await _new(client, w, "@x_bot", _me(sid))
    assert sent["status"] == "queued" and sent["paced"] is False
    assert sent["auto"] == {"role": ROLE, "by": "bot_chat", "mode": None}
    row = _outbox(db, sent["outbox_id"])
    assert (row["status"], row["paced"]) == ("queued", 0)
    assert row["not_before_ts"] is None or row["not_before_ts"] <= _now(clock)
    assert (row["auto_role"], row["auto_session"]) == (ROLE, sid)
    with db.reader() as conn:
        assert pace.today(conn, w["acc"], _now(clock))[0] == paced_before
    audit = _audits(db, "outbox_autosend")[-1]
    assert audit["by"] == "bot_chat" and audit["bot_new"] == w["acc"] and "new_account" not in audit
    assert audit["hub_session_id"] == sid
    assert _audits(db, "outbox_approve") == []

    # Строка выдаётся коннектору сразу (не ждёт окна).
    h = _conn_headers(db, w["acc"])
    leased = {item["outbox_id"] for item in await _lease_outbox(ic, h)}
    assert sent["outbox_id"] in leased and human["outbox_id"] not in leased


async def test_reply_right_after_rename_goes_without_draft(client, ui, ic, db, clock, world):  # noqa: F811
    """П2: отчёт `sent` по заготовке несёт `peer_bot` — ядро ставит новой беседе `attrs.tg_type = "bot"`, и
    ответ боту сразу после `/start` идёт веткой `bot_chat` (без признака — черновиком)."""
    w = world
    sid = await _auto(client, ui, w)
    await _put(ui, "global", "bot_chat", False)
    h = _conn_headers(db, w["acc"])

    first = await _new(client, w, "@x_bot", _me(sid))
    (item,) = [i for i in await _lease_outbox(ic, h) if i["outbox_id"] == first["outbox_id"]]
    await _report(ic, h, item, ext_id="1", peer_ext_id="9001", peer_title="Икс", peer_bot=True)
    chan = _channel(db, first["channel_id"])
    assert chan["ext_id"] == "9001" and chan["project_id"] == w["p3"]
    assert chan["meta"]["attrs"]["tg_type"] == "bot" and "new_to" not in chan["meta"]["attrs"]

    reply = await _draft(client, w, first["channel_id"], _me(sid), text="Покажи тарифы")
    assert reply["status"] == "queued" and reply["auto"]["by"] == "bot_chat"

    # Без `peer_bot` (старый коннектор) признака нет — ответ лёг черновиком.
    second = await _new(client, w, "@y_bot", _me(sid))
    (item,) = [i for i in await _lease_outbox(ic, h) if i["outbox_id"] == second["outbox_id"]]
    await _report(ic, h, item, ext_id="1", peer_ext_id="9002", peer_title="Игрек")
    assert "tg_type" not in _channel(db, second["channel_id"])["meta"].get("attrs", {})
    assert (await _draft(client, w, second["channel_id"], _me(sid)))["status"] == "pending_approval"


async def test_nick_resolved_to_a_person_is_not_sent(client, ui, ic, db, clock, world):  # noqa: F811
    """П3: коннектор отказал «@… — не бот» (`permanent`) — отправки нет, черновик `failed`, заготовка в
    архиве по обычной механике."""
    w = world
    sid = await _auto(client, ui, w)
    await _put(ui, "global", "bot_chat", False)
    h = _conn_headers(db, w["acc"])
    sent = await _new(client, w, "@fake_bot", _me(sid))
    (item,) = [i for i in await _lease_outbox(ic, h) if i["outbox_id"] == sent["outbox_id"]]
    await _report(ic, h, item, outcome="permanent", error="@fake_bot — не бот")
    assert _outbox(db, sent["outbox_id"])["status"] == "failed"
    assert _channel(db, sent["channel_id"])["archived"] == 1


async def test_bot_new_daily_is_its_own_limit(client, ui, db, clock, world):  # noqa: F811
    """П9: `bot_new_daily` — свой счёт (по журналу `bot_new`); `auto_new_daily = 0` ботам не мешает."""
    w = world
    _set_config(db, w["acc"], bot_new_daily=1, auto_new_daily=0)
    sid = await _auto(client, ui, w)
    await _put(ui, "global", "bot_chat", False)
    await _put(ui, "global", "hub_send_new", False)
    assert (await _new(client, w, "@a_bot", _me(sid)))["auto"]["by"] == "bot_chat"
    over = await _new(client, w, "@b_bot", _me(sid))
    assert over["status"] == "pending_approval"
    assert (over["auto_denied"], over["auto_limit"]) == ("limit", "bot_new_daily")
    # Человеку `auto_new_daily = 0` по-прежнему закрыт (ботский счёт его не трогает).
    human = await _new(client, w, "@petr_ivanov", _me(sid))
    assert (human["auto_denied"], human["auto_limit"]) == ("limit", "new_daily")


async def test_existing_bot_chat_refuses_new_draft(client, ui, db, clock, world):  # noqa: F811
    """П7: личка с этим ботом уже есть (контакт `@ник`, без учёта регистра) — `hub_draft_new` отказывает с
    номером и проектом; невидимой токену — без номера."""
    w = world
    sid = await _auto(client, ui, w)
    channel_id, _mid = _chat(db, w, clock, ext="9100", project="p4", title="Магазин")
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.contact SET handle = '@Shop_Bot' WHERE account_id = ? AND ext_id = '9100'",
            (w["acc"],),
        )
    )
    r = await client.post(
        f"{API}/drafts/new",
        json={"account_id": w["acc"], "project_id": w["p3"], "to": "@shop_bot", "text": "/start", **_me(sid)},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert f"#{channel_id}" in detail and "Initech" in detail and "hub_draft" in detail
    p3_only, _ = _token(db, name="mcp:p3", scopes=("read", "draft"), project_ids=[w["p3"]])
    r = await client.post(
        f"{API}/drafts/new",
        json={"account_id": w["acc"], "project_id": w["p3"], "to": "@shop_bot", "text": "/start"},
        headers=_h(p3_only),
    )
    assert r.status_code == 409 and f"#{channel_id}" not in r.json()["detail"]
    # Человек с ником не на «bot» — прежний путь (заготовка, склейка при отправке).
    assert (await _new(client, w, "@shop_human", _me(sid)))["status"] == "pending_approval"


# --- ответы в беседе с ботом и чьи пути (1.2, П2, П7) -------------------------------------------------------


async def test_who_may_write_to_a_bot_at_once(client, ui, db, clock, world):  # noqa: F811
    """Только сессия хаба в авто, своего проекта, при снятом `bot_chat`; внешняя, ручная, чужой проект,
    человек с названием «@x_bot» — черновик."""
    w = world
    sid = await _auto(client, ui, w)
    manual = await _auto(client, ui, w, role="тестер", mode="manual")
    bot, _ = _chat(db, w, clock)
    foreign, _ = _chat(db, w, clock, ext="9200", project="p4")
    person, _ = _chat(db, w, clock, ext="9300", tg_type="user", title="@x_bot")

    assert (await _draft(client, w, bot, _me(sid)))["status"] == "pending_approval"  # стоит
    await _put(ui, "global", "bot_chat", False)
    assert (await _draft(client, w, bot, _me(manual)))["status"] == "pending_approval"
    await _sub(client, w, A, role="внешний")
    assert (await _draft(client, w, bot, {"session": _sess(A), "session_key": "k" * 64}))[
        "status"
    ] == "pending_approval"
    assert (await _draft(client, w, foreign, _me(sid)))["status"] == "pending_approval"
    assert (await _draft(client, w, person, _me(sid)))["status"] == "pending_approval"
    assert _audits(db, "outbox_autosend") == []

    sent = await _draft(client, w, bot, _me(sid))
    assert sent["status"] == "queued" and sent["auto"] == {"role": ROLE, "by": "bot_chat", "mode": None}
    assert _audits(db, "outbox_autosend")[-1]["by"] == "bot_chat"


# --- страховка (1.3, П4–П6, П9) -----------------------------------------------------------------------------


async def test_risky_texts_and_files_to_a_bot_go_at_once(client, ui, db, clock, world):  # noqa: F811
    """владелец 24.10: страховки текстов боту нет — оплата, коды, почта, удаление и файлы уходят сразу при снятом
    `bot_chat` и из списка «без подтверждения»; аудита `bot_guard` нет."""
    w = world
    sid = await _auto(client, ui, w)
    await _put(ui, "global", "bot_chat", False)
    bot, _ = _chat(db, w, clock, text="Чем помочь?")
    ask = insert_message(db, bot, ext_id="650", ts=_now(clock) - 1000, text="Списать 990 ₽?")
    assert ask
    yes = await _draft(client, w, bot, _me(sid), text="Да", reply_to="650")
    assert yes["status"] == "queued" and yes.get("auto_guard") is None, yes
    risky = ("4276 1234 5678 9012", "123 456", "пишите на me@example.com", "/stop", "Код сервиса пишут")
    for text in risky:
        body = await _draft(client, w, bot, _me(sid), text=text)
        assert (body["status"], body.get("auto_guard")) == ("queued", None), text

    async def _with_file(ident):
        r = await client.post(
            f"{API}/uploads?channel_id={bot}", content=PDF, headers={**_h(w["tok"]), "X-Filename": "a.pdf"}
        )
        assert r.status_code == 200, r.text
        return await _draft(client, w, bot, ident, text="договор", files=[r.json()["upload_id"]])

    assert (await _with_file(_me(sid)))["status"] == "queued"
    await _sub(client, w, A, role=ROLE)
    r = await ui.put(f"{API}/channels/{bot}/autosend", {"role": ROLE, "on": True})
    assert r.status_code == 200, r.text
    assert (await _with_file({"session": _sess(A)}))["status"] == "queued"
    listed = await _draft(client, w, bot, {"session": _sess(A)}, text="Удалите аккаунт")
    assert listed["status"] == "queued" and listed["auto"] == {"role": ROLE}
    assert _guard_audits(db) == []


async def test_explicit_paced_is_kept_in_bot_chat(client, ui, ic, db, clock, world):  # noqa: F811
    """Ревью Ф63-2: явный `paced=True` сессии в существующей беседе с ботом не снимается (снимается только
    неявный график первого сообщения); при выдаче страховки нет (владелец 24.10)."""
    w = world
    sid = await _auto(client, ui, w)
    await _put(ui, "global", "bot_chat", False)
    bot, _ = _chat(db, w, clock)
    body = await _draft(client, w, bot, _me(sid), text="Покажи тарифы", paced=True)
    assert body["status"] == "queued" and body["paced"] is True, body
    assert body["auto"] == {"role": ROLE, "by": "bot_chat", "mode": "paced"}
    row = _outbox(db, body["outbox_id"])
    assert row["paced"] == 1
    clock.advance_ms(row["not_before_ts"] - _now(clock) + 2000)
    h = _conn_headers(db, w["acc"])
    assert body["outbox_id"] in {i["outbox_id"] for i in await _lease_outbox(ic, h)}
    assert _guard_audits(db) == []


async def test_send_at_draft_to_bot_goes_on_lease(client, ui, ic, db, clock, world):  # noqa: F811
    """Черновик с `send_at`, ушедший веткой `bot_chat`, выдаётся в срок, даже если бот спросил об оплате
    (владелец 24.10: страховки нет)."""
    w = world
    sid = await _auto(client, ui, w)
    await _put(ui, "global", "bot_chat", False)
    h = _conn_headers(db, w["acc"])
    bot, _ = _chat(db, w, clock)
    when = _now(clock) + 30 * MIN
    body = await _draft(client, w, bot, _me(sid), text="Да", send_at=_iso(when))
    assert (body["status"], body["auto"]["by"], body["auto"]["mode"]) == ("queued", "bot_chat", "send_at")
    insert_message(db, bot, ext_id="702", ts=_now(clock) + 5 * MIN, text="Списать 990 ₽?")
    clock.advance_ms(31 * MIN)
    assert body["outbox_id"] in {i["outbox_id"] for i in await _lease_outbox(ic, h)}
    assert _guard_audits(db) == []


# --- нажатия кнопок (1.2, П4, П8) ---------------------------------------------------------------------------


def _kb(*texts: str) -> dict:
    rows = [
        [
            {"t": t, "k": "callback", "d": base64.b64encode(f"cb-{i}".encode()).decode()}
            for i, t in enumerate(texts)
        ]
    ]
    return {"kind": "inline", "rows": rows}


async def test_press_by_bot_chat_and_the_guard(client, ui, ic, db, clock, world):  # noqa: F811
    w = world
    sid = await _auto(client, ui, w)
    h = _conn_headers(db, w["acc"])
    _bot, menu = _chat(db, w, clock, text="Выбери раздел", kb=_kb("Тарифы", "Помощь", "Оплатить"))
    _pay, ask = _chat(db, w, clock, ext="9400", msg_ext="701", text="Списать 990 ₽?", kb=_kb("Да", "Нет"))

    # `bot_chat` стоит, `hub_press` стоит — карточка.
    first = await _press(client, w, menu, _me(sid), 2, text="Помощь")
    assert first["sent"] is False
    await _put(ui, "global", "bot_chat", False)
    sent = await _press(client, w, menu, _me(sid), 1, text="Тарифы")
    assert sent["sent"] is True and sent["press"]["auto_by"] == "autopilot"
    assert _audits(db, "press_auto")[-1]["press_id"] == sent["press"]["id"]
    with db.reader() as conn:
        result = conn.execute(
            "SELECT result FROM main.audit WHERE action = 'press_auto' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert result == "bot_chat"
    # П8: при выдаче не возвращается в карточку, хотя `hub_press` стоит.
    (item,) = await _lease(ic, h)
    assert item["id"] == sent["press"]["id"]

    # Владелец 24.10: страховки у нажатий нет — «Оплатить» и «Да» под «Списать 990 ₽?» уходят сразу.
    pay = await _press(client, w, menu, _me(sid), 3, text="Оплатить")
    assert pay["sent"] is True and pay["press"]["auto_by"] == "autopilot"
    yes = await _press(client, w, ask, _me(sid), 1, text="Да")
    assert yes["sent"] is True
    assert _guard_audits(db) == []

    # Группа (не личка с ботом): `bot_chat` ничего не даёт.
    group_msg = _chat_group(db, w, clock)
    assert (await _press(client, w, group_msg, _me(sid), 1, text="Тарифы"))["sent"] is False


def _chat_group(db, w, clock) -> int:
    now = _now(clock)
    event = {
        "type": "upsert",
        "channel": {"ext_id": "team", "kind": "group"},
        "ext_id": "880",
        "ts": now - MIN,
        "sender": {"ext_id": "777", "name": "Бот", "is_bot": True},
        "is_me": False,
        "kind": "text",
        "text": "Меню",
        "mentions_me": False,
        "buttons": _kb("Тарифы"),
        "buttons_ts": now,
    }
    result = db.write_sync(
        lambda conn: ingest.apply_batch(conn, account_id=w["acc"], events=[event], cursor=None, now_ms=now)
    )
    assert result.dead == 0, result
    with db.reader() as conn:
        return conn.execute(
            "SELECT m.id FROM main.message m JOIN main.channel c ON c.id = m.channel_id "
            "WHERE c.ext_id = 'team' AND m.ext_id = '880'"
        ).fetchone()[0]


async def test_press_guard_is_rechecked_on_lease(client, ui, ic, db, clock, world):  # noqa: F811
    """владелец 24.10: страховки у нажатий нет — бот переписал сообщение с кнопкой («Списать 990 ₽?») между
    нажатием и выдачей, нажатие всё равно выдаётся; П8: выключатель снова поставлен — карточка."""
    w = world
    sid = await _auto(client, ui, w)
    h = _conn_headers(db, w["acc"])
    await _put(ui, "global", "bot_chat", False)
    _bot, menu = _chat(db, w, clock, text="Выбери тариф", kb=_kb("Тариф А", "Тариф Б"))
    made = (await _press(client, w, menu, _me(sid), 1, text="Тариф А"))["press"]
    assert made["status"] == "approved"
    _chat(db, w, clock, text="Списать 990 ₽?", kb=_kb("Тариф А", "Тариф Б"), etype="edit", bump=5)
    (item,) = await _lease(ic, h)
    assert item["id"] == made["id"]
    assert _guard_audits(db) == []

    other = (await _press(client, w, menu, _me(sid), 2, text="Тариф Б"))["press"]
    assert other["status"] == "approved"  # сообщение про списание — всё равно сразу
    (item,) = await _lease(ic, h)
    assert item["id"] == other["id"]

    # Выключатель снова поставлен между нажатием и выдачей — карточка (без пометки страховки).
    _bot2, menu2 = _chat(db, w, clock, ext="9500", msg_ext="702", text="Выбери тариф", kb=_kb("Тариф В"))
    made = (await _press(client, w, menu2, _me(sid), 1, text="Тариф В"))["press"]
    assert made["status"] == "approved"
    await _put(ui, "global", "bot_chat", True)
    assert await _lease(ic, h) == []
    row = _row(db, made["id"])
    assert (row.status, row.auto, row.reason) == ("pending", 0, None)
    assert presses.BOT_SWITCH == "bot_chat"
