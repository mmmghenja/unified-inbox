"""Общая обвязка тестов внутреннего сокета. Помощники наполнения БД (`insert_account`, `insert_channel`,
`insert_project`, `insert_message`, `make_bearer_token`, `FakeClock`) — те же, что в `tests/api/conftest.py`;
импортируем их оттуда по файловому пути (не через точечный импорт: `tests/` — не пакет, у него нет
`__init__.py`, а трогать существующий `tests/api/conftest.py` запрещено условиями задачи).
"""

import importlib.util
import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import connector_auth
from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.feed import Notifier
from hub.core.outbox import enqueue

_API_CONFTEST_PATH = Path(__file__).resolve().parents[1] / "api" / "conftest.py"
_spec = importlib.util.spec_from_file_location("_hub_test_api_conftest", _API_CONFTEST_PATH)
assert _spec is not None and _spec.loader is not None
_api_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_api_conftest)

FakeClock = _api_conftest.FakeClock
insert_account = _api_conftest.insert_account
insert_channel = _api_conftest.insert_channel
insert_project = _api_conftest.insert_project
insert_message = _api_conftest.insert_message
insert_thread = _api_conftest.insert_thread
make_bearer_token = _api_conftest.make_bearer_token

INTERNAL_BASE_URL = "http://hub-internal"
ADMIN_SECRET = ADMIN_PREFIX + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHI"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def notifier() -> Notifier:
    return Notifier()


@pytest.fixture
def internal_app(db, clock, notifier):
    db.on_commit(notifier.notify)
    return create_internal_app(db, clock=clock, notifier=notifier, admin_key_hash=hash_secret(ADMIN_SECRET))


@pytest.fixture
async def internal_client(internal_app):
    transport = ASGITransport(app=internal_app)
    async with AsyncClient(transport=transport, base_url=INTERNAL_BASE_URL) as c:
        yield c


def connector_headers(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_SECRET}"}


def insert_connector_secret(db, account_id: int, *, secret: str, now_ms: int = 0) -> None:
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.connector_secret (account_id, hash, created_ts) VALUES (?, ?, ?)",
            (account_id, connector_auth.hash_secret(secret), now_ms),
        )
    )


def make_connector_account(
    db, *, source="tg", db_name="main", idempotent=False, flood_until=None
) -> tuple[int, str]:
    """Аккаунт + секрет коннектора, готовые к использованию в заголовке Authorization. Возвращает
    `(account_id, secret)`."""
    config = json.dumps({"idempotent_send": True}) if idempotent else None

    def _fn(conn):
        cur = conn.execute(
            "INSERT INTO main.account (source, db, config_json, flood_until) VALUES (?, ?, ?, ?)",
            (source, db_name, config, flood_until),
        )
        return cur.lastrowid

    account_id = db.write_sync(_fn)
    secret = connector_auth.generate_connector_secret()
    insert_connector_secret(db, account_id, secret=secret)
    return account_id, secret


def insert_api_token_row(db, *, kind="ui", name="tok") -> int:
    """`audit.api_token_id` — FK на `main.api_token`: `enqueue`/`report` пишут строку в audit, поэтому
    строка `api_token` должна реально существовать, даже если сам токен тестам не нужен."""
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.api_token (name, kind, hash) VALUES (?, ?, 'x')", (name, kind)
            ).lastrowid
        )
    )


def enqueue_outbox(
    db, channel_id: int, *, api_token_id: int | None = None, text="hi", origin="ui", now_ms=1_000, **kwargs
):
    """Тонкая обёртка над `outbox.enqueue` для наполнения строк, которые потом берут `lease`/`report`."""
    if api_token_id is None:
        api_token_id = insert_api_token_row(db)
    return db.write_sync(
        lambda conn: enqueue(
            conn,
            channel_id=channel_id,
            text=text,
            origin=origin,
            api_token_id=api_token_id,
            idempotency_key=kwargs.pop("idempotency_key", None),
            now_ms=now_ms,
            **kwargs,
        )
    )
