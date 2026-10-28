"""Ф51-1 A2 (docs/architecture.md): причина отказа входа — в журнал и в `last_error` аккаунта."""

import logging

from hub.connectors.base import AuthRequired, ConnectorBase, SendResult


class _Client:
    def __init__(self, leases: list[dict] | None = None) -> None:
        self._leases = leases or []
        self.statuses: list[tuple[str, str | None]] = []

    async def lease(self, *, limit=None, lease_ms=None) -> list[dict]:
        del limit, lease_ms
        leases, self._leases = self._leases, []
        return leases

    async def report(self, **kwargs) -> dict:
        return {"ok": True}

    async def set_status(self, status: str, *, last_error: str | None = None) -> dict:
        self.statuses.append((status, last_error))
        return {"ok": True}


class _Connector(ConnectorBase):
    def __init__(self, client, *, health_exc: BaseException | None = None) -> None:
        super().__init__(client)
        self._health_exc = health_exc

    async def sync(self) -> None:
        pass

    async def send(self, lease: dict) -> SendResult:
        raise AuthRequired("imap: вход не принят (other)")

    async def reconcile(self, lease: dict) -> SendResult:
        return SendResult("sent", ext_id="e1")

    async def health(self) -> None:
        if self._health_exc is not None:
            raise self._health_exc


def _lease() -> dict:
    return {
        "outbox_id": 1,
        "channel_ext_id": "c1",
        "text": "hi",
        "reply_to_ext_id": None,
        "thread_ext_id": None,
        "send_token": "tok",
        "mode": "send",
        "send_started_ts": 0,
        "attempts": 1,
        "lease_gen": 1,
    }


async def test_send_auth_required_passes_reason_to_status_and_log(caplog) -> None:
    client = _Client([_lease()])
    connector = _Connector(client)
    with caplog.at_level(logging.WARNING, logger="hub.connectors.base"):
        await connector.step_outbox()
    assert client.statuses == [("auth_required", "imap: вход не принят (other)")]
    assert any("imap: вход не принят (other)" in r.getMessage() for r in caplog.records)
    assert connector._exit_code == 78  # noqa: SLF001


async def test_heartbeat_auth_required_passes_reason() -> None:
    client = _Client()
    connector = _Connector(client, health_exc=AuthRequired("imap: вход не принят (bad_password)"))
    await connector._heartbeat_loop()  # noqa: SLF001 — выходит сам после потери входа
    assert client.statuses == [("auth_required", "imap: вход не принят (bad_password)")]


async def test_reason_is_one_line_and_bounded_and_survives_late_call_without_reason(caplog) -> None:
    client = _Client()
    connector = _Connector(client)
    with caplog.at_level(logging.WARNING, logger="hub.connectors.base"):
        await connector._on_auth_required("первая\nстрока " + "я" * 500)  # noqa: SLF001
        await connector._on_auth_required()  # noqa: SLF001 — работник зовёт без причины
    first, second = (reason for _status, reason in client.statuses)
    assert first is not None and "\n" not in first and len(first) <= 200
    assert second == first  # поздний вызов без причины её не стирает


async def test_no_reason_is_none_as_before(caplog) -> None:
    client = _Client()
    connector = _Connector(client)
    with caplog.at_level(logging.WARNING, logger="hub.connectors.base"):
        await connector._on_auth_required()  # noqa: SLF001
    assert client.statuses == [("auth_required", None)]
    assert any("причина не названа" in r.getMessage() for r in caplog.records)
