"""Отправка почты (спека 5a, тесты 10–13, 15 и поправки М2, М7–М9): `send()`/`reconcile()` через настоящее
ядро (outbox, лизы, отчёты) и `FakeGmail`/`FakeSmtp`."""

from __future__ import annotations

import asyncio
import email
import threading
from email import policy

import aiosmtplib
import pytest

from hub.connectors.mail import convert
from hub.core import outbox as outbox_mod

from .conftest import make_connector
from .helpers import incoming, init_cursor, messages, mine


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


async def _token(db) -> int:
    return await db.write(
        lambda conn: (
            conn.execute("INSERT INTO main.api_token (name, kind, hash) VALUES ('ui', 'ui', 'x')").lastrowid
        )
    )


async def enqueue(
    db, clock, thrid: int, *, text: str = "Спасибо, принято.", reply_to: str | None = None
) -> int:
    token_id = await _token(db)

    def _fn(conn):
        channel_id = conn.execute("SELECT id FROM main.channel WHERE ext_id = ?", (str(thrid),)).fetchone()[0]
        return outbox_mod.enqueue(
            conn,
            channel_id=channel_id,
            text=text,
            reply_to_ext_id=reply_to,
            origin="ui",
            api_token_id=token_id,
            idempotency_key=None,
            now_ms=int(clock() * 1000),
        ).outbox_id

    return await db.write(_fn)


def outbox_row(db, oid: int) -> dict:
    with db.reader() as conn:
        status, ext_id, error, token, maybe_sent = conn.execute(
            "SELECT status, ext_id, error, send_token, maybe_sent FROM main.outbox WHERE id = ?", (oid,)
        ).fetchone()
    return {"status": status, "ext_id": ext_id, "error": error, "token": token, "maybe_sent": maybe_sent}


async def _thread(connector, gm, **kw):
    await init_cursor(connector)
    parent = incoming(gm, **kw)
    await connector.sync("live")
    return parent


def _sent_message(gm) -> email.message.EmailMessage:
    return email.message_from_bytes(gm.sent[-1], policy=policy.default)


def _smtp_steps(gm) -> list[str]:
    return [e[0] for e in gm.smtp_log]


# --- тест 10: заголовки ответа ------------------------------------------------------------------------------


async def test_10_reply_headers(env, gm, db, clock):
    connector, _gw, _client, _aid = await env()
    await init_cursor(connector)
    first = incoming(gm, subject="Проект")
    parent = incoming(
        gm,
        thrid=first.thrid,
        subject="Re: Проект",
        in_reply_to=first.message_id,
        references=first.message_id,
        reply_to="list@example.com",
    )
    await connector.sync("live")
    oid = await enqueue(db, clock, parent.thrid)
    await connector.step_outbox()

    row = outbox_row(db, oid)
    assert row["status"] == "sent"
    msg = _sent_message(gm)
    assert msg["Message-ID"] == f"<hub-{row['token']}@gmail.com>" == row["ext_id"]
    assert msg["In-Reply-To"] == parent.message_id
    assert msg["References"].split() == [first.message_id, parent.message_id]
    assert msg["Subject"] == "Re: Проект"
    assert msg["To"] == "petr@example.com"  # From родителя, не Reply-To (М14)
    assert "владелец" in msg["From"] and "me@gmail.com" in msg["From"]
    assert not msg.is_multipart()
    assert msg.get_content_type() == "text/plain"
    assert msg.get_content_charset() == "utf-8"
    assert msg.get_content().strip() == "Спасибо, принято."
    assert ("rcpt", "petr@example.com") in gm.smtp_log
    assert ("mail", "me@gmail.com") in gm.smtp_log


async def test_10_reply_to_my_message_goes_to_its_recipients(env, gm, db, clock):
    connector, _gw, _client, _aid = await env()
    first = await _thread(connector, gm)
    my = mine(gm, thrid=first.thrid, to="Анна <anna@example.com>, boss@example.com")
    await connector.sync("live")
    oid = await enqueue(db, clock, first.thrid, reply_to=my.message_id)
    await connector.step_outbox()
    assert outbox_row(db, oid)["status"] == "sent"
    rcpts = [e[1] for e in gm.smtp_log if e[0] == "rcpt"]
    assert rcpts == ["anna@example.com", "boss@example.com"]


async def test_10_message_id_deterministic_from_send_token():
    assert convert.our_message_id("abc123", "Me@Gmail.com") == "<hub-abc123@gmail.com>"


# --- тест 11: исходы SMTP (E4 + М9) -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stage", "exc", "status"),
    [
        ("connect", aiosmtplib.SMTPConnectError("down"), "queued"),
        ("ehlo", aiosmtplib.SMTPServerDisconnected("gone"), "queued"),
        ("mail", aiosmtplib.SMTPSenderRefused(451, "try later", "me@gmail.com"), "queued"),
        ("rcpt", aiosmtplib.SMTPRecipientRefused(550, "5.1.1 no such user", "petr@example.com"), "failed"),
        ("rcpt", aiosmtplib.SMTPRecipientRefused(450, "4.2.1 try later", "petr@example.com"), "queued"),
        ("rcpt", aiosmtplib.SMTPServerDisconnected("gone"), "queued"),
        ("data", aiosmtplib.SMTPDataError(451, "4.3.0 later"), "queued"),
        ("data", aiosmtplib.SMTPDataError(554, "5.7.0 rejected"), "failed"),
        ("data", aiosmtplib.SMTPServerDisconnected("lost"), "unknown"),
        ("data", aiosmtplib.SMTPReadTimeoutError("timeout"), "unknown"),
    ],
)
async def test_11_outcomes(env, gm, db, clock, stage, exc, status):
    connector, _gw, _client, _aid = await env()
    parent = await _thread(connector, gm)
    oid = await enqueue(db, clock, parent.thrid)
    gm.smtp_faults[stage] = exc
    await connector.step_outbox()
    assert outbox_row(db, oid)["status"] == status


async def test_11_disconnect_after_body_is_unknown(env, gm, db, clock):
    connector, _gw, _client, _aid = await env()
    parent = await _thread(connector, gm)
    oid = await enqueue(db, clock, parent.thrid)
    gm.smtp_deliver_then_fail = aiosmtplib.SMTPServerDisconnected("lost after body")
    await connector.step_outbox()
    assert outbox_row(db, oid)["status"] == "unknown"


async def test_11_535_reports_temporary_then_auth_required(env, gm, db, clock, admin_client):
    connector, _gw, _client, account_id = await env()
    parent = await _thread(connector, gm)
    oid = await enqueue(db, clock, parent.thrid)
    gm.smtp_faults["login"] = aiosmtplib.SMTPAuthenticationError(
        535, "5.7.8 Username and Password not accepted"
    )
    await connector.step_outbox()
    assert outbox_row(db, oid)["status"] == "queued"  # строка не осталась в sending
    assert connector.auth_required is True
    r = await admin_client.get("/internal/v1/admin/status")
    acc = next(a for a in r.json()["accounts"] if a["id"] == account_id)
    assert acc["status"] == "auth_required"
    assert "data" not in _smtp_steps(gm)


@pytest.mark.parametrize("stage", ["mail", "data"])
async def test_m9_daily_quota_is_flood(env, gm, db, clock, stage):
    connector, _gw, _client, _aid = await env()
    parent = await _thread(connector, gm)
    oid = await enqueue(db, clock, parent.thrid)
    if stage == "mail":
        gm.smtp_faults["mail"] = aiosmtplib.SMTPSenderRefused(
            550, "5.4.5 Daily user sending quota exceeded.", "me@gmail.com"
        )
    else:
        gm.smtp_faults["data"] = aiosmtplib.SMTPDataError(550, "5.4.5 Daily sending quota exceeded.")
    await connector.step_outbox()
    assert outbox_row(db, oid)["status"] == "queued"
    with db.reader() as conn:
        flood_until = conn.execute("SELECT flood_until FROM main.account").fetchone()[0]
    assert flood_until is not None and flood_until - int(clock() * 1000) >= 23 * 3600 * 1000


async def test_m9_imap_phase_timeout_is_temporary_without_smtp(env, gm, db, clock, monkeypatch):
    from hub.connectors.mail import connector as connector_mod

    connector, _gw, _client, _aid = await env()
    parent = await _thread(connector, gm)
    oid = await enqueue(db, clock, parent.thrid)
    monkeypatch.setattr(connector_mod, "SEND_IMAP_BUDGET_S", 0.05)
    gate = threading.Event()
    orig = gm.search

    def _slow(args):
        gate.wait(1.0)
        return orig(args)

    gm.search = _slow
    await connector.step_outbox()
    gate.set()
    assert outbox_row(db, oid)["status"] == "queued"
    assert gm.smtp_log == []


# --- тест 12: сверка (E5 + М9) ------------------------------------------------------------------------------


async def _expire_lease(connector, client, clock):
    leases = await client.lease(lease_ms=connector.lease_ms)
    assert leases and leases[0]["mode"] == "send"
    clock.advance_ms(connector.lease_ms + 1000)
    return leases[0]


async def test_12_reconcile_found_is_sent_without_smtp(env, gm, db, clock):
    connector, _gw, client, _aid = await env()
    parent = await _thread(connector, gm)
    oid = await enqueue(db, clock, parent.thrid)
    lease = await _expire_lease(connector, client, clock)
    # процесс «умер» после DATA: копия письма уже в ящике
    token = lease["send_token"]
    gm.deliver_sent(
        convert.build_reply(
            parent=convert.reply_parent(
                (await connector._gw.sender.fetch_parent_headers([parent.uid]))[0],  # noqa: SLF001
                connector._me,  # noqa: SLF001
            ),
            recipients=["petr@example.com"],
            text="Спасибо, принято.",
            address="me@gmail.com",
            display_name="владелец",
            message_id=f"<hub-{token}@gmail.com>",
            now_ts=1.0,
        )
    )
    await connector.step_outbox()
    row = outbox_row(db, oid)
    assert row["status"] == "sent" and row["ext_id"] == f"<hub-{token}@gmail.com>"
    assert gm.smtp_log == []


async def test_12_reconcile_not_found_is_unknown_without_smtp(env, gm, db, clock):
    connector, _gw, client, _aid = await env()
    parent = await _thread(connector, gm)
    oid = await enqueue(db, clock, parent.thrid)
    await _expire_lease(connector, client, clock)
    await connector.step_outbox()
    assert outbox_row(db, oid)["status"] == "unknown"
    assert gm.smtp_log == []


async def test_12_reconcile_falls_back_to_rfc822msgid_other_thread(env, gm, db, clock):
    connector, _gw, client, _aid = await env()
    parent = await _thread(connector, gm)
    oid = await enqueue(db, clock, parent.thrid)
    lease = await _expire_lease(connector, client, clock)
    gm.sent_copy_mode = "other_thread"
    raw = (
        f"From: me@gmail.com\r\nTo: petr@example.com\r\nSubject: Re: x\r\n"
        f"Message-ID: <hub-{lease['send_token']}@gmail.com>\r\nIn-Reply-To: {parent.message_id}\r\n\r\nтело"
    ).encode()
    gm.deliver_sent(raw)
    await connector.step_outbox()
    assert outbox_row(db, oid)["status"] == "sent"
    assert any("rfc822msgid:" in c for c in gm.command_texts())


# --- тест 13 и М7: эхо --------------------------------------------------------------------------------------


async def test_13_echo_after_sent_glued_to_same_row(env, gm, db, clock):
    connector, _gw, _client, _aid = await env()
    parent = await _thread(connector, gm)
    oid = await enqueue(db, clock, parent.thrid)
    await connector.step_outbox()
    row = outbox_row(db, oid)
    assert row["status"] == "sent"
    await connector.sync("live")  # копия из «Вся почта» с тем же Message-ID
    mine_rows = [r for r in messages(db) if r["thread"] == str(parent.thrid) and r["is_me"]]
    assert len(mine_rows) == 1
    assert mine_rows[0]["ext_id"] == row["ext_id"] and mine_rows[0]["oid"] == oid
    assert mine_rows[0]["text"] == "Спасибо, принято."


async def test_m7_echo_before_report(env, gm, db, clock):
    connector, _gw, client, _aid = await env()
    parent = await _thread(connector, gm)
    oid = await enqueue(db, clock, parent.thrid)
    [lease] = await client.lease(lease_ms=connector.lease_ms)
    result = await connector.send(lease)
    assert result.outcome == "sent"
    await connector.sync("live")  # эхо раньше отчёта
    assert outbox_row(db, oid)["status"] == "sent"
    await client.report(
        outbox_id=oid, lease_gen=lease["lease_gen"], outcome="sent", ext_id=result.ext_id
    )  # дубль отчёта — тихий успех
    mine_rows = [r for r in messages(db) if r["thread"] == str(parent.thrid) and r["is_me"]]
    assert len(mine_rows) == 1 and mine_rows[0]["oid"] == oid


async def test_m7_echo_resolves_unknown(env, gm, db, clock):
    connector, _gw, _client, _aid = await env()
    parent = await _thread(connector, gm)
    oid = await enqueue(db, clock, parent.thrid)
    gm.smtp_deliver_then_fail = aiosmtplib.SMTPServerDisconnected("lost after body")
    await connector.step_outbox()
    assert outbox_row(db, oid)["status"] == "unknown"
    await connector.sync("live")
    assert outbox_row(db, oid)["status"] == "sent"
    mine_rows = [r for r in messages(db) if r["thread"] == str(parent.thrid) and r["is_me"]]
    assert len(mine_rows) == 1


async def test_m7_maybe_sent_searches_first_without_smtp(env, gm, db, clock):
    connector, _gw, client, _aid = await env()
    parent = await _thread(connector, gm)
    oid = await enqueue(db, clock, parent.thrid)
    gm.sent_copy_mode = "none"
    await _expire_lease(connector, client, clock)
    await connector.step_outbox()  # сверка: копии нет -> unknown
    assert outbox_row(db, oid)["status"] == "unknown"
    await db.write(
        lambda conn: outbox_mod.resolve_unknown(conn, oid, decision="retry", now_ms=int(clock() * 1000))
    )
    row = outbox_row(db, oid)
    assert row["maybe_sent"] == 1
    # копия всё-таки появилась (Gmail положил её поздно)
    gm.sent_copy_mode = "same_thread"
    raw = (
        f"From: me@gmail.com\r\nTo: petr@example.com\r\nSubject: Re: x\r\n"
        f"Message-ID: <hub-{row['token']}@gmail.com>\r\nIn-Reply-To: {parent.message_id}\r\n\r\nтело"
    ).encode()
    gm.deliver_sent(raw)
    gm.smtp_log.clear()
    await connector.step_outbox()
    assert outbox_row(db, oid)["status"] == "sent"
    assert gm.smtp_log == []


# --- тест 15 и М8: родитель ---------------------------------------------------------------------------------


async def test_15_parent_not_found_failed_without_smtp(env, gm, db, clock):
    connector, _gw, _client, _aid = await env()
    parent = await _thread(connector, gm)
    oid = await enqueue(db, clock, parent.thrid, reply_to="<nope@example.com>")
    await connector.step_outbox()
    row = outbox_row(db, oid)
    assert row["status"] == "failed"
    assert "не найдено" in row["error"]
    assert gm.smtp_log == []


async def test_m4_draft_never_parent(env, gm, db, clock):
    connector, _gw, _client, _aid = await env()
    parent = await _thread(connector, gm)
    mine(gm, thrid=parent.thrid, labels=("\\Draft",), flags=("\\Draft",), to="draft@example.com")
    oid = await enqueue(db, clock, parent.thrid)
    await connector.step_outbox()
    assert outbox_row(db, oid)["status"] == "sent"
    assert [e[1] for e in gm.smtp_log if e[0] == "rcpt"] == ["petr@example.com"]


async def test_m8_odd_message_id_never_in_command(env, gm, db, clock):
    connector, _gw, _client, _aid = await env()
    await init_cursor(connector)
    odd = '<we"ird id@example.com>'
    raw = (
        "From: Petr <petr@example.com>\r\nTo: me@gmail.com\r\nSubject: odd\r\n"
        f"Message-ID: {odd}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nтело\r\n"
    ).encode()
    parent = gm.add(raw)
    await connector.sync("live")
    ext_id = convert.normalize_msgid(odd)
    assert any(r["ext_id"] == ext_id for r in messages(db))
    oid = await enqueue(db, clock, parent.thrid, reply_to=ext_id)
    await connector.step_outbox()
    assert outbox_row(db, oid)["status"] == "sent"
    assert _sent_message(gm)["In-Reply-To"] == ext_id
    for c in gm.command_texts():
        assert "we" not in c or "weird" not in c.replace('"', "")
        assert 'we"ird' not in c


async def test_m8_unsafe_own_id_no_raw_search(env, gm):
    connector, _gw, _client, _aid = await env()
    parent = await _thread(connector, gm)
    before = len(gm.command_texts())
    assert await connector._find_own(parent.thrid, '<a"b c@x>') is False  # noqa: SLF001
    assert not any("X-GM-RAW" in c for c in gm.command_texts()[before:])


async def test_m8_duplicate_parent_message_id_is_permanent(env, gm, db, clock):
    connector, _gw, _client, _aid = await env()
    parent = await _thread(connector, gm)
    incoming(gm, thrid=parent.thrid, msgid=parent.message_id)
    oid = await enqueue(db, clock, parent.thrid, reply_to=parent.message_id)
    await connector.step_outbox()
    assert outbox_row(db, oid)["status"] == "failed"
    assert gm.smtp_log == []


# --- М2: отправка во время долгой стартовой загрузки --------------------------------------------------------


async def test_m2_send_during_long_load_not_unknown(env, gm, db, clock):
    connector, _gw, _client, _aid = await env()
    parent = await _thread(connector, gm)
    for _ in range(5):
        incoming(gm)
    gm.fetch_gate = threading.Event()  # рабочее соединение повисло на телах писем
    sync_task = asyncio.ensure_future(connector.sync("live"))
    await asyncio.sleep(0.1)
    assert not sync_task.done()
    oid = await enqueue(db, clock, parent.thrid)
    await asyncio.wait_for(connector.step_outbox(), timeout=10)
    assert outbox_row(db, oid)["status"] in ("sent", "queued")
    assert outbox_row(db, oid)["status"] == "sent"
    gm.fetch_gate.set()
    await asyncio.wait_for(sync_task, timeout=10)


# --- лимитер и прочее ---------------------------------------------------------------------------------------


async def test_limiter_one_mail_per_5s(env, gm, db, clock):
    slept: list[float] = []

    async def _sleep(s):
        slept.append(s)

    connector, _gw, _client, _aid = await env(sleep=_sleep)
    parent = await _thread(connector, gm)
    await enqueue(db, clock, parent.thrid, text="раз")
    await connector.step_outbox()
    await enqueue(db, clock, parent.thrid, text="два")
    await connector.step_outbox()
    assert any(4.0 < s <= 5.0 for s in slept)


async def test_empty_and_too_long_text_permanent(env, gm, db, clock):
    connector, _gw, _client, _aid = await env()
    parent = await _thread(connector, gm)
    res = await connector.send(
        {"text": " ", "channel_ext_id": str(parent.thrid), "send_token": "t", "outbox_id": 1, "lease_gen": 1}
    )
    assert res.outcome == "permanent"
    res = await connector.send(
        {"text": "я" * 100_001, "channel_ext_id": str(parent.thrid), "send_token": "t"}
    )
    assert res.outcome == "permanent"
    assert gm.smtp_log == []


# --- живой запуск 24.09: ящик «только на приём» — ответ из хаба отказывается сразу, SMTP не трогается


async def test_receive_only_account_rejects_send_without_smtp(env, gm, db, clock):
    connector, _gw, _client, _aid = await env(config={"send_enabled": False})
    parent = await _thread(connector, gm)
    oid = await enqueue(db, clock, parent.thrid)
    await connector.step_outbox()
    row = outbox_row(db, oid)
    assert row["status"] == "failed"
    assert "только на приём" in row["error"]
    assert gm.smtp_log == []
