"""Ф11-3b B4 (`docs/architecture.md` + П2, П6, П7; тест 17): вложения в ответе на
письмо (Gmail и обычная почта `plain`). `build_letter` без вложений — байт в байт как до правки; с вложениями
— `multipart/mixed`, круговой путь через разбор приёма; `SIZE` сервера; таймаут `data()` растёт с размером и
передаётся в `smtp.data(timeout=…)`; копия `APPEND` у `plain` — в остатке срока. Байты файлов — подменный
`CoreClient.fetch_outbox_file`; Gmail — `FakeGmail`, обычная почта — `FakePlain`. Сети нет.
"""

from __future__ import annotations

import email
import hashlib
import time
from email import policy
from pathlib import Path

import pytest

from hub.connectors.base import FILE_SEND_TIMEOUT_S, OutboxFileError
from hub.connectors.mail import convert, textproc
from hub.connectors.mail import gateway as gateway_mod
from hub.connectors.mail.connector import SEND_DEADLINE_MARGIN_S

from .conftest import Mono, make_connector
from .helpers import incoming
from .test_plain import _thread_with_mine, env, srv  # noqa: F401 — фикстуры обычной почты
from .test_plain import lease as plain_lease

PDF = b"%PDF-1.7\n" + b"p" * 500
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40 + b"img"
TOKEN = "0123456789abcdef0123456789abcdef"

# Письмо без вложений, собранное кодом ДО Ф11-3b (TZ=UTC, `now_ts=1_700_000_000`, Message-ID задан).
LETTER_BEFORE = (
    b"From: =?utf-8?b?0LLQu9Cw0LTQtdC70LXRhg==?= <me@gmail.com>\r\nTo: petr@example.com, =?utf-8?b?0JDQvdC90LA=?= "
    b"<anna@example.com>\r\nSubject: Re: =?utf-8?b?0J/RgNC+0LXQutGCIMKr0KDQvtC80LDRiNC60LDCuw==?=\r\n"
    b"Message-ID: <hub-tok@gmail.com>\r\nIn-Reply-To: <p@example.com>\r\nReferences: <a@example.com> "
    b"<p@example.com>\r\nDate: Tue, 14 Nov 2023 22:13:20 +0000\r\nMIME-Version: 1.0\r\nContent-Type: "
    b'text/plain; charset="utf-8"\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\n'
    b"=D0=9F=D1=80=D0=B8=D0=B2=D0=B5=D1=82!\r\n=D0=A1=D1=82=D1=80=D0=BE=D0=BA=D0=B0 =D0=B4=D0=B2=D0=B0 "
    b"=E2=80=94 =D0=B4=D0=\r\n=BB=D0=B8=D0=BD=D0=BD=D0=B0=D1=8F "
    b"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx=\r\n"
    b"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\r\n"
)


@pytest.fixture
def utc(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _meta(data: bytes, *, name: str, mime: str, kind: str = "document") -> dict:
    sha = hashlib.sha256(data).hexdigest()
    return {"size": len(data), "mime": mime, "name": name, "kind": kind, "sha256": sha}


def _serve_files(client, blobs: list[bytes], fetches: list | None = None, *, fail_idx: int | None = None):
    """Подменный `CoreClient.fetch_outbox_file`: файл `idx` — `blobs[idx]`."""

    async def fetch(outbox_id, lease_gen, idx, dest_path, *, expected_size=None):
        if fetches is not None:
            fetches.append(idx)
        if idx == fail_idx:
            raise OutboxFileError("core_404")
        Path(dest_path).write_bytes(blobs[idx])  # noqa: ASYNC240 — двойник ядра, локальный файл
        return len(blobs[idx])

    client.fetch_outbox_file = fetch


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


# --- build_letter ---


def test_build_letter_without_attachments_is_byte_identical(utc) -> None:
    kwargs = {
        "recipients": ["petr@example.com", "Анна <anna@example.com>"],
        "subject": "Re: Проект «Ромашка»",
        "text": "Привет!\nСтрока два — длинная " + "x" * 90,
        "address": "me@gmail.com",
        "display_name": "владелец",
        "message_id": "<hub-tok@gmail.com>",
        "now_ts": 1_700_000_000.0,
        "in_reply_to": "<p@example.com>",
        "references": ["<a@example.com>", "<p@example.com>"],
    }
    assert convert.build_letter(**kwargs) == LETTER_BEFORE
    assert convert.build_letter(**kwargs, attachments=None) == LETTER_BEFORE
    assert convert.build_letter(**kwargs, attachments=[]) == LETTER_BEFORE


def test_build_letter_with_attachments_round_trips_through_receive(tmp_path) -> None:
    pdf = _write(tmp_path, "1.part", PDF)
    png = _write(tmp_path, "2.part", PNG)
    raw = convert.build_letter(
        recipients=["petr@example.com"],
        subject="Re: Акт",
        text="Во вложении акт и скриншот.",
        address="me@gmail.com",
        display_name="владелец",
        message_id="<hub-t@gmail.com>",
        now_ts=1_700_000_000.0,
        attachments=[(pdf, "Акт сверки №5.pdf", "application/pdf"), (png, "shot.png", "image/png")],
    )
    msg = email.message_from_bytes(raw, policy=policy.default)
    assert msg.get_content_type() == "multipart/mixed"
    first, *rest = msg.iter_parts()
    assert first.get_content_type() == "text/plain"
    assert first.get_content_charset() == "utf-8"
    assert first.get_content().strip() == "Во вложении акт и скриншот."
    assert [p.get_content_disposition() for p in rest] == ["attachment", "attachment"]
    assert b"filename*" in raw  # не-ASCII имя — RFC 2231

    body = textproc.extract_body(raw)
    assert body.text.strip() == "Во вложении акт и скриншот."
    assert [(a["name"], a["mime"], a["size"]) for a in body.attachments] == [
        ("Акт сверки №5.pdf", "application/pdf", len(PDF)),
        ("shot.png", "image/png", len(PNG)),
    ]
    assert textproc.find_attachment(raw, 0)[1] == PDF
    assert textproc.find_attachment(raw, 1)[1] == PNG


def test_build_letter_empty_text_with_attachment_and_bad_mime(tmp_path) -> None:
    pdf = _write(tmp_path, "1.part", PDF)
    raw = convert.build_letter(
        recipients=["petr@example.com"],
        subject="Re: x",
        text="",
        address="me@gmail.com",
        display_name="",
        message_id="<hub-t@gmail.com>",
        now_ts=1_700_000_000.0,
        attachments=[(pdf, "a.bin", "не mime")],
    )
    body = textproc.extract_body(raw)
    assert not (body.text or "").strip()
    assert [(a["name"], a["mime"], a["size"]) for a in body.attachments] == [
        ("a.bin", "application/octet-stream", len(PDF))
    ]


# --- SMTP: SIZE и таймаут data() ---


def test_smtp_data_timeout_grows_with_size_and_is_capped() -> None:
    assert gateway_mod.smtp_data_timeout(0) == gateway_mod.SMTP_DATA_TIMEOUT_S
    size = 24 * 1024 * 1024  # ~17 МиБ вложений после base64
    expected = gateway_mod.SMTP_DATA_TIMEOUT_S + size / (128 * 1024)
    assert gateway_mod.smtp_data_timeout(size) == pytest.approx(expected)
    assert gateway_mod.smtp_data_timeout(size, 100.0) == 100.0  # noqa: PLR2004
    assert gateway_mod.smtp_data_timeout(size, -5.0) == 1.0


# --- Gmail: ответ с вложениями ---


def _gmail_lease(thrid: int, files: list[dict] | None, *, text: str = "", **extra) -> dict:
    lease = {
        "outbox_id": 7,
        "lease_gen": 3,
        "channel_ext_id": str(thrid),
        "text": text,
        "reply_to_ext_id": None,
        "thread_ext_id": None,
        "send_token": TOKEN,
        "mode": "send",
        "send_started_ts": 0,
        "attempts": 1,
        "maybe_sent": False,
        "files": files,
    }
    lease.update(extra)
    return lease


@pytest.fixture
async def gmail(admin_client, internal_transport, gm):
    made = []

    async def _make(**kw):
        c = await make_connector(admin_client, internal_transport, gm, **kw)
        made.append(c[1])
        return c

    yield _make
    for gw in made:
        await gw.close()


def _files() -> list[dict]:
    return [
        _meta(PDF, name="Акт сверки.pdf", mime="application/pdf"),
        _meta(PNG, name="shot.png", mime="image/png", kind="photo"),
    ]


async def test_gmail_reply_with_attachments_and_empty_text_is_sent(gmail, gm, tmp_path) -> None:
    connector, _gw, client, _aid = await gmail()
    connector.outbox_tmp_dir = tmp_path / "tmp-conn"
    fetches: list[int] = []
    _serve_files(client, [PDF, PNG], fetches)
    parent = incoming(gm, subject="Акт")

    result = await connector.send(_gmail_lease(parent.thrid, _files(), text="  "))

    assert result.outcome == "sent"
    assert fetches == [0, 1]
    raw = gm.sent[-1]
    msg = email.message_from_bytes(raw, policy=policy.default)
    assert msg["In-Reply-To"] == parent.message_id
    assert msg.get_content_type() == "multipart/mixed"
    got = textproc.extract_body(raw).attachments
    assert [(a["name"], a["size"]) for a in got] == [("Акт сверки.pdf", len(PDF)), ("shot.png", len(PNG))]
    assert list((tmp_path / "tmp-conn").iterdir()) == []  # временные файлы удалены после сборки


async def test_gmail_empty_text_without_files_is_still_permanent(gmail, gm) -> None:
    connector, _gw, _client, _aid = await gmail()
    parent = incoming(gm)
    result = await connector.send(_gmail_lease(parent.thrid, None, text=" "))
    assert (result.outcome, result.error) == ("permanent", "пустое сообщение")


async def test_gmail_core_refuses_file_is_temporary_without_smtp(gmail, gm) -> None:
    connector, _gw, client, _aid = await gmail()
    _serve_files(client, [PDF, PNG], fail_idx=1)
    parent = incoming(gm)
    result = await connector.send(_gmail_lease(parent.thrid, _files(), text="акт"))
    assert (result.outcome, result.error) == ("temporary", "core_404")
    assert gm.sent == []
    assert not any(e[0] == "connect" for e in gm.smtp_log)


# П7 (файл в новое письмо — `permanent`) отменён Ф12-5 B1: новое письмо с вложениями —
# `test_send_new_files.py`.


async def test_gmail_size_over_server_limit_is_permanent_before_mail_from(gmail, gm) -> None:
    connector, _gw, client, _aid = await gmail()
    _serve_files(client, [PDF, PNG])
    gm.smtp_extensions = {"size": "300"}
    parent = incoming(gm)
    result = await connector.send(_gmail_lease(parent.thrid, _files(), text="акт"))
    assert result.outcome == "permanent"
    assert result.error == "письмо больше, чем принимает сервер (1 МБ)"
    steps = [e[0] for e in gm.smtp_log]
    assert "ehlo" in steps
    assert "mail" not in steps and "login" not in steps and "data" not in steps


async def test_gmail_size_zero_means_no_limit(gmail, gm) -> None:
    connector, _gw, client, _aid = await gmail()
    _serve_files(client, [PDF, PNG])
    gm.smtp_extensions = {"size": "0"}
    parent = incoming(gm)
    assert (await connector.send(_gmail_lease(parent.thrid, _files()))).outcome == "sent"


async def test_gmail_data_timeout_passed_to_data_and_grows_with_size(gmail, gm) -> None:
    """П2: срок `data()` уходит и в `smtp.data(timeout=…)`; у письма с большим вложением он больше."""
    mono = Mono()
    connector, _gw, client, _aid = await gmail(mono=mono)
    big = b"z" * (1024 * 1024)
    _serve_files(client, [big])
    parent = incoming(gm)

    assert (await connector.send(_gmail_lease(parent.thrid, None, text="коротко"))).outcome == "sent"
    small_timeout = gm.smtp_data_timeouts[-1]
    assert small_timeout == pytest.approx(gateway_mod.smtp_data_timeout(len(gm.sent[-1])))

    files = [_meta(big, name="big.bin", mime="application/octet-stream")]
    lease = _gmail_lease(parent.thrid, files, send_token="f" * 32)
    assert (await connector.send(lease)).outcome == "sent"
    big_timeout = gm.smtp_data_timeouts[-1]
    cap = FILE_SEND_TIMEOUT_S - SEND_DEADLINE_MARGIN_S
    assert big_timeout == pytest.approx(gateway_mod.smtp_data_timeout(len(gm.sent[-1]), cap))
    assert big_timeout > small_timeout + 9  # 1 МиБ -> base64 ~1,4 МБ -> +10 с


async def test_gmail_data_timeout_capped_by_send_deadline(gmail, gm) -> None:
    mono = Mono()
    connector, gw, client, _aid = await gmail(mono=mono)
    _serve_files(client, [PDF])
    parent = incoming(gm)
    original = gw.smtp_send
    seen: dict = {}

    async def spy(**kw):
        seen.update(kw)
        return await original(**kw)

    gw.smtp_send = spy
    started = mono()

    async def late_fetch(outbox_id, lease_gen, idx, dest_path, *, expected_size=None):
        mono.advance(262.0)  # байты шли долго — от срока send() осталось 38 с, потолок data() — 8 с
        Path(dest_path).write_bytes(PDF)  # noqa: ASYNC240 — двойник ядра, локальный файл
        return len(PDF)

    client.fetch_outbox_file = late_fetch
    files = [_meta(PDF, name="a.pdf", mime="application/pdf")]
    assert (await connector.send(_gmail_lease(parent.thrid, files))).outcome == "sent"
    deadline = started + FILE_SEND_TIMEOUT_S
    assert seen["data_cap_s"] == pytest.approx(deadline - SEND_DEADLINE_MARGIN_S - mono())
    assert seen["data_cap_s"] == pytest.approx(8.0)
    assert gm.smtp_data_timeouts[-1] == pytest.approx(8.0)  # меньше обычных 15 с — упёрлись в срок send()


# --- обычная почта (plain) ---


async def test_plain_reply_with_attachment_and_copy_in_sent(env, srv, tmp_path) -> None:  # noqa: F811
    connector, _gw, client, _aid = await env()
    _serve_files(client, [PDF])
    a, _mine = await _thread_with_mine(connector, srv)
    lease = plain_lease(
        text="", channel_ext_id="t:p5@company.ru", reply_to_ext_id=a.message_id,
        files=[_meta(PDF, name="счёт.pdf", mime="application/pdf")],
    )
    result = await connector.send(lease)
    assert result.outcome == "sent"
    raw = srv.sent[-1]
    got = textproc.extract_body(raw).attachments
    assert [(x["name"], x["size"]) for x in got] == [("счёт.pdf", len(PDF))]
    copy = srv.sent_box.messages[-1]
    assert copy.message_id == result.ext_id
    assert b"application/pdf" in copy.raw


async def test_plain_copy_skipped_when_send_deadline_is_near(env, srv, caplog) -> None:  # noqa: F811
    mono = Mono()
    connector, gw, client, _aid = await env(mono=mono)
    _serve_files(client, [PDF])
    a, _mine = await _thread_with_mine(connector, srv)
    original = gw.smtp_send

    async def slow_smtp(**kw):
        result = await original(**kw)
        mono.advance(285.0)  # после SMTP от срока send() осталось 15 с — меньше APPEND_BUDGET_S
        return result

    gw.smtp_send = slow_smtp
    n_copies = len(srv.sent_box.messages)
    lease = plain_lease(
        text="счёт", channel_ext_id="t:p5@company.ru", reply_to_ext_id=a.message_id,
        files=[_meta(PDF, name="счёт.pdf", mime="application/pdf")],
    )
    result = await connector.send(lease)
    assert result.outcome == "sent"
    assert len(srv.sent_box.messages) == n_copies  # копию не клали
    assert "не осталось времени" in caplog.text
