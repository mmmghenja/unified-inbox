"""Ф29-1 (docs/architecture.md: сквозной путь на стенде ядра — сессия зовёт
`POST /api/v1/tg/search` и `/tg/peek`, коннектор Telegram (`TelegramConnector` с `FakeGateway`, без сети)
берёт заявку с внутреннего сокета, отвечает, сессия получает ответ; в БД хаба ничего, кроме строки аудита."""

import asyncio

import pytest
from connectors.telegram.helpers import FakeGateway, make_channel, make_message
from telethon import utils
from telethon.tl import types

from hub.core.app_internal import create_internal_app
from hub.core.app_public import create_public_app
from hub.core.connector_auth import hash_secret
from hub.core.tg_lookup import GAP_MS, TgLookup

from .conftest import ADMIN_SECRET, PUBLIC_PORT, create_project, create_token
from .test_telegram_send import create_tg_account, make_tg_connector


@pytest.fixture
def lookup():
    return TgLookup(answer_timeout_s=5.0)


@pytest.fixture
def public_app(db, clock, notifier, lookup):
    return create_public_app(db, clock=clock, port=PUBLIC_PORT, notifier=notifier, tg_lookup=lookup)


@pytest.fixture
def internal_app(db, clock, notifier, lookup):
    db.on_commit(notifier.notify)
    return create_internal_app(
        db, clock=clock, notifier=notifier, admin_key_hash=hash_secret(ADMIN_SECRET), tg_lookup=lookup
    )


def _count(db, table: str) -> int:
    with db.reader() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0]  # noqa: S608


async def test_search_and_peek_through_connector(admin_client, public_client, internal_transport, db, clock):
    account_id, secret = await create_tg_account(admin_client)
    project_id = await create_project(admin_client, name="Umbrella")
    token = await create_token(
        admin_client, name="mcp:all", scopes=("read", "draft"), project_ids=[project_id]
    )
    gateway = FakeGateway()
    connector = make_tg_connector(
        internal_transport, secret=secret, gateway=gateway, account_id=account_id, clock=clock
    )
    await connector._client.set_status("ok")  # noqa: SLF001 — пульс: коннектор жив

    chan = make_channel(4242, title="Продавцы WB", broadcast=True, username="sellers_wb")
    chan.participants_count = 900
    chan.left = True
    gateway.search_found["продавцы"] = types.contacts.Found(
        my_results=[], results=[types.PeerChannel(4242)], chats=[chan], users=[]
    )
    marked = utils.get_peer_id(chan)
    gateway.history[marked] = [make_message(5, types.PeerChannel(4242), message="пост про ЭЦП")]
    lookuper = connector._make_lookuper()  # noqa: SLF001
    headers = {"Authorization": f"Bearer {token}"}
    channels_before, messages_before = _count(db, "channel"), _count(db, "message")

    task = asyncio.ensure_future(
        public_client.post("/api/v1/tg/search", json={"query": "продавцы"}, headers=headers)
    )
    assert await lookuper.step() is True
    r = await task
    assert r.status_code == 200, r.text
    [row] = r.json()["results"]
    assert row["username"] == "sellers_wb" and row["participants"] == 900 and row["channel_id"] is None

    clock.advance_ms(GAP_MS)
    task = asyncio.ensure_future(
        public_client.post("/api/v1/tg/peek", json={"target": "t.me/sellers_wb"}, headers=headers)
    )
    assert await lookuper.step() is True
    r = await task
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["chat"]["title"] == "Продавцы WB" and body["chat"]["ext_id"] == str(marked)
    assert [m["text"] for m in body["messages"]] == ["пост про ЭЦП"]
    # Сущность из поиска — в кэше коннектора: резолва по имени не было.
    assert gateway.resolve_calls == []
    # Ничего не сохранено: ни бесед, ни сообщений.
    assert (_count(db, "channel"), _count(db, "message")) == (channels_before, messages_before)
    await connector._client.aclose()  # noqa: SLF001
