"""Ф57-1 (docs/architecture.md): письмо-рассылка — флаг `bulk`
события коннектора почты (не `is_bot` отправителя). Без сети.

- по письму на каждый признак: `List-Unsubscribe`, `Precedence: bulk | junk`, `Auto-Submitted` ≠ `no`,
  служебная локальная часть адреса `From`;
- П2: `List-Id` и `Precedence: list` сами по себе — не признак (человек пишет через группу);
- своё письмо — никогда; ключ кладётся только когда истинно; отправитель ботом не помечается;
- П3: письмо трекера (`tracker_event`) с `Precedence: bulk` признак не несёт и с упоминанием остаётся `react`;
- событие с `bulk` принимает модель ядра.
"""

from __future__ import annotations

import pytest

from hub.connectors.mail import convert, trackers
from hub.connectors.mail.imapwire import FetchedMsg
from hub.core import attention
from hub.core import events as core_events

from .test_trackers_jira_html import _fetched as _tracker_fetched
from .test_trackers_jira_html import _raw as _tracker_raw

ME = convert.MeInfo(address="me@gmail.com", aliases=["myself@work.example.com"])
CHANNEL = convert.channel_ref(7, title="Тема", attrs=None)


def _letter(from_: str = "Пётр <petr@acme.example>", *, extra: str = "", labels=None) -> FetchedMsg:
    raw = (
        f"From: {from_}\r\nTo: me@gmail.com\r\nSubject: Тема\r\nMessage-ID: <m1@acme.example>\r\n"
        f"{extra}Content-Type: text/plain; charset=utf-8\r\n\r\nтекст письма\r\n"
    ).encode()
    return FetchedMsg(uid=1, labels=labels or ["\\Inbox"], internaldate_ms=1_700_000_000_000, body=raw)


def _event(msg: FetchedMsg) -> dict:
    return convert.message_event(msg, me=ME, channel=CHANNEL, thread_msgids=set())


def test_human_letter_is_not_bulk_and_key_is_absent():
    event = _event(_letter())
    assert "bulk" not in event
    assert event["mentions_me"] is True and "is_bot" not in event["sender"]


@pytest.mark.parametrize(
    "extra",
    [
        "List-Unsubscribe: <mailto:unsub@news.example>\r\n",
        "list-unsubscribe: <https://news.example/u/1>\r\nList-Id: <news.example>\r\n",
        "Precedence: bulk\r\n",
        "Precedence:  Bulk \r\n",
        "Precedence: junk\r\n",
        "Auto-Submitted: auto-generated\r\n",
        "Auto-Submitted: auto-replied; owner-email=x@y.z\r\n",
        "Auto-Submitted: AUTO-NOTIFIED\r\n",
    ],
)
def test_header_signs_mark_bulk(extra):
    event = _event(_letter(extra=extra))
    assert event["bulk"] is True
    assert "is_bot" not in event["sender"]  # П1: рассылка — не бот
    core_events.parse_event(event)  # ядро принимает событие с признаком


@pytest.mark.parametrize(
    "extra",
    [
        "List-Id: <dev.acme.example>\r\n",  # П2: человек пишет через группу Google
        "Precedence: list\r\n",
        "List-Id: <dev.acme.example>\r\nPrecedence: list\r\nList-Post: <mailto:dev@acme.example>\r\n",
        "Auto-Submitted: no\r\n",
        "Auto-Submitted: No \r\n",
        "Precedence: first-class\r\n",
        "X-List-Unsubscribe-Note: nope\r\n",
    ],
)
def test_list_id_and_precedence_list_alone_are_not_bulk(extra):
    assert "bulk" not in _event(_letter(extra=extra))


@pytest.mark.parametrize(
    "address",
    [
        "no-reply@service.example",
        "NoReply@service.example",
        "no_reply@service.example",
        "donotreply@service.example",
        "do-not-reply@service.example",
        "MAILER-DAEMON@mx.example",
        "notifications@github.example",
        "notification@service.example",
        "noreply-123@service.example",
        "notifications+abc@service.example",
        "no-reply.billing@service.example",
    ],
)
def test_service_local_part_marks_bulk(address):
    assert _event(_letter(f"Сервис <{address}>"))["bulk"] is True


@pytest.mark.parametrize(
    "address",
    [
        "petr@noreply.example",  # признак — локальная часть, не домен
        "noreplyman@service.example",
        "notificationsteam@service.example",
        "ivan.noreply@service.example",
        "reply@service.example",
        "info@service.example",
    ],
)
def test_ordinary_local_part_is_not_bulk(address):
    assert "bulk" not in _event(_letter(f"Человек <{address}>"))


def test_own_letter_is_never_bulk():
    extra = "List-Unsubscribe: <mailto:u@x>\r\nPrecedence: bulk\r\n"
    by_address = _event(_letter("Я <me@gmail.com>", extra=extra))
    by_label = _event(_letter("noreply@service.example", extra=extra, labels=["\\Sent"]))
    assert by_address["is_me"] is True and "bulk" not in by_address
    assert by_label["is_me"] is True and "bulk" not in by_label


def test_is_bulk_pure_function():
    assert convert.is_bulk({}, None) is False
    assert convert.is_bulk({}, "petr@acme.example") is False
    assert convert.is_bulk({"list-unsubscribe": ""}, "petr@acme.example") is True
    assert convert.is_bulk({"precedence": "list"}, "petr@acme.example") is False
    assert convert.is_bulk({"auto-submitted": ""}, "petr@acme.example") is False
    assert convert.is_bulk({}, "noreply") is True  # адрес без домена


def test_tracker_letter_with_precedence_bulk_carries_no_flag_and_stays_react():
    """П3: признак ставится только в `message_event`. Письмо Jira (у настоящих — `Precedence: bulk` и
    `Auto-Submitted`) с комментарием для меня остаётся `react`."""
    raw = _tracker_raw("jira_comment_update.eml")
    head, sep, body = raw.partition(b"\r\n\r\n") if b"\r\n\r\n" in raw else raw.partition(b"\n\n")
    eol = b"\r\n" if sep == b"\r\n\r\n" else b"\n"
    marked = head + eol + b"Precedence: bulk" + eol + b"Auto-Submitted: auto-generated" + sep + body
    msg = _tracker_fetched(marked)
    assert convert.is_bulk(convert.raw_headers(marked), "jira@acme.atlassian.net") is True  # как обычное — да
    (part,) = trackers.route(msg, thread_msgs=[msg], known=set())
    event = convert.tracker_event(msg, part, me=ME)
    assert "bulk" not in event and event["mentions_me"] is True
    mode = attention.effective_mode(0, 0, None, 2, "issue", None, None, 3)
    bulk = event.get("bulk", 0)
    assert attention.message_attention(mode, event["mentions_me"], "issue", 0, bulk) == "react"
