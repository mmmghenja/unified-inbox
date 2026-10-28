"""Ф11-3a A1/A3 (docs/architecture.md): вид вложения письма в конвертере и загрузчик
вложений коннектора почты — письмо по Message-ID в «Вся почта», `BODY.PEEK[]`, вложение номер `idx` тем же
обходом MIME, что у конвертера (тесты 1, 3, 4 спеки). Gmail — `FakeGmail`, сети нет."""

from __future__ import annotations

import io
from email import policy
from email.message import EmailMessage

import pytest

from hub.connectors.base import AuthRequired
from hub.connectors.mail import convert, textproc
from hub.connectors.mail.connector import MailConnector
from hub.connectors.mail.imapwire import FetchedMsg
from hub.connectors.media import MediaDownloader, MediaSink

from .conftest import ADDRESS, make_gateway
from .fakegmail import FakeGmail, new_msgid

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64 + b"png-end"
PDF = b"%PDF-1.4\n" + b"pdf-body " * 20
CSV = b"a;b\n1;2\n"


def _letter(msgid: str, *, extra_inline_png: bool = True) -> bytes:
    """Письмо: текст + html-альтернатива, inline-картинка, два файла и вложенное письмо."""
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = "Пётр <petr@example.com>"
    msg["To"] = ADDRESS
    msg["Subject"] = "Счёт"
    msg["Message-ID"] = msgid
    msg.set_content("Счёт во вложении.")
    msg.add_alternative("<p>Счёт во вложении.</p>", subtype="html")
    if extra_inline_png:
        html = msg.get_payload()[1]
        html.add_related(PNG, maintype="image", subtype="png", cid="<logo>", filename="logo.png")
    msg.add_attachment(PDF, maintype="application", subtype="pdf", filename="счет.pdf")
    msg.add_attachment(CSV, maintype="text", subtype="csv", filename="data.csv")
    inner = EmailMessage(policy=policy.SMTP)
    inner["From"] = "x@example.com"
    inner["Subject"] = "Вложенное"
    inner.set_content("Текст вложенного письма.")
    msg.add_attachment(inner)
    return msg.as_bytes()


def _job(mid: str, idx: int, *, name: str, mime: str, max_bytes: int = 300 * 1024 * 1024) -> dict:
    return {
        "media_id": 1,
        "lease_gen": 1,
        "max_bytes": max_bytes,
        "message": {"channel_ext_id": "123", "ext_id": mid, "thread_ext_id": None},
        "idx": idx,
        "kind": "document",
        "mime": mime,
        "size": None,
        "name": name,
        "src_id": None,
        "ref": None,
    }


def _sink(max_bytes: int | None = None) -> tuple[MediaSink, io.BytesIO]:
    buf = io.BytesIO()
    return MediaSink(buf, max_bytes), buf


@pytest.fixture
async def conn_gm():
    gm = FakeGmail(address=ADDRESS)
    gw = make_gateway(gm)
    connector = MailConnector(None, gw, account_id=1, config={"address": ADDRESS})
    try:
        yield connector, gm
    finally:
        await gw.close()


# --- тест 1: вид вложения в конвертере ------------------------------------------------------------------


def test_1_converter_kind_by_mime_and_body_is_not_attachment():
    body = textproc.extract_body(_letter("<a1@example.com>"))
    assert body.text == "Счёт во вложении."
    by_name = {a["name"]: a for a in body.attachments}
    assert by_name["logo.png"]["type"] == "photo"
    assert by_name["счет.pdf"]["type"] == "document"
    assert by_name["data.csv"]["type"] == "document"
    nested = [a for a in body.attachments if a["mime"] == "message/rfc822"]
    assert len(nested) == 1 and nested[0]["type"] == "document"
    # тело письма (plain и html-альтернатива) во вложения не попадает
    assert {a["mime"] for a in body.attachments} == {
        "image/png",
        "application/pdf",
        "text/csv",
        "message/rfc822",
    }
    assert len(body.attachments) == 4


@pytest.mark.parametrize(
    ("mime", "kind"),
    [
        ("image/jpeg", "photo"),
        ("image/png", "photo"),
        ("image/webp", "photo"),
        ("image/gif", "photo"),
        ("image/svg+xml", "document"),
        ("image/tiff", "document"),
        ("application/pdf", "document"),
    ],
)
def test_1_kind_rule(mime, kind):
    msg = EmailMessage(policy=policy.SMTP)
    msg["Message-ID"] = "<k@example.com>"
    msg.set_content("x")
    main, sub = mime.split("/")
    msg.add_attachment(b"data", maintype=main, subtype=sub, filename="f")
    (att,) = textproc.extract_body(msg.as_bytes()).attachments
    assert att["type"] == kind


def test_1_message_event_carries_type():
    mid = new_msgid("ev")
    gm = FakeGmail(address=ADDRESS)
    m = gm.add(_letter(mid))
    fm = FetchedMsg(uid=m.uid, msgid=m.msgid, thrid=m.thrid, internaldate_ms=1, body=m.raw)
    event = convert.message_event(
        fm, me=convert.MeInfo(address=ADDRESS, aliases=[]), channel={}, thread_msgids=set()
    )
    assert [a["type"] for a in event["attachments"]] == ["photo", "document", "document", "document"]


# --- тест 4: порядок вложений — один обход на конвертер и загрузчик ---------------------------------------


async def test_4_loader_idx_matches_converter_idx(conn_gm):
    connector, gm = conn_gm
    mid = new_msgid("order")
    raw = _letter(mid)
    gm.add(raw)
    atts = textproc.extract_body(raw).attachments
    expected = {0: PNG, 1: PDF, 2: CSV}
    for idx, att in enumerate(atts):
        sink, buf = _sink()
        result = await connector.fetch_media(_job(mid, idx, name=att["name"], mime=att["mime"]), sink)
        assert result.outcome == "ready", (idx, result)
        assert len(buf.getvalue()) == att["size"]
        if idx in expected:
            assert buf.getvalue() == expected[idx]
    # последнее — вложенное письмо, целиком (`message/rfc822` -> `as_bytes()`)
    assert b"Content-Type: message/rfc822" in buf.getvalue()
    assert b"From: x@example.com" in buf.getvalue()


# --- тест 3: загрузчик -------------------------------------------------------------------------------------


async def test_3_found_bytes_in_sink_flags_untouched(conn_gm):
    connector, gm = conn_gm
    mid = new_msgid("found")
    m = gm.add(_letter(mid), flags=())
    sink, buf = _sink()
    result = await connector.fetch_media(_job(mid, 1, name="счет.pdf", mime="application/pdf"), sink)
    assert result.outcome == "ready"
    assert buf.getvalue() == PDF
    assert "\\Seen" not in gm.by_uid(m.uid).flags
    cmds = gm.command_texts()
    assert not any(c.startswith("SELECT") for c in cmds)
    assert not any(c.startswith("UID STORE") for c in cmds)
    fetches = [c for c in cmds if c.startswith("UID FETCH")]
    assert fetches and all("BODY[" not in c.replace("BODY.PEEK[", "") for c in fetches)
    assert any("BODY.PEEK[]" in c for c in fetches)


async def test_3_not_found_is_gone(conn_gm):
    connector, _gm = conn_gm
    sink, buf = _sink()
    result = await connector.fetch_media(_job("<nope@example.com>", 0, name="a", mime="b/c"), sink)
    assert result.outcome == "gone"
    assert buf.getvalue() == b""


async def test_3_name_mismatch_is_gone(conn_gm):
    connector, gm = conn_gm
    mid = new_msgid("name")
    gm.add(_letter(mid))
    sink, buf = _sink()
    result = await connector.fetch_media(_job(mid, 1, name="другое.pdf", mime="application/pdf"), sink)
    assert result.outcome == "gone"
    assert buf.getvalue() == b""
    sink, buf = _sink()
    result = await connector.fetch_media(_job(mid, 1, name="счет.pdf", mime="image/png"), sink)
    assert result.outcome == "gone"


async def test_3_idx_out_of_range_is_gone(conn_gm):
    connector, gm = conn_gm
    mid = new_msgid("range")
    gm.add(_letter(mid))
    sink, _buf = _sink()
    result = await connector.fetch_media(_job(mid, 9, name="счет.pdf", mime="application/pdf"), sink)
    assert result.outcome == "gone"


async def test_3_network_is_temporary(conn_gm):
    connector, gm = conn_gm
    mid = new_msgid("net")
    gm.add(_letter(mid))
    gm.connect_errors.append(OSError("сеть"))
    sink, _buf = _sink()
    result = await connector.fetch_media(_job(mid, 1, name="счет.pdf", mime="application/pdf"), sink)
    assert result.outcome == "temporary"


async def test_3_auth_lost_raises_auth_required(conn_gm):
    connector, gm = conn_gm
    mid = new_msgid("auth")
    gm.add(_letter(mid))
    gm.login_error = b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)"
    sink, _buf = _sink()
    with pytest.raises(AuthRequired):
        await connector.fetch_media(_job(mid, 1, name="счет.pdf", mime="application/pdf"), sink)


async def test_3_letter_bigger_than_max_bytes_is_too_big_with_size(conn_gm):
    connector, gm = conn_gm
    mid = new_msgid("big")
    m = gm.add(_letter(mid))
    sink, buf = _sink()
    result = await connector.fetch_media(
        _job(mid, 1, name="счет.pdf", mime="application/pdf", max_bytes=len(m.raw) - 1), sink
    )
    assert result.outcome == "too_big"
    assert result.size == len(m.raw)
    assert buf.getvalue() == b""
    assert not any("BODY.PEEK[]" in c for c in gm.command_texts())


async def test_3_rfc822_attachment_whole(conn_gm):
    connector, gm = conn_gm
    mid = new_msgid("eml")
    raw = _letter(mid)
    gm.add(raw)
    att = textproc.extract_body(raw).attachments[3]
    assert att["mime"] == "message/rfc822"
    sink, buf = _sink()
    result = await connector.fetch_media(_job(mid, 3, name=att["name"], mime=att["mime"]), sink)
    assert result.outcome == "ready"
    data = buf.getvalue()
    assert len(data) == att["size"]
    assert b"From: x@example.com" in data
    inner = textproc.parse_message(data).get_payload()[0]
    assert inner["Subject"] == "Вложенное"
    assert inner.get_content().strip() == "Текст вложенного письма."


async def test_3_bad_message_id_is_gone_without_imap(conn_gm):
    connector, gm = conn_gm
    sink, _buf = _sink()
    result = await connector.fetch_media(_job('<a "b"@x>', 0, name="a", mime="b/c"), sink)
    assert result.outcome == "gone"
    assert not gm.command_texts()


async def test_downloader_created_only_with_tmp_dir(tmp_path):
    gm = FakeGmail(address=ADDRESS)
    gw = make_gateway(gm)
    try:
        with_dir = MailConnector(None, gw, account_id=1, config={"address": ADDRESS}, media_tmp_dir=tmp_path)
        without = MailConnector(None, gw, account_id=1, config={"address": ADDRESS})
        assert isinstance(with_dir._media, MediaDownloader)  # noqa: SLF001
        assert without._media is None  # noqa: SLF001
    finally:
        await gw.close()
