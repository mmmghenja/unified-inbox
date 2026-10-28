"""Фикстуры тестов почты (кусок 5a). Ядро — настоящее, в процессе (`ASGITransport`), как у Slack; Gmail —
`FakeGmail` (подделка соединения `imaplib` и `aiosmtplib.SMTP`), сети нет вообще."""

from __future__ import annotations

import asyncio
import threading

import pytest

from hub.connectors.base import CoreClient
from hub.connectors.mail.connector import MailConnector
from hub.connectors.mail.gateway import GmailGateway

from .. import core_stand as _core
from .fakegmail import FakeGmail

ADDRESS = "me@gmail.com"

# фикстуры стенда ядра (`tests/connectors/core_stand.py`) переиспользуются как есть (настоящее ядро в процессе
# и `hub core` подпроцессом)
db = _core.db
clock = _core.clock
notifier = _core.notifier
internal_app = _core.internal_app
internal_transport = _core.internal_transport
admin_client = _core.admin_client
core_env = _core.core_env
sync_admin_client = _core.sync_admin_client


class Mono:
    """Подменяемые монотонные часы (потокобезопасно достаточно для тестов)."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.value

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.value += seconds


def fast_wait(event: threading.Event, seconds: float) -> None:
    """Паузы слушателя в тестах — реальные, но очень короткие (длительность пишется в `waits`)."""
    event.wait(min(seconds, 0.01))


@pytest.fixture
def gm() -> FakeGmail:
    return FakeGmail(address=ADDRESS)


def make_gateway(gm: FakeGmail, **kwargs) -> GmailGateway:
    params = {
        "address": ADDRESS,
        "password": gm.password,
        "imap_factory": gm.imap_factory,
        "smtp_factory": gm.smtp_factory,
        "listener_wait": fast_wait,
    }
    params.update(kwargs)
    return GmailGateway(**params)


async def create_mail_account(
    admin_client, *, db: str = "main", config: dict | None = None
) -> tuple[int, str]:
    cfg = {"address": ADDRESS, "aliases": [], "display_name": "владелец", "provider": "gmail", "proxy": None}
    if config:
        cfg.update(config)
    r = await admin_client.post(
        "/internal/v1/admin/accounts", json={"source": "mail", "title": ADDRESS, "db": db, "config": cfg}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body["account_id"], body["connector_secret"]


async def make_connector(
    admin_client,
    internal_transport,
    gm: FakeGmail,
    *,
    db: str = "main",
    config: dict | None = None,
    gw_kwargs: dict | None = None,
    **conn_kwargs,
):
    account_id, secret = await create_mail_account(admin_client, db=db, config=config)
    client = CoreClient(secret=secret, transport=internal_transport)
    gw = make_gateway(gm, **(gw_kwargs or {}))
    cfg = {"address": ADDRESS, "display_name": "владелец"}
    if config:
        cfg.update(config)
    if "sleep" not in conn_kwargs:

        async def _sleep(_s: float) -> None:
            await asyncio.sleep(0)

        conn_kwargs["sleep"] = _sleep
    connector = MailConnector(client, gw, account_id=account_id, config=cfg, **conn_kwargs)
    connector._loop = asyncio.get_running_loop()  # noqa: SLF001 — как после start_source
    return connector, gw, client, account_id


def schema_of(db_name: str) -> str:
    return "work" if db_name == "work" else "main"
