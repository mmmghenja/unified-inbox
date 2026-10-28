"""Ф84-1 (спека phase84-01-mail-notify-keys): письма-уведомления с домена -> беседа по ключу.
Разбор правил, образцы jobs.example, Gmail (событие и перенос), обычный IMAP."""

from __future__ import annotations

import asyncio
import logging

import pytest

from hub.connectors.base import CoreClient
from hub.connectors.mail import notify_keys
from hub.connectors.mail.connector import MailConnector, parse_mail_config
from hub.connectors.mail.imapwire import FetchedMsg
from hub.connectors.mail.plain import PlainMailConnector

from .conftest import make_connector, make_gateway
from .fakegmail import BASE_TS, FakeGmail, make_email, new_msgid
from .helpers import channels, core_cursor, init_cursor, messages, spy_push
from .test_plain import ADDRESS as PLAIN_ADDRESS
from .test_plain import PLAIN_CFG, letter, make_plain_gateway, srv  # noqa: F401 — фикстура srv
from .test_trackers_connector import JIRA_ASSIGNEE, jira

FL_FROM = "Market <no-reply@market.example.com>"
FL_RULE = {
    "name": "market",
    "from": "market.example.com",
    "key": r"thread=(\d+)",
    "where": "text",
    "title": "«([^»]{1,200})",
    "title_prefix": "MK: ",
    "url": "https://market.example.com/inbox?thread={key}",
    "cut": ["Перейти к сообщению", "Приятной работы"],
}
FL_TEXT = (
    "Заказчик [buyer18] оставил вам новое сообщение в сделке «Настройка витрины и каталога "
    "(https://www.jobs.example/projects/1000001/manage/executors//executor/2000002/view)»: ок, отпишусь\n\n"
    "Перейти к сообщению (https://market.example.com/inbox?thread=2000002)\n\n"
    "Приятной работы, команда Market\n"
)
FL_TEXT_2 = FL_TEXT.replace("ок, отпишусь", "жду макет")
FL_TEXT_OTHER = FL_TEXT.replace("2000002", "999").replace("Настройка витрины и каталога", "Лендинг")
NO_KEY_TEXT = "Здравствуйте! Рассылка без ссылки на сделку."


def raw_fl(
    text: str = FL_TEXT, *, subject: str = "Новое сообщение", from_: str = FL_FROM, mid: str | None = None
):
    return make_email(msgid=mid or new_msgid("market"), from_=from_, subject=subject, body=text, date_ts=BASE_TS)


def fetched(raw: bytes, uid: int = 1) -> FetchedMsg:
    return FetchedMsg(uid=uid, body=raw, body_partial=False)


# --- разбор правил -----------------


def test_parse_valid_rule():
    [rule] = notify_keys.parse_rules([FL_RULE])
    assert rule["name"] == "market" and rule["from"] == "market.example.com" and rule["where"] == "text"
    assert rule["cut"] == FL_RULE["cut"]
    assert notify_keys.fingerprint([rule]) and notify_keys.fingerprint([]) == ""


def test_parse_bad_rules_skipped_with_one_warning_each(caplog):
    bad = [
        {**FL_RULE, "name": "BAD NAME"},
        {**FL_RULE, "key": "(["},
        {**FL_RULE, "key": r"\d+"},  # нет группы 1
        {**FL_RULE, "where": "nowhere"},
        {**FL_RULE, "url": "http://x/{key}"},
        {**FL_RULE, "url": "https://x/"},
        {**FL_RULE, "cut": ["x"] * 11},
        {**FL_RULE, "title_prefix": "p" * 21},
        "не объект",
    ]
    with caplog.at_level(logging.WARNING, logger="hub.mail"):
        rules = notify_keys.parse_rules([*bad, FL_RULE, {**FL_RULE}])  # последний — повтор имени
    assert [r["name"] for r in rules] == ["market"]
    assert len([r for r in caplog.records if "notify_keys" in r.getMessage()]) == len(bad) + 1
    assert FL_TEXT[:20] not in caplog.text  # текста писем в журнале нет


def test_parse_limits_and_config_wiring():
    many = [{**FL_RULE, "name": f"r{i}"} for i in range(25)]
    assert len(notify_keys.parse_rules(many)) == notify_keys.MAX_RULES
    assert notify_keys.parse_rules(None) == [] and notify_keys.parse_rules("{не json") == []
    assert (
        parse_mail_config({"address": "a@b.ru", "notify_keys": [FL_RULE]})["notify_keys"][0]["name"] == "market"
    )
    assert parse_mail_config({"address": "a@b.ru"})["notify_keys"] == []


# --- маршрутизация -----------------


def route(raw: bytes, *, known=()):
    return notify_keys.route(fetched(raw), notify_keys.parse_rules([FL_RULE]), thread_msgs=[], known=known)


def test_route_fl_sample():
    [part] = route(raw_fl())
    ch = part.channel
    assert ch["ext_id"] == "notify:market:2000002" and ch["kind"] == "task" and ch["own_id_space"] is True
    assert ch["title"] == "MK: Настройка витрины и каталога"
    assert ch["attrs"]["tracker"] == "notify" and ch["attrs"]["key"] == "2000002"
    assert ch["attrs"]["no_reply"] is True
    assert ch["attrs"]["url"] == "https://market.example.com/inbox?thread=2000002"
    assert "Заказчик [buyer18]" in part.text and "ок, отпишусь" in part.text
    assert "Приятной работы" not in part.text and "Перейти к сообщению" not in part.text


def test_route_subdomain_and_other_domain():
    assert route(raw_fl(from_="x <a@mail.market.example.com>")) is not None
    assert route(raw_fl(from_="x <a@evilmarket.example.com>")) is None
    assert route(raw_fl(from_="x <a@other.ru>")) is None


def test_route_no_key_is_regular_letter():
    assert route(raw_fl(NO_KEY_TEXT)) is None


def test_route_partial_body_is_regular_letter():
    msg = FetchedMsg(uid=1, body=raw_fl(), body_partial=True)
    assert notify_keys.route(msg, notify_keys.parse_rules([FL_RULE]), thread_msgs=[]) is None


def test_route_text_never_lost_when_cut_removes_all():
    rule = {**FL_RULE, "cut": ["Заказчик"]}
    [part] = notify_keys.route(fetched(raw_fl()), notify_keys.parse_rules([rule]), thread_msgs=[], known=())
    assert "Заказчик" in part.text


def test_route_title_fallback_only_for_new_channel():
    rule = {k: v for k, v in FL_RULE.items() if k != "title"}
    rules = notify_keys.parse_rules([rule])
    [new] = notify_keys.route(fetched(raw_fl()), rules, thread_msgs=[], known=())
    assert new.channel["title"] == "MK: market 2000002"
    [old] = notify_keys.route(fetched(raw_fl()), rules, thread_msgs=[], known={"notify:market:2000002"})
    assert "title" not in old.channel


def test_route_unsafe_key_normalized_or_rejected():
    rule = {**FL_RULE, "key": r"thread=(\S+)"}
    rules = notify_keys.parse_rules([rule])
    [part] = notify_keys.route(fetched(raw_fl("thread=a/../b ссылка")), rules, thread_msgs=[])
    assert part.channel["ext_id"] == "notify:market:a_.._b"
    assert notify_keys.route(fetched(raw_fl("thread=сделка ссылка")), rules, thread_msgs=[]) is None


Initech_RULE = {
    "name": "initech-order",
    "from": "initech.example.com",
    "key": r"(?s)Order\s+ID\s+\d+.*?Full name.*?Email\s+(\S+@\S+)",
    "where": "text",
    "title": r"Full name\s+([^\n]+)",
    "title_prefix": "Заявки: ",
    "cut": ["Best Regards,"],
}
Initech_TEXT = (
    "Order\n\nID  14710\n\nType  Account opening\n\nClient\n\nFull name  Test Client\n\n"
    "Email  client.one@example.com\n\nPhone  +79000000077\n\nBest Regards,\n\nInitech Brokerage\n"
)


def test_route_key_from_email_one_channel_per_client():
    rules = notify_keys.parse_rules([Initech_RULE])
    raw = make_email(
        msgid=new_msgid("initech"),
        from_="Initech (test) <noreply@initech.example.com>",
        subject="[Account opening][New][14710][Test Client] Order Notification",
        body=Initech_TEXT,
        date_ts=BASE_TS,
    )
    [part] = notify_keys.route(fetched(raw), rules, thread_msgs=[])
    assert part.channel["ext_id"] == "notify:initech-order:client.one_example.com"
    assert part.channel["title"] == "Заявки: Test Client"
    assert "Account opening" in part.text and "Best Regards" not in part.text
    other = make_email(
        msgid=new_msgid("initech"),
        from_="Коллега <ivan@initech.example.com>",
        subject="Созвон",
        body="Привет, созвон в 12",
        date_ts=BASE_TS,
    )
    assert notify_keys.route(fetched(other), rules, thread_msgs=[]) is None


def test_mutation_guard_no_reply_in_attrs(monkeypatch):
    """Мутация: убрать `no_reply` из attrs — проверка выше его требует (тест ловит подмену)."""
    [part] = route(raw_fl())
    assert part.channel["attrs"].get("no_reply") is True
    monkeypatch.setattr(notify_keys.convert, "thread_attrs", lambda msgs, extra=None: {})
    [broken] = route(raw_fl())
    assert broken.channel["attrs"].get("no_reply") is not True


# --- Gmail -----------------


@pytest.fixture
async def env(admin_client, internal_transport, gm):
    made = []

    async def _make(**kw):
        c = await make_connector(admin_client, internal_transport, gm, **kw)
        made.append(c[1])
        return c

    yield _make
    for gw in made:
        await gw.close()


def add_fl(gm: FakeGmail, text: str = FL_TEXT, *, thrid=None, date_ts=None, subject="Новое сообщение"):
    raw = make_email(
        msgid=new_msgid("market"), from_=FL_FROM, subject=subject, body=text, date_ts=date_ts or BASE_TS
    )
    return gm.add(raw, thrid=thrid, labels=("\\Inbox",), date_ts=date_ts)


async def test_gmail_two_threads_one_notify_channel(env, gm, db):
    connector, _gw, _client, _aid = await env(config={"notify_keys": [FL_RULE]})
    await init_cursor(connector)
    add_fl(gm, FL_TEXT, date_ts=BASE_TS + 10)
    add_fl(gm, FL_TEXT_2, date_ts=BASE_TS + 20)
    add_fl(gm, FL_TEXT_OTHER, date_ts=BASE_TS + 30)
    plain_letter = add_fl(gm, NO_KEY_TEXT, date_ts=BASE_TS + 40, subject="Рассылка")
    await connector.sync("live")

    chans = channels(db)
    assert chans["notify:market:2000002"]["title"] == "MK: Настройка витрины и каталога"
    assert chans["notify:market:2000002"]["kind"] == "task"
    assert chans["notify:market:2000002"]["attrs"]["no_reply"] is True
    assert "notify:market:999" in chans
    assert str(plain_letter.thrid) in chans  # письмо без ключа — обычная беседа-тред
    assert len(chans) == 3  # тредов двух FL-писем с ключом нет
    texts = [m["text"] for m in messages(db) if m["thread"] == "notify:market:2000002"]
    assert len(texts) == 2 and any("жду макет" in t for t in texts)


async def test_gmail_jira_not_intercepted_by_rules(env, gm, db):
    rule = {"name": "atl", "from": "atlassian.net", "key": r"([A-Z]+-\d+)", "where": "both"}
    connector, _gw, _client, _aid = await env(config={"notify_keys": [rule]})
    await init_cursor(connector)
    jira(gm)
    await connector.sync("live")
    assert JIRA_ASSIGNEE
    assert not any(ext.startswith("notify:") for ext in channels(db))
    assert any(ext.startswith("jira:") for ext in channels(db))


async def test_gmail_resort_old_threads_by_rules(env, gm, db, admin_client, internal_transport):
    connector, _gw, client, account_id = await env()  # без правил
    await init_cursor(connector)
    a = add_fl(gm, FL_TEXT, date_ts=BASE_TS + 10)
    add_fl(gm, FL_TEXT_2, date_ts=BASE_TS + 20)
    await connector.sync("live")
    assert str(a.thrid) in channels(db)
    assert (await core_cursor(client))["trackers"] == {"done": 1}

    gw2 = make_gateway(gm)
    try:
        c2 = MailConnector(
            client, gw2, account_id=account_id, config={"address": "me@gmail.com", "notify_keys": [FL_RULE]}
        )
        calls = spy_push(client)
        await c2.sync("scheduled")
        migrates = [e for evs, _c, _r in calls for e in evs if e["type"] == "channel_migrate"]
        assert {e["channel"]["ext_id"] for e in migrates} == {"notify:market:2000002"}
        fp = notify_keys.fingerprint(notify_keys.parse_rules([FL_RULE]))
        assert (await core_cursor(client))["trackers"] == {"done": 1, "notify": fp}
        assert len([m for m in messages(db) if m["thread"] == "notify:market:2000002"]) == 2
        again = spy_push(client)
        await c2.sync("scheduled")
        assert not any(evs for evs, _c, _r in again)  # правила пройдены один раз
    finally:
        await gw2.close()


# --- обычный IMAP -----------------


@pytest.fixture
async def plain_env(admin_client, internal_transport, srv, tmp_path):  # noqa: F811
    made = []

    async def _make():
        cfg = {**PLAIN_CFG, "notify_keys": [FL_RULE]}
        r = await admin_client.post(
            "/internal/v1/admin/accounts",
            json={"source": "mail", "title": PLAIN_ADDRESS, "db": "main", "config": cfg},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        client = CoreClient(secret=body["connector_secret"], transport=internal_transport)
        gw = make_plain_gateway(srv)
        made.append(gw)

        async def _sleep(_s: float) -> None:
            await asyncio.sleep(0)

        connector = PlainMailConnector(
            client,
            gw,
            account_id=body["account_id"],
            config=cfg,
            state_path=tmp_path / "mail" / "state.json",
            sleep=_sleep,
            wall=lambda: srv.now_ts,
        )
        connector._loop = asyncio.get_running_loop()  # noqa: SLF001
        return connector

    yield _make
    for gw in made:
        await gw.close()


async def test_plain_two_threads_one_notify_channel(plain_env, srv, db):  # noqa: F811
    connector = await plain_env()
    await connector.sync("start")
    from_ = "Market <no-reply@market.example.com>"
    letter(srv, msgid="<f1@market.example.com>", from_=from_, subject="Сообщение", body=FL_TEXT)
    letter(srv, msgid="<f2@market.example.com>", from_=from_, subject="Сообщение 2", body=FL_TEXT_2)
    letter(srv, msgid="<n1@market.example.com>", from_=from_, subject="Рассылка", body=NO_KEY_TEXT)
    await connector.sync("live")
    await connector.sync("live")  # повторная сверка ничего не дублирует
    chans = channels(db)
    assert chans["notify:market:2000002"]["attrs"]["no_reply"] is True
    assert chans["notify:market:2000002"]["title"] == "MK: Настройка витрины и каталога"
    assert "t:f1@market.example.com" not in chans and "t:f2@market.example.com" not in chans
    assert "t:n1@market.example.com" in chans  # без ключа — обычный путь, текст не потерян
    assert len([m for m in messages(db) if m["thread"] == "notify:market:2000002"]) == 2


def test_account_config_accepts_notify_keys_but_not_secret_keys():
    from hub.core import admin as admin_mod

    admin_mod._check_config_safe({"notify_keys": [FL_RULE]})  # noqa: SLF001 — имя с «key», но не секрет
    with pytest.raises(admin_mod.ConfigHasSecrets):
        admin_mod._check_config_safe({"api_key": "x"})  # noqa: SLF001


def test_cut_marker_mid_line():
    """Письмо из HTML одной строкой: маркер посреди строки — всё равно режем; маркера нет — текст как есть."""
    line = "Заказчик оставил сообщение: ок, отпишусь  Перейти к сообщению (https://x) Приятной работы"
    assert notify_keys.cut_text(line, ["Перейти к сообщению", "Приятной работы"]) == (
        "Заказчик оставил сообщение: ок, отпишусь"
    )
    assert notify_keys.cut_text("текст", ["нет такого"]) == "текст"
