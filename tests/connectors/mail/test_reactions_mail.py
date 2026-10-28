"""Ф15-1 C1/C2 (docs/architecture.md): реакции в письмах.

Приём: письмо-реакция Gmail (`text/vnd.google.email-reaction+json`) и RFC 9078 (`Content-Disposition:
reaction`) — не строка ленты, а значок на письме-цели; кривая часть — обычное письмо. Отправка: письмо из трёх
частей plain / реакция / html, адресаты как у ответа, ограничения Gmail -> `drop`, повтор после сбоя находит
своё письмо и не шлёт второе. Ядро — настоящее в процессе, ящики — `FakeGmail` и `FakePlain`."""

from __future__ import annotations

import email
import json
from email import policy
from email.message import EmailMessage

import aiosmtplib
import pytest

from hub.connectors.mail import convert
from hub.connectors.mail.imapwire import FetchedMsg

from .conftest import make_connector
from .fakegmail import new_msgid
from .fakeplain import ADDRESS as PLAIN_ADDRESS
from .helpers import ME, incoming, init_cursor, messages, mine
from .test_plain import ME as PLAIN_ME
from .test_plain import env as plain_env  # noqa: F401 — фикстура вида `imap`
from .test_plain import (
    letter,
    srv,  # noqa: F401 — сервер вида `imap`
)


def reaction_raw(
    *,
    msgid: str,
    target: str | None,
    emoji: object = "👍",
    from_: str = "Пётр Петров <petr@gmail.com>",
    to: str = ME,
    kind: str = "gmail",
    version: object = 1,
    in_reply_to: str | None = None,
    attachment: bool = False,
    subject: str = "Re: Проект",
) -> bytes:
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = from_
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = msgid
    irt = in_reply_to if in_reply_to is not None else target
    if irt:
        msg["In-Reply-To"] = irt
        msg["References"] = irt
    msg["Date"] = "Tue, 29 Sep 2026 10:00:00 +0000"
    text = emoji if isinstance(emoji, str) else "?"
    msg.set_content(text)
    if kind == "gmail":
        body = json.dumps({"emoji": emoji, "version": version}, ensure_ascii=False)
        msg.add_alternative(body, subtype="vnd.google.email-reaction+json")
        if attachment:
            msg.get_payload()[1]["Content-Disposition"] = "attachment"
        msg.add_alternative(f"<p>{text}</p>", subtype="html")
    else:  # RFC 9078
        msg.add_attachment(text, subtype="plain", disposition="reaction")
    return msg.as_bytes()


def fetched(raw: bytes, *, labels=("\\Inbox",)) -> FetchedMsg:
    return FetchedMsg(uid=1, labels=list(labels), body=raw, header=raw, internaldate_ms=1_790_000_000_000)


def target_reactions(db, ext_id: str, schema: str = "main"):
    with db.reader() as conn:
        row = conn.execute(
            f"SELECT reactions_json FROM {schema}.message WHERE ext_id = ?", (ext_id,)
        ).fetchone()
    return json.loads(row[0]) if row and row[0] else None


# --- п. 9: разбор ---------------------------------------------------------------------------------------


def test_9_gmail_and_rfc9078_are_reactions():
    target = "<t1@example.com>"
    assert convert.reaction_of(reaction_raw(msgid="<r1@x>", target=target)) == ("👍", target)
    assert convert.reaction_of(reaction_raw(msgid="<r2@x>", target=target, kind="rfc9078")) == ("👍", target)
    assert convert.reaction_of(reaction_raw(msgid="<r3@x>", target=target, emoji="❤️")) == ("❤️", target)


@pytest.mark.parametrize(
    "kw",
    [
        {"version": "1"},
        {"version": True},
        {"version": 2},
        {"emoji": "👍👍"},
        {"emoji": "да"},
        {"emoji": 5},
        {"in_reply_to": "<a@x> <b@x>"},
        {"target": None},
        {"attachment": True},
        {"kind": "rfc9078", "emoji": "👍 👍"},
    ],
)
def test_9_crooked_reaction_is_ordinary_letter(kw):
    params = {"msgid": "<r@x>", "target": "<t@x>", **kw}
    raw = reaction_raw(**params)
    assert convert.reaction_of(raw) is None
    event = convert.letter_event(
        fetched(raw), me=convert.MeInfo(ME), channel={"ext_id": "1"}, thread_msgids=set()
    )
    assert event["type"] == "upsert"


def test_9_ordinary_letter_is_not_reaction():
    raw = "From: a@x\r\nMessage-ID: <m@x>\r\nIn-Reply-To: <t@x>\r\n\r\n👍".encode()
    assert convert.reaction_of(raw) is None


def _reaction_letter() -> EmailMessage:
    r = EmailMessage(policy=policy.SMTP)
    r["From"] = "x@gmail.com"
    r["To"] = "y@gmail.com"
    r["Subject"] = "Re: old"
    r["Message-ID"] = "<inner@x>"
    r["In-Reply-To"] = "<other@x>"
    r.set_content("👍")
    r.add_alternative(json.dumps({"emoji": "👍", "version": 1}), subtype="vnd.google.email-reaction+json")
    return r


def _letter(text: str) -> EmailMessage:
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = "Пётр <petr@gmail.com>"
    msg["To"] = "me@gmail.com"
    msg["Subject"] = "Re: Проект"
    msg["Message-ID"] = "<outer@x>"
    msg["In-Reply-To"] = "<target@x>"
    msg.set_content(text)
    return msg


def test_review2_forward_with_attached_reaction_letter_stays_letter():
    """Ревью 2: письмо с текстом и вложенным письмом-реакцией (`message/rfc822`) — обычное письмо."""
    msg = _letter("Важный ответ: договор подписан.")
    msg.add_attachment(_reaction_letter())
    assert convert.reaction_of(msg.as_bytes()) is None


def test_review2_text_pdf_and_reaction_part_stays_letter():
    """Ревью 2: длинный текст + PDF + часть реакции — обычное письмо (текст и файл не пропадают)."""
    msg = _letter("Длинный текст ответа. " * 150)
    msg.add_attachment(b"%PDF-1.4 ...", maintype="application", subtype="pdf", filename="invoice.pdf")
    part = EmailMessage(policy=policy.SMTP)
    part.set_content(json.dumps({"emoji": "👍", "version": 1}), subtype="vnd.google.email-reaction+json")
    msg.attach(part)
    assert convert.reaction_of(msg.as_bytes()) is None
    msg2 = _letter("Длинный текст ответа. " * 150)
    msg2.add_alternative(json.dumps({"emoji": "👍", "version": 1}), subtype="vnd.google.email-reaction+json")
    assert convert.reaction_of(msg2.as_bytes()) is None


def test_9_letter_event_sender_and_me():
    raw = reaction_raw(msgid="<r@x>", target="<t@x>", from_=f"владелец <{ME}>", to="petr@gmail.com")
    event = convert.letter_event(
        fetched(raw, labels=("\\Sent",)), me=convert.MeInfo(ME), channel={"ext_id": "7"}, thread_msgids=set()
    )
    assert event == {
        "type": "mail_reaction",
        "channel": {"ext_id": "7"},
        "target_ext_id": "<t@x>",
        "src_ext_id": "<r@x>",
        "emoji": "👍",
        "sender": {"ext_id": ME, "name": "владелец", "handle": ME, "is_me": True},
        "ts": 1_790_000_000_000,
    }


@pytest.fixture
async def gmail_env(admin_client, internal_transport, gm):
    made = []

    async def _make(**kw):
        c = await make_connector(admin_client, internal_transport, gm, **kw)
        made.append(c[1])
        return c

    yield _make
    for gw in made:
        await gw.close()


async def test_9_gmail_sync_reaction_is_badge_not_row(gmail_env, gm, db):
    connector, *_ = await gmail_env()
    await init_cursor(connector)
    parent = incoming(gm, from_="Пётр <petr@gmail.com>")
    await connector.sync("live")
    before = messages(db)
    assert [m["ext_id"] for m in before] == [parent.message_id]

    gm.add(reaction_raw(msgid=new_msgid("r"), target=parent.message_id), thrid=parent.thrid)
    gm.add(
        reaction_raw(
            msgid=new_msgid("r"), target=parent.message_id, emoji="👍", from_="Алиса <alice@gmail.com>"
        ),
        thrid=parent.thrid,
    )
    gm.add(
        reaction_raw(
            msgid=new_msgid("r"),
            target=parent.message_id,
            from_=f"владелец <{ME}>",
            to="petr@gmail.com",
            emoji="🔥",
        ),
        thrid=parent.thrid,
        labels=("\\Sent",),
        flags=("\\Seen",),
    )
    # кривая реакция — обычное письмо
    crooked = new_msgid("c")
    gm.add(reaction_raw(msgid=crooked, target=parent.message_id, version="1"), thrid=parent.thrid)
    await connector.sync("live")
    after = messages(db)
    assert [m["ext_id"] for m in after] == [parent.message_id, crooked]
    assert target_reactions(db, parent.message_id) == [
        {"emoji": "👍", "count": 2, "me": False, "by": ["Пётр", "alice@gmail.com"]},
        {"emoji": "🔥", "count": 1, "me": True},
    ]


async def test_9_plain_sync_reaction(plain_env, srv, db):  # noqa: F811
    connector, *_ = await plain_env()
    await connector.sync("start")
    a = letter(srv, msgid="<p9@company.ru>", subject="Счёт", from_="Пётр <petr@gmail.com>")
    await connector.sync("live")
    letter_raw = reaction_raw(msgid="<r9@gmail.com>", target=a.message_id, to=PLAIN_ADDRESS, emoji="🙏")
    srv.add("INBOX", letter_raw)
    await connector.sync("live")
    assert [m["ext_id"] for m in messages(db)] == [a.message_id]
    assert target_reactions(db, a.message_id) == [{"emoji": "🙏", "count": 1, "me": False, "by": ["Пётр"]}]


# --- п. 10: отправка ------------------------------------------------------------------------------------


def job(*, thrid, target: str, ops, current_mine=(), message_id: int = 555) -> dict:
    return {
        "message_id": message_id,
        "channel_id": 1,
        "gen": 1,
        "lease_gen": 1,
        "channel": {"ext_id": str(thrid), "kind": "mail_thread", "parent_ext_id": None},
        "message_ext_id": target,
        "ops": [list(op) for op in ops],
        "current_mine": list(current_mine),
        "origin": "ui",
    }


def parts_of(raw: bytes):
    msg = email.message_from_bytes(raw, policy=policy.default)
    return msg, [p.get_content_type() for p in msg.iter_parts()]


async def test_10_gmail_sends_three_part_letter_and_badge(gmail_env, gm, db):
    connector, *_ = await gmail_env()
    await init_cursor(connector)
    parent = incoming(gm, from_="Пётр <petr@gmail.com>", references="<old@x>")
    await connector.sync("live")
    res = await connector.push_reaction(job(thrid=parent.thrid, target=parent.message_id, ops=[["👍", True]]))
    assert res.outcome == "done"
    assert len(gm.sent) == 1
    msg, types = parts_of(gm.sent[0])
    assert msg.get_content_type() == "multipart/alternative"
    assert types == ["text/plain", "text/vnd.google.email-reaction+json", "text/html"]
    part = list(msg.iter_parts())[1]
    assert json.loads(part.get_content()) == {"emoji": "👍", "version": 1}
    assert msg["In-Reply-To"] == parent.message_id
    assert msg["References"].split() == ["<old@x>", parent.message_id]
    assert msg["To"] == "petr@gmail.com"
    token = convert.reaction_token(connector._account_id, 555, "👍")  # noqa: SLF001
    assert msg["Message-ID"] == convert.our_message_id(token, ME)
    assert convert.reaction_of(gm.sent[0]) == ("👍", parent.message_id)
    # значок сразу, событием от меня; эхо из «Отправленных» строки ленты не даёт
    assert target_reactions(db, parent.message_id) == [{"emoji": "👍", "count": 1, "me": True}]
    await connector.sync("live")
    assert [m["ext_id"] for m in messages(db)] == [parent.message_id]
    assert target_reactions(db, parent.message_id) == [{"emoji": "👍", "count": 1, "me": True}]


async def test_10_my_letter_goes_to_its_recipients_and_current_mine_skipped(gmail_env, gm, db):
    connector, *_ = await gmail_env()
    await init_cursor(connector)
    parent = incoming(gm, from_="Пётр <petr@gmail.com>")
    await connector.sync("live")
    my = mine(gm, thrid=parent.thrid, to="Алиса <alice@gmail.com>", in_reply_to=parent.message_id)
    await connector.sync("live")
    res = await connector.push_reaction(
        job(thrid=parent.thrid, target=my.message_id, ops=[["🔥", True], ["👍", True]], current_mine=["👍"])
    )
    assert res.outcome == "done"
    assert len(gm.sent) == 1  # 👍 уже моя — не отправляется
    msg, _types = parts_of(gm.sent[0])
    assert msg["To"] == "alice@gmail.com"
    assert convert.reaction_of(gm.sent[0]) == ("🔥", my.message_id)


def _raw_target(msgid: str, *, to: str, cc: str | None = None, list_id: str | None = None) -> bytes:
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = "Пётр <petr@gmail.com>"
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if list_id:
        msg["List-Id"] = list_id
    msg["Subject"] = "Проект"
    msg["Message-ID"] = msgid
    msg["Date"] = "Tue, 29 Sep 2026 09:00:00 +0000"
    msg.set_content("Привет")
    return msg.as_bytes()


@pytest.mark.parametrize(
    "kw, reason",
    [
        ({"to": ME, "cc": ", ".join(f"u{i}@gmail.com" for i in range(20))}, "too_many_recipients"),
        ({"to": "team@gmail.com"}, "not_addressed"),
        ({"to": ME, "list_id": "<dev.lists.example.com>"}, "list"),
    ],
)
async def test_10_gmail_limits_drop(gmail_env, gm, kw, reason, caplog):
    connector, *_ = await gmail_env()
    await init_cursor(connector)
    mid = new_msgid("t")
    parent = gm.add(_raw_target(mid, **kw))
    await connector.sync("live")
    res = await connector.push_reaction(job(thrid=parent.thrid, target=mid, ops=[["👍", True]]))
    assert (res.outcome, res.reason) == ("drop", reason)
    assert gm.sent == []
    assert "petr@" not in caplog.text and "u1@" not in caplog.text


async def test_10_twenty_recipients_is_fine(gmail_env, gm):
    connector, *_ = await gmail_env()
    await init_cursor(connector)
    mid = new_msgid("t")
    parent = gm.add(_raw_target(mid, to=ME, cc=", ".join(f"u{i}@gmail.com" for i in range(19))))
    await connector.sync("live")
    res = await connector.push_reaction(job(thrid=parent.thrid, target=mid, ops=[["👍", True]]))
    assert res.outcome == "done" and len(gm.sent) == 1


async def test_10_retry_after_failure_finds_copy_no_second_smtp(gmail_env, gm, db):
    connector, *_ = await gmail_env()
    await init_cursor(connector)
    parent = incoming(gm, from_="Пётр <petr@gmail.com>")
    await connector.sync("live")
    gm.smtp_deliver_then_fail = aiosmtplib.SMTPServerDisconnected("обрыв после DATA")
    item = job(thrid=parent.thrid, target=parent.message_id, ops=[["👍", True]])
    first = await connector.push_reaction(item)
    assert first.outcome == "retry"
    assert len(gm.sent) == 1
    gm.smtp_deliver_then_fail = None
    second = await connector.push_reaction(item)
    assert second.outcome == "done"
    assert len(gm.sent) == 1  # копия нашлась — второго SMTP нет
    assert target_reactions(db, parent.message_id) == [{"emoji": "👍", "count": 1, "me": True}]


async def test_10_smtp_permanent_is_drop_and_target_missing_is_drop(gmail_env, gm):
    connector, *_ = await gmail_env()
    await init_cursor(connector)
    parent = incoming(gm, from_="Пётр <petr@gmail.com>")
    await connector.sync("live")
    missing = await connector.push_reaction(job(thrid=parent.thrid, target="<nope@x>", ops=[["👍", True]]))
    assert (missing.outcome, missing.reason) == ("drop", "target_not_found")
    gm.smtp_faults["rcpt"] = aiosmtplib.SMTPRecipientRefused(550, "no such user", "petr@gmail.com")
    res = await connector.push_reaction(job(thrid=parent.thrid, target=parent.message_id, ops=[["👍", True]]))
    assert (res.outcome, res.reason) == ("drop", "smtp_permanent")


async def test_10_receive_only_box_has_no_reactor(gmail_env, gm):
    connector, *_ = await gmail_env(config={"send_enabled": False})
    assert connector.reactions_enabled is False
    res = await connector.push_reaction(job(thrid=1, target="<t@x>", ops=[["👍", True]]))
    assert (res.outcome, res.reason) == ("drop", "receive_only")
    on, *_ = await gmail_env()
    assert on.reactions_enabled is True


async def test_10_plain_sends_and_appends_copy_then_retry_finds_it(plain_env, srv, db):  # noqa: F811
    connector, *_ = await plain_env()
    await connector.sync("start")
    a = letter(srv, msgid="<p10@company.ru>", subject="Счёт", from_="Пётр <petr@gmail.com>")
    await connector.sync("live")
    channel = next(m["thread"] for m in messages(db) if m["ext_id"] == a.message_id)
    item = job(thrid=channel, target=a.message_id, ops=[["👍", True]])
    res = await connector.push_reaction(item)
    assert res.outcome == "done"
    assert len(srv.sent) == 1
    msg, types = parts_of(srv.sent[0])
    assert types == ["text/plain", "text/vnd.google.email-reaction+json", "text/html"]
    assert msg["To"] == "petr@gmail.com" and msg["In-Reply-To"] == a.message_id
    assert PLAIN_ME.split("<")[0].strip() in msg["From"]
    copy = srv.sent_box.messages[-1]
    assert copy.message_id == msg["Message-ID"] and copy.flags == {"\\Seen"}
    assert target_reactions(db, a.message_id) == [{"emoji": "👍", "count": 1, "me": True}]
    # повтор того же задания (лиз истёк после SMTP) — своё письмо в «Отправленных», второго SMTP нет
    again = await connector.push_reaction(item)
    assert again.outcome == "done" and len(srv.sent) == 1
    # эхо из «Отправленных» — не строка ленты
    await connector.sync("live")
    assert [m["ext_id"] for m in messages(db)] == [a.message_id]
