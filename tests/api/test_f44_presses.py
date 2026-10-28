"""Ф44-1c (docs/architecture.md–4.3, 7 и поправки П2–П4, П6–П8): нажатие
кнопки под сообщением Telegram-бота — ядро и ручки.

- проверки формы и устаревшей кнопки; владелец (кука) — сразу; сессия — карточка; решает только кука;
- без карточки — ровно два случая (список «без подтверждения» и выключатель `hub_press`) и их пределы;
- очередь коннектору: исходы, `unknown` без повтора, FloodWait, срок годности решения, перепроверка
  условия «сразу» и кнопки при выдаче, поздний отчёт;
- ответ бота — только владельцу и автору; сырой callback — нигде, кроме `lease`;
- чистка; сигнал исхода карточки автору (вид `press`, протокол ожидания 6).

Мир и помощники — из test_hub_sessions.py / test_f41_autopilot_5b.py; коннектор — внутреннее приложение на
той же БД и тех же часах."""

import base64
import json

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import connector_auth, ingest, presses, subs
from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.feed import Notifier

from . import test_hub_sessions as base
from .conftest import insert_account, insert_channel, insert_message
from .test_f41_autopilot import _put
from .test_f41_autopilot_5b import _auto, _me
from .test_hub_sessions import ROLE, A, B, _audits, _h, _now, _sess, _sub, _token

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"
INTERNAL = "/internal/v1"
ADMIN_SECRET = ADMIN_PREFIX + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHI"
RAW = "SECRETCALLBACK"
MIN = 60_000


def _d(n: int) -> str:
    return base64.b64encode(f"{RAW}-{n}".encode()).decode()


def _keyboard(n: int = 6, *, prefix: str = "К") -> dict:
    """Ряд 1 — `n` callback-кнопок «К1…Кn»; ряд 2 — ссылка, кнопка с паролем, «другое»."""
    return {
        "kind": "inline",
        "rows": [
            [{"t": f"{prefix}{i}", "k": "callback", "d": _d(i)} for i in range(1, n + 1)],
            [
                {"t": "Сайт", "k": "url", "u": "https://e.com/x"},
                {"t": "Удалить", "k": "callback", "d": _d(99), "pw": True},
                {"t": "Оплатить", "k": "other"},
            ],
        ],
    }


def _bot_msg(db, w, clock, *, chat="team", ext="500", kb=None, etype="upsert", bump=0) -> int:
    """Сообщение бота с кнопками в беседе мира — приёмом, как от коннектора; возвращает id сообщения."""
    now = _now(clock)
    event = {
        "type": etype,
        "channel": {"ext_id": chat, "kind": "group"},
        "ext_id": ext,
        "ts": now - MIN,
        "sender": {"ext_id": "777", "name": "Бот", "is_bot": True},
        "is_me": False,
        "kind": "text",
        "text": "Выбери действие",
        "mentions_me": False,
        "buttons": kb if kb is not None else _keyboard(),
        "buttons_ts": now + bump,
    }
    result = db.write_sync(
        lambda conn: ingest.apply_batch(conn, account_id=w["acc"], events=[event], cursor=None, now_ms=now)
    )
    assert result.dead == 0, result
    with db.reader() as conn:
        return conn.execute(
            "SELECT m.id FROM main.message m JOIN main.channel c ON c.id = m.channel_id "
            "WHERE c.ext_id = ? AND m.ext_id = ?",
            (chat, ext),
        ).fetchone()[0]


@pytest.fixture
async def ic(db, clock):
    notifier = Notifier()
    db.on_commit(notifier.notify)
    app = create_internal_app(db, clock=clock, notifier=notifier, admin_key_hash=hash_secret(ADMIN_SECRET))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://hub-internal") as c:
        yield c


def _conn_headers(db, account_id) -> dict:
    secret = connector_auth.generate_connector_secret()
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.connector_secret (account_id, hash, created_ts) VALUES (?, ?, 0)",
            (account_id, connector_auth.hash_secret(secret)),
        )
    )
    return {"Authorization": f"Bearer {secret}"}


async def _ui_press(ui, mid, col=1, *, row=1, text=None, expect=201) -> dict:  # noqa: F811
    body = {"row": row, "col": col, "text": text if text is not None else f"К{col}"}
    r = await ui.post(f"{API}/messages/{mid}/buttons/press", body)
    assert r.status_code == expect, r.text
    return r.json()


async def _press(client, w, mid, ident, col=1, *, row=1, text=None, token=None, expect=201) -> dict:
    body = {"row": row, "col": col, "text": text if text is not None else f"К{col}", **ident}
    r = await client.post(f"{API}/messages/{mid}/buttons/press", json=body, headers=_h(token or w["tok"], 6))
    assert r.status_code == expect, r.text
    return r.json()


async def _lease(ic, h) -> list[dict]:
    r = await ic.post(f"{INTERNAL}/button-presses/lease", headers=h, json={})
    assert r.status_code == 200, r.text
    return r.json()


async def _report(ic, h, item, outcome="done", *, expect=200, **extra) -> dict:
    body = {"lease_gen": item["lease_gen"], "outcome": outcome, **extra}
    r = await ic.post(f"{INTERNAL}/button-presses/{item['id']}/report", headers=h, json=body)
    assert r.status_code == expect, r.text
    return r.json()


async def _ready(ic, h) -> bool:
    r = await ic.get(f"{INTERNAL}/button-presses/wait", headers=h, params={"timeout_s": 0})
    assert r.status_code == 200, r.text
    return r.json()["ready"]


async def _one(ui, press_id) -> dict:  # noqa: F811
    r = await ui.get(f"{API}/button-presses/{press_id}")
    assert r.status_code == 200, r.text
    return r.json()


def _row(db, press_id) -> presses.Press:
    with db.reader() as conn:
        return presses.get(conn, press_id)


def _ident(who) -> dict:
    return {"session": _sess(who)}


async def _listed(client, ui, w, who=A, *, channel="ch", role=ROLE) -> dict:  # noqa: F811
    """Внешняя сессия `who` с ролью в проекте 3 и беседа в списке «без подтверждения» для этой роли."""
    sub = await _sub(client, w, who, role=role)
    r = await ui.put(f"{API}/channels/{w[channel]}/autosend", {"role": role, "on": True})
    assert r.status_code == 200, r.text
    return sub


# --- 4.2: проверки формы, устаревшая кнопка ----------------------------------------------------------------


async def test_form_checks_and_stale_button(client, ui, db, clock, world):  # noqa: F811
    w = world
    mid = _bot_msg(db, w, clock)
    # Текст другой, места нет — 409 `stale_button`; не callback, с паролем — 422; ряд и место — с 1.
    r = await _ui_press(ui, mid, 1, text="Не тот", expect=409)
    assert "stale_button" in r["detail"] and "перечитай" in r["detail"]
    assert "stale_button" in (await _ui_press(ui, mid, 7, expect=409))["detail"]
    assert "stale_button" in (await _ui_press(ui, mid, 1, row=3, text="К1", expect=409))["detail"]
    await _ui_press(ui, mid, 1, row=2, text="Сайт", expect=422)
    await _ui_press(ui, mid, 2, row=2, text="Удалить", expect=422)
    await _ui_press(ui, mid, 3, row=2, text="Оплатить", expect=422)
    await _ui_press(ui, mid, 0, expect=422)
    # Нет сообщения, сообщение без кнопок, скрытое — 404 / 409; не Telegram — 422.
    await _ui_press(ui, 999_999, 1, expect=404)
    assert "stale_button" in (await _ui_press(ui, w["last"], 1, expect=409))["detail"]
    slack = insert_account(db, source="slack")
    sch = insert_channel(db, slack, project_id=w["p3"], ext_id="C1", kind="group", title="Slack")
    smid = insert_message(db, sch, ext_id="1.1", ts=_now(clock), text="x")
    kb = json.dumps(_keyboard())
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.message SET buttons_json = ? WHERE id = ?", (kb, smid))
    )
    await _ui_press(ui, smid, 1, expect=422)
    db.write_sync(lambda conn: conn.execute("UPDATE main.message SET hidden = 1 WHERE id = ?", (mid,)))
    await _ui_press(ui, mid, 1, expect=404)
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.button_press").fetchone()[0] == 0


async def test_keyboard_changed_between_read_and_press(client, ui, db, clock, world):  # noqa: F811
    """П4: сессия прочла «К1», бот сменил клавиатуру — 409, строки нет."""
    w = world
    mid = _bot_msg(db, w, clock)
    _bot_msg(db, w, clock, kb=_keyboard(2, prefix="Новая"), etype="edit", bump=5)
    r = await _press(client, w, mid, _ident(A), 1, expect=409)
    assert "stale_button" in r["detail"]
    assert (await _press(client, w, mid, _ident(A), 1, text="Новая1"))["press"]["status"] == "pending"


# --- кука: сразу; П2: одна живая строка --------------------------------------------------------------------


async def test_ui_click_goes_at_once_double_click_is_one_row(client, ui, ic, db, clock, world):  # noqa: F811
    w = world
    h = _conn_headers(db, w["acc"])
    mid = _bot_msg(db, w, clock)
    assert not await _ready(ic, h)
    first = await _ui_press(ui, mid, 1)
    press = first["press"]
    assert first["created"] is True and "sent" not in first
    assert (press["status"], press["by"], press["auto"], press["text"]) == ("approved", "ui", False, "К1")
    again = await _ui_press(ui, mid, 1, expect=200)  # двойной клик
    assert again["created"] is False and again["press"]["id"] == press["id"]
    # Сессия на ту же кнопку — та же строка (П2), не вторая.
    dup = await _press(client, w, mid, _ident(A), 1, expect=200)
    assert dup["created"] is False and dup["press"]["id"] == press["id"] and "result" not in dup["press"]

    assert await _ready(ic, h)
    (item,) = await _lease(ic, h)
    assert item == {
        "id": press["id"],
        "lease_gen": 1,
        "channel_ext_id": "team",
        "message_ext_id": "500",
        "data_b64": _d(1),
    }
    assert await _lease(ic, h) == [] and (await _one(ui, press["id"]))["status"] == "leased"
    await _ui_press(ui, mid, 1, expect=200)  # пока строка жива — она же
    long_text = "о" * 700
    await _report(ic, h, item, result={"message": long_text, "alert": True, "url": "https://pay.example/1"})
    done = await _one(ui, press["id"])
    assert done["status"] == "done"
    assert done["result"] == {"message": "о" * 500, "alert": True, "url": "https://pay.example/1"}
    # После исхода кнопку можно нажать снова — новая строка.
    assert (await _ui_press(ui, mid, 1))["press"]["id"] != press["id"]

    # Сырой callback — нигде, кроме `lease`: ни в ответах, ни в /sync, ни в аудите.
    sync = (await ui.get(f"{API}/sync", params={"since": 0})).json()
    listing = (await ui.get(f"{API}/button-presses")).json()
    assert [p["id"] for p in sync["presses"]][0] == press["id"] and sync["presses"][0]["result"]
    with db.reader() as conn:
        audit = conn.execute("SELECT action, detail_json FROM main.audit ORDER BY id").fetchall()
    assert {"press_create"} <= {a for a, _d2 in audit}
    for blob in (first, again, dup, done, sync, listing, audit):
        text = json.dumps(blob, ensure_ascii=False)
        assert _d(1) not in text and RAW not in text and "data_b64" not in text
    # Ответ бота и текст кнопки в аудит не пишутся.
    assert "pay.example" not in json.dumps(audit, ensure_ascii=False)
    assert "К1" not in json.dumps(audit, ensure_ascii=False)
    # Сессии /sync нажатий не несёт (в строке — ответ бота).
    r = await client.get(f"{API}/sync", params={"since": 0}, headers=_h(w["tok"]))
    assert "presses" not in r.json()


# --- bearer: карточка; решает только кука ------------------------------------------------------------------


async def test_session_press_is_a_card_and_only_cookie_decides(client, ui, ic, db, clock, world):  # noqa: F811
    w = world
    h = _conn_headers(db, w["acc"])
    mid = _bot_msg(db, w, clock)
    made = await _press(client, w, mid, _ident(A), 1)
    press = made["press"]
    assert (made["sent"], made["created"]) == (False, True)
    assert (press["status"], press["by"], press["auto"]) == ("pending", "session", False)
    assert press["channel_title"] == "Команда" and press["quote"] == "Выбери действие"
    assert await _lease(ic, h) == [] and not await _ready(ic, h)

    # Решение — только кука: bearer 403 (и запись `ui_only_denied`).
    for action in ("approve", "decline"):
        r = await client.post(f"{API}/button-presses/{press['id']}/{action}", headers=_h(w["tok"]))
        assert r.status_code == 403, r.text
    assert _row(db, press["id"]).status == "pending"

    r = await ui.post(f"{API}/button-presses/{press['id']}/approve")
    assert r.status_code == 200 and r.json()["status"] == "approved"
    r = await ui.post(f"{API}/button-presses/{press['id']}/decline")
    assert r.status_code == 409  # решено
    (item,) = await _lease(ic, h)
    assert item["id"] == press["id"]

    other = (await _press(client, w, mid, _ident(A), 2))["press"]
    r = await ui.post(f"{API}/button-presses/{other['id']}/decline")
    assert r.json()["status"] == "declined"
    assert (await ui.post(f"{API}/button-presses/{other['id']}/approve")).status_code == 409
    assert (await ui.post(f"{API}/button-presses/999/approve")).status_code == 404
    assert [a["by"] for a in _audits(db, "press_decide")] == ["session", "session"]
    assert _audits(db, "press_auto") == []


async def test_bearer_refusals(client, ui, db, clock, world):  # noqa: F811
    w = world
    mid = _bot_msg(db, w, clock)
    # Без `session` — 422; ключ без сессии — 422; кука с `session` — 422.
    await _press(client, w, mid, {}, 1, expect=422)
    await _press(client, w, mid, {"session_key": "k" * 64}, 1, expect=422)
    r = await ui.post(f"{API}/messages/{mid}/buttons/press", {"row": 1, "col": 1, "text": "К1", **_ident(A)})
    assert r.status_code == 422
    # Дежурная, оркестратор, фоновый токен, токен без `draft` — 403.
    for name, kind, scopes in (
        ("mcp:wake-all", "interactive", ("read", "draft")),
        ("mcp:orchestrator", "interactive", ("read", "draft")),
        ("mcp:bg", "background", ("read", "draft")),
        ("mcp:ro", "interactive", ("read",)),
    ):
        token, _ = _token(db, name=name, kind=kind, scopes=scopes)
        await _press(client, w, mid, _ident(A), 1, token=token, expect=403)
    # Проект беседы токену не виден — 404.
    narrow, _ = _token(db, name="mcp:initech", project_ids=[w["p4"]])
    await _press(client, w, mid, _ident(A), 1, token=narrow, expect=404)
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.button_press").fetchone()[0] == 0


# --- 4.3, случай 1: список «без подтверждения» -------------------------------------------------------------


async def test_list_case_goes_at_once_with_its_own_limits(client, ui, ic, db, clock, world):  # noqa: F811
    w = world
    h = _conn_headers(db, w["acc"])
    mid = _bot_msg(db, w, clock)
    mid2 = _bot_msg(db, w, clock, chat="other", ext="600")
    sub = await _listed(client, ui, w)

    made = await _press(client, w, mid, _ident(A), 1)
    assert made["sent"] is True and made["press"]["status"] == "approved"
    assert (made["press"]["auto"], made["press"]["auto_by"], made["press"]["role"]) == (True, "list", ROLE)
    (audit,) = _audits(db, "press_auto")
    assert (audit["sub_id"], audit["role"], audit["session_id"]) == (sub["sub"]["id"], ROLE, A[2])
    (item,) = await _lease(ic, h)
    assert item["id"] == made["press"]["id"]

    # Каждое условие по отдельности не выполнено — карточка: беседа не в списке, другая роль, сессия без
    # подписки, беседа без проекта.
    assert (await _press(client, w, mid2, _ident(A), 1))["sent"] is False
    await _sub(client, w, B, role="тестер")
    assert (await _press(client, w, mid, _ident(B), 2))["sent"] is False
    stranger = {"session": {"pid": base.EXTRA[0][0], "proc_start": base.EXTRA[0][1], "session_id": "s-x"}}
    assert (await _press(client, w, mid, stranger, 3))["sent"] is False
    insert_channel(db, w["acc"], project_id=None, ext_id="loose", kind="group", title="Ничья")
    loose = _bot_msg(db, w, clock, chat="loose", ext="700")
    assert (await _press(client, w, loose, _ident(A), 1))["sent"] is False

    # Пределы — по нажатиям «сразу», отдельно от сообщений: 40 в час на беседу (Ф66-1, было 20), 60 на токен.
    per_hour = presses.PRESS_PER_HOUR
    assert (per_hour, presses.PRESS_TOKEN_PER_HOUR) == (40, 60)
    presses.PRESS_PER_HOUR = 1
    try:
        denied = await _press(client, w, mid, _ident(A), 4)
    finally:
        presses.PRESS_PER_HOUR = per_hour
    assert (denied["sent"], denied["auto_denied"], denied["auto_limit"]) == (False, "limit", "channel")
    assert denied["press"]["status"] == "pending" and denied["press"]["auto"] is False
    presses.PRESS_TOKEN_PER_HOUR = 1
    try:
        denied = await _press(client, w, mid, _ident(A), 5)
    finally:
        presses.PRESS_TOKEN_PER_HOUR = 60
    assert (denied["sent"], denied["auto_limit"]) == (False, "token")
    # Через час окно свободно.
    clock.advance_ms(61 * MIN)
    mid3 = _bot_msg(db, w, clock, ext="501")
    presses.PRESS_PER_HOUR = 1
    try:
        assert (await _press(client, w, mid3, _ident(A), 1))["sent"] is True
    finally:
        presses.PRESS_PER_HOUR = per_hour
    # Сообщения авто-отправки нажатиями не считаются и наоборот: в outbox пусто.
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.outbox").fetchone()[0] == 0


async def test_list_is_closed_for_a_partner_with_autosend_off(client, ui, db, clock, world):  # noqa: F811
    """П7: напарник (сессия хаба с `autosend_ok = 0`) по списку не нажимает — та же `_list_closed`, что у
    черновика; ветку выключателя держит выключатель."""
    w = world
    mid = _bot_msg(db, w, clock)
    sid = await _auto(client, ui, w, mode="manual")
    r = await ui.put(f"{API}/channels/{w['ch']}/autosend", {"role": ROLE, "on": True})
    assert r.status_code == 200, r.text
    assert (await _press(client, w, mid, _me(sid, None), 1))["sent"] is True
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.hub_session SET autosend_ok = 0 WHERE id = ?", (sid,))
    )
    closed = await _press(client, w, mid, _me(sid, None), 2)
    assert closed["sent"] is False and "auto_denied" not in closed


# --- 4.3, случай 2: выключатель `hub_press` ----------------------------------------------------------------


async def test_autopilot_case_each_refusal_and_limits(client, ui, ic, db, clock, world):  # noqa: F811
    w = world
    mid = _bot_msg(db, w, clock, kb=_keyboard(12))
    sid = await _auto(client, ui, w)
    manual = await _auto(client, ui, w, role="тестер", mode="manual")
    col = iter(range(1, 13))

    async def go(ident, **kw):
        return await _press(client, w, mid, ident, next(col), **kw)

    # Выключатель стоит (по умолчанию) — карточка; ручной режим — карточка и при снятом.
    assert (await go(_me(sid)))["sent"] is False
    await _put(ui, "global", "hub_press", False)
    assert (await go(_me(manual)))["sent"] is False
    # Ключ не дошёл, чужой ключ, внешняя сессия с ролью — карточка.
    assert (await go(_me(sid, None)))["sent"] is False
    assert (await go(_me(sid, "x" * 64)))["sent"] is False
    await _sub(client, w, A, role="внешний")
    assert (await go({"session": _sess(A), "session_key": "k" * 64}))["sent"] is False
    assert _audits(db, "press_auto") == []
    # Беседа чужого проекта и беседа без проекта — карточка.
    insert_channel(db, w["acc"], project_id=w["p4"], ext_id="p4-chat", kind="group", title="Чужая")
    foreign = _bot_msg(db, w, clock, chat="p4-chat", ext="800")
    assert (await _press(client, w, foreign, _me(sid), 1))["sent"] is False

    # Удачный путь.
    sent = await go(_me(sid))
    assert sent["sent"] is True and sent["press"]["auto_by"] == "autopilot"
    (audit,) = _audits(db, "press_auto")
    assert (audit["hub_session_id"], audit["role"], audit["by"]) == (sid, ROLE, "session")
    assert _row(db, sent["press"]["id"]).autopilot_session == sid

    # Пределы — как у `_try_autopilot`: на беседу, 60 в час и 300 в сутки на сессию.
    assert (presses.PRESS_SESSION_PER_HOUR, presses.PRESS_SESSION_PER_DAY) == (60, 300)
    for name, limit_of in (
        ("PRESS_PER_HOUR", "channel"),
        ("PRESS_SESSION_PER_HOUR", "session"),
        ("PRESS_SESSION_PER_DAY", "session_day"),
    ):
        saved = getattr(presses, name)
        setattr(presses, name, 1)
        try:
            denied = await go(_me(sid))
        finally:
            setattr(presses, name, saved)
        assert (denied["sent"], denied["auto_denied"], denied["auto_limit"]) == (False, "limit", limit_of)

    # Проект придержал выключатель — карточка; снял — снова сразу.
    await _put(ui, w["p3"], "hub_press", True)
    assert (await go(_me(sid)))["sent"] is False
    await _put(ui, w["p3"], "hub_press", None)
    assert (await go(_me(sid)))["sent"] is True


# --- П3: срок, перепроверка при выдаче ----------------------------------------------------------------------


async def test_approved_expires_and_keyboard_is_rechecked_on_lease(client, ui, ic, db, clock, world):  # noqa: F811
    w = world
    h = _conn_headers(db, w["acc"])
    mid = _bot_msg(db, w, clock)
    stale = (await _ui_press(ui, mid, 1))["press"]
    clock.advance_ms(59_000)
    late = (await _ui_press(ui, mid, 2))["press"]
    clock.advance_ms(2_000)  # первой 61 с — просрочена, второй 2 с
    assert await _ready(ic, h)
    (item,) = await _lease(ic, h)
    assert item["id"] == late["id"]
    expired = await _one(ui, stale["id"])
    assert (expired["status"], expired["reason"]) == ("expired", "not_sent_in_time")
    await _report(ic, h, item, result={})

    # Просрочку видит и чтение без коннектора (опрос интерфейса и MCP).
    lonely = (await _ui_press(ui, mid, 3))["press"]
    clock.advance_ms(61_000)
    assert (await _one(ui, lonely["id"]))["status"] == "expired"

    # Клавиатура сменилась между решением и выдачей — `failed`, `stale_button`, коннектору не выдаётся.
    changed = (await _ui_press(ui, mid, 4))["press"]
    _bot_msg(db, w, clock, kb=_keyboard(4, prefix="Иная"), etype="edit", bump=5)
    assert await _lease(ic, h) == []
    failed = await _one(ui, changed["id"])
    assert (failed["status"], failed["reason"]) == ("failed", "stale_button")

    # Карточка: срок идёт от решения владельца, не от создания.
    card = (await _press(client, w, mid, _ident(A), 1, text="Иная1"))["press"]
    clock.advance_ms(10 * MIN)
    assert await _lease(ic, h) == []
    assert (await ui.post(f"{API}/button-presses/{card['id']}/approve")).status_code == 200
    clock.advance_ms(30_000)
    (item,) = await _lease(ic, h)
    assert item["id"] == card["id"]


async def test_auto_condition_is_rechecked_on_lease(client, ui, ic, db, clock, world):  # noqa: F811
    """П3: между «сразу» и выдачей владелец убрал роль из списка / поставил выключатель — строка становится
    карточкой и уходит только по его решению."""
    w = world
    h = _conn_headers(db, w["acc"])
    mid = _bot_msg(db, w, clock)
    await _listed(client, ui, w)
    made = (await _press(client, w, mid, _ident(A), 1))["press"]
    assert made["status"] == "approved"
    r = await ui.put(f"{API}/channels/{w['ch']}/autosend", {"role": ROLE, "on": False})
    assert r.status_code == 200, r.text
    assert await _lease(ic, h) == []
    card = await _one(ui, made["id"])
    assert (card["status"], card["auto"]) == ("pending", False)
    assert [a["press_id"] for a in _audits(db, "press_auto")] == [made["id"], made["id"]]
    clock.advance_ms(5 * MIN)  # карточка не сгорает за минуту
    assert await _lease(ic, h) == []
    assert (await ui.post(f"{API}/button-presses/{made['id']}/approve")).status_code == 200
    (item,) = await _lease(ic, h)
    assert item["id"] == made["id"]
    await _report(ic, h, item, result={})

    # То же с выключателем автопилота.
    sid = await _auto(client, ui, w, role="авто")
    await _put(ui, "global", "hub_press", False)
    auto = (await _press(client, w, mid, _me(sid), 2))["press"]
    assert (auto["status"], auto["auto_by"]) == ("approved", "autopilot")
    await _put(ui, "global", "hub_press", True)
    assert await _lease(ic, h) == []
    assert (await _one(ui, auto["id"]))["status"] == "pending"
    # Подписка сессии завершена — условие списка тоже не выполняется.
    r = await ui.put(f"{API}/channels/{w['ch']}/autosend", {"role": ROLE, "on": True})
    assert r.status_code == 200, r.text
    third = (await _press(client, w, mid, _ident(A), 3))["press"]
    assert third["auto_by"] == "list"
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.session_sub SET ended_ts = 1 WHERE session_pid = ? AND ended_ts IS NULL", (A[0],)
        )
    )
    assert await _lease(ic, h) == []
    assert (await _one(ui, third["id"]))["status"] == "pending"


# --- 4.4: исходы коннектора ---------------------------------------------------------------------------------


async def test_connector_outcomes_unknown_is_never_retried(client, ui, ic, db, clock, world):  # noqa: F811
    w = world
    h = _conn_headers(db, w["acc"])
    other_acc = insert_account(db, source="telegram")
    other = _conn_headers(db, other_acc)
    mid = _bot_msg(db, w, clock)

    async def leased(col):
        press = (await _ui_press(ui, mid, col))["press"]
        (item,) = await _lease(ic, h)
        assert item["id"] == press["id"]
        return item

    # Бот на запрос не ответил — это `done` с `timeout`.
    item = await leased(1)
    assert await _lease(ic, other) == []
    await _report(ic, other, item, expect=404)  # чужой аккаунт
    await _report(ic, h, {**item, "lease_gen": 7}, expect=409)  # не тот лиз
    assert (await _report(ic, h, item, result={"timeout": True, "message": "x"}))["status"] == "done"
    assert (await _one(ui, item["id"]))["result"] == {"timeout": True}
    await _report(ic, h, item, expect=409)  # строка закрыта

    # Устаревшая кнопка по ответу Telegram.
    item = await leased(2)
    await _report(ic, h, item, "failed", reason="stale_button")
    failed = await _one(ui, item["id"])
    assert (failed["status"], failed["reason"], failed["result"]) == ("failed", "stale_button", None)

    # Обрыв после отправки — `unknown`; в очередь строка не возвращается никогда.
    item = await leased(3)
    await _report(ic, h, item, "unknown", reason="step_timeout")
    assert (await _one(ui, item["id"]))["status"] == "unknown"
    clock.advance_ms(10 * MIN)
    assert await _lease(ic, h) == [] and not await _ready(ic, h)

    # Истёкший лиз — тоже `unknown`, не возврат в очередь; поздний отчёт с верным лизом — уточнение.
    item = await leased(4)
    clock.advance_ms(presses.LEASE_MS + 1)
    assert await _ready(ic, h)
    assert await _lease(ic, h) == []
    lost = await _one(ui, item["id"])
    assert (lost["status"], lost["reason"]) == ("unknown", "lease_expired")
    assert await _lease(ic, h) == [] and _row(db, item["id"]).attempts == 1
    await _report(ic, h, {**item, "lease_gen": 2}, expect=409)
    assert (await _report(ic, h, item, result={"message": "готово"}))["status"] == "done"
    assert (await _one(ui, item["id"]))["result"] == {"message": "готово"}
    await _report(ic, h, item, expect=409)  # уточнение — один раз

    # Просроченный лиз закрывает и часовая чистка.
    item = await leased(5)
    clock.advance_ms(presses.LEASE_MS + 1)
    now = _now(clock)
    db.write_sync(lambda conn: presses.cleanup(conn, now_ms=now))
    assert _row(db, item["id"]).status == "unknown"


async def test_flood_wait_pauses_or_fails_by_the_deadline(client, ui, ic, db, clock, world):  # noqa: F811
    w = world
    h = _conn_headers(db, w["acc"])
    mid = _bot_msg(db, w, clock)
    first = (await _ui_press(ui, mid, 1))["press"]
    second = (await _ui_press(ui, mid, 2))["press"]
    (item,) = await _lease(ic, h)
    # Короткий FloodWait: строка ждёт паузу и уходит снова; пауза — на весь аккаунт.
    r = await _report(ic, h, item, "retry", reason="flood", retry_after_ms=10_000)
    assert r["status"] == "approved"
    assert await _lease(ic, h) == [] and not await _ready(ic, h)
    third = (await _ui_press(ui, mid, 3))["press"]  # новая строка паузу не обходит
    assert await _lease(ic, h) == []
    clock.advance_ms(10_000)
    (item,) = await _lease(ic, h)
    assert (item["id"], item["lease_gen"]) == (first["id"], 2)
    # FloodWait длиннее остатка срока — `failed`, `flood`, без возврата; ждущие, которым не успеть, — тоже.
    r = await _report(ic, h, item, "retry", reason="flood", retry_after_ms=5 * MIN)
    assert r["status"] == "failed"
    assert await _lease(ic, h) == []
    for press_id in (first["id"], second["id"], third["id"]):
        got = await _one(ui, press_id)
        assert (got["status"], got["reason"]) == ("failed", "flood"), got
    # Запрос не ушёл не из-за FloodWait (нет связи) — короткая пауза, потом снова.
    fourth = (await _ui_press(ui, mid, 4))["press"]
    assert (await _one(ui, fourth["id"]))["status"] == "failed"  # пауза аккаунта ещё идёт
    clock.advance_ms(5 * MIN)
    fifth = (await _ui_press(ui, mid, 5))["press"]
    (item,) = await _lease(ic, h)
    assert (await _report(ic, h, item, "retry", reason="no_connection"))["status"] == "approved"
    clock.advance_ms(presses.RETRY_MS)
    (item,) = await _lease(ic, h)
    assert item["id"] == fifth["id"]
    await _report(ic, h, item, result={})
    assert (await _one(ui, fifth["id"]))["result"] == {}


# --- П8: ответ бота — только автору -------------------------------------------------------------------------


async def test_bot_answer_is_only_for_the_author_and_owner(client, ui, ic, db, clock, world):  # noqa: F811
    w = world
    h = _conn_headers(db, w["acc"])
    mid = _bot_msg(db, w, clock)
    await _listed(client, ui, w)
    press = (await _press(client, w, mid, _ident(A), 1))["press"]
    (item,) = await _lease(ic, h)
    await _report(ic, h, item, result={"message": "ключ ABC-123"})

    async def listing(who=None, token=None):
        params = {"pid": who[0], "proc_start": who[1], "session_id": who[2]} if who else {}
        r = await client.get(f"{API}/button-presses", params=params, headers=_h(token or w["tok"]))
        assert r.status_code == 200, r.text
        return r.json()

    async def one(who=None, token=None):
        params = {"pid": who[0], "proc_start": who[1], "session_id": who[2]} if who else {}
        return await client.get(
            f"{API}/button-presses/{press['id']}", params=params, headers=_h(token or w["tok"])
        )

    (mine,) = await listing(A)
    assert mine["id"] == press["id"] and mine["result"] == {"message": "ключ ABC-123"}
    assert (await one(A)).json()["result"] == {"message": "ключ ABC-123"}
    # Сессия другого проекта с ТЕМ ЖЕ токеном (другая тройка) и запрос без тройки — ничего.
    await _sub(client, w, B, role="брокер", cwd=w["dir4"])
    assert await listing(B) == [] and await listing() == []
    assert (await one(B)).status_code == 404 and (await one()).status_code == 404
    # Своя тройка, но проект токену не виден — тоже ничего.
    narrow, _ = _token(db, name="mcp:initech", project_ids=[w["p4"]])
    assert await listing(A, narrow) == [] and (await one(A, narrow)).status_code == 404
    # Владелец видит всё.
    assert (await _one(ui, press["id"]))["result"] == {"message": "ключ ABC-123"}
    assert [p["id"] for p in (await ui.get(f"{API}/button-presses")).json()] == [press["id"]]
    r = await ui.get(f"{API}/button-presses", params={"status": "pending"})
    assert r.json() == []


# --- чистка -------------------------------------------------------------------------------------------------


async def test_cleanup_expires_cards_drops_old_rows_and_scrubs_purged(client, ui, ic, db, clock, world):  # noqa: F811
    w = world
    h = _conn_headers(db, w["acc"])
    mid = _bot_msg(db, w, clock)
    mid2 = _bot_msg(db, w, clock, ext="501")
    done = (await _ui_press(ui, mid, 1))["press"]
    (item,) = await _lease(ic, h)
    await _report(ic, h, item, result={"message": "ответ"})
    card = (await _press(client, w, mid, _ident(A), 2))["press"]
    keep = (await _press(client, w, mid2, _ident(A), 1))["press"]

    def clean():
        now = _now(clock)
        return db.write_sync(lambda conn: presses.cleanup(conn, now_ms=now))

    clock.advance_ms(23 * 60 * MIN)
    assert clean() == {"swept": 0, "expired": 0, "removed": 0, "scrubbed": 0}
    # Сообщение стёрто — снимок кнопки, callback и ответ бота обнуляются; ждущая карточка закрывается.
    db.write_sync(lambda conn: conn.execute("UPDATE main.message SET purged = 1 WHERE id = ?", (mid,)))
    assert clean()["scrubbed"] == 2
    for press_id in (done["id"], card["id"]):
        row = _row(db, press_id)
        assert (row.text, row.data_b64, row.result_json) == (None, None, None)
    assert (_row(db, card["id"]).status, _row(db, card["id"]).reason) == ("failed", "stale_button")
    assert _row(db, done["id"]).status == "done" and clean()["scrubbed"] == 0
    # Карточка старше суток — `expired`.
    clock.advance_ms(61 * MIN)
    assert clean()["expired"] == 1
    assert (_row(db, keep["id"]).status, _row(db, keep["id"]).reason) == ("expired", "not_decided")
    # Строки старше 30 дней — долой.
    clock.advance_ms(29 * 24 * 60 * MIN)
    assert clean()["removed"] == 3
    assert _row(db, done["id"]) is None and _row(db, keep["id"]) is None


# --- 4.5: исход карточки — автору сигналом ------------------------------------------------------------------


async def _wait(client, w, sub_id, who, proto=6):
    r = await client.post(
        f"{API}/subs/{sub_id}/wait",
        json={"session": _sess(who), "waiter": "0123456789abcdef", "timeout_s": 1},
        headers=_h(w["tok"], proto),
    )
    assert r.status_code == 200, r.text
    return r.json().get("signals") or []


async def test_card_outcome_is_signalled_to_the_author_without_text(client, ui, ic, db, clock, world):  # noqa: F811
    w = world
    h = _conn_headers(db, w["acc"])
    mid = _bot_msg(db, w, clock)
    sub_id = (await _sub(client, w, A, role=ROLE))["sub"]["id"]
    declined = (await _press(client, w, mid, _ident(A), 1))["press"]
    pressed = (await _press(client, w, mid, _ident(A), 2))["press"]
    assert await _wait(client, w, sub_id, A) == []  # карточка ждёт — сигнала нет
    await ui.post(f"{API}/button-presses/{declined['id']}/decline")
    await ui.post(f"{API}/button-presses/{pressed['id']}/approve")
    (item,) = await _lease(ic, h)
    await _report(ic, h, item, result={"message": "секретный ответ бота"})
    # Клиент протокола 5 вида `press` не знает — ему сигнал не выдаётся и не сгорает.
    assert await _wait(client, w, sub_id, A, proto=5) == []
    signals = await _wait(client, w, sub_id, A)
    assert signals == [
        {"kind": "press", "press_id": declined["id"], "channel_id": w["ch"], "outcome": "declined"},
        {"kind": "press", "press_id": pressed["id"], "channel_id": w["ch"], "outcome": "done"},
    ]
    assert await _wait(client, w, sub_id, A) == []  # один раз
    delivered = [a for a in _audits(db, "sub_signal") if a.get("kind") == "press"]
    assert len(delivered) == 2 and "секретный" not in json.dumps(delivered, ensure_ascii=False)

    # Нажатие, ушедшее сразу, сигнала не даёт (исхода ждёт сам вызов `hub_press`); нажатие владельца — тоже.
    r = await ui.put(f"{API}/channels/{w['ch']}/autosend", {"role": ROLE, "on": True})
    assert r.status_code == 200, r.text
    auto = (await _press(client, w, mid, _ident(A), 3))["press"]
    assert auto["auto"] is True
    (item,) = await _lease(ic, h)
    await _report(ic, h, item, result={})
    await _ui_press(ui, mid, 4)
    (item,) = await _lease(ic, h)
    await _report(ic, h, item, result={})
    assert await _wait(client, w, sub_id, A) == []
    # Сессия без подписки карточку завести может, сигнала не получит (читает `hub_presses`).
    assert subs.MAX_SIGNALS_PER_DELIVERY >= 2
