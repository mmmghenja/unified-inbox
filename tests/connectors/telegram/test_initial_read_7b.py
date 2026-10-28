"""Кусок 7b, К1 (`docs/architecture.md`): стартовая загрузка
неизвестного активного диалога ставит маркер прочтения сразу (`read` по `read_inbox_max_id` после сообщений
чата), а не на следующей сверке через 10 ± 3 мин. Иначе 30 сообщений истории висели непрочитанными и в UI,
и для дежурного агента, который разбирает непрочитанное. Настоящее ядро в процессе (`ASGITransport`)."""

from hub.connectors.telegram.connector import TelegramConnector
from hub.core.ingest import unread_breakdown

from . import helpers as h
from .conftest import create_tg_account, make_core_client
from .helpers import FakeGateway

_FAKE_NOW_S = 1_700_000_000.0  # совпадает с умолчаниями `helpers.make_dialog`/`make_message`


async def test_7b_k1_initial_load_sets_read_marker_immediately(admin_client, internal_transport, db):
    account_id, secret = await create_tg_account(admin_client)
    client = make_core_client(internal_transport, secret)
    gw = FakeGateway()
    connector = TelegramConnector(client, gw, account_id=account_id, wall=lambda: _FAKE_NOW_S)
    connector._me_id = gw.me.id  # noqa: SLF001 — обычно ставит start_source(), тут его не гоняем целиком
    other = h.make_user(9, first_name="Новый")
    gw.add_dialog(h.make_dialog(other, top_message=50, read_inbox_max_id=48))
    for i in range(1, 51):
        gw.add_message(9, h.make_message(i, h.peer_of(other), message=f"m{i}", from_id=None))

    emitted: list[dict] = []
    connector.emit = lambda ev: emitted.append(ev)
    await connector.sync("start")

    upserts = [e for e in emitted if e["type"] == "upsert" and e["channel"]["ext_id"] == "9"]
    reads = [e for e in emitted if e["type"] == "read" and e["channel"]["ext_id"] == "9"]
    assert len(upserts) == 30
    assert [r["up_to_ext_id"] for r in reads] == ["48"]
    assert emitted.index(reads[0]) > emitted.index(upserts[-1])  # маркер — после сообщений чата

    await client.push_events(emitted)
    with db.reader() as conn:
        channel_id = conn.execute("SELECT id FROM main.channel WHERE ext_id = '9'").fetchone()[0]
        unread_top, _threads = unread_breakdown(conn, channel_id)
    assert unread_top == 2  # 49 и 50; прочитанная история (21–48) непрочитанным не считается


async def test_7b_k1_initial_load_without_read_marker_sends_no_read(admin_client, internal_transport):
    account_id, secret = await create_tg_account(admin_client)
    client = make_core_client(internal_transport, secret)
    gw = FakeGateway()
    connector = TelegramConnector(client, gw, account_id=account_id, wall=lambda: _FAKE_NOW_S)
    connector._me_id = gw.me.id  # noqa: SLF001
    other = h.make_user(10, first_name="Совсем новый")
    gw.add_dialog(h.make_dialog(other, top_message=3, read_inbox_max_id=0))
    for i in range(1, 4):
        gw.add_message(10, h.make_message(i, h.peer_of(other), message=f"m{i}", from_id=None))

    emitted: list[dict] = []
    connector.emit = lambda ev: emitted.append(ev)
    await connector.sync("start")
    assert [e for e in emitted if e["type"] == "read"] == []
    assert len([e for e in emitted if e["type"] == "upsert"]) == 3
