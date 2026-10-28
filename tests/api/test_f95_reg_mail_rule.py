"""Ф95-1 (docs/architecture.md): правило почты по домену регистрации.

- заявка «нужен человек» с адресом площадки заводит на КАЖДОМ почтовом аккаунте правило
  `from_domains = <домен>` -> проект заявки (`origin_human_id`), беседа с этого домена из «Личного»
  переезжает сразу; аудит `mail_rule_auto`; ответ ручки — `mail_rule`;
- правило уже есть (в любой проект) — не дублируется и не снимается при провале; повтор заявки (П7) —
  второго правила нет; запретный домен и заявка без адреса — правил нет;
- итог `done`/`later` — правила на месте; `failed`, `expired` (через `sweep`), `cancelled` — сняты, беседа
  вернулась, аудит `mail_rule_auto_removed`;
- «Раскладка» показывает правило с пометкой заявки; миграция 0117."""

import json

from hub.core import human_requests as hr
from hub.core import mapping

from . import test_hub_sessions as base
from .conftest import insert_account
from .test_f54_human_requests import _cancel, _create, _decide
from .test_hub_sessions import MIN, ROLE, A, _audits, _now, _sub

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"
DOMAIN = "signup.example"
URL = "https://www.signup.example/login/"


def _mail_world(db, w) -> dict:
    """Два почтовых ящика (основной файл и рабочий), у первого — «весь аккаунт -> Личное» и беседа с
    домена площадки, лежащая по нему в «Личном»."""
    m1 = insert_account(db, source="mail")
    m2 = insert_account(db, source="mail", db_name="work")

    def _fn(conn):
        rule_all, _ = mapping.add_rule(
            conn, account_id=m1, project_id=w["nodir"], match={"all": True}, priority=None, now_ms=0
        )
        meta = json.dumps({"attrs": {"from_domains": [DOMAIN], "labels": []}})
        ch = conn.execute(
            "INSERT INTO main.channel (account_id, ext_id, kind, title, meta_json, map_source) "
            "VALUES (?, 'thr-1', 'dm', 'Код входа', ?, 'auto')",
            (m1, meta),
        ).lastrowid
        mapping.recompute_channel(conn, "main", ch, 0)
        return rule_all, ch

    rule_all, ch = db.write_sync(_fn)
    return {"m1": m1, "m2": m2, "rule_all": rule_all, "mail_ch": ch}


def _rules(db, origin=None) -> list[tuple]:
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT id, account_id, match_json, project_id, priority, origin_human_id FROM main.map_rule "
            "ORDER BY id"
        ).fetchall()
    out = [(rid, acc, json.loads(mj), pid, prio, oh) for rid, acc, mj, pid, prio, oh in rows]
    return [r for r in out if origin is None or r[5] == origin]


def _channel_project(db, channel_id) -> int | None:
    with db.reader() as conn:
        return conn.execute("SELECT project_id FROM main.channel WHERE id = ?", (channel_id,)).fetchone()[0]


async def _registered(client, db, w, **fields) -> dict:
    await _sub(client, w, A, role=ROLE)
    return await _create(client, w, kind="login", need="Войти на площадку", url=URL, **fields)


async def test_rule_on_every_mail_account_and_channel_moves(client, ui, db, clock, world):  # noqa: F811
    w = world
    m = _mail_world(db, w)
    assert _channel_project(db, m["mail_ch"]) == w["nodir"]
    made = await _registered(client, db, w)
    human_id = made["request"]["id"]
    rules = _rules(db, origin=human_id)
    assert [(r[1], r[3], r[4]) for r in rules] == [(m["m1"], w["p3"], 0), (m["m2"], w["p3"], 0)]
    # Основной файл — с подписью; рабочий — без (граница main/work, `storable_match`).
    assert rules[0][2] == {
        "attr": "from_domains",
        "has": DOMAIN,
        "label": f"отправитель @{DOMAIN} (регистрация, заявка #{human_id})",
    }
    assert rules[1][2] == {"attr": "from_domains", "has": DOMAIN}
    # Беседа из «Личного» переехала в проект заявки сразу.
    assert _channel_project(db, m["mail_ch"]) == w["p3"]
    # Аудит — домен, аккаунт, проект, номер правила.
    audits = _audits(db, "mail_rule_auto")
    assert [(a["account_id"], a["project_id"], a["rule_id"], a["domain"]) for a in audits] == [
        (m["m1"], w["p3"], rules[0][0], DOMAIN),
        (m["m2"], w["p3"], rules[1][0], DOMAIN),
    ]
    assert audits[0]["human_id"] == human_id and audits[0]["channels_relaid"] == 1
    # Ответ ручки называет правила.
    assert made["mail_rule"] == {
        "domain": DOMAIN,
        "project_id": w["p3"],
        "project_name": "Umbrella",
        "rules": [
            {"rule_id": rules[0][0], "account_id": m["m1"]},
            {"rule_id": rules[1][0], "account_id": m["m2"]},
        ],
    }
    # «Раскладка» показывает правило как обычное, с пометкой заявки.
    listed = {r["id"]: r for r in (await ui.get(f"{API}/map-rules")).json()}
    assert listed[rules[0][0]]["description"] == f"отправитель @{DOMAIN} (регистрация, заявка #{human_id})"
    assert listed[rules[0][0]]["kind"] == "domain" and listed[m["rule_all"]]["description"] == "весь аккаунт"

    # Повтор заявки (П7): та же заявка, второго правила нет.
    again = await _create(client, w, kind="login", need="Войти на площадку", url=URL, expect=200)
    assert again["created"] is False and again["mail_rule"] is None
    assert len(_rules(db)) == 3


async def test_done_and_later_keep_rules(client, ui, db, clock, world):  # noqa: F811
    w = world
    m = _mail_world(db, w)
    first = (await _registered(client, db, w))["request"]["id"]
    await _decide(ui, first, "done")
    assert len(_rules(db, origin=first)) == 2
    assert _channel_project(db, m["mail_ch"]) == w["p3"]
    assert _audits(db, "mail_rule_auto_removed") == []
    # Другой домен, итог «не сейчас» — правила тоже остаются.
    second = (await _create(client, w, kind="code", need="Код", url="https://shop.example.com.ru/x"))[
        "request"
    ]
    assert [r[2]["has"] for r in _rules(db, origin=second["id"])] == ["example.com.ru"] * 2
    await _decide(ui, second["id"], "later")
    assert len(_rules(db, origin=second["id"])) == 2
    assert len(_rules(db)) == 5


async def test_failed_expired_cancelled_drop_rules_and_return_channel(client, ui, db, clock, world):  # noqa: F811
    w = world
    m = _mail_world(db, w)
    # «Не вышло».
    failed = (await _registered(client, db, w))["request"]["id"]
    assert _channel_project(db, m["mail_ch"]) == w["p3"]
    await _decide(ui, failed, "failed")
    assert _rules(db, origin=failed) == [] and len(_rules(db)) == 1
    assert _channel_project(db, m["mail_ch"]) == w["nodir"]
    removed = _audits(db, "mail_rule_auto_removed")
    assert [(a["human_id"], a["account_id"], a["domain"]) for a in removed] == [
        (failed, m["m1"], DOMAIN),
        (failed, m["m2"], DOMAIN),
    ]
    # Снята автором.
    cancelled = (await _create(client, w, kind="login", need="Войти", url=URL))["request"]["id"]
    assert len(_rules(db, origin=cancelled)) == 2 and _channel_project(db, m["mail_ch"]) == w["p3"]
    await _cancel(client, w, cancelled)
    assert _rules(db, origin=cancelled) == [] and _channel_project(db, m["mail_ch"]) == w["nodir"]
    # Срок вышел — снимает `sweep` (минутный цикл ядра).
    expired = (await _create(client, w, kind="login", need="Войти", url=URL, ttl_min=1))["request"]["id"]
    assert len(_rules(db, origin=expired)) == 2
    clock.advance_ms(MIN + 1)
    now = _now(clock)
    assert db.write_sync(lambda conn: hr.sweep(conn, now)) == 1
    with db.reader() as conn:
        assert hr.get(conn, expired).status == "expired"
    assert _rules(db, origin=expired) == [] and _channel_project(db, m["mail_ch"]) == w["nodir"]
    assert len(_audits(db, "mail_rule_auto_removed")) == 6
    assert [r[0] for r in _rules(db)] == [m["rule_all"]]


async def test_existing_rule_is_not_duplicated_nor_removed(client, ui, db, clock, world):  # noqa: F811
    w = world
    m = _mail_world(db, w)
    # У первого ящика правило с этим доменом уже есть — в другой проект (Initech).
    own, _ = db.write_sync(
        lambda conn: mapping.add_rule(
            conn,
            account_id=m["m1"],
            project_id=w["p4"],
            match={"attr": "from_domains", "has": DOMAIN, "label": f"отправитель @{DOMAIN}"},
            priority=None,
            now_ms=0,
        )
    )
    assert _channel_project(db, m["mail_ch"]) == w["p4"]
    made = await _registered(client, db, w)
    human_id = made["request"]["id"]
    rules = _rules(db, origin=human_id)
    assert [r[1] for r in rules] == [m["m2"]]  # только на втором ящике
    assert made["mail_rule"]["rules"] == [{"rule_id": rules[0][0], "account_id": m["m2"]}]
    assert _channel_project(db, m["mail_ch"]) == w["p4"]
    await _decide(ui, human_id, "failed")
    # Чужое правило на месте, беседа там же; снято только своё.
    assert [r[0] for r in _rules(db)] == [m["rule_all"], own]
    assert _channel_project(db, m["mail_ch"]) == w["p4"]
    # Правило есть на обоих ящиках — заявка ничего не заводит, строки в ответе нет.
    db.write_sync(
        lambda conn: mapping.add_rule(
            conn,
            account_id=m["m2"],
            project_id=w["p3"],
            match={"attr": "from_domains", "has": DOMAIN},
            priority=None,
            now_ms=0,
        )
    )
    again = await _create(client, w, kind="login", need="Войти", url=URL)
    assert again["mail_rule"] is None and _rules(db, origin=again["request"]["id"]) == []


async def test_blocked_domain_bad_address_and_site_fallback(client, ui, db, clock, world):  # noqa: F811
    w = world
    _mail_world(db, w)
    await _sub(client, w, A, role=ROLE)
    # Почтовик и крупный сервис — правила нет (поддомен сервиса тоже).
    for url in ("https://e.mail.ru/login", "https://accounts.google.com/signin", "https://www.gosuslugi.ru/"):
        made = await _create(client, w, kind="login", need="Войти", url=url)
        assert made["mail_rule"] is None
        await _cancel(client, w, made["request"]["id"])
    # IP и localhost — домена нет.
    for url in ("http://192.168.0.1/admin", "http://localhost:8080/"):
        made = await _create(client, w, kind="login", need="Войти", url=url)
        assert made["mail_rule"] is None
        await _cancel(client, w, made["request"]["id"])
    # Без адреса и без сайта — правил нет; сайт похож на домен — правило по нему.
    bare = await _create(client, w, kind="login", need="Войти")
    assert bare["mail_rule"] is None
    await _cancel(client, w, bare["request"]["id"])
    by_site = await _create(client, w, kind="login", need="Войти", site="Shop.Example.org")
    assert by_site["mail_rule"]["domain"] == "example.org"
    assert len(_rules(db)) == 1 + 2


async def test_no_mail_accounts_no_rules(client, ui, db, clock, world):  # noqa: F811
    w = world
    made = await _registered(client, db, w)
    assert made["mail_rule"] is None and _rules(db) == []
    assert _audits(db, "mail_rule_auto") == []


def test_migration_adds_origin_column(db):
    with db.reader() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA main.table_info(map_rule)")}
    assert "origin_human_id" in columns
