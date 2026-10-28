"""Ф12-5 B1 (`docs/architecture.md`): новое письмо с вложениями — Gmail и
обычная почта (`plain`). Та же сборка, что ответ Ф11-3b (`_letter` / `build_letter(attachments=…)`):
`multipart/mixed`, вложения по порядку задания, без `In-Reply-To`/`References`; `SIZE` сервера меньше
письма — `permanent` до `MAIL FROM`; сбой байтов — `temporary` без SMTP; Gmail ищет копию в остатке срока
задания с файлами (`FILE_SEND_TIMEOUT_S`), `plain` кладёт копию с вложениями в «Отправленные». Сети нет.
"""

from __future__ import annotations

import email
from email import policy
from pathlib import Path

from hub.connectors.mail import textproc

from .conftest import Mono
from .test_plain import env, srv  # noqa: F401 — фикстуры обычной почты
from .test_plain import lease as plain_lease
from .test_send_files import PDF, PNG, TOKEN, _gmail_lease, _meta, _serve_files, gmail  # noqa: F401

NEW_TO = {"kind": "email", "to": "petr@example.com", "subject": "Счёт и схема", "from": None}


def _files() -> list[dict]:
    return [
        _meta(PDF, name="Счёт.pdf", mime="application/pdf"),
        _meta(PNG, name="схема.png", mime="image/png", kind="photo"),
    ]


def _new_lease(files, *, text="Добрый день! Счёт во вложении.", **extra) -> dict:
    return _gmail_lease(0, files, text=text, channel_ext_id="new:" + "a" * 32, new_to=NEW_TO, **extra)


def _names(raw: bytes) -> list[tuple[str, int]]:
    return [(a["name"], a["size"]) for a in textproc.extract_body(raw).attachments]


# --- Gmail ---


async def test_gmail_new_letter_with_two_files(gmail, gm) -> None:  # noqa: F811
    connector, _gw, client, _aid = await gmail()
    fetches: list[int] = []
    _serve_files(client, [PDF, PNG], fetches)

    result = await connector.send(_new_lease(_files()))

    assert result.outcome == "sent"
    assert fetches == [0, 1]
    raw = gm.sent[-1]
    msg = email.message_from_bytes(raw, policy=policy.default)
    assert msg.get_content_type() == "multipart/mixed"
    assert msg["To"] == "petr@example.com"
    assert str(msg["Subject"]) == "Счёт и схема"
    assert msg["In-Reply-To"] is None and msg["References"] is None
    assert msg["Message-ID"] == f"<hub-{TOKEN}@gmail.com>"
    assert _names(raw) == [("Счёт.pdf", len(PDF)), ("схема.png", len(PNG))]
    assert "Счёт во вложении" in textproc.extract_body(raw).text
    copy = gm.by_message_id(msg["Message-ID"])
    assert result.peer_ext_id == str(copy[0].thrid)  # копия найдена — привязка по её треду


async def test_gmail_new_letter_file_fetch_failure_is_temporary(gmail, gm) -> None:  # noqa: F811
    connector, _gw, client, _aid = await gmail()
    _serve_files(client, [PDF, PNG], fail_idx=1)
    result = await connector.send(_new_lease(_files()))
    assert (result.outcome, result.error) == ("temporary", "core_404")
    assert gm.sent == []
    assert not any(e[0] == "connect" for e in gm.smtp_log)


async def test_gmail_new_letter_size_over_server_limit_is_permanent(gmail, gm) -> None:  # noqa: F811
    connector, _gw, client, _aid = await gmail()
    _serve_files(client, [PDF, PNG])
    gm.smtp_extensions = {"size": "300"}
    result = await connector.send(_new_lease(_files()))
    assert result.outcome == "permanent"
    steps = [e[0] for e in gm.smtp_log]
    assert "ehlo" in steps and "mail" not in steps and "data" not in steps


async def test_gmail_new_letter_copy_searched_in_file_deadline(gmail, gm) -> None:  # noqa: F811
    """Байты шли 100 с — у письма без файлов (55 с) на поиск копии времени бы не осталось; у задания с
    файлами срок — `FILE_SEND_TIMEOUT_S`, копия находится и беседа привязывается."""
    mono = Mono()
    connector, _gw, client, _aid = await gmail(mono=mono)

    async def slow_fetch(outbox_id, lease_gen, idx, dest_path, *, expected_size=None):
        mono.advance(50.0)
        Path(dest_path).write_bytes([PDF, PNG][idx])  # noqa: ASYNC240 — двойник ядра, локальный файл
        return len([PDF, PNG][idx])

    client.fetch_outbox_file = slow_fetch
    result = await connector.send(_new_lease(_files()))
    assert result.outcome == "sent"
    assert result.peer_ext_id is not None
    assert sum(1 for c in gm.command_texts() if "X-GM-RAW" in c) >= 1


async def test_gmail_new_letter_without_files_unchanged(gmail, gm) -> None:  # noqa: F811
    connector, _gw, client, _aid = await gmail()
    fetches: list[int] = []
    _serve_files(client, [PDF], fetches)
    result = await connector.send(_new_lease(None))
    assert result.outcome == "sent"
    assert fetches == []
    msg = email.message_from_bytes(gm.sent[-1], policy=policy.default)
    assert msg.get_content_type() == "text/plain"


# --- обычная почта (plain) ---


async def test_plain_new_letter_with_two_files_and_copy_in_sent(env, srv) -> None:  # noqa: F811
    connector, _gw, client, _aid = await env()
    fetches: list[int] = []
    _serve_files(client, [PDF, PNG], fetches)
    new_to = {"kind": "email", "to": "info@company.ru", "subject": "Предложение"}

    result = await connector.send(plain_lease(new_to=new_to, text="", files=_files()))

    assert result.outcome == "sent"
    assert fetches == [0, 1]
    raw = srv.sent[-1]
    msg = email.message_from_bytes(raw, policy=policy.default)
    assert msg["To"] == "info@company.ru" and msg["Subject"] == "Предложение"
    assert msg["In-Reply-To"] is None and msg["References"] is None
    assert _names(raw) == [("Счёт.pdf", len(PDF)), ("схема.png", len(PNG))]
    copy = srv.sent_box.messages[-1]
    assert copy.message_id == result.ext_id
    assert _names(copy.raw) == [("Счёт.pdf", len(PDF)), ("схема.png", len(PNG))]
    assert result.peer_ext_id == f"t:hub-{TOKEN}@umbrella.example.com"


async def test_plain_new_letter_size_over_server_limit_is_permanent(env, srv) -> None:  # noqa: F811
    connector, _gw, client, _aid = await env()
    _serve_files(client, [PDF, PNG])
    srv.smtp_extensions = {"size": "300"}
    new_to = {"kind": "email", "to": "info@company.ru", "subject": "Предложение"}
    result = await connector.send(plain_lease(new_to=new_to, files=_files()))
    assert result.outcome == "permanent"
    assert srv.sent == []
