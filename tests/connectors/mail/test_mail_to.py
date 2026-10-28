"""Ф11-8: `convert.message_event` кладёт `mail_to` (To+Cc) только у СВОЕГО письма; фильтр — в коннекторе
(адреса с `@`, 3..254, нижний регистр, без повторов, порядок заголовков, первые 20), чтобы ядро не отказало
событию целиком. У чужих писем, трекеров и заглушек поля нет."""

from __future__ import annotations

from hub.connectors.mail import convert
from hub.connectors.mail.imapwire import FetchedMsg
from hub.core.events import parse_event

ME = convert.MeInfo(address="me@gmail.com", aliases=["myself@work.example.com"])


def _raw(from_: str, *, to: str | None = None, cc: str | None = None, mid: str = "<m1@x>") -> bytes:
    out = f"From: {from_}\r\nMessage-ID: {mid}\r\nSubject: Тема\r\n"
    if to is not None:
        out += f"To: {to}\r\n"
    if cc is not None:
        out += f"Cc: {cc}\r\n"
    out += "Content-Type: text/plain; charset=utf-8\r\n\r\nтело\r\n"
    return out.encode()


def _event(raw: bytes, *, labels=("\\Sent",)) -> dict:
    msg = FetchedMsg(uid=1, msgid=11, thrid=22, labels=list(labels), internaldate_ms=1000, body=raw)
    return convert.message_event(
        msg, me=ME, channel={"ext_id": "22", "kind": "mail_thread"}, thread_msgids=set()
    )


def test_own_letter_has_to_and_cc_in_header_order():
    raw = _raw(
        "владелец <me@gmail.com>",
        to='"Алиса" <Alisa@Client.example>, boss@client.example',
        cc="Бух <buh@client.example>",
    )
    event = _event(raw)
    assert event["is_me"] is True
    assert event["mail_to"] == ["alisa@client.example", "boss@client.example", "buh@client.example"]
    assert parse_event(event).mail_to == event["mail_to"]  # ядро такое событие принимает


def test_foreign_letter_has_no_mail_to():
    raw = _raw("petr@example.com", to="me@gmail.com, other@example.com")
    event = _event(raw, labels=("\\Inbox",))
    assert event["is_me"] is False
    assert "mail_to" not in event


def test_own_letter_without_recipients_has_no_field():
    assert "mail_to" not in _event(_raw("me@gmail.com"))
    # Одни мусорные адреса — тоже без поля (пустой список не шлём).
    assert "mail_to" not in _event(_raw("me@gmail.com", to="undisclosed-recipients:;"))


def test_duplicates_dropped_across_to_and_cc():
    raw = _raw("me@gmail.com", to="a@x.ru, A@X.RU", cc="a@x.ru, b@x.ru")
    assert _event(raw)["mail_to"] == ["a@x.ru", "b@x.ru"]


def test_cap_twenty_and_to_first():
    to = ", ".join(f"t{i}@x.ru" for i in range(15))
    cc = ", ".join(f"c{i}@x.ru" for i in range(15))
    mail_to = _event(_raw("me@gmail.com", to=to, cc=cc))["mail_to"]
    assert len(mail_to) == convert.MAIL_TO_MAX == 20
    assert mail_to == [f"t{i}@x.ru" for i in range(15)] + [f"c{i}@x.ru" for i in range(5)]


def test_garbage_addresses_filtered():
    long_addr = "a" * 250 + "@x.ru"  # 255 > 254
    raw = _raw("me@gmail.com", to=f"nobody, {long_addr}, ok@x.ru")
    assert _event(raw)["mail_to"] == ["ok@x.ru"]


def test_recipients_filters_short_and_at_less():
    # `addresses()` отдаёт и такое — ядро «a@» (короче 3) отвергло бы вместе со всем письмом.
    raw = b"To: a@, @, local, ok@x.ru\r\n\r\n"
    assert convert.recipients(raw) == ["ok@x.ru"]


def test_stub_event_has_no_mail_to():
    raw = _raw("me@gmail.com", to="a@x.ru")
    msg = FetchedMsg(uid=1, msgid=11, thrid=22, labels=["\\Sent"], internaldate_ms=1000, body=raw)
    stub = convert.stub_event(msg, me=ME, channel={"ext_id": "22", "kind": "mail_thread"})
    assert "mail_to" not in stub
