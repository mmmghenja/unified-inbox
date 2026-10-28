"""Ф10-5 часть A — маршрутизация писем трекеров в `MailConnector` (настоящее ядро в процессе + `FakeGmail`):
тесты A-T6, A-T9…A-T14 спеки `docs/architecture.md`."""

from __future__ import annotations

import json

import pytest

from hub.connectors.mail import connector as connector_mod
from hub.connectors.mail import textproc
from hub.connectors.mail.connector import MailConnector

from .conftest import make_connector, make_gateway
from .fakegmail import BASE_TS, FakeGmail, make_email, new_msgid
from .helpers import channels, core_cursor, init_cursor, messages, mine, spy_push
from .test_trackers import CARD1_TEXT, JIRA_ASSIGNEE, TRELLO_DIGEST, fixture

JIRA_CH = "jira:acme.atlassian.net:WEB-39"


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


def tracker_letter(
    gm: FakeGmail,
    name: str,
    from_: str,
    subject: str,
    *,
    thrid: int | None = None,
    flags=(),
    labels=("\\Inbox",),
    date_ts: int | None = None,
    replace: tuple[str, str] | None = None,
):
    text = fixture(name)
    if replace:
        text = text.replace(*replace)
    raw = make_email(
        msgid=new_msgid("trk"),
        from_=from_,
        subject=subject,
        body=text,
        html="<html><body><p>HTML-версия</p></body></html>",
        date_ts=date_ts or BASE_TS,
    )
    return gm.add(raw, thrid=thrid, labels=labels, flags=flags, date_ts=date_ts)


def jira(gm, **kw):
    return tracker_letter(gm, "jira_update_assignee.txt", *JIRA_ASSIGNEE, **kw)


def trello(gm, **kw):
    return tracker_letter(gm, "trello_digest_two_cards.txt", *TRELLO_DIGEST, **kw)


def _archived(db, ext_id: str) -> int:
    with db.reader() as conn:
        return conn.execute("SELECT archived FROM main.channel WHERE ext_id = ?", (ext_id,)).fetchone()[0]


# --- A-T6: подделка домена — обычное письмо ----------------------------------------------------------------


async def test_a_t6_spoofed_jira_domain_goes_to_gmail_thread(env, gm, db):
    connector, _gw, _client, _aid = await env()
    await init_cursor(connector)
    fake = tracker_letter(
        gm, "jira_update_assignee.txt", '"Jira" <jira@evil-atlassian.net>', JIRA_ASSIGNEE[1]
    )
    await connector.sync("live")
    chans = channels(db)
    assert set(chans) == {str(fake.thrid)}
    assert chans[str(fake.thrid)]["kind"] == "mail_thread"
    [row] = messages(db)
    assert row["text"] == textproc.extract_body(fake.raw).text  # текст как раньше


# --- A-T9: новое письмо Jira -> беседа задачи, беседы-треда нет ---------------------------------------------


async def test_a_t9_jira_letter_routes_to_issue_and_second_fetches_only_new_body(env, gm, db):
    """A-T9: `upsert` в `jira:…` с `attrs`; НЕТ события с `channel.ext_id == str(thrid)`; второе письмо
    того же треда -> тело качается только у нового."""
    connector, _gw, client, _aid = await env()
    await init_cursor(connector)
    first = jira(gm, labels=("\\Inbox", "Initech"), flags=("\\Seen",))  # прочитано: старый код дал бы read треда
    calls = spy_push(client)
    await connector.sync("live")

    events = [e for evs, _c, _r in calls for e in evs]
    assert not any(e.get("channel", {}).get("ext_id") == str(first.thrid) for e in events)
    [up] = [e for e in events if e["type"] == "upsert"]
    assert up["channel"]["ext_id"] == JIRA_CH
    assert up["channel"]["kind"] == "issue"
    attrs = up["channel"]["attrs"]
    assert attrs["labels"] == ["INBOX", "Initech"]
    assert attrs["from_domains"] == ["acme.atlassian.net"]
    assert attrs["tracker"] == "jira" and attrs["key"] == "WEB-39" and attrs["no_reply"] is True
    assert attrs["url"] == "https://acme.atlassian.net/browse/WEB-39"
    chans = channels(db)
    assert set(chans) == {JIRA_CH}
    assert chans[JIRA_CH]["title"] == "WEB-39 · Утилиты на Tailwind"
    assert chans[JIRA_CH]["kind"] == "issue"
    assert chans[JIRA_CH]["last_read"] is None  # ревью Ф10-5: `\\Seen` Gmail беседу трекера не двигает
    [row] = messages(db)
    assert row["ext_id"] == first.message_id and row["text"] == "Исполнитель: Иван Петров → admin"

    second = jira(gm, thrid=first.thrid, replace=("Иван Петров", "Пётр Сидоров"))
    start = len(gm.commands)
    await connector.sync("live")
    bodies = [c for _n, c in gm.commands[start:] if "BODY.PEEK[]" in c]
    assert bodies == [f"UID FETCH {second.uid} (UID BODY.PEEK[])"]
    assert [r["ext_id"] for r in messages(db)] == [first.message_id, second.message_id]
    assert set(channels(db)) == {JIRA_CH}


# --- A-T10: прочитанность из Gmail беседы трекера не двигает (ревью Ф10-5, находки 2–3) --------------------


async def test_a_t10_seen_letters_do_not_mark_issue_read(env, gm, db):
    """A-T10 (после ревью): тред Gmail ≠ карточка — `\\Seen` писем трекера маркер беседы трекера не двигает,
    события `read` в беседу трекера коннектор не шлёт."""
    connector, _gw, client, _aid = await env()
    await init_cursor(connector)
    a = jira(gm, flags=("\\Seen",))
    jira(gm, thrid=a.thrid, flags=("\\Seen",))
    calls = spy_push(client)
    await connector.sync("live")
    assert not [e for evs, _c, _r in calls for e in evs if e["type"] == "read"]
    assert channels(db)[JIRA_CH]["last_read"] is None


@pytest.mark.parametrize("condstore", [True, False])
async def test_a_t10_flag_change_in_sync_does_not_touch_issue(
    admin_client, internal_transport, db, condstore
):
    """A-T10 (после ревью): флаг письма трекера сменился в сверке -> ни маркера беседы трекера, ни
    беседы-треда."""
    gm = FakeGmail(condstore=condstore)
    connector, gw, client, _aid = await make_connector(admin_client, internal_transport, gm)
    try:
        await init_cursor(connector)
        older = jira(gm)
        newer = jira(gm, thrid=older.thrid)
        await connector.sync("live")
        gm.set_flags(older.uid, add=("\\Seen",))
        gm.set_flags(newer.uid, add=("\\Seen",))
        calls = spy_push(client)
        await connector.sync("live")  # новых писем нет — только флаги
        assert not [e for evs, _c, _r in calls for e in evs if e["type"] == "read"]
        assert channels(db)[JIRA_CH]["last_read"] is None
        assert str(older.thrid) not in channels(db)
    finally:
        await gw.close()


# --- A-T11: стартовая загрузка на ящике из одних трекеров заканчивается -------------------------------------


async def test_a_t11_startup_with_only_tracker_threads_finishes(env, gm, db, monkeypatch):
    monkeypatch.setattr(connector_mod, "STARTUP_BATCH", 2)
    connector, _gw, client, _aid = await env()
    for i in range(5):
        tracker_letter(
            gm,
            "jira_update_assignee.txt",
            JIRA_ASSIGNEE[0],
            f"[Jira] (WEB-{i + 1}) «Задача {i + 1}»",
            replace=("WEB-39", f"WEB-{i + 1}"),
        )
    connector._request_sync = lambda _r: None  # noqa: SLF001
    await connector.sync("start")
    for _ in range(5):
        if (await core_cursor(client)).get("startup") is None:
            break
        await connector.sync("initial")
    assert (await core_cursor(client)).get("startup") is None
    assert set(channels(db)) == {f"jira:acme.atlassian.net:WEB-{i + 1}" for i in range(5)}
    assert len(messages(db)) == 5


# --- A-T12: пересортировка старых бесед-тредов -------------------------------------------------------------


async def test_a_t12_resort_migrates_clean_threads_reads_old_and_runs_once(
    env, gm, db, monkeypatch, admin_client
):
    """A-T12 (после ревью): старый тред (в heads) только из писем Jira -> одним `push_events`: `upsert` в
    `jira:…` и `channel_migrate(old_ext_id=thrid)`; прочитанность — от ядра по тем же письмам старой
    беседы (a1 прочитано там -> прочитано и в беседе задачи), письмо a2, пришедшее во время деплоя (его в
    старой беседе не было, догон той же сверки уже положил тред в `_tracker_threads`), — непрочитано и тред
    всё равно пересортирован (находка 4); курсор `trackers.done`; второй прогон — без событий. Тред
    «Trello + моё письмо» — события трекера есть, миграции и `read` нет."""
    connector, gw, client, account_id = await env()
    r = await admin_client.post("/internal/v1/admin/projects", json={"name": "Initech"})
    project_id = r.json()["project_id"]
    r = await admin_client.post(
        "/internal/v1/admin/map-rules",
        json={
            "account_id": account_id,
            "project_id": project_id,
            "match": {"attr": "from_domains", "has": "acme.atlassian.net"},
        },
    )
    assert r.status_code == 200, r.text

    # --- «до деплоя»: коннектор без разбора трекеров, письма лежат в беседах-тредах
    with monkeypatch.context() as mp:
        mp.setattr(connector_mod.trackers, "route", lambda *_a, **_k: None)
        await init_cursor(connector)
        a1 = jira(gm, date_ts=BASE_TS + 60, flags=("\\Seen",))
        t1 = trello(gm, date_ts=BASE_TS + 120)
        mine(gm, thrid=t1.thrid, date_ts=BASE_TS + 130)
        await connector.sync("live")
    old = channels(db)
    assert set(old) == {str(a1.thrid), str(t1.thrid)}
    assert old[str(a1.thrid)]["project_id"] == project_id
    assert old[str(a1.thrid)]["last_read"] == a1.message_id
    cur = await core_cursor(client)
    cur.pop("trackers")  # курсор старого коннектора ключа не знал
    await client.push_events([], cursor={"value": json.dumps(cur, sort_keys=True), "ts": 0})

    # --- во время деплоя в старый тред пришло новое письмо; новый процесс: догон, затем пересортировка
    a2 = jira(gm, thrid=a1.thrid, date_ts=BASE_TS + 600, replace=("Иван Петров", "Пётр Сидоров"))
    gw2 = make_gateway(gm)
    try:
        c2 = MailConnector(client, gw2, account_id=account_id, config={"address": "me@gmail.com"})
        calls = spy_push(client)
        await c2.sync("scheduled")

        [jira_call] = [evs for evs, _c, _r in calls if any(e["type"] == "channel_migrate" for e in evs)]
        kinds = [(e["type"], e["channel"]["ext_id"]) for e in jira_call]
        assert ("upsert", JIRA_CH) in kinds
        migrate = next(e for e in jira_call if e["type"] == "channel_migrate")
        assert migrate["old_ext_id"] == str(a1.thrid)
        assert migrate["channel"]["ext_id"] == JIRA_CH
        assert migrate["channel"]["title"] == "WEB-39 · Утилиты на Tailwind"
        assert migrate["channel"]["attrs"]["no_reply"] is True
        tracker_reads = [
            e
            for evs, _c, _r in calls
            for e in evs
            if e["type"] == "read" and e["channel"]["ext_id"].startswith(("jira:", "trello:"))
        ]
        assert not tracker_reads  # прочитанность беседы трекера — только от ядра при миграции
        assert kinds.index(("upsert", JIRA_CH)) < kinds.index(("channel_migrate", JIRA_CH))

        trello_calls = [
            evs for evs, _c, _r in calls if any(e["channel"]["ext_id"].startswith("trello:") for e in evs)
        ]
        assert len(trello_calls) == 1
        assert not any(e["type"] == "channel_migrate" for e in trello_calls[0])

        cur = await core_cursor(client)
        assert cur["trackers"] == {"done": 1}
        chans = channels(db)
        assert chans[JIRA_CH]["last_read"] == a1.message_id  # от старой беседы; a2 — непрочитано
        assert chans["trello:c:Xy1Ab2Cd"]["last_read"] is None
        assert chans[JIRA_CH]["project_id"] == project_id
        assert _archived(db, str(a1.thrid)) == 1
        assert _archived(db, str(t1.thrid)) == 0  # смешанный тред остался как есть
        card = next(r for r in messages(db) if r["thread"] == "trello:c:Xy1Ab2Cd")
        assert card["text"] == CARD1_TEXT
        assert {r["ext_id"] for r in messages(db) if r["thread"] == JIRA_CH} == {a1.message_id, a2.message_id}

        again = spy_push(client)
        await c2.sync("scheduled")
        assert not any(evs for evs, _c, _r in again)
    finally:
        await gw2.close()


# --- A-T13: прочитанное в хабе -> Gmail для беседы трекера --------------------------------------------------


def _read_item(channel_ext_id: str, kind: str, mid: str, ts) -> dict:
    return {
        "channel_id": 7,
        "gen": 1,
        "lease_gen": 1,
        "channel": {"ext_id": channel_ext_id, "kind": kind, "parent_ext_id": None},
        "message": {"ext_id": mid, "ts": ts, "ext_sort": 0, "thread_ext_id": None},
    }


async def test_a_t13_push_read_tracker_channel(env, gm, db):
    connector, _gw, _client, _aid = await env()
    await init_cursor(connector)
    a = jira(gm)
    other = jira(gm, replace=("WEB-39", "WEB-40"))  # другой тред Gmail — не трогается
    await connector.sync("live")

    start = len(gm.commands)
    res = await connector.push_read(_read_item(JIRA_CH, "issue", a.message_id, a.date_ts * 1000))
    assert res.outcome == "done"
    cmds = [c for _n, c in gm.commands[start:]]
    assert f'UID SEARCH X-GM-RAW "rfc822msgid:{a.message_id.strip("<>")}"' in cmds
    assert f"UID STORE {a.uid} +FLAGS.SILENT (\\Seen)" in cmds
    assert "\\Seen" in a.flags and "\\Seen" not in other.flags

    bad = await connector.push_read(_read_item(JIRA_CH, "issue", '<a"b c@x.example>', a.date_ts * 1000))
    assert bad.outcome == "drop"
    start = len(gm.commands)
    missing = await connector.push_read(_read_item(JIRA_CH, "issue", "<nope@x.example>", a.date_ts * 1000))
    assert missing.outcome == "done"
    assert not any("STORE" in c for _n, c in gm.commands[start:])
    no_ts = await connector.push_read(_read_item("trello:c:Xy1Ab2Cd", "card", a.message_id, None))
    assert no_ts.outcome == "drop"


async def test_a_t13_push_read_regular_thread_unchanged(env, gm, db):
    from .helpers import incoming

    connector, _gw, _client, _aid = await env()
    a = incoming(gm)
    res = await connector.push_read(
        _read_item(str(a.thrid), "mail_thread", "<x@example.com>", a.date_ts * 1000)
    )
    assert res.outcome == "done"
    assert "\\Seen" in a.flags
    assert not any("rfc822msgid" in c for c in gm.command_texts())


# --- A-T14: ответа в беседу трекера нет ---------------------------------------------------------------------


async def test_a_t14_send_to_tracker_channel_is_permanent_without_smtp(env, gm):
    connector, _gw, _client, _aid = await env()
    lease = {
        "outbox_id": 1,
        "lease_gen": 1,
        "send_token": "0123456789abcdef0123456789abcdef",
        "text": "Посмотрю завтра",
        "channel_ext_id": JIRA_CH,
    }
    res = await connector.send(lease)
    assert res.outcome == "permanent"
    res = await connector.send(dict(lease, channel_ext_id="trello:c:Xy1Ab2Cd"))
    assert res.outcome == "permanent"
    rec = await connector.reconcile(dict(lease, maybe_sent=True))
    assert rec.outcome == "unknown"
    assert gm.smtp_log == []
    assert gm.commands == []
