"""`convert.py` без сети: Message-ID как ext_id, send_token эха, метки -> attrs, маркер прочтения, ответ."""

from __future__ import annotations

import email
from email import policy

from hub.connectors.mail import convert
from hub.connectors.mail.imapwire import FetchedMsg

ME = convert.MeInfo(address="me@gmail.com", aliases=["myself@work.example.com"])


def _hdr(from_: str, mid: str | None = None) -> bytes:
    out = f"From: {from_}\r\n"
    if mid:
        out += f"Message-ID: {mid}\r\n"
    return (out + "\r\n").encode()


def test_ext_id_same_from_body_and_header_fetch():
    raw = b"Message-ID:\r\n <abc@x.example>\r\nFrom: a@b.c\r\n\r\nbody"
    assert convert.ext_id_from_headers(raw, 5) == "<abc@x.example>"
    assert convert.ext_id_from_headers(b"From: a@b.c\r\n\r\n", 5) == "<gm-5@hub>"


def test_send_token_only_for_hub_ids():
    assert convert.send_token_from_msgid("<hub-0123456789abcdef0123456789abcdef@gmail.com>") == (
        "0123456789abcdef0123456789abcdef"
    )
    assert convert.send_token_from_msgid("<hub-x@gmail.com>") is None
    assert convert.send_token_from_msgid("<CAF@mail.gmail.com>") is None


def test_is_mine_by_alias_or_sent_label():
    m = FetchedMsg(uid=1, labels=[])
    assert convert.is_mine(m, "myself@work.example.com", ME)
    assert convert.is_mine(FetchedMsg(uid=2, labels=["\\Sent"]), "other@x.y", ME)
    assert not convert.is_mine(m, "other@x.y", ME)


def test_labels_normalized():
    assert convert.normalize_labels(
        ["\\Inbox", "\\Sent", "\\Important", "\\Starred", "Работа", "[Gmail]/x"]
    ) == [
        "INBOX",
        "SENT",
        "Работа",
    ]


def test_read_marker_prefix_rule():
    def msg(uid, *, seen, mine=False, draft=False):
        labels = ["\\Sent"] if mine else ["\\Inbox"]
        flags = (["\\Seen"] if seen else []) + (["\\Draft"] if draft else [])
        return FetchedMsg(
            uid=uid,
            labels=labels,
            flags=flags,
            internaldate_ms=uid * 1000,
            header=_hdr("p@x.y", f"<m{uid}@x>"),
        )

    assert convert.read_marker([msg(1, seen=False), msg(2, seen=True)], ME) is None
    assert convert.read_marker([msg(1, seen=True), msg(2, seen=False)], ME) == "<m1@x>"
    assert convert.read_marker([msg(1, seen=True), msg(2, seen=False, mine=True)], ME) == "<m2@x>"
    assert convert.read_marker([msg(1, seen=True), msg(3, seen=False, draft=True)], ME) == "<m1@x>"


def test_reply_recipients_from_not_reply_to():
    parent = convert.ReplyParent(
        ext_id="<p@x>",
        subject="S",
        references=[],
        from_addrs=["petr@example.com"],
        to_addrs=["me@gmail.com"],
        reply_to_addrs=["list@example.com"],
        mine=False,
    )
    assert convert.reply_recipients(parent) == ["petr@example.com"]


def test_build_reply_references_last_20():
    refs = [f"<r{i}@x>" for i in range(30)]
    parent = convert.ReplyParent(
        ext_id="<p@x>",
        subject="Тема",
        references=refs,
        from_addrs=["a@b.c"],
        to_addrs=[],
        reply_to_addrs=[],
        mine=False,
    )
    raw = convert.build_reply(
        parent=parent,
        recipients=["a@b.c"],
        text="Привет\n.\nточка в начале строки",
        address="me@gmail.com",
        display_name="владелец Петров",
        message_id="<hub-t@gmail.com>",
        now_ts=1_790_000_000,
    )
    msg = email.message_from_bytes(raw, policy=policy.default)
    references = msg["References"].split()
    assert len(references) == 20 and references[-1] == "<p@x>" and references[0] == "<r11@x>"
    assert msg["Subject"] == "Re: Тема"
    assert msg.get_content().replace("\r\n", "\n") == "Привет\n.\nточка в начале строки\n"
    assert msg["From"].addresses[0].display_name == "владелец Петров"
