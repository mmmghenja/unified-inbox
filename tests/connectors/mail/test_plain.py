"""Ф12-4 (`docs/architecture.md`): обычная почта по IMAP/SMTP — `PlainMailConnector` и шлюз
вида `imap`. Обязательные тесты 1–10 спеки; ядро — настоящее, в процессе (как у тестов Gmail), сервер —
`FakePlain` (подделка соединения `imaplib`), SMTP — `FakeSmtp`. Сети нет."""

from __future__ import annotations

import asyncio
import email
import hashlib
import imaplib
import io
import json
import os
import subprocess
import sys
from email import policy
from email.message import EmailMessage
from types import SimpleNamespace

import pytest

from hub.connectors.base import AuthRequired, CoreClient
from hub.connectors.mail import gateway as gateway_mod
from hub.connectors.mail import plain
from hub.connectors.mail.auth import cmd_auth_mail
from hub.connectors.mail.gateway import GmailGateway, MailConfigError
from hub.connectors.mail.imapwire import encode_mutf7
from hub.connectors.mail.plain import PlainMailConnector, channel_ext_for_key, thread_key
from hub.connectors.media import MediaSink
from hub.core import outbox as outbox_mod

from .conftest import fast_wait, sync_admin_client
from .fakegmail import make_email
from .fakeplain import ADDRESS, FakePlain
from .helpers import channels, core_cursor, messages

ME = f"Максим Орлов <{ADDRESS}>"
PLAIN_CFG = {
    "address": ADDRESS,
    "aliases": [],
    "display_name": "Максим Орлов",
    "provider": "imap",
    "flavor": "imap",
    "imap_host": "imap.timeweb.ru",
    "imap_port": 993,
    "smtp_host": "smtp.timeweb.ru",
    "smtp_port": 465,
    "proxy": None,
    "read_ack": False,
    "send_enabled": True,
}
TOKEN = "0123456789abcdef0123456789abcdef"
DAY = 86400


def make_plain_gateway(srv: FakePlain, **kw) -> GmailGateway:
    params = {
        "address": ADDRESS,
        "password": srv.password,
        "imap_factory": srv.imap_factory,
        "smtp_factory": srv.smtp_factory,
        "listener_wait": fast_wait,
        "flavor": "imap",
        "imap_host": "imap.timeweb.ru",
        "imap_port": 993,
        "smtp_host": "smtp.timeweb.ru",
        "smtp_port": 465,
    }
    params.update(kw)
    return GmailGateway(**params)


@pytest.fixture
def srv() -> FakePlain:
    return FakePlain()


@pytest.fixture
async def env(admin_client, internal_transport, srv, tmp_path):
    made: list[GmailGateway] = []

    async def _make(*, gw_kwargs: dict | None = None, **kw):
        r = await admin_client.post(
            "/internal/v1/admin/accounts",
            json={"source": "mail", "title": ADDRESS, "db": "main", "config": PLAIN_CFG},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        client = CoreClient(secret=body["connector_secret"], transport=internal_transport)
        gw = make_plain_gateway(srv, **(gw_kwargs or {}))
        made.append(gw)

        async def _sleep(_s: float) -> None:
            await asyncio.sleep(0)

        connector = PlainMailConnector(
            client,
            gw,
            account_id=body["account_id"],
            config=PLAIN_CFG,
            state_path=tmp_path / "mail" / "state.json",
            sleep=_sleep,
            wall=lambda: srv.now_ts,
            **kw,
        )
        connector._loop = asyncio.get_running_loop()  # noqa: SLF001 — как после start_source
        return connector, gw, client, body["account_id"]

    yield _make
    for gw in made:
        await gw.close()


def letter(
    srv: FakePlain,
    *,
    box: str = "INBOX",
    msgid: str | None,
    from_: str = "Пётр Петров <petr@company.ru>",
    to: str = ADDRESS,
    subject: str = "Предложение",
    body: str = "Здравствуйте!",
    in_reply_to: str | None = None,
    references: str | None = None,
    flags=(),
    date_ts: int | None = None,
):
    raw = make_email(
        msgid=msgid,
        from_=from_,
        to=to,
        subject=subject,
        body=body,
        in_reply_to=in_reply_to,
        references=references,
    )
    return srv.add(srv.sent_wire if box == "Sent" else box, raw, flags=flags, date_ts=date_ts)


def by_thread(db) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for m in messages(db):
        out.setdefault(m["thread"], []).append(m)
    return out


async def _token(db) -> int:
    return await db.write(
        lambda conn: (
            conn.execute("INSERT INTO main.api_token (name, kind, hash) VALUES ('ui', 'ui', 'x')").lastrowid
        )
    )


async def enqueue(db, clock, channel_ext: str, *, text: str = "Спасибо, получили.", reply_to=None) -> int:
    token_id = await _token(db)

    def _fn(conn):
        cid = conn.execute("SELECT id FROM main.channel WHERE ext_id = ?", (channel_ext,)).fetchone()[0]
        return outbox_mod.enqueue(
            conn,
            channel_id=cid,
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
        status, ext_id, token = conn.execute(
            "SELECT status, ext_id, send_token FROM main.outbox WHERE id = ?", (oid,)
        ).fetchone()
    return {"status": status, "ext_id": ext_id, "token": token}


def lease(**kw) -> dict:
    base = {
        "outbox_id": 1,
        "lease_gen": 1,
        "text": "Добрый день! Предлагаем сотрудничество.",
        "send_token": TOKEN,
        "channel_ext_id": None,
        "reply_to_ext_id": None,
        "maybe_sent": False,
    }
    base.update(kw)
    return base


def last_sent(srv: FakePlain) -> email.message.EmailMessage:
    return email.message_from_bytes(srv.sent[-1], policy=policy.default)


# --- тест 1: тред = корень цепочки ------------------------------------------------------------------------


def test_1_thread_key_rules():
    # первый Message-ID из References (мутация «последний» ломает эту строку)
    assert thread_key("<c@x>", ["<a@x>", "<b@x>"], ["<b@x>"], {}) == "<a@x>"
    # References нет — In-Reply-To; ничего нет — свой
    assert thread_key("<c@x>", [], ["<b@x>"], {}) == "<b@x>"
    assert thread_key("<c@x>", [], [], {}) == "<c@x>"
    # урезанный References: ответ ссылается на письмо, чей ключ известен, — берётся его ключ
    assert thread_key("<c@x>", [], ["<b@x>"], {"<b@x>": "<a@x>"}) == "<a@x>"
    assert thread_key("<d@x>", ["<b@x>", "<c@x>"], [], {"<c@x>": "<a@x>"}) == "<a@x>"
    # письмо уже в карте — его ключ
    assert thread_key("<c@x>", ["<z@x>"], [], {"<c@x>": "<a@x>"}) == "<a@x>"
    # беседа: "t:" + ключ без скобок; длиннее 300 — "t:" + sha256
    assert channel_ext_for_key("<a@x>") == "t:a@x"
    long_key = "<" + "x" * 400 + "@example.com>"
    ext = channel_ext_for_key(long_key)
    assert ext == "t:" + hashlib.sha256(long_key.strip("<>").encode()).hexdigest()
    assert len(channel_ext_for_key("<" + "y" * 297 + ">")) == 299


async def test_1_threads_by_references_and_map(env, srv, db):
    connector, _gw, _client, _aid = await env()
    await connector.sync("start")
    letter(srv, msgid="<a1@company.ru>", subject="Предложение")
    letter(
        srv,
        msgid="<b1@company.ru>",
        subject="Re: Предложение",
        references="<a1@company.ru>",
        in_reply_to="<a1@company.ru>",
    )
    # ответ без References, только In-Reply-To на письмо, чей ключ уже в карте, — в тот же тред
    letter(srv, msgid="<c1@company.ru>", subject="Re: Предложение", in_reply_to="<b1@company.ru>")
    # письмо без обоих — новый тред
    letter(srv, msgid="<d1@other.ru>", subject="Другое", from_="Анна <anna@other.ru>")
    long_mid = "<" + "k" * 320 + "@company.ru>"
    letter(srv, msgid=long_mid, subject="Длинный")
    letter(srv, msgid="<e1@company.ru>", subject="Re: Длинный", references=long_mid, in_reply_to=long_mid)
    await connector.sync("live")

    threads = by_thread(db)
    assert [m["ext_id"] for m in threads["t:a1@company.ru"]] == [
        "<a1@company.ru>",
        "<b1@company.ru>",
        "<c1@company.ru>",
    ]
    assert [m["ext_id"] for m in threads["t:d1@other.ru"]] == ["<d1@other.ru>"]
    long_ext = channel_ext_for_key(long_mid)
    assert long_ext.startswith("t:") and len(long_ext) == 66
    assert [m["ext_id"] for m in threads[long_ext]] == [long_mid, "<e1@company.ru>"]
    chans = channels(db)
    assert chans["t:a1@company.ru"]["kind"] == "mail_thread"
    assert chans["t:a1@company.ru"]["title"] == "Предложение"
    # карта пережила сверку — в файле состояния
    state = json.loads((connector._threads._path).read_text())  # noqa: SLF001
    assert ["<c1@company.ru>", "<a1@company.ru>"] in state["threads"]


# --- тест 2: INBOX и «Отправленные», свои письма, склейка с отправленным из хаба ----------------------------


async def test_2_inbox_and_sent_cursors_is_me_and_echo(env, srv, db, clock):
    connector, _gw, client, _aid = await env()
    await connector.sync("start")
    cur = await core_cursor(client)
    assert cur["boxes"] == {
        "inbox": {"uidvalidity": 11, "last_uid": 0},
        "sent": {"uidvalidity": 22, "last_uid": 0},
    }

    a = letter(srv, msgid="<a2@company.ru>")
    # моё письмо в неизвестный тред — не берётся (как правило (в) Gmail)
    letter(srv, box="Sent", msgid="<solo@umbrella.example.com>", from_=ME, to="x@y.ru", subject="Сам")
    await connector.sync("live")
    mine = letter(
        srv,
        box="Sent",
        msgid="<m2@umbrella.example.com>",
        from_=ME,
        to="Пётр Петров <petr@company.ru>",
        subject="Re: Предложение",
        references=a.message_id,
        in_reply_to=a.message_id,
        flags=("\\Seen",),
    )
    await connector.sync("live")
    cur = await core_cursor(client)
    assert cur["boxes"]["inbox"]["last_uid"] == a.uid
    assert cur["boxes"]["sent"]["last_uid"] == mine.uid
    rows = by_thread(db)["t:a2@company.ru"]
    assert [(m["ext_id"], m["is_me"]) for m in rows] == [("<a2@company.ru>", 0), ("<m2@umbrella.example.com>", 1)]
    assert "t:solo@umbrella.example.com" not in channels(db)

    # ответ из хаба: копия ложится APPEND в «Отправленные», при следующей сверке склеивается с той же строкой
    oid = await enqueue(db, clock, "t:a2@company.ru")
    await connector.step_outbox()
    row = outbox_row(db, oid)
    assert row["status"] == "sent"
    assert srv.sent_box.messages[-1].message_id == row["ext_id"]
    await connector.sync("live")
    ours = [m for m in messages(db) if m["ext_id"] == row["ext_id"]]
    assert len(ours) == 1
    assert ours[0]["oid"] == oid and ours[0]["is_me"] == 1 and ours[0]["thread"] == "t:a2@company.ru"
    # повторная сверка — ни одного нового письма
    before = messages(db)
    await connector.sync("scheduled")
    assert messages(db) == before


# --- тест 3: стартовая загрузка 14 дн. / 300 тредов; UIDVALIDITY -> ресинк 7 дн. ----------------------------


async def test_3_startup_and_resync(env, srv, db, monkeypatch):
    assert plain.STARTUP_DAYS == 14 and plain.STARTUP_THREADS_MAX == 300 and plain.RESYNC_DAYS == 7
    monkeypatch.setattr(plain, "STARTUP_THREADS_MAX", 3)
    monkeypatch.setattr(plain, "STARTUP_BATCH", 2)
    now = srv.now_ts
    t1 = letter(srv, msgid="<t1@c.ru>", subject="Т1", date_ts=now - 1 * DAY)
    letter(srv, msgid="<t2@c.ru>", subject="Т2", date_ts=now - 2 * DAY)
    letter(srv, msgid="<t3@c.ru>", subject="Т3", date_ts=now - 3 * DAY)
    letter(srv, msgid="<t4@c.ru>", subject="Т4", date_ts=now - 10 * DAY)  # в окне, но не в новейших трёх
    letter(srv, msgid="<old@c.ru>", subject="Старое", date_ts=now - 20 * DAY)  # вне 14 дней
    # моё письмо в треде Т1 из «Отправленных» — догружается вместе с тредом
    letter(
        srv,
        box="Sent",
        msgid="<t1r@umbrella.example.com>",
        from_=ME,
        to="petr@company.ru",
        subject="Re: Т1",
        references=t1.message_id,
        date_ts=now - DAY + 60,
    )

    connector, _gw, client, _aid = await env()
    await connector.sync("start")
    cur = await core_cursor(client)
    assert cur.get("startup")  # 2 из 3 — за первую сверку
    assert set(channels(db)) == {"t:t1@c.ru", "t:t2@c.ru"}
    await connector.sync("initial")
    cur = await core_cursor(client)
    assert "startup" not in cur
    assert set(channels(db)) == {"t:t1@c.ru", "t:t2@c.ru", "t:t3@c.ru"}
    assert [m["ext_id"] for m in by_thread(db)["t:t1@c.ru"]] == ["<t1@c.ru>", "<t1r@umbrella.example.com>"]

    # UIDVALIDITY INBOX сменился: письма за 7 дней — снова тем же отбором, без дублей; новое — в хабе
    before = messages(db)
    srv.renumber("INBOX", 99)
    letter(srv, msgid="<x@c.ru>", subject="Новое", date_ts=now - 2 * DAY)
    await connector.sync("scheduled")
    after = messages(db)
    assert len(after) == len(before) + 1
    assert len({m["ext_id"] for m in after}) == len(after)
    assert "t:x@c.ru" in channels(db)
    assert "t:t4@c.ru" not in channels(db) and "t:old@c.ru" not in channels(db)
    cur = await core_cursor(client)
    assert cur["boxes"]["inbox"] == {"uidvalidity": 99, "last_uid": srv.inbox.next_uid - 1}


# --- тест 4: \Seen из INBOX -> прочитанность; хаб -> \Seen выключено ----------------------------------------


async def test_4_seen_to_hub_and_no_push_read(env, srv, db):
    connector, _gw, _client, _aid = await env()
    await connector.sync("start")
    a = letter(srv, msgid="<ra@company.ru>")
    b = letter(srv, msgid="<rb@company.ru>", references=a.message_id, in_reply_to=a.message_id)
    await connector.sync("live")
    assert channels(db)["t:ra@company.ru"]["last_read"] is None

    srv.set_flags("INBOX", a.uid, add=("\\Seen",))
    await connector.sync("live")
    assert channels(db)["t:ra@company.ru"]["last_read"] == a.message_id
    srv.set_flags("INBOX", b.uid, add=("\\Seen",))
    await connector.sync("live")
    assert channels(db)["t:ra@company.ru"]["last_read"] == b.message_id

    # решение 4: толкатель прочитанности не запускается, а прямой вызов ничего не шлёт в ящик
    assert PlainMailConnector.read_ack_enabled is False
    n = len(srv.commands)
    res = await connector.push_read(
        {"channel": {"ext_id": "t:ra@company.ru"}, "message": {"ext_id": b.message_id}}
    )
    assert res.outcome == "drop"
    assert len(srv.commands) == n
    assert all("STORE" not in c and "SELECT" not in c for c in srv.command_texts())


# --- тест 5: ответ в тред, APPEND в «Отправленные» ---------------------------------------------------------


async def _thread_with_mine(connector, srv):
    await connector.sync("start")
    a = letter(srv, msgid="<p5@company.ru>", subject="Счёт")
    mine = letter(
        srv,
        box="Sent",
        msgid="<m5@umbrella.example.com>",
        from_=ME,
        to="Бухгалтерия <buh@company.ru>",
        subject="Re: Счёт",
        references=a.message_id,
        in_reply_to=a.message_id,
    )
    await connector.sync("live")
    return a, mine


async def test_5_reply_to_inbox_parent_headers_and_append(env, srv, db, clock):
    connector, _gw, _client, _aid = await env()
    a, _mine = await _thread_with_mine(connector, srv)
    oid = await enqueue(db, clock, "t:p5@company.ru", reply_to=a.message_id)
    await connector.step_outbox()
    row = outbox_row(db, oid)
    assert row["status"] == "sent"
    msg = last_sent(srv)
    assert msg["Message-ID"] == row["ext_id"] == f"<hub-{row['token']}@umbrella.example.com>"
    assert msg["In-Reply-To"] == a.message_id
    assert msg["References"].split() == [a.message_id]
    assert msg["Subject"] == "Re: Счёт"
    assert msg["To"] == "petr@company.ru"
    assert "Максим Орлов" in msg["From"] and ADDRESS in msg["From"]
    assert ("rcpt", "petr@company.ru") in srv.smtp_log
    assert srv.smtp_log[0][1]["hostname"] == "smtp.timeweb.ru" and srv.smtp_log[0][1]["port"] == 465
    # копия — в «Отправленных», с \Seen
    copy = srv.sent_box.messages[-1]
    assert copy.message_id == row["ext_id"] and copy.flags == {"\\Seen"}
    assert any(c.startswith(f'APPEND "{srv.sent_wire}" (\\Seen)') for c in srv.command_texts())
    # родитель искался по заголовку, флаги не тронуты
    assert any("HEADER Message-ID" in c for c in srv.command_texts())
    assert not any("BODY[" in c for c in srv.command_texts())


async def test_5_reply_to_my_letter_in_sent_goes_to_its_recipients(env, srv, db, clock):
    connector, _gw, _client, _aid = await env()
    _a, mine = await _thread_with_mine(connector, srv)
    oid = await enqueue(db, clock, "t:p5@company.ru", reply_to=mine.message_id)
    await connector.step_outbox()
    assert outbox_row(db, oid)["status"] == "sent"
    msg = last_sent(srv)
    assert msg["To"] == "buh@company.ru"
    assert msg["In-Reply-To"] == mine.message_id
    assert msg["References"].split() == ["<p5@company.ru>", "<m5@umbrella.example.com>"]


async def test_5_append_failure_still_sent(env, srv, db, clock, caplog):
    connector, _gw, _client, _aid = await env()
    a, _mine = await _thread_with_mine(connector, srv)
    oid = await enqueue(db, clock, "t:p5@company.ru", reply_to=a.message_id)
    srv.append_error = imaplib.IMAP4.error("[OVERQUOTA] Quota exceeded")
    n_sent = len(srv.sent_box.messages)
    await connector.step_outbox()
    assert outbox_row(db, oid)["status"] == "sent"
    assert len(srv.sent_box.messages) == n_sent
    assert "Отправленные" in caplog.text


# --- тест 6: новое письмо (new_to, контракт Ф12-1) ----------------------------------------------------------


async def test_6_new_letter_peer_ext_id_without_search(env, srv):
    connector, _gw, _client, _aid = await env()
    new_to = {"kind": "email", "to": "Компания <info@company.ru>", "subject": "Предложение", "from": ADDRESS}
    res = await connector.send(lease(new_to=new_to))
    our = f"<hub-{TOKEN}@umbrella.example.com>"
    assert res.outcome == "sent"
    assert res.ext_id == our
    assert res.peer_ext_id == f"t:hub-{TOKEN}@umbrella.example.com"
    msg = last_sent(srv)
    assert msg["Message-ID"] == our and msg["To"] == "info@company.ru" and msg["Subject"] == "Предложение"
    assert msg["In-Reply-To"] is None and msg["References"] is None
    assert not any("SEARCH" in c for c in srv.command_texts())  # поиска копии нет
    assert srv.sent_box.messages[-1].message_id == our
    # ответ компании на это письмо попадёт в беседу peer_ext_id
    assert channel_ext_for_key(thread_key("<r@company.ru>", [our], [our], {})) == res.peer_ext_id


async def test_6_new_letter_foreign_from_refused(env, srv):
    connector, _gw, _client, _aid = await env()
    res = await connector.send(
        lease(new_to={"kind": "email", "to": "a@b.ru", "subject": "x", "from": "x@evil.ru"})
    )
    assert res.outcome == "permanent"
    assert srv.sent == []


# --- тест 7: maybe_sent и reconcile ------------------------------------------------------------------------


async def test_7_maybe_sent_and_reconcile(env, srv):
    connector, _gw, _client, _aid = await env()
    await connector.sync("start")
    a = letter(srv, msgid="<p7@company.ru>")
    await connector.sync("live")
    ours = f"<hub-{TOKEN}@umbrella.example.com>"
    base = lease(channel_ext_id="t:p7@company.ru", reply_to_ext_id=a.message_id)

    # reconcile: копии нет -> unknown, SMTP не трогается
    assert (await connector.reconcile(base)).outcome == "unknown"
    # maybe_sent, копии нет -> обычная отправка
    res = await connector.send(dict(base, maybe_sent=True))
    assert res.outcome == "sent" and res.ext_id == ours
    assert [e for e in srv.smtp_log if e[0] == "data"]
    # теперь копия есть (APPEND) -> maybe_sent находит её без SMTP, reconcile -> sent
    srv.smtp_log.clear()
    res = await connector.send(dict(base, maybe_sent=True))
    assert res.outcome == "sent" and res.ext_id == ours
    assert srv.smtp_log == []
    rec = await connector.reconcile(base)
    assert rec.outcome == "sent" and rec.ext_id == ours
    assert srv.smtp_log == []


# --- тест 8: без IDLE — опрос; без «Отправленных» — понятная ошибка -----------------------------------------


class _Events:
    """Приёмник событий слушателя (зовётся из его потока)."""

    def __init__(self, loop: asyncio.AbstractEventLoop, got_new: asyncio.Event) -> None:
        self.kinds: list[str] = []
        self._loop = loop
        self._got_new = got_new

    def __call__(self, kind: str, _detail: str | None) -> None:
        self.kinds.append(kind)
        if kind == "new":
            self._loop.call_soon_threadsafe(self._got_new.set)


def _collector() -> tuple[_Events, asyncio.Event]:
    got_new = asyncio.Event()
    return _Events(asyncio.get_running_loop(), got_new), got_new


async def test_8_no_idle_polls():
    srv = FakePlain(idle=False)
    gw = make_plain_gateway(srv, poll_interval_s=0.01)
    events, got_new = _collector()
    gw.start_listener(events)
    try:
        await asyncio.wait_for(got_new.wait(), 5)
    finally:
        await gw.stop_listener()
        await gw.close()
    events = events.kinds
    assert "connected" in events and "new" in events
    texts = srv.command_texts()
    assert "NOOP" in texts and "IDLE" not in texts


async def test_8_idle_server_uses_idle():
    srv = FakePlain(idle=True)
    gw = make_plain_gateway(srv)
    events, got_new = _collector()
    gw.start_listener(events)
    try:
        assert await asyncio.to_thread(srv.idle_started.wait, 5)
        srv.idle_queue.put(("EXISTS", [b"1"]))
        await asyncio.wait_for(got_new.wait(), 5)
    finally:
        await gw.close()
    assert "new" in events.kinds
    assert 'EXAMINE "INBOX"' in srv.command_texts()


async def test_8_no_sent_folder_is_config_error(tmp_path):
    srv = FakePlain(sent_name="")
    gw = make_plain_gateway(srv)
    try:
        with pytest.raises(MailConfigError) as ei:
            await gw.work.connect()
        assert "Отправленные" in str(ei.value)
        connector = PlainMailConnector(
            None, gw, account_id=1, config=PLAIN_CFG, state_path=tmp_path / "s.json"
        )
        with pytest.raises(AuthRequired) as ei2:
            await connector.start_source()
        assert "Отправленные" in str(ei2.value) and "gmail" not in str(ei2.value).lower()
    finally:
        await gw.close()


async def test_8_sent_folder_by_name_without_special_use():
    srv = FakePlain(sent_name="Отправленные", sent_special_use=False)
    gw = make_plain_gateway(srv)
    try:
        info = await gw.work.connect()
    finally:
        await gw.close()
    assert info.all_mailbox == "INBOX"
    assert info.sent_mailbox == encode_mutf7("Отправленные")


def test_8_hosts_from_config(monkeypatch):
    seen = {}

    def _fake_imap(host, port, *, proxy, timeout):
        seen.update(host=host, port=port, proxy=proxy)
        return object()

    monkeypatch.setattr(gateway_mod, "_GmailImap", _fake_imap)
    gw = GmailGateway(
        address=ADDRESS, password="p", flavor="imap", imap_host="imap.timeweb.ru", imap_port=993
    )
    gw._default_imap(60.0)  # noqa: SLF001
    assert seen == {"host": "imap.timeweb.ru", "port": 993, "proxy": None}
    # Gmail — как раньше
    GmailGateway(address="me@gmail.com", password="p")._default_imap(60.0)  # noqa: SLF001
    assert seen["host"] == "imap.gmail.com"


# --- тест 9: hub auth mail --imap … --smtp … ----------------------------------------------------------------


def _auth_args(**kw) -> SimpleNamespace:
    base = {
        "address": ADDRESS,
        "title": "Umbrella",
        "db": None,
        "account": None,
        "proxy": "none",
        "receive_only": False,
        "imap": "imap.timeweb.ru:993",
        "smtp": "smtp.timeweb.ru:465",
        "name": "Максим Орлов",
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _run_auth(core_env, srv: FakePlain, *, password: str | None = None, **kw):
    data_dir, config_dir = core_env
    printed: list[str] = []
    prompts: list[str] = []
    factory_kw: list[dict] = []

    def _factory(*, address, password, proxy, **extra):
        factory_kw.append(dict(extra, proxy=proxy))
        return GmailGateway(
            address=address,
            password=password,
            proxy=proxy,
            imap_factory=srv.imap_factory,
            smtp_factory=srv.smtp_factory,
            **extra,
        )

    def _getpass(prompt: str) -> str:
        prompts.append(prompt)
        return password if password is not None else srv.password

    with sync_admin_client(config_dir, data_dir) as admin:
        code = cmd_auth_mail(
            _auth_args(**kw),
            admin_client=admin,
            config_dir=config_dir,
            gateway_factory=_factory,
            getpass_fn=_getpass,
            print_fn=printed.append,
        )
    return code, printed, prompts, factory_kw


def _mail_accounts(core_env) -> list[dict]:
    data_dir, config_dir = core_env
    with sync_admin_client(config_dir, data_dir) as admin:
        return [a for a in admin.get("/admin/status").json()["accounts"] if a["source"] == "mail"]


def test_9_auth_plain_success(core_env):
    data_dir, config_dir = core_env
    srv = FakePlain()
    code, printed, prompts, factory_kw = _run_auth(core_env, srv)
    assert code == 0, printed
    assert prompts == [f"Пароль ящика {ADDRESS} (ввод не отображается): "]
    assert factory_kw[0]["flavor"] == "imap" and factory_kw[0]["imap_host"] == "imap.timeweb.ru"
    [acc] = _mail_accounts(core_env)
    assert acc["title"] == "Umbrella"
    key = (config_dir / "connectors" / f"{acc['id']}.key").read_text().strip()
    with sync_admin_client(config_dir, data_dir) as admin:
        cfg = admin.get("/account", headers={"Authorization": f"Bearer {key}"}).json()["config"]
    assert cfg["flavor"] == "imap"
    assert (cfg["imap_host"], cfg["imap_port"], cfg["smtp_host"], cfg["smtp_port"]) == (
        "imap.timeweb.ru",
        993,
        "smtp.timeweb.ru",
        465,
    )
    assert cfg["display_name"] == "Максим Орлов"
    assert cfg["read_ack"] is False and cfg["send_enabled"] is True and cfg["proxy"] is None
    assert srv.password not in json.dumps(cfg)
    creds = config_dir / "connectors" / f"{acc['id']}.mail.json"
    assert json.loads(creds.read_text()) == {"password": srv.password}
    assert oct(os.stat(creds).st_mode & 0o777) == "0o600"
    # проверка: LIST, EXAMINE INBOX, SMTP AUTH без отправки
    texts = srv.command_texts()
    assert "LIST" in texts and 'EXAMINE "INBOX"' in texts
    steps = [e[0] for e in srv.smtp_log]
    assert "login" in steps and "data" not in steps and "mail" not in steps
    assert any(f"--account {acc['id']} --project P --all" in p for p in printed)
    assert any(f"hub-connector@{acc['id']}" in p for p in printed)
    assert all(srv.password not in p for p in printed)

    # повторный вход по --account без --imap: хосты — из конфига
    code, printed, _p, factory_kw = _run_auth(
        core_env, srv, account=acc["id"], imap=None, smtp=None, title=None, name=None, proxy=None
    )
    assert code == 0, printed
    assert factory_kw[0]["flavor"] == "imap" and factory_kw[0]["smtp_host"] == "smtp.timeweb.ru"
    assert len(_mail_accounts(core_env)) == 1


def test_9_auth_plain_wrong_password_writes_nothing(core_env, capsys):
    _data_dir, config_dir = core_env
    srv = FakePlain()
    code, _printed, _p, _f = _run_auth(core_env, srv, password="wrong-password")
    assert code == 1
    err = capsys.readouterr().err
    assert "Ничего не записано" in err
    assert "gmail" not in err.lower() and "приложения" not in err and "wrong-password" not in err
    assert _mail_accounts(core_env) == []
    d = config_dir / "connectors"
    assert not d.exists() or list(d.iterdir()) == []


def test_9_auth_plain_requires_explicit_proxy(core_env, capsys):
    srv = FakePlain()
    code, _printed, prompts, _f = _run_auth(core_env, srv, proxy=None)
    assert code == 1
    assert "--proxy none" in capsys.readouterr().err
    assert prompts == []
    assert _mail_accounts(core_env) == []


def test_9_cli_parser_accepts_plain_flags():
    from hub.cli import _build_parser

    args = _build_parser().parse_args(
        [
            "auth",
            "mail",
            "--imap",
            "imap.timeweb.ru:993",
            "--smtp",
            "smtp.timeweb.ru:465",
            "--address",
            ADDRESS,
            "--name",
            "Максим Орлов",
            "--title",
            "Umbrella",
            "--proxy",
            "none",
        ]
    )
    assert (args.imap, args.smtp, args.name, args.title, args.proxy) == (
        "imap.timeweb.ru:993",
        "smtp.timeweb.ru:465",
        "Максим Орлов",
        "Umbrella",
        "none",
    )


def test_9_cli_connector_plain_without_creds_exits_78(core_env):
    data_dir, config_dir = core_env
    with sync_admin_client(config_dir, data_dir) as admin:
        body = admin.post(
            "/admin/accounts",
            json={"source": "mail", "title": "Umbrella", "db": "main", "config": PLAIN_CFG},
        ).json()
    connectors = config_dir / "connectors"
    connectors.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = connectors / f"{body['account_id']}.key"
    key.write_text(body["connector_secret"])
    key.chmod(0o600)
    env = {**os.environ, "HUB_DATA_DIR": str(data_dir), "HUB_CONFIG_DIR": str(config_dir)}
    proc = subprocess.run(  # noqa: S603 — фиксированный локальный интерпретатор
        [sys.executable, "-c", "from hub.cli import main; main()", "connector", str(body["account_id"])],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 78, proc.stderr
    assert "hub auth mail" in proc.stderr


def test_parse_plain_config():
    cfg = plain.parse_plain_config(PLAIN_CFG)
    assert (cfg["imap_host"], cfg["imap_port"], cfg["smtp_host"], cfg["smtp_port"]) == (
        "imap.timeweb.ru",
        993,
        "smtp.timeweb.ru",
        465,
    )
    with pytest.raises(ValueError):  # noqa: PT011
        plain.parse_plain_config(dict(PLAIN_CFG, imap_host=""))
    with pytest.raises(ValueError):  # noqa: PT011
        plain.parse_plain_config(dict(PLAIN_CFG, flavor="gmail"))


# --- тест 10: вложение письма — загрузчиком по HEADER Message-ID --------------------------------------------


def _with_attachment(msgid: str, data: bytes) -> bytes:
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = ME
    msg["To"] = "buh@company.ru"
    msg["Subject"] = "Счёт"
    msg["Message-ID"] = msgid
    msg.set_content("Счёт во вложении.")
    msg.add_attachment(data, maintype="application", subtype="pdf", filename="счет.pdf")
    return msg.as_bytes()


@pytest.mark.parametrize("box", ["INBOX", "Sent"])
async def test_10_attachment_fetched_by_header_message_id(tmp_path, box):
    srv = FakePlain()
    pdf = b"%PDF-1.4\n" + b"x" * 500
    srv.add(srv.sent_wire if box == "Sent" else "INBOX", _with_attachment("<att@umbrella.example.com>", pdf))
    gw = make_plain_gateway(srv)
    connector = PlainMailConnector(None, gw, account_id=1, config=PLAIN_CFG, state_path=tmp_path / "s.json")
    job = {
        "media_id": 1,
        "lease_gen": 1,
        "max_bytes": 10 * 1024 * 1024,
        "message": {
            "channel_ext_id": "t:att@umbrella.example.com",
            "ext_id": "<att@umbrella.example.com>",
            "thread_ext_id": None,
        },
        "idx": 0,
        "kind": "document",
        "mime": "application/pdf",
        "size": None,
        "name": "счет.pdf",
        "src_id": None,
        "ref": None,
    }
    buf = io.BytesIO()
    try:
        res = await connector.fetch_media(job, MediaSink(buf, None))
        missing = await connector.fetch_media(
            dict(job, message=dict(job["message"], ext_id="<nope@umbrella.example.com>")),
            MediaSink(io.BytesIO(), None),
        )
    finally:
        await gw.close()
    assert res.outcome == "ready"
    assert buf.getvalue() == pdf
    assert missing.outcome == "gone"
    texts = srv.command_texts()
    assert any('HEADER Message-ID "att@umbrella.example.com"' in c for c in texts)
    assert not any("BODY[" in c for c in texts) and not any("STORE" in c for c in texts)


# --- мелочи шлюза вида imap ---------------------------------------------------------------------------------


async def test_examine_without_uidnext_finds_last_uid(env, srv, db):
    """Сервер без `UIDNEXT` в ответе `EXAMINE`: курсор — по последнему UID поиском (иначе встал бы на 0 и
    первый же догон взял бы весь ящик)."""
    srv.send_uidnext = False
    letter(srv, msgid="<old1@c.ru>", date_ts=srv.now_ts - 30 * DAY)
    letter(srv, msgid="<old2@c.ru>", date_ts=srv.now_ts - 30 * DAY)
    connector, _gw, client, _aid = await env()
    await connector.sync("start")
    assert (await core_cursor(client))["boxes"]["inbox"]["last_uid"] == 2
    await connector.sync("scheduled")
    assert messages(db) == []  # старые письма (вне 14 дней) догон не взял


async def test_smtp_587_uses_starttls():
    srv = FakePlain()
    gw = make_plain_gateway(srv, smtp_host="smtp.example.ru", smtp_port=587)
    try:
        await gw.check_smtp_login()
    finally:
        await gw.close()
    kwargs = srv.smtp_log[0][1]
    assert kwargs["hostname"] == "smtp.example.ru" and kwargs["port"] == 587
    assert kwargs["use_tls"] is False and kwargs["start_tls"] is True


# --- Ф11-8: «кому» у своего письма (ящик IMAP/SMTP) ---------------------------------------------------------


async def test_f11_8_own_letter_mail_to_reaches_core(env, srv, db):
    connector, _gw, _client, _aid = await env()
    await connector.sync("start")
    a = letter(srv, msgid="<a8@company.ru>")
    await connector.sync("live")
    letter(
        srv,
        box="Sent",
        msgid="<m8@umbrella.example.com>",
        from_=ME,
        to="Пётр Петров <Petr@Company.ru>, anna@company.ru",
        subject="Re: Предложение",
        references=a.message_id,
        in_reply_to=a.message_id,
        flags=("\\Seen",),
    )
    await connector.sync("live")
    with db.reader() as conn:
        got = dict(conn.execute("SELECT ext_id, mail_to_json FROM main.message").fetchall())
    assert json.loads(got["<m8@umbrella.example.com>"]) == ["petr@company.ru", "anna@company.ru"]
    assert got["<a8@company.ru>"] is None


# --- mail.ru (29.09): IDLE только после входа, поиска по References нет ------------------------------------


async def test_mailru_idle_announced_only_after_login():
    # `imaplib` проверяет IDLE по CAPABILITY до входа; шлюз подставляет ответ после входа
    srv = FakePlain(idle=True, idle_before_login=False)
    gw = make_plain_gateway(srv)
    events, got_new = _collector()
    gw.start_listener(events)
    try:
        assert await asyncio.to_thread(srv.idle_started.wait, 5)
        srv.idle_queue.put(("EXISTS", [b"1"]))
        await asyncio.wait_for(got_new.wait(), 5)
    finally:
        await gw.close()
    assert "new" in events.kinds
    assert "NOOP" not in srv.command_texts()


async def test_mailru_threads_without_references_search(env, srv, db, clock, caplog):
    srv.search_headers = {"message-id"}
    now = srv.now_ts
    a = letter(srv, msgid="<ma@company.ru>", subject="Договор", date_ts=now - 2 * DAY)
    # мой ответ — до первого запуска: в карту его кладёт стартовая загрузка («Отправленные» за те же дни)
    mine = letter(
        srv,
        box="Sent",
        msgid="<mm@mail.ru>",
        from_=ME,
        to="petr@company.ru",
        subject="Re: Договор",
        references=a.message_id,
        in_reply_to=a.message_id,
        date_ts=now - 2 * DAY + 60,
    )
    connector, _gw, _client, _aid = await env()
    await connector.sync("start")
    assert [m["ext_id"] for m in by_thread(db)["t:ma@company.ru"]] == [a.message_id, mine.message_id]

    b = letter(
        srv,
        msgid="<mb@company.ru>",
        subject="Re: Договор",
        references=f"{a.message_id} {mine.message_id}",
        in_reply_to=mine.message_id,
    )
    await connector.sync("live")
    assert [m["ext_id"] for m in by_thread(db)["t:ma@company.ru"]] == [
        a.message_id,
        mine.message_id,
        b.message_id,
    ]

    # прочитанность по треду (письма треда — по карте)
    srv.set_flags("INBOX", a.uid, add=("\\Seen",))
    srv.set_flags("INBOX", b.uid, add=("\\Seen",))
    await connector.sync("live")
    assert channels(db)["t:ma@company.ru"]["last_read"] == b.message_id

    # ответ из хаба без reply_to — родитель по карте: новейшее письмо треда
    oid = await enqueue(db, clock, "t:ma@company.ru")
    await connector.step_outbox()
    assert outbox_row(db, oid)["status"] == "sent"
    assert last_sent(srv)["In-Reply-To"] == b.message_id

    # References спрошен один раз (проба), дальше — только Message-ID; предупреждение одно
    refs = [c for c in srv.command_texts() if "SEARCH" in c and "References" in c]
    assert refs == ['UID SEARCH HEADER References "hub-probe@references.invalid"']
    assert caplog.text.count("не ищет по References") == 1


async def test_mailru_refusal_mid_work_switches_to_map(env, srv, db):
    # проба прошла (сервер «умел»), потом отказал — тред всё равно собирается, сверка не падает
    connector, _gw, _client, _aid = await env()
    await connector.sync("start")
    a = letter(srv, msgid="<qa@company.ru>")
    await connector.sync("live")
    assert connector._refs_ok is True  # noqa: SLF001
    srv.search_headers = {"message-id"}
    b = letter(srv, msgid="<qb@company.ru>", references=a.message_id, in_reply_to=a.message_id)
    await connector.sync("live")
    assert connector._refs_ok is False  # noqa: SLF001
    assert [m["ext_id"] for m in by_thread(db)["t:qa@company.ru"]] == [a.message_id, b.message_id]


async def test_mailru_search_refusal_text_in_error(srv):
    srv.search_headers = {"message-id"}
    gw = make_plain_gateway(srv)
    try:
        await gw.work.connect()
        with pytest.raises(gateway_mod.MailCommandError) as ei:
            await gw.work.search_box("INBOX", "HEADER", "References", '"x"')
    finally:
        await gw.close()
    assert "[CANNOT] Unsupported search criterion: REFERENCES" in str(ei.value)
