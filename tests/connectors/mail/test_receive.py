"""Приём почты (спека 5a, тесты 2–7 и поправки М1, М3–М6, М11, М12): `MailConnector.sync()` с настоящим
ядром в процессе и `FakeGmail` на месте соединения `imaplib`."""

from __future__ import annotations

import json

import httpx
import pytest

from hub.connectors.mail import connector as connector_mod

from .conftest import make_connector
from .fakegmail import make_email, new_msgid
from .helpers import (
    channels,
    core_cursor,
    incoming,
    init_cursor,
    max_seq,
    messages,
    mine,
    spy_push,
)


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


# --- тест 2: догон ------------------------------------------------------------------------------------------


async def test_2_catch_up_two_threads_cursor_and_idempotent_repeat(env, gm, db):
    connector, _gw, client, _aid = await env()
    await init_cursor(connector)
    assert (await core_cursor(client))["last_uid"] == 0

    a1 = incoming(gm, subject="Смета")
    b1 = incoming(gm, subject="Отпуск")
    a2 = incoming(gm, thrid=a1.thrid, subject="Re: Смета", in_reply_to=a1.message_id)
    await connector.sync("live")

    rows = messages(db)
    assert [r["ext_id"] for r in rows] == [a1.message_id, b1.message_id, a2.message_id]
    assert [r["ts"] for r in rows] == sorted(r["ts"] for r in rows)
    assert rows[0]["ts"] == a1.date_ts * 1000
    chans = channels(db)
    assert set(chans) == {str(a1.thrid), str(b1.thrid)}
    assert {c["kind"] for c in chans.values()} == {"mail_thread"}
    assert chans[str(a1.thrid)]["title"] == "Смета"
    assert rows[2]["reply_to"] == a1.message_id
    cur = await core_cursor(client)
    assert cur["last_uid"] == a2.uid == 3
    assert cur["uidvalidity"] == gm.uidvalidity

    before = max_seq(db)
    calls = spy_push(client)
    await connector.sync("scheduled")
    assert max_seq(db) == before
    assert all(r["applied"] == 0 for _e, _c, r in calls)


# --- тест 3: отбор C3 ---------------------------------------------------------------------------------------


async def test_3_selection_rules(env, gm, db):
    connector, _gw, _client, _aid = await env()
    await init_cursor(connector)

    known = incoming(gm, subject="Договор")
    await connector.sync("live")

    promo = incoming(gm, subject="Скидки 50%", categories=("promotions",))
    # моё письмо в неизвестный тред — даже с «Входящими» (письмо себе/ответ всем) беседу не открывает
    my_unknown = mine(gm, subject="Новая тема", labels=("\\Sent", "\\Inbox"), to="petr@example.com")
    archived_reply = incoming(gm, thrid=known.thrid, labels=(), subject="Re: Договор")
    my_web_reply = mine(gm, thrid=known.thrid, subject="Re: Договор")
    await connector.sync("live")

    ids = {r["ext_id"] for r in messages(db)}
    assert promo.message_id not in ids
    assert my_unknown.message_id not in ids
    assert archived_reply.message_id in ids
    assert my_web_reply.message_id in ids
    assert str(promo.thrid) not in channels(db)
    assert str(my_unknown.thrid) not in channels(db)
    web = next(r for r in messages(db) if r["ext_id"] == my_web_reply.message_id)
    assert web["is_me"] == 1


async def test_3_social_excluded_but_updates_included_by_default(env, gm, db):
    connector, _gw, _client, _aid = await env()
    await init_cursor(connector)
    social = incoming(gm, categories=("social",))
    updates = incoming(gm, categories=("updates",))
    await connector.sync("live")
    ids = {r["ext_id"] for r in messages(db)}
    assert social.message_id not in ids
    assert updates.message_id in ids


# --- тест 4: только PEEK, никакого STORE --------------------------------------------------------------------


async def test_4_only_peek_never_store(env, gm, db):
    connector, _gw, _client, _aid = await env()
    t = incoming(gm, subject="Первое")
    await connector.sync("start")  # стартовая загрузка
    incoming(gm, thrid=t.thrid, subject="Re: Первое")
    incoming(gm, subject="Второе")
    await connector.sync("live")
    await connector.sync("scheduled")

    cmds = gm.command_texts()
    assert any("BODY.PEEK[]" in c for c in cmds)
    for c in cmds:
        words = c.upper().split()
        assert "BODY[" not in c, c
        assert "STORE" not in words[:2], c  # `STORE` / `UID STORE` — хаб флаги не трогает
        assert words[0] != "SELECT", c
    # ни одно письмо не стало прочитанным из-за хаба
    assert all("\\Seen" not in m.flags for m in gm.messages)
    assert len(messages(db)) == 3


# --- тест 5: смена UIDVALIDITY ------------------------------------------------------------------------------


async def test_5_uidvalidity_change_resync_without_duplicates(env, gm, db):
    connector, _gw, client, _aid = await env()
    await init_cursor(connector)
    a = incoming(gm, subject="Тема A")
    b = incoming(gm, thrid=a.thrid, subject="Re: Тема A")
    await connector.sync("live")
    seq_before = {r["ext_id"]: r["seq"] for r in messages(db)}

    # Gmail пересобрал «Вся почта»: новый UIDVALIDITY, UID перенумерованы
    gm.uidvalidity += 1
    for m in gm.messages:
        m.uid += 1000
    gm.next_uid += 1000
    c = incoming(gm, thrid=a.thrid, subject="Re: Тема A")  # пришло в разрыв
    d = incoming(gm, subject="Новая тема")
    await connector.sync("live")

    rows = messages(db)
    ids = [r["ext_id"] for r in rows]
    assert sorted(ids) == sorted([a.message_id, b.message_id, c.message_id, d.message_id])
    assert len(ids) == len(set(ids))
    after = {r["ext_id"]: r["seq"] for r in rows}
    assert after[a.message_id] == seq_before[a.message_id]  # повторный upsert — no-op
    cur = await core_cursor(client)
    assert cur["uidvalidity"] == gm.uidvalidity
    assert cur["last_uid"] == d.uid


# --- тест 6: прочитанность C4 -------------------------------------------------------------------------------


@pytest.mark.parametrize("condstore", [True, False])
async def test_6_read_marker(admin_client, internal_transport, db, condstore):
    from .fakegmail import FakeGmail

    gm = FakeGmail(condstore=condstore)
    connector, gw, _client, _aid = await make_connector(admin_client, internal_transport, gm)
    try:
        await init_cursor(connector)
        older = incoming(gm, subject="Вопрос")
        newer = incoming(gm, thrid=older.thrid, subject="Re: Вопрос")
        await connector.sync("live")
        assert channels(db)[str(older.thrid)]["last_read"] is None

        gm.set_flags(older.uid, add=("\\Seen",))
        await connector.sync("live")
        assert channels(db)[str(older.thrid)]["last_read"] == older.message_id

        gm.set_flags(newer.uid, add=("\\Seen",))
        await connector.sync("live")
        assert channels(db)[str(older.thrid)]["last_read"] == newer.message_id

        # непрочитанное на телефоне обратно — маркер назад не едет
        gm.set_flags(newer.uid, remove=("\\Seen",))
        await connector.sync("live")
        assert channels(db)[str(older.thrid)]["last_read"] == newer.message_id
    finally:
        await gw.close()


async def test_6_only_newer_seen_keeps_marker_empty(env, gm, db):
    connector, _gw, _client, _aid = await env()
    await init_cursor(connector)
    older = incoming(gm)
    newer = incoming(gm, thrid=older.thrid)
    await connector.sync("live")
    gm.set_flags(newer.uid, add=("\\Seen",))
    await connector.sync("live")
    assert channels(db)[str(older.thrid)]["last_read"] is None


async def test_6_my_messages_do_not_block_marker(env, gm, db):
    connector, _gw, _client, _aid = await env()
    await init_cursor(connector)
    a = incoming(gm, flags=("\\Seen",))
    me = mine(gm, thrid=a.thrid, flags=())  # своё без \Seen — не «непрочитанное»
    await connector.sync("live")
    assert channels(db)[str(a.thrid)]["last_read"] == me.message_id


# --- тест 7: стартовая загрузка C5 --------------------------------------------------------------------------


async def test_7_startup_load_batches_limits_backfill_and_read(env, gm, db):
    connector, _gw, client, _aid = await env()
    big = incoming(gm, subject="Большой тред", flags=("\\Seen",))
    big_msgs = [big] + [incoming(gm, thrid=big.thrid, flags=("\\Seen",)) for _ in range(34)]
    threads = [incoming(gm, subject=f"Тред {i}") for i in range(24)]
    promo = incoming(gm, subject="Акция", categories=("promotions",))
    old = incoming(gm, subject="Старьё", date_ts=gm.now_ts - 20 * 86400)

    activity: list[bool] = []
    orig = client.set_activity

    async def _spy(*, backfill):
        activity.append(backfill)
        return await orig(backfill=backfill)

    client.set_activity = _spy
    requested: list[str] = []
    connector._request_sync = requested.append  # noqa: SLF001

    await connector.sync("start")
    chans = channels(db)
    assert len(chans) == 20  # порция 20 тредов
    assert "initial" in requested
    assert activity == [True]
    assert (await core_cursor(client)).get("startup") is not None

    await connector.sync("initial")
    chans = channels(db)
    assert len(chans) == 25
    assert str(promo.thrid) not in chans
    assert str(old.thrid) not in chans
    assert activity == [True, True, False]
    assert (await core_cursor(client)).get("startup") is None

    big_rows = [r for r in messages(db) if r["thread"] == str(big.thrid)]
    assert len(big_rows) == 30
    assert {r["ext_id"] for r in big_rows} == {m.message_id for m in big_msgs[-30:]}
    assert chans[str(big.thrid)]["last_read"] == big_msgs[-1].message_id
    assert chans[str(threads[0].thrid)]["last_read"] is None


async def test_m6_startup_survives_restart(env, gm, db, admin_client, internal_transport):
    connector, gw, client, account_id = await env()
    for i in range(23):
        incoming(gm, subject=f"Тред {i}")
    connector._request_sync = lambda _r: None  # noqa: SLF001
    await connector.sync("start")
    assert len(channels(db)) == 20

    # рестарт процесса: новый коннектор (память пустая) того же аккаунта
    from hub.connectors.mail.connector import MailConnector

    from .conftest import make_gateway

    gw2 = make_gateway(gm)
    try:
        c2 = MailConnector(client, gw2, account_id=account_id, config={"address": "me@gmail.com"})
        await c2.sync("start")
        assert len(channels(db)) == 23
        ids = [r["ext_id"] for r in messages(db)]
        assert len(ids) == len(set(ids)) == 23
        assert (await core_cursor(client)).get("startup") is None
    finally:
        await gw2.close()


# --- М1: курсор только вместе с событиями -------------------------------------------------------------------


async def test_m1_core_5xx_keeps_cursor_and_retry_has_no_duplicates(env, gm, db):
    connector, _gw, client, _aid = await env()
    await init_cursor(connector)
    a = incoming(gm)
    b = incoming(gm)

    orig = client.push_events
    failed = []

    async def _flaky(events, cursor=None):
        if events and not failed:
            failed.append(True)
            request = httpx.Request("POST", "http://hub-internal/internal/v1/events")
            raise httpx.HTTPStatusError(
                "boom", request=request, response=httpx.Response(503, request=request)
            )
        return await orig(events, cursor=cursor)

    client.push_events = _flaky
    with pytest.raises(httpx.HTTPStatusError):
        await connector.sync("live")
    assert (await core_cursor(client))["last_uid"] == 0
    assert messages(db) == []

    await connector.sync("live")
    ids = [r["ext_id"] for r in messages(db)]
    assert sorted(ids) == sorted([a.message_id, b.message_id])
    assert (await core_cursor(client))["last_uid"] == b.uid


async def test_m1_cursor_travels_in_same_batch_as_events(env, gm):
    connector, _gw, client, _aid = await env()
    await init_cursor(connector)
    incoming(gm)
    calls = spy_push(client)
    await connector.sync("live")
    with_events = [(e, c) for e, c, _r in calls if any(x["type"] == "upsert" for x in e)]
    assert with_events and all(c is not None for _e, c in with_events)
    assert json.loads(with_events[-1][1]["value"])["last_uid"] == 1


async def test_m1_mail_never_uses_emit(env, gm):
    connector, _gw, _client, _aid = await env()
    emitted = []
    connector.emit = emitted.append
    incoming(gm)
    await connector.sync("start")
    incoming(gm)
    await connector.sync("live")
    assert emitted == []


async def test_m1_portions_of_100(env, gm, monkeypatch):
    monkeypatch.setattr(connector_mod, "PORTION", 3)
    connector, _gw, client, _aid = await env()
    await init_cursor(connector)
    for _ in range(7):
        incoming(gm)
    calls = spy_push(client)
    await connector.sync("live")
    cursors = [json.loads(c["value"])["last_uid"] for _e, c, _r in calls if c is not None]
    assert cursors[:3] == [3, 6, 7]


# --- М3: attrs треда ----------------------------------------------------------------------------------------


async def test_m3_labels_union_keeps_thread_in_project(env, gm, db, admin_client):
    connector, _gw, _client, account_id = await env()
    r = await admin_client.post("/internal/v1/admin/projects", json={"name": "Globex"})
    project_id = r.json()["project_id"]
    r = await admin_client.post(
        "/internal/v1/admin/map-rules",
        json={
            "account_id": account_id,
            "project_id": project_id,
            "match": {"attr": "labels", "has": "Работа"},
        },
    )
    assert r.status_code == 200, r.text
    await init_cursor(connector)

    first = incoming(gm, labels=("\\Inbox", "Работа"))
    await connector.sync("live")
    assert channels(db)[str(first.thrid)]["project_id"] == project_id

    mine(gm, thrid=first.thrid)  # мой ответ без метки
    await connector.sync("live")
    ch = channels(db)[str(first.thrid)]
    assert ch["project_id"] == project_id
    assert "Работа" in ch["attrs"]["labels"]
    assert "SENT" in ch["attrs"]["labels"] and "INBOX" in ch["attrs"]["labels"]


async def test_m3_many_recipients_and_domains_attrs_small(env, gm, db):
    connector, _gw, _client, _aid = await env()
    await init_cursor(connector)
    to = ", ".join(f"user{i}@domain{i}.example.com" for i in range(200))
    first = incoming(
        gm, to=to + ", me@gmail.com", labels=("\\Inbox",) + tuple(f"метка-{i:02d}" for i in range(60))
    )
    for i in range(30):
        incoming(gm, thrid=first.thrid, from_=f"u{i} <u{i}@sender{i}.example.org>")
    await connector.sync("live")
    ch = channels(db)[str(first.thrid)]
    assert len(json.dumps(ch["attrs"], ensure_ascii=False).encode()) <= 2048
    assert len(ch["attrs"]["from_domains"]) <= 20
    assert "to" not in ch["attrs"]
    assert len([r for r in messages(db) if r["thread"] == str(first.thrid)]) == 31


# --- М4: черновики ------------------------------------------------------------------------------------------


async def test_m4_draft_in_known_thread_not_taken(env, gm, db):
    connector, _gw, _client, _aid = await env()
    await init_cursor(connector)
    a = incoming(gm)
    await connector.sync("live")
    draft = mine(gm, thrid=a.thrid, labels=("\\Draft",), flags=("\\Draft",))
    draft2 = mine(gm, thrid=a.thrid, labels=("\\Draft",), flags=())
    await connector.sync("live")
    ids = {r["ext_id"] for r in messages(db)}
    assert draft.message_id not in ids and draft2.message_id not in ids


# --- М5: отбор по треду порции и догрузка треда целиком -----------------------------------------------------


async def test_m5_thread_opened_by_inbox_loads_whole_thread(env, gm, db):
    connector, _gw, _client, _aid = await env()
    early = mine(gm, subject="Предложение")  # моё раннее письмо — до курсора
    await init_cursor(connector)  # C5 его не берёт: тред не во «Входящих»
    assert messages(db) == []
    my_now = mine(gm, thrid=early.thrid, subject="Re: Предложение")
    reply = incoming(gm, thrid=early.thrid, subject="Re: Предложение")
    await connector.sync("live")
    ids = [r["ext_id"] for r in messages(db)]
    assert ids == [early.message_id, my_now.message_id, reply.message_id]


# --- М11: пустой диапазон -----------------------------------------------------------------------------------


async def test_m11_empty_range_returns_last_but_nothing_emitted(env, gm, db):
    connector, _gw, client, _aid = await env()
    incoming(gm)
    await connector.sync("start")
    count = len(messages(db))
    calls = spy_push(client)
    await connector.sync("live")  # `UID 2:*` у сервера вернёт UID 1 — отфильтровать
    assert any("UID SEARCH UID 2:*" in c for c in gm.command_texts())
    assert len(messages(db)) == count
    assert not any(e for e, _c, _r in calls)


async def test_m11_big_message_only_first_2mb(env, gm, db, monkeypatch):
    from hub.connectors.mail import gateway as gateway_mod

    monkeypatch.setattr(gateway_mod, "BODY_FULL_MAX", 1000)
    monkeypatch.setattr(gateway_mod, "BODY_PARTIAL_BYTES", 600)
    connector, _gw, _client, _aid = await env()
    await init_cursor(connector)
    big = incoming(gm, body="длинный текст " * 200)
    await connector.sync("live")
    assert any("BODY.PEEK[]<0.600>" in c for c in gm.command_texts())
    row = next(r for r in messages(db) if r["ext_id"] == big.message_id)
    assert "письмо большое" in row["text"]


# --- М12: прочитанность на каждой сверке --------------------------------------------------------------------


async def test_m12_read_marker_on_live_sync_without_new_mail(env, gm, db):
    connector, _gw, _client, _aid = await env()
    await init_cursor(connector)
    a = incoming(gm)
    await connector.sync("live")
    gm.set_flags(a.uid, add=("\\Seen",))
    await connector.sync("live")  # новых писем нет — только флаги
    assert channels(db)[str(a.thrid)]["last_read"] == a.message_id


# --- C1: поля события ---------------------------------------------------------------------------------------


async def test_c1_event_fields(env, gm, db):
    connector, _gw, _client, _aid = await env()
    await init_cursor(connector)
    to_me = incoming(gm, to="me@gmail.com", body="Задача DEV-123 тут https://x.example.com/a")
    cc_me = incoming(gm, to="boss@example.com")
    no_id = incoming(gm, msgid=None)
    await connector.sync("live")
    rows = {r["ext_id"]: r for r in messages(db)}
    assert rows[to_me.message_id]["mentions_me"] == 1
    assert rows[cc_me.message_id]["mentions_me"] == 0
    assert f"<gm-{no_id.msgid}@hub>" in rows
    with db.reader() as conn:
        refs = conn.execute(
            "SELECT refs_json FROM main.message WHERE ext_id = ?", (to_me.message_id,)
        ).fetchone()[0]
        sender = conn.execute(
            "SELECT c.ext_id, c.name FROM main.message m JOIN main.contact c ON c.id = m.sender_contact_id "
            "WHERE m.ext_id = ?",
            (to_me.message_id,),
        ).fetchone()
    assert json.loads(refs) == ["DEV-123", "https://x.example.com/a"]
    assert sender == ("petr@example.com", "Пётр Петров")


async def test_c1_work_account_goes_to_work_db(env, gm, db):
    connector, _gw, _client, _aid = await env(db="work")
    await init_cursor(connector)
    a = incoming(gm)
    await connector.sync("live")
    assert messages(db, "main") == []
    assert [r["ext_id"] for r in messages(db, "work")] == [a.message_id]


async def test_m14_broken_letter_becomes_stub_not_lost(env, gm, db, monkeypatch, caplog):
    from hub.connectors.mail import convert

    connector, _gw, _client, _aid = await env()
    await init_cursor(connector)
    good = incoming(gm, body="нормальное")
    bad = incoming(gm, subject="плохое", body="ТАЙНА")
    orig = convert.message_event

    def _flaky(m, **kw):
        if m.uid == bad.uid:
            raise UnicodeDecodeError("utf-8", "ТАЙНА".encode(), 0, 1, "boom")
        return orig(m, **kw)

    monkeypatch.setattr(convert, "message_event", _flaky)
    await connector.sync("live")
    rows = {r["ext_id"]: r for r in messages(db)}
    assert rows[good.message_id]["text"] == "нормальное"
    assert rows[bad.message_id]["text"] == "[письмо не разобрано]"
    assert "ТАЙНА" not in caplog.text and "\\xd0" not in caplog.text


# --- Ф11-8: «кому» у своего письма -------------------------------------------------------------------------


async def test_f11_8_own_letter_mail_to_reaches_core(env, gm, db):
    connector, _gw, _client, _aid = await env()
    await init_cursor(connector)
    known = incoming(gm, subject="Договор")
    raw = make_email(
        msgid=new_msgid("my"),
        from_="владелец <me@gmail.com>",
        to="Алиса <Alisa@Client.example>",
        cc="boss@client.example",
        subject="Re: Договор",
    )
    my_letter = gm.add(raw, thrid=known.thrid, labels=("\\Sent",), flags=("\\Seen",))
    await connector.sync("live")
    with db.reader() as conn:
        got = dict(conn.execute("SELECT ext_id, mail_to_json FROM main.message").fetchall())
    assert json.loads(got[my_letter.message_id]) == ["alisa@client.example", "boss@client.example"]
    assert got[known.message_id] is None  # чужое письмо — без адресатов
