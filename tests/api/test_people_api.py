"""Ф21-1 (docs/architecture.md): ручки людей `routes/people.py` — раздел E и поправки П6, П10
(обязательные тесты I.6, I.13–I.15, I.18 (`recipients`), I.19, I.24, I.28). Решения владельца — через публичное
приложение под cookie-сессией, сессии — bearer-токеном отдельным клиентом."""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import hiding, people

from .conftest import BASE_URL, csrf_headers, insert_project, login_via_code, make_bearer_token
from .people_helpers import (
    DAY_MS,
    MIN_MS,
    NOW_MS,
    account,
    channel,
    contact,
    link,
    msg,
    person_of,
    recompute,
    slack_people,
)

API = "/api/v1"
BASE = NOW_MS - 30 * DAY_MS


@pytest.fixture
async def ui(client, db, clock):
    cookie, _ = await login_via_code(client, db, clock)
    return csrf_headers(cookie)


@pytest.fixture
async def bearer_client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as c:
        yield c


def _bearer(db, *, project_ids=None, scopes=("read", "draft")) -> dict:
    return {"Authorization": f"Bearer {make_bearer_token(db, scopes=scopes, project_ids=project_ids)}"}


def _audit(db, action) -> list[dict]:
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT actor, result, detail_json FROM main.audit WHERE action = ? ORDER BY id", (action,)
        ).fetchall()
    return [{"actor": r[0], "result": r[1], "detail": json.loads(r[2]) if r[2] else None} for r in rows]


@pytest.fixture
def world(db):
    """Пётр: Telegram (личка, проект 1) + почта рабочего ящика (группа писем, проект 2); Кира — Slack."""
    p1 = insert_project(db, name="Globex")
    p2 = insert_project(db, name="Личное")
    tg = account(db, "telegram", title="Мой Telegram")
    box = account(db, "mail", schema="work", config={"address": "me@corp.io"}, title="Почта")
    sl = account(db, "slack", schema="work", title="Slack")
    petr_tg = contact(db, tg, "100", "Пётр Иванов", "@petr")
    petr_mail = contact(db, box, "petr@client.ru", "Пётр И.", "petr@client.ru", schema="work")
    kira = contact(db, sl, "U0KIRA", "Кира", "@kira", schema="work")
    dm = channel(db, tg, "100", project_id=p1, title="Пётр")
    thread = channel(db, box, "1", kind="mail_thread", schema="work", project_id=p2, title="Договор")
    kdm = channel(db, sl, "D1", schema="work", project_id=p1, title="Кира")
    msg(db, dm, ts=BASE, sender=petr_tg)
    ex_chat = msg(db, dm, ts=BASE + 5 * MIN_MS, me=True, text="Привет, ты договор видел?")
    msg(db, thread, ts=BASE, sender=petr_mail)
    ex_mail = msg(
        db,
        thread,
        ts=BASE + 9 * MIN_MS,
        me=True,
        text="Добрый день, отправляю Вам договор.",
        mail_to=["petr@client.ru"],
    )
    msg(db, kdm, ts=BASE, sender=kira)
    link(db)
    db.write_sync(lambda conn: people.merge(conn, person_of(db, petr_tg), [person_of(db, petr_mail)]))
    recompute(db)
    return {
        "p1": p1,
        "p2": p2,
        "tg": tg,
        "box": box,
        "sl": sl,
        "petr": person_of(db, petr_tg),
        "petr_tg": petr_tg,
        "petr_mail": petr_mail,
        "kira": person_of(db, kira),
        "kira_contact": kira,
        "dm": dm,
        "thread": thread,
        "kdm": kdm,
        "ex_chat": ex_chat,
        "ex_mail": ex_mail,
    }


# --- E2: карточка ------------------------------------------------------------------------------------------


async def test_e2_card_ui(client, ui, world):
    r = await client.get(f"{API}/people/{world['petr']}", headers=ui)
    assert r.status_code == 200, r.text
    card = r.json()
    assert card["id"] == world["petr"] and card["known"] is True
    # E3: у обоих контактов in + out = 2 — имя контакта с более свежим last_ts (письмо).
    assert card["name"] == "Пётр И." and set(card["names"]) == {"Пётр Иванов", "Пётр И."}
    assert {c["contact_id"] for c in card["contacts"]} == {world["petr_tg"], world["petr_mail"]}
    tg_contact = next(c for c in card["contacts"] if c["source"] == "telegram")
    assert tg_contact == {
        "contact_id": world["petr_tg"],
        "account_id": world["tg"],
        "source": "telegram",
        "account_title": "Мой Telegram",
        "name": "Пётр Иванов",
        "handle": "@petr",
        "email": None,
        "manual": True,
    }
    assert card["projects"] == [
        {"id": world["p1"], "title": "Globex"},
        {"id": world["p2"], "title": "Личное"},
    ]
    assert {c["channel_id"] for c in card["channels"]} == {world["dm"], world["thread"]}
    stats = card["stats"]
    assert stats["in"]["total"] == 2 and stats["out"]["total"] == 2 and stats["in"]["personal"] == 1
    assert stats["reply_me"]["n"] == 2 and stats["reply_me"]["median_ms"] == 7 * MIN_MS
    assert stats["style"]["chat"]["address"] == "ты" and stats["style"]["mail"]["address"] == "вы"
    # Пётр после писем владельца не писал больше 7 суток — дважды «без ответа»; выборки нет — нули.
    assert stats["reply_them"] == {"median_ms": 0, "p75_ms": 0, "n": 0, "unanswered": 2}
    assert {(e["message_id"], e["kind"]) for e in card["examples"]} == {
        (world["ex_chat"], "chat"),
        (world["ex_mail"], "mail"),
    }
    assert card["stats_ts"] == NOW_MS


async def test_e2_no_stats_yet_and_missing(client, ui, db, world):
    newbie = contact(db, world["tg"], "300", "Новенький")
    dm = channel(db, world["tg"], "300")
    msg(db, dm, ts=NOW_MS, sender=newbie)
    link(db)
    r = await client.get(f"{API}/people/{person_of(db, newbie)}", headers=ui)
    assert r.status_code == 200
    assert r.json()["stats"] is None and r.json()["stats_ts"] is None and r.json()["channels"] == []
    r = await client.get(f"{API}/people/999999", headers=ui)
    assert r.status_code == 404 and r.json() == {"detail": "not_found"}


# --- I.13, I.24: E1 токен проекта ---------------------------------------------------------------------------


async def test_i13_i24_token_project_scope(bearer_client, client, db, world):
    headers = _bearer(db, project_ids=[world["p1"]])
    r = await bearer_client.get(f"{API}/people/{world['petr']}", headers=headers)
    assert r.status_code == 200, r.text
    card = r.json()
    assert card["stats"]["in"]["total"] == 2 and card["stats"]["out"]["total"] == 2  # цифры полные
    assert [c["channel_id"] for c in card["channels"]] == [world["dm"]]  # беседы — только свои
    assert [e["message_id"] for e in card["examples"]] == [world["ex_chat"]]
    # П6: название — только у проектов токена.
    assert card["projects"] == [{"id": world["p1"], "title": "Globex"}, {"id": world["p2"], "title": None}]
    r = await bearer_client.get(f"{API}/people", params={"scope": "all"}, headers=headers)
    petr = next(p for p in r.json()["items"] if p["id"] == world["petr"])
    assert petr["projects"] == card["projects"]
    # Участники чужой беседы — тот же ответ, что у самой беседы.
    own = await bearer_client.get(f"{API}/channels/{world['thread']}", headers=headers)
    people_r = await bearer_client.get(f"{API}/channels/{world['thread']}/people", headers=headers)
    assert (
        (people_r.status_code, people_r.json())
        == (own.status_code, own.json())
        == (404, {"detail": "not found"})
    )
    r = await bearer_client.get(f"{API}/channels/{world['dm']}/people", headers=headers)
    assert r.status_code == 200 and [p["person_id"] for p in r.json()] == [world["petr"]]


# --- I.14: of-contact ------------------------------------------------------------------------------------


async def test_i14_of_contact(client, ui, db, world):
    fresh = contact(db, world["tg"], "400", "Свежий")
    dm = channel(db, world["tg"], "400")
    msg(db, dm, ts=NOW_MS, sender=fresh)
    assert person_of(db, fresh) is None
    r = await client.get(f"{API}/people/of-contact/{fresh}", headers=ui)
    assert r.status_code == 200, r.text
    created = r.json()["id"]
    assert person_of(db, fresh) == created
    r = await client.get(f"{API}/people/of-contact/{fresh}", headers=ui)
    assert r.json()["id"] == created
    me = contact(db, world["tg"], "1", "Я", is_me=1)
    msg(db, dm, ts=NOW_MS, sender=me, me=True)
    r = await client.get(f"{API}/people/of-contact/{me}", headers=ui)
    assert r.status_code == 404 and r.json() == {"detail": "is_me"}
    bot = contact(db, world["sl"], "B0BOT", "Бот", schema="work")
    msg(db, world["kdm"], ts=NOW_MS, sender=bot)
    r = await client.get(f"{API}/people/of-contact/{bot}", headers=ui)
    assert r.status_code == 404 and r.json() == {"detail": "not_person"}
    r = await client.get(f"{API}/people/of-contact/{world['petr_mail']}", headers=ui)
    assert r.json()["id"] == world["petr"]
    r = await client.get(f"{API}/people/of-contact/999999", headers=ui)
    assert r.status_code == 404 and r.json() == {"detail": "not_found"}


# --- E4: список и поиск ------------------------------------------------------------------------------------


async def test_e4_list_search_paging(client, ui, db, world):
    stranger = contact(db, world["tg"], "500", "Ёжик Незнакомый")
    grp = channel(db, world["tg"], "-100500", kind="group", project_id=world["p1"])
    msg(db, grp, ts=NOW_MS - DAY_MS, sender=stranger)
    link(db)
    recompute(db)
    r = await client.get(f"{API}/people", headers=ui)
    body = r.json()
    assert body["total"] == 2 and [p["id"] for p in body["items"]] == [world["petr"], world["kira"]]
    assert person_of(db, stranger) not in [p["id"] for p in body["items"]]  # не знакомый
    r = await client.get(f"{API}/people", params={"scope": "all"}, headers=ui)
    all_ids = [p["id"] for p in r.json()["items"]]
    assert all_ids[0] == person_of(db, stranger)  # свежее всех — по max(last_ts, seen_ts)
    r = await client.get(f"{API}/people", params={"scope": "all", "q": "ежик"}, headers=ui)
    assert [p["id"] for p in r.json()["items"]] == [person_of(db, stranger)]
    r = await client.get(f"{API}/people", params={"q": "CLIENT.ru"}, headers=ui)
    assert [p["id"] for p in r.json()["items"]] == [world["petr"]]
    brief = r.json()["items"][0]
    assert brief["name"] == "Пётр И." and brief["known"] is True
    assert {"source": "mail", "handle": "petr@client.ru", "email": "petr@client.ru"} in brief["sources"]
    assert brief["reply_me_median_ms"] == 7 * MIN_MS and brief["reply_them_median_ms"] is None
    r = await client.get(f"{API}/people", params={"scope": "all", "limit": 1, "offset": 1}, headers=ui)
    assert r.json()["total"] == 3 and len(r.json()["items"]) == 1 and r.json()["items"][0]["id"] == all_ids[1]


# --- I.6: правка только из UI ------------------------------------------------------------------------------


async def test_i6_ui_edits_and_bearer_denied(client, ui, bearer_client, db, world):
    headers = _bearer(db, scopes=("read", "draft", "organize"))
    for method, path, body in (
        ("POST", "/people/merge", {"into": world["petr"], "from": [world["kira"]]}),
        ("POST", "/people/split", {"contact_ids": [world["petr_mail"]]}),
        ("PATCH", f"/people/{world['petr']}", {"name": "X"}),
        ("POST", "/people/not-same", {"a": world["petr"], "b": world["kira"]}),
        ("GET", "/people/suggestions", None),
    ):
        r = await bearer_client.request(method, f"{API}{path}", json=body, headers=headers)
        assert r.status_code == 403, (path, r.text)
    denied = _audit(db, "ui_only_denied")
    assert len(denied) == 5 and all(d["result"] == "denied" for d in denied)
    assert person_of(db, world["kira_contact"]) == world["kira"]  # ничего не изменилось

    r = await client.patch(f"{API}/people/{world['petr']}", json={"name": "  Пётр Петрович  "}, headers=ui)
    assert r.status_code == 200 and r.json()["name"] == "Пётр Петрович" and r.json()["name_manual"] is True
    r = await client.patch(f"{API}/people/{world['petr']}", json={"name": "   "}, headers=ui)
    assert r.status_code == 422
    r = await client.post(f"{API}/people/split", json={"contact_ids": [world["petr_mail"]]}, headers=ui)
    assert r.status_code == 200, r.text
    split_id = r.json()["id"]
    assert split_id != world["petr"] and [c["contact_id"] for c in r.json()["contacts"]] == [
        world["petr_mail"]
    ]
    r = await client.post(
        f"{API}/people/split", json={"contact_ids": [world["petr_tg"]]}, headers=ui
    )  # единственный контакт
    assert r.status_code == 422
    r = await client.post(
        f"{API}/people/split", json={"contact_ids": [world["petr_tg"], world["kira_contact"]]}, headers=ui
    )
    assert r.status_code == 422
    r = await client.post(f"{API}/people/not-same", json={"a": world["petr"], "b": world["kira"]}, headers=ui)
    assert r.status_code == 204
    r = await client.post(f"{API}/people/merge", json={"into": world["petr"], "from": [999999]}, headers=ui)
    assert r.status_code == 404
    r = await client.post(
        f"{API}/people/merge", json={"into": world["petr"], "from": [world["kira"]]}, headers=ui
    )
    assert r.status_code == 200 and person_of(db, world["kira_contact"]) == world["petr"]
    for action in ("person_merge", "person_split", "person_rename", "person_not_same"):
        rows = _audit(db, action)
        assert rows and rows[-1]["actor"] == "ui" and rows[-1]["result"] == "ok", action
        assert "Пётр" not in json.dumps(rows, ensure_ascii=False)  # в аудите — только id


async def test_i6_split_is_not_merged_back_by_email(client, ui, db):
    box = account(db, "mail", config={"address": "me@home.ru"})
    wbox = account(db, "mail", schema="work", config={"address": "me@corp.io"})
    a = contact(db, box, "anya@corp.io", "Анна")
    b = contact(db, wbox, "anya@corp.io", "Anna", schema="work")
    for acc, cid, schema in ((box, a, "main"), (wbox, b, "work")):
        ch = channel(db, acc, f"t{cid}", kind="mail_thread", schema=schema)
        msg(db, ch, ts=NOW_MS, sender=cid)
    link(db)
    assert person_of(db, a) == person_of(db, b)
    r = await client.post(f"{API}/people/split", json={"contact_ids": [b]}, headers=ui)
    assert r.status_code == 200, r.text
    link(db)
    assert person_of(db, a) != person_of(db, b)


# --- I.15: E6 предложения ----------------------------------------------------------------------------------


def _person(db, acc, ext, name, handle=None, *, known=True, schema="main"):
    cid = contact(db, acc, ext, name, handle, schema=schema)
    kind = "dm" if known else "group"
    ch = channel(db, acc, f"c-{ext}", kind=kind, schema=schema)
    msg(db, ch, ts=NOW_MS - DAY_MS, sender=cid)
    return cid


async def test_i15_suggestions(client, ui, db):
    tg = account(db, "telegram")
    sl = account(db, "slack", schema="work")
    box = account(db, "mail", config={"address": "me@home.ru"})
    a = _person(db, tg, "1", "Иван Петров")
    b = _person(db, sl, "U1", "петров иван", schema="work")  # другой порядок слов
    c = _person(db, box, "ivan.p@x.ru", "Ivan Petrov")  # транслит
    d = _person(db, tg, "2", "Семён", "@semyon_k")
    e = _person(db, box, "semyon_k@y.ru", "С. К.")  # ник = локальная часть почты
    f1 = _person(db, tg, "3", "Иван")
    f2 = _person(db, sl, "U3", "Иван", schema="work")  # одно слово — нет
    h1 = _person(db, tg, "4", "Анна Смирнова")
    h2 = _person(db, tg, "5", "Анна Смирнова")  # один аккаунт — нет
    j1 = _person(db, tg, "6", "Пётр Ёлкин")
    j2 = _person(db, sl, "U6", "петр елкин", schema="work")  # «ё» = «е»
    k1 = _person(db, tg, "7", "Олег Сидоров")
    k2 = _person(db, sl, "U7", "Олег Сидоров", schema="work")  # «разные» — нет
    m1 = _person(db, tg, "8", "Мария Серова", known=False)
    m2 = _person(db, sl, "U8", "Мария Серова", known=False, schema="work")  # оба незнакомые — нет
    link(db)
    recompute(db)
    pid = {x: person_of(db, x) for x in (a, b, c, d, e, f1, f2, h1, h2, j1, j2, k1, k2, m1, m2)}
    db.write_sync(lambda conn: people.mark_not_same(conn, pid[k1], pid[k2], NOW_MS))
    r = await client.get(f"{API}/people/suggestions", headers=ui)
    assert r.status_code == 200, r.text
    got = {(frozenset((s["a"]["id"], s["b"]["id"])), s["reason"]) for s in r.json()}
    assert got == {
        (frozenset((pid[a], pid[b])), "name"),
        (frozenset((pid[a], pid[c])), "name"),
        (frozenset((pid[b], pid[c])), "name"),
        (frozenset((pid[d], pid[e])), "handle"),
        (frozenset((pid[j1], pid[j2])), "name"),
    }


# --- I.18: recipients не ломается от `email` в справочнике --------------------------------------------------


async def test_i18_recipients_with_email_field(client, ui, db):
    sl = account(db, "slack", schema="work")
    slack_people(
        db, sl, [{"id": "U0QQQ", "real_name": "Пётр Слак", "display_name": "", "email": "p@corp.io"}]
    )
    r = await client.get(f"{API}/recipients", params={"q": "Слак"}, headers=ui)
    assert r.status_code == 200, r.text
    people_rows = [row for row in r.json() if row["type"] == "person"]
    assert [row["to"] for row in people_rows] == [{"kind": "slack_users", "users": ["U0QQQ"]}]


# --- I.19: «удалить у себя» --------------------------------------------------------------------------------


async def test_i19_hidden_dm_not_counted_and_example_gone(client, ui, db, world, clock):
    grp = channel(db, world["tg"], "-1007", kind="group", project_id=world["p1"])
    msg(db, grp, ts=BASE, sender=world["petr_tg"])  # Пётр виден и вне лички
    recompute(db)
    db.write_sync(lambda conn: hiding.hide_channel(conn, world["dm"], now_ms=NOW_MS))
    r = await client.get(f"{API}/people/{world['petr']}", headers=ui)
    assert world["ex_chat"] not in [e["message_id"] for e in r.json()["examples"]]  # сразу, без пересчёта
    recompute(db)
    card = (await client.get(f"{API}/people/{world['petr']}", headers=ui)).json()
    assert world["dm"] not in [c["channel_id"] for c in card["channels"]]
    assert card["stats"]["out"]["total"] == 1 and card["stats"]["style"]["chat"] is None


# --- I.28: П10 видимость -----------------------------------------------------------------------------------


async def test_i28_p10_only_hidden_messages_invisible(client, ui, db, world):
    ghost = contact(db, world["tg"], "600", "Призрак Скрытый")
    dm = channel(db, world["tg"], "600", project_id=world["p1"])
    msg(db, dm, ts=NOW_MS - DAY_MS, sender=ghost)
    msg(db, world["dm"], ts=NOW_MS - DAY_MS, sender=ghost, mentions_me=1)
    link(db)
    recompute(db)
    pid = person_of(db, ghost)
    assert (await client.get(f"{API}/people/{pid}", headers=ui)).status_code == 200
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.message SET hidden = 1 WHERE sender_contact_id = ?", (ghost,))
    )
    r = await client.get(f"{API}/people", params={"scope": "all"}, headers=ui)
    assert pid not in [p["id"] for p in r.json()["items"]]
    r = await client.get(f"{API}/people", params={"scope": "all", "q": "призрак"}, headers=ui)
    assert r.json() == {"items": [], "total": 0}
    r = await client.get(f"{API}/channels/{world['dm']}/people", headers=ui)
    assert pid not in [p["person_id"] for p in r.json()]
    r = await client.get(f"{API}/people/of-contact/{ghost}", headers=ui)
    assert r.status_code == 404 and r.json() == {"detail": "not_found"}
    r = await client.get(f"{API}/people/{pid}", headers=ui)
    assert r.status_code == 404 and r.json() == {"detail": "not_found"}


async def test_channel_people_top(client, ui, db, world):
    grp = channel(db, world["tg"], "-1009", kind="group", project_id=world["p1"])
    for _ in range(3):
        msg(db, grp, ts=NOW_MS - DAY_MS, sender=world["petr_tg"])
    other = contact(db, world["tg"], "700", "Ольга")
    msg(db, grp, ts=NOW_MS - 2 * DAY_MS, sender=other)
    bot = contact(db, world["tg"], "701", "Бот", "@news_bot")
    msg(db, grp, ts=NOW_MS, sender=bot)
    msg(db, grp, ts=NOW_MS - 500 * DAY_MS, sender=other)  # за окном
    link(db)
    r = await client.get(f"{API}/channels/{grp}/people", headers=ui)
    assert r.json() == [
        {"person_id": world["petr"], "name": "Пётр И.", "n": 3, "last_ts": NOW_MS - DAY_MS},
        {"person_id": person_of(db, other), "name": "Ольга", "n": 1, "last_ts": NOW_MS - 2 * DAY_MS},
    ]
