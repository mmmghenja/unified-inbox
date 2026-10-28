"""Разбор ответов IMAP без сети (`imapwire.py`): литералы, метки, LIST, SEARCH, UTF-7, INTERNALDATE."""

from __future__ import annotations

import pytest

from hub.connectors.mail import imapwire as w


@pytest.mark.parametrize(
    ("encoded", "decoded"),
    [
        ("INBOX", "INBOX"),
        ("&BBIEQQRP- &BD8EPgRHBEIEMA-", "Вся почта"),
        ("&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-", "Отправленные"),
        ("A&-B", "A&B"),
        ("&BCAEMAQxBD4EQgQw-/2026", "Работа/2026"),
    ],
)
def test_mutf7_roundtrip(encoded, decoded):
    assert w.decode_mutf7(encoded) == decoded
    assert w.encode_mutf7(decoded) == encoded


def test_mutf7_broken_kept_raw():
    assert w.decode_mutf7("&%%%-x") == "&%%%-x"


def test_fetch_with_two_literals_and_trailer():
    data = [
        (b"12 (UID 5 BODY[HEADER.FIELDS (FROM MESSAGE-ID)] {3}", b"abc"),
        (b" BODY[]<0> {2}", b"xy"),
        b" FLAGS (\\Seen \\Draft) MODSEQ (77))",
    ]
    [m] = w.parse_fetch(data)
    assert (m.uid, m.seq, m.header, m.body, m.body_partial) == (5, 12, b"abc", b"xy", True)
    assert m.flags == ["\\Seen", "\\Draft"] and m.is_draft
    assert m.modseq == 77


def test_fetch_nil_body_and_foreign_untagged_skipped():
    data = [b"1 (FLAGS (\\Seen))", b"2 (UID 9 BODY[] NIL)"]
    [m] = w.parse_fetch(data)
    assert m.uid == 9 and m.body is None


def test_fetch_quoted_label_with_parens_and_brackets():
    data = [b'3 (UID 1 X-GM-LABELS ("a (b) [c]" "\\\\Inbox" Plain))']
    [m] = w.parse_fetch(data)
    assert m.labels == ["a (b) [c]", "\\Inbox", "Plain"]


def test_fetch_garbage_raises_parse_error():
    with pytest.raises(w.ImapParseError):
        w.parse_fetch([b'1 (UID 1 X-GM-LABELS ("unterminated)'])


def test_list_and_search():
    entries = w.parse_list(
        [b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"', (b'(\\All) "/" {8}', b"All Mail")]
    )
    assert [(e.flags, e.name) for e in entries] == [
        (("\\HasNoChildren", "\\Sent"), "[Gmail]/Sent Mail"),
        (("\\All",), "All Mail"),
    ]
    assert w.parse_search([b"4 5 6"]) == [4, 5, 6]
    assert w.parse_search([None]) == [] and w.parse_search([b""]) == []


def test_internaldate_space_padded_day_and_tz():
    assert w.parse_internaldate(" 4-Sep-2026 10:00:00 +0300") == w.parse_internaldate(
        "04-Sep-2026 07:00:00 +0000"
    )
    with pytest.raises(w.ImapParseError):
        w.parse_internaldate("2026-09-04")


def test_uid_set_and_quote():
    assert w.uid_set([5, 1, 2, 3, 9]) == "1:3,5,9"
    assert w.quote('a"b\\c') == '"a\\"b\\\\c"'
