"""Ф12-1 B3 + П5, П6 (тест 15): новое письмо новому адресату через настоящее ядро (беседа-заготовка, outbox,
лизы, отчёты) и `FakeGmail`/`FakeSmtp` (`docs/architecture.md`)."""

from __future__ import annotations

import email
import json
from email import policy

import pytest

from hub.core import new_recipient, outbox

from .conftest import Mono, make_connector
from .fakegmail import make_email


class _NoPause:
    """Первая строка графика уходит без случайной паузы `[0, gap_min]` (24.10) — здесь проверяется путь
    письма, а не график (как в `tests/e2e/test_new_recipient_tg.py`)."""

    def randint(self, a: int, b: int) -> int:
        return a


@pytest.fixture(autouse=True)
def _no_first_pause(monkeypatch):
    monkeypatch.setattr("hub.core.outbox._rng", _NoPause())


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


async def _draft(db, clock, account_id, *, subject="Электронная подпись для ООО «Ромашка»", sender=None):
    def _fn(conn):
        # Ф12-2: окно графика — круглые сутки, первый слот — «сейчас» (график здесь не проверяется).
        conn.execute(
            "UPDATE main.account SET config_json = json_set(COALESCE(config_json, '{}'), '$.pace_window', "
            "'00:00-24:00') WHERE id = ?",
            (account_id,),
        )
        project_id = conn.execute("INSERT INTO main.project (name) VALUES ('Umbrella')").lastrowid
        token_id = conn.execute(
            "INSERT INTO main.api_token (name, kind, hash) VALUES ('mcp:iip', 'interactive', 'x')"
        ).lastrowid
        created = new_recipient.create_draft(
            conn,
            account_id=account_id,
            project_id=project_id,
            to="client@example.com",
            text="Добрый день! Предлагаю подпись.",
            subject=subject,
            from_address=sender,
            api_token_id=token_id,
            idempotency_key=None,
            now_ms=int(clock() * 1000),
        )
        outbox.approve(conn, created.outbox_id, now_ms=int(clock() * 1000))
        token = conn.execute(
            "SELECT send_token FROM main.outbox WHERE id = ?", (created.outbox_id,)
        ).fetchone()
        return created, token[0]

    return await db.write(_fn)


def _row(db, created) -> dict:
    with db.reader() as conn:
        status, ext_id, error = conn.execute(
            "SELECT status, ext_id, error FROM main.outbox WHERE id = ?", (created.outbox_id,)
        ).fetchone()
        channel_ext, meta = conn.execute(
            "SELECT ext_id, meta_json FROM main.channel WHERE id = ?", (created.channel_id,)
        ).fetchone()
    return {
        "status": status,
        "ext_id": ext_id,
        "error": error,
        "channel_ext": channel_ext,
        "meta": json.loads(meta),
    }


def _raw_searches(gm) -> int:
    return sum(1 for c in gm.command_texts() if "X-GM-RAW" in c)


async def test_15_new_letter_headers_and_copy_binds(env, gm, db, clock):
    connector, _gw, _client, account_id = await env()
    created, token = await _draft(db, clock, account_id)
    await connector.step_outbox()

    row = _row(db, created)
    mid = f"<hub-{token}@gmail.com>"
    assert row["status"] == "sent" and row["ext_id"] == mid
    raw = gm.sent[-1]
    msg = email.message_from_bytes(raw, policy=policy.default)
    assert msg["In-Reply-To"] is None and msg["References"] is None
    assert msg["Message-ID"] == mid
    assert msg["To"] == "client@example.com"
    assert str(msg["Subject"]) == "Электронная подпись для ООО «Ромашка»"
    assert b"Subject: =?utf-8?" in raw  # RFC 2047
    assert "me@gmail.com" in msg["From"] and "владелец" in msg["From"]
    assert msg.get_content().strip() == "Добрый день! Предлагаю подпись."
    assert ("rcpt", "client@example.com") in gm.smtp_log

    copy = gm.by_message_id(mid)
    assert len(copy) == 1
    assert row["channel_ext"] == str(copy[0].thrid)  # привязка по X-GM-THRID копии
    assert "new_to" not in (row["meta"].get("attrs") or {})


async def test_15_from_alias(env, gm, db, clock):
    connector, _gw, _client, account_id = await env(config={"aliases": ["sales@umbrella.example.com"]})
    created, _token = await _draft(db, clock, account_id, sender="sales@umbrella.example.com")
    await connector.step_outbox()
    assert _row(db, created)["status"] == "sent"
    msg = email.message_from_bytes(gm.sent[-1], policy=policy.default)
    assert "sales@umbrella.example.com" in msg["From"]
    assert ("mail", "me@gmail.com") in gm.smtp_log  # конверт — от ящика


async def test_15_copy_not_found_three_attempts_sent_without_peer(env, gm, db, clock):
    connector, _gw, _client, account_id = await env()
    gm.sent_copy_mode = "none"
    created, _token = await _draft(db, clock, account_id)
    await connector.step_outbox()
    row = _row(db, created)
    assert row["status"] == "sent"
    assert row["channel_ext"].startswith("new:")  # привяжет приём (П4б)
    assert _raw_searches(gm) == 3


async def test_p6_copy_search_stays_within_budget(env, gm, db, clock):
    mono = Mono()

    async def _slow_sleep(seconds: float) -> None:
        mono.advance(60.0)  # пауза «съела» остаток 55 с

    connector, _gw, _client, account_id = await env(mono=mono, sleep=_slow_sleep)
    gm.sent_copy_mode = "none"
    created, _token = await _draft(db, clock, account_id)
    await connector.step_outbox()
    row = _row(db, created)
    assert row["status"] == "sent"
    assert _raw_searches(gm) == 1  # вторая попытка — уже за пределом, не TimeoutError


async def test_15_maybe_sent_own_letter_found_no_smtp(env, gm, db, clock):
    connector, _gw, _client, account_id = await env()
    created, token = await _draft(db, clock, account_id)
    mid = f"<hub-{token}@gmail.com>"
    copy = gm.add(
        make_email(msgid=mid, from_="владелец <me@gmail.com>", to="client@example.com", subject="Подпись"),
        labels=("\\Sent",),
    )
    await db.write(
        lambda conn: conn.execute("UPDATE main.outbox SET maybe_sent = 1 WHERE id = ?", (created.outbox_id,))
    )
    await connector.step_outbox()
    row = _row(db, created)
    assert row["status"] == "sent" and row["ext_id"] == mid
    assert gm.sent == [] and gm.smtp_log == []
    assert row["channel_ext"] == str(copy.thrid)


async def test_p5_reconcile_new_letter(env, gm, db, clock):
    """П5: истёкший лиз (почта не идемпотентна) -> сверка ищет своё письмо по всему ящику."""
    connector, _gw, _client, account_id = await env()
    created, token = await _draft(db, clock, account_id)
    mid = f"<hub-{token}@gmail.com>"
    leases = await db.write(
        lambda conn: outbox.lease(conn, account_id=account_id, owner="other", now_ms=int(clock() * 1000))
    )
    assert leases[0].new_to["kind"] == "email"
    clock.advance_ms(10 * 60 * 1000)
    copy = gm.add(
        make_email(msgid=mid, from_="владелец <me@gmail.com>", to="client@example.com", subject="Подпись"),
        labels=("\\Sent",),
    )
    await connector.step_outbox()
    row = _row(db, created)
    assert row["status"] == "sent" and row["channel_ext"] == str(copy.thrid)
    assert gm.sent == []


async def test_p5_reconcile_not_found_is_unknown(env, gm, db, clock):
    connector, _gw, _client, account_id = await env()
    created, _token = await _draft(db, clock, account_id)
    await db.write(
        lambda conn: outbox.lease(conn, account_id=account_id, owner="other", now_ms=int(clock() * 1000))
    )
    clock.advance_ms(10 * 60 * 1000)
    await connector.step_outbox()
    row = _row(db, created)
    assert row["status"] == "unknown"
    assert row["channel_ext"].startswith("new:")
    assert gm.sent == []


async def test_new_lease_without_address_is_permanent(env, gm):
    connector, _gw, _client, _aid = await env()
    lease = {
        "outbox_id": 1,
        "lease_gen": 1,
        "text": "привет",
        "channel_ext_id": "new:" + "a" * 32,
        "send_token": "b" * 32,
        "new_to": None,
    }
    result = await connector.send(lease)
    assert result.outcome == "permanent"
    bad = {**lease, "new_to": {"kind": "email", "to": "a@b.c", "subject": "x\r\nBcc: z@y.x", "from": None}}
    assert (await connector.send(bad)).outcome == "permanent"
    assert gm.smtp_log == []


@pytest.mark.parametrize("sep", [" ", "\x85", "\x0b", "​", "‮"])
async def test_review_subject_control_chars_permanent_not_unknown(env, gm, sep):
    """Ревью Ф12-1: тема с разделителем строк/символом категории C* — `permanent` с причиной, без SMTP
    (раньше сборка письма падала ValueError -> каркас ставил `unknown`)."""
    connector, _gw, _client, _aid = await env()
    lease = {
        "outbox_id": 1,
        "lease_gen": 1,
        "text": "привет",
        "channel_ext_id": "new:" + "a" * 32,
        "send_token": "b" * 32,
        "new_to": {"kind": "email", "to": "a@b.cc", "subject": f"Подпись{sep}для ООО", "from": None},
    }
    result = await connector.send(lease)
    assert result.outcome == "permanent" and "тема письма" in result.error
    assert gm.smtp_log == []
