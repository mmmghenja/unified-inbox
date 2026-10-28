"""Ф8-7 (C3 + П3, тест 13): прочитанность из хаба в Gmail — `GmailGateway.mark_thread_seen` на уровне команд
IMAP (`FakeGmail`) и `MailConnector.push_read` с настоящим ядром в процессе."""

from __future__ import annotations

import asyncio
import re

import pytest

from hub.connectors.base import AuthRequired
from hub.connectors.mail.gateway import MailCommandError

from .conftest import make_connector, make_gateway
from .fakegmail import ALL_BOX, BASE_TS
from .helpers import channels, incoming, init_cursor, messages

# ни одной команды, которая может удалить письма или снять/поставить иной флаг
_FORBIDDEN = re.compile(r"\b(CLOSE|EXPUNGE|COPY|MOVE|APPEND)\b|-FLAGS", re.IGNORECASE)


def _conn_cmds(gm, conn_name: str) -> list[str]:
    return [c for n, c in gm.commands if n == conn_name]


def _assert_no_forbidden(gm) -> None:
    for c in gm.command_texts():
        words = c.split()
        if "STORE" in words[:2]:
            # единственная форма STORE в шлюзе — по UID, добавить, только \Seen
            assert words[0] == "UID" and words[1] == "STORE", c
            assert words[3:] == ["+FLAGS.SILENT", "(\\Seen)"], c
            continue
        assert not _FORBIDDEN.search(c), c


@pytest.fixture
async def gw(gm):
    g = make_gateway(gm)
    yield g
    await g.close()


def _thread(gm):
    """Тред из пяти писем: старое непрочитанное, прочитанное, черновик, черновик-меткой, свежее — и чужой
    тред рядом. Даты — по минуте."""
    a = incoming(gm, subject="Смета", date_ts=BASE_TS + 60)
    thrid = a.thrid
    b = incoming(gm, thrid=thrid, flags=("\\Seen",), date_ts=BASE_TS + 120)
    draft = incoming(gm, thrid=thrid, flags=("\\Draft",), date_ts=BASE_TS + 150)
    draft_label = incoming(gm, thrid=thrid, labels=("\\Draft",), date_ts=BASE_TS + 160)
    c = incoming(gm, thrid=thrid, date_ts=BASE_TS + 180)
    fresh = incoming(gm, thrid=thrid, date_ts=BASE_TS + 240)
    other = incoming(gm, subject="Другое", date_ts=BASE_TS + 60)
    return thrid, a, b, draft, draft_label, c, fresh, other


async def test_13_order_only_unseen_up_to_ts_and_back_to_examine(gm, gw):
    thrid, a, b, draft, draft_label, c, fresh, other = _thread(gm)
    await gw.work.connect()
    start = len(gm.commands)

    marked = await gw.mark_thread_seen(thrid, (BASE_TS + 180) * 1000)

    assert marked == 2
    [conn] = gm.connections
    cmds = [cmd for _n, cmd in gm.commands[start:]]
    box = f'"{ALL_BOX}"'
    assert cmds == [
        f"SELECT {box}",
        f"UID SEARCH X-GM-THRID {thrid}",
        f"UID FETCH {a.uid}:{fresh.uid} (UID INTERNALDATE FLAGS X-GM-LABELS)",
        f"UID STORE {a.uid},{c.uid} +FLAGS.SILENT (\\Seen)",
        f"EXAMINE {box} (CONDSTORE)",
    ]
    assert not conn.read_write  # соединение вернулось в EXAMINE
    assert "\\Seen" in a.flags and "\\Seen" in c.flags
    for m in (draft, draft_label, fresh, other):
        assert "\\Seen" not in m.flags
    assert "\\Seen" in b.flags  # уже был
    _assert_no_forbidden(gm)


async def test_13_nothing_to_mark_and_empty_thread(gm, gw):
    a = incoming(gm, flags=("\\Seen",))
    await gw.work.connect()
    start = len(gm.commands)
    assert await gw.mark_thread_seen(a.thrid, a.date_ts * 1000) == 0
    assert await gw.mark_thread_seen(123456789, a.date_ts * 1000) == 0  # треда нет (Корзина/Спам)
    cmds = [cmd for _n, cmd in gm.commands[start:]]
    assert not any(c.split()[:2] == ["UID", "STORE"] for c in cmds)
    assert [c.split()[0] for c in cmds] == ["SELECT", "UID", "UID", "EXAMINE", "SELECT", "UID", "EXAMINE"]
    _assert_no_forbidden(gm)


@pytest.mark.parametrize("fail", ["STORE", "EXAMINE", "SELECT"])
async def test_p3_no_inside_drops_connection_next_call_on_fresh_examine(gm, gw, fail):
    a = incoming(gm)
    await gw.work.connect()
    gm.no_once.append(fail)

    with pytest.raises(MailCommandError):
        await gw.mark_thread_seen(a.thrid, a.date_ts * 1000)

    first = gm.connections[0]
    assert first.closed  # испорчено и закрыто, в SELECT не осталось
    state = await gw.work.examine()  # обычная команда сверки — уже на новом соединении
    assert state.uidvalidity == gm.uidvalidity
    assert len(gm.connections) == 2
    second = _conn_cmds(gm, gm.connections[1].name)
    assert second[0].startswith("LOGIN")
    assert not any(c.startswith("SELECT") for c in second)
    assert not gm.connections[1].read_write
    _assert_no_forbidden(gm)


async def test_p3_one_job_no_interleaving(gm, gw):
    """П3: весь метод — одно задание сессии: параллельная команда сверки не вклинивается между SELECT и
    возвратным EXAMINE."""
    thrid, *_rest = _thread(gm)
    await gw.work.connect()
    start = len(gm.commands)
    await asyncio.gather(
        gw.mark_thread_seen(thrid, (BASE_TS + 600) * 1000),
        gw.work.search_new(0),
        gw.work.fetch_flags_range(1, 3),
    )
    heads = [" ".join(cmd.split()[:2]) for _n, cmd in gm.commands[start:]]
    i = next(i for i, c in enumerate(heads) if c.startswith("SELECT"))
    assert [h.split()[-1] for h in heads[i + 1 : i + 4]] == ["SEARCH", "FETCH", "STORE"]
    assert heads[i + 4].startswith("EXAMINE")
    assert len(heads) == 7  # две команды сверки — до или после, но не внутри


# --- MailConnector.push_read -----------------------------------------------------------------------------


@pytest.fixture
async def env(admin_client, internal_transport, gm):
    made = []

    async def _make(**kw):
        c = await make_connector(admin_client, internal_transport, gm, **kw)
        made.append(c[1])
        return c

    yield _make
    for g in made:
        await g.close()


def _item(thrid, ts, channel_id=5) -> dict:
    return {
        "channel_id": channel_id,
        "gen": 1,
        "lease_gen": 1,
        "channel": {"ext_id": str(thrid), "kind": "mail_thread", "parent_ext_id": None},
        "message": {"ext_id": "<x@example.com>", "ts": ts, "ext_sort": 0, "thread_ext_id": None},
    }


async def test_13_push_read_then_regular_sync_still_works(env, gm, db):
    connector, _gw, _client, _aid = await env()
    await init_cursor(connector)
    a = incoming(gm, subject="Смета")
    await connector.sync("live")
    assert channels(db)[str(a.thrid)]["last_read"] is None

    res = await connector.push_read(_item(a.thrid, a.date_ts * 1000))
    assert res.outcome == "done"
    assert "\\Seen" in a.flags

    b = incoming(gm, subject="Новое")
    await connector.sync("live")  # обычная сверка после метода: новое письмо принято, \Seen вернулся маркером
    assert b.message_id in {r["ext_id"] for r in messages(db)}
    assert channels(db)[str(a.thrid)]["last_read"] == a.message_id
    assert "\\Seen" not in b.flags
    _assert_no_forbidden(gm)


async def test_push_read_zero_marked_is_done_and_bad_ids_drop(env, gm):
    connector, _gw, _client, _aid = await env()
    a = incoming(gm, flags=("\\Seen",))
    assert (await connector.push_read(_item(a.thrid, a.date_ts * 1000))).outcome == "done"
    bad = await connector.push_read(_item("abc", a.date_ts * 1000))
    assert bad.outcome == "drop" and bad.reason == "bad_thread_id"
    no_ts = await connector.push_read(_item(a.thrid, None))
    assert no_ts.outcome == "drop"
    assert connector.read_ack_enabled is True


async def test_push_read_imap_error_is_retry_and_auth_raises(env, gm):
    connector, gw, _client, _aid = await env()
    a = incoming(gm)
    await gw.work.connect()
    gm.no_once.append("STORE")
    res = await connector.push_read(_item(a.thrid, a.date_ts * 1000))
    assert res.outcome == "retry" and res.reason == "MailCommandError"
    assert "\\Seen" not in a.flags

    for conn in gm.connections:  # следующее соединение — с отказом во входе
        conn.killed = True
    gw.work.kill()
    gm.login_error = b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)"
    with pytest.raises(AuthRequired):
        await connector.push_read(_item(a.thrid, a.date_ts * 1000))
