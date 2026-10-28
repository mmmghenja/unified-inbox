"""Тест 12 куска 1b: старт коннектора. Нет прокси в config -> код 78 (`hub connector`, реальный core-стенд,
тот же приём, что `tests/test_cli.py`/`tests/connectors/test_run_process.py`); не авторизован ->
`auth_required` и код 78 (`ConnectorBase.run()` напрямую, без реального сокета).

Кусок 1c включил отправку (`sends_enabled=True`) — тест ниже, ранее проверявший обратное (`lease()` ни разу
не вызван, пока `sends_enabled=False`), обновлён: теперь `wait_outbox()` вызывается (цикл отправки идёт), но
`lease()` по-прежнему не вызывается, пока `wait_outbox()` отвечает «нечего выдавать» (`_StubClient` ниже).
"""

import asyncio
import sys

import pytest

from hub.connectors.telegram.connector import TelegramConnector

from .helpers import FakeGateway


async def _instant_sleep(_seconds: float) -> None:
    # `asyncio.sleep(0)`, не голое `return None`: последнее не даёт точки переключения вовсе (`async def`
    # без единого `await` внутри выполняется синхронно до конца), и цикл детектора сна/пульса в `run()`
    # крутится без единой уступки event loop — реальный зависон теста (проверено на себе), а не просто
    # высокая загрузка CPU.
    await asyncio.sleep(0)


class _StubClient:
    def __init__(self) -> None:
        self.lease_calls = 0
        self.statuses: list[str] = []

    async def push_events(self, events, cursor=None):
        del cursor
        return {"applied": len(events), "noop": 0, "dead": 0, "max_seq": 0}

    async def set_status(self, status, *, last_error=None):
        del last_error
        self.statuses.append(status)
        return {"ok": True}

    async def wait_outbox(self, timeout_s):
        del timeout_s
        await asyncio.sleep(0)
        return False

    async def lease(self, *, limit=None, lease_ms=None):
        del limit, lease_ms
        self.lease_calls += 1
        return []

    async def channels_heads(self):
        return []

    async def channels_window(self, *, ext_id, limit=30):
        del ext_id, limit
        return []

    async def set_activity(self, *, backfill):
        del backfill
        return {"ok": True}

    async def aclose(self):
        return None


# --- не авторизован -> auth_required, код 78 ---------------------------------------------------------------


async def test_1b_12_not_authorized_exits_78_and_sets_auth_required():
    client = _StubClient()
    gw = FakeGateway()
    gw.authorized = False
    connector = TelegramConnector(client, gw, account_id=1, sleep=_instant_sleep)

    code = await asyncio.wait_for(connector.run(source="telegram", account_id=1), timeout=5)

    assert code == 78
    assert client.statuses == ["auth_required"]


# --- sends_enabled=True (1c) -> цикл отправки идёт, но lease() не вызывается, пока нечего выдавать --------


async def test_1c_sends_enabled_true_idle_outbox_never_calls_lease():
    assert TelegramConnector.sends_enabled is True

    client = _StubClient()
    gw = FakeGateway()
    connector = TelegramConnector(client, gw, account_id=1, sleep=_instant_sleep)

    run_task = asyncio.ensure_future(connector.run(source="telegram", account_id=1))
    for _ in range(50):
        await asyncio.sleep(0)

    connector._stopping = True  # noqa: SLF001 — штатная остановка, как SIGTERM
    connector._stop_event.set()  # noqa: SLF001
    code = await asyncio.wait_for(run_task, timeout=5)

    assert code == 0
    assert client.lease_calls == 0  # wait_outbox() всегда отвечает False — lease() не берётся вовсе


# --- нет прокси в config -> код 78 (реальный core-стенд, `hub connector` в процессе теста) ------------------


def _add_telegram_account(config_dir, data_dir, *, config: dict | None) -> int:
    from .conftest import sync_admin_client

    with sync_admin_client(config_dir, data_dir) as client:
        r = client.post(
            "/admin/accounts", json={"source": "telegram", "title": None, "db": "main", "config": config}
        )
        assert r.status_code == 200, r.text
        body = r.json()

    account_id = body["account_id"]
    connectors_dir = config_dir / "connectors"
    connectors_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_path = connectors_dir / f"{account_id}.key"
    key_path.write_text(body["connector_secret"], encoding="utf-8")
    key_path.chmod(0o600)
    return account_id


def test_1b_12_missing_proxy_in_config_exits_78(core_env) -> None:
    data_dir, config_dir = core_env
    account_id = _add_telegram_account(config_dir, data_dir, config=None)  # никакого proxy вовсе

    from hub import cli as hub_cli

    old_argv = sys.argv
    sys.argv = ["hub", "connector", str(account_id)]
    try:
        with pytest.raises(SystemExit) as exc_info:
            hub_cli.main()
    finally:
        sys.argv = old_argv

    assert exc_info.value.code == 78
