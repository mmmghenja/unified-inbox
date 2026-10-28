"""Тест 8: правила каркаса `ConnectorBase`. Спецификация: docs/architecture.md.

`CoreClient` здесь не участвует — заглушка `_FakeCoreClient` просто записывает вызовы `lease`/`report`/
`set_status`, без реального сокета (это отдельно проверяется в `tests/internal/test_socket.py`, кусок 5а,
тест 1). `ConnectorBase` работает с любым объектом такой формы — реальный тип в конструкторе не проверяется.
"""

import pytest

from hub.connectors.base import AuthRequired, ConnectorBase, SendResult


class _FakeCoreClient:
    def __init__(self, leases: list[dict]) -> None:
        self._leases = leases
        self.reports: list[dict] = []
        self.statuses: list[str] = []

    async def lease(self, *, limit=None, lease_ms=None) -> list[dict]:
        del limit, lease_ms
        leases, self._leases = self._leases, []
        return leases

    async def report(self, **kwargs) -> dict:
        self.reports.append(kwargs)
        return {"ok": True}

    async def set_status(self, status: str, *, last_error: str | None = None) -> dict:
        del last_error
        self.statuses.append(status)
        return {"ok": True}


def _lease(outbox_id=1, mode="send", lease_gen=1) -> dict:
    return {
        "outbox_id": outbox_id,
        "channel_ext_id": "c1",
        "text": "hi",
        "reply_to_ext_id": None,
        "thread_ext_id": None,
        "send_token": "tok",
        "mode": mode,
        "send_started_ts": 0,
        "attempts": 1,
        "lease_gen": lease_gen,
    }


class _Connector(ConnectorBase):
    def __init__(self, client, *, send_impl=None, reconcile_impl=None) -> None:
        super().__init__(client)
        self.send_calls: list[dict] = []
        self.reconcile_calls: list[dict] = []
        self._send_impl = send_impl
        self._reconcile_impl = reconcile_impl

    async def sync(self) -> None:
        pass

    async def send(self, lease: dict) -> SendResult:
        self.send_calls.append(lease)
        if self._send_impl is not None:
            return await self._send_impl(lease)
        return SendResult("sent", ext_id="e1")

    async def reconcile(self, lease: dict) -> SendResult:
        self.reconcile_calls.append(lease)
        if self._reconcile_impl is not None:
            return await self._reconcile_impl(lease)
        return SendResult("sent", ext_id="e1")

    async def health(self) -> None:
        pass


async def test_exception_in_send_reports_unknown_not_temporary() -> None:
    async def _boom(_lease: dict) -> SendResult:
        raise RuntimeError("сеть моргнула")

    client = _FakeCoreClient([_lease()])
    connector = _Connector(client, send_impl=_boom)

    processed = await connector.step_outbox()

    assert processed == 1
    assert len(client.reports) == 1
    assert client.reports[0]["outcome"] == "unknown"
    assert client.reports[0]["outcome"] != "temporary"


async def test_reconcile_lease_never_calls_send() -> None:
    client = _FakeCoreClient([_lease(mode="reconcile")])
    connector = _Connector(client)

    await connector.step_outbox()

    assert connector.send_calls == []
    assert len(connector.reconcile_calls) == 1
    assert client.reports[0]["outcome"] == "sent"


async def test_auth_required_stops_sending() -> None:
    async def _auth_fail(_lease: dict) -> SendResult:
        raise AuthRequired("сессия протухла")

    client = _FakeCoreClient([_lease(outbox_id=1), _lease(outbox_id=2)])
    connector = _Connector(client, send_impl=_auth_fail)

    processed = await connector.step_outbox()

    assert processed == 1  # первый лиз забрал auth_required и остановил обработку пачки
    assert connector.auth_required is True
    assert client.statuses == ["auth_required"]
    assert client.reports == []  # об auth_required не отчитываются как об исходе отправки

    # После установки флага новый шаг цикла не берёт лизы вовсе — выход из цикла без ретраев.
    client_2 = _FakeCoreClient([_lease(outbox_id=3)])
    connector._client = client_2  # noqa: SLF001 — подменяем клиента, чтобы доказать отсутствие lease()
    processed_2 = await connector.step_outbox()
    assert processed_2 == 0


@pytest.mark.parametrize("mode", ["send", "reconcile"])
async def test_step_outbox_reports_exact_lease_identity(mode: str) -> None:
    client = _FakeCoreClient([_lease(outbox_id=42, mode=mode, lease_gen=7)])
    connector = _Connector(client)

    await connector.step_outbox()

    assert client.reports[0]["outbox_id"] == 42
    assert client.reports[0]["lease_gen"] == 7


# Поправки после opus-критика кода 2c+2d (`docs/architecture.md`): core отказал в
# отчёте по ОДНОМУ лизу (409 StaleLease, прочие 4xx) — строка в журнал, остальные лизы пачки обрабатываются;
# 401 (секрет отозван) по-прежнему уходит наверх (цикл отправки -> выход 78).
def _http_error(status: int):
    import httpx

    request = httpx.Request("POST", "http://hub-internal/internal/v1/outbox/report")
    return httpx.HTTPStatusError("x", request=request, response=httpx.Response(status, request=request))


class _RejectingCoreClient(_FakeCoreClient):
    def __init__(self, leases: list[dict], *, reject: dict[int, int]) -> None:
        super().__init__(leases)
        self._reject = reject

    async def report(self, **kwargs) -> dict:
        self.reports.append(kwargs)
        status = self._reject.get(kwargs["outbox_id"])
        if status is not None:
            raise _http_error(status)
        return {"ok": True}


@pytest.mark.parametrize("status", [409, 422])
async def test_rejected_report_does_not_orphan_rest_of_batch(status: int, caplog) -> None:
    client = _RejectingCoreClient([_lease(outbox_id=1), _lease(outbox_id=2)], reject={1: status})
    connector = _Connector(client)
    with caplog.at_level("WARNING"):
        processed = await connector.step_outbox()
    assert processed == 2
    assert [c["outbox_id"] for c in connector.send_calls] == [1, 2]
    assert [r["outbox_id"] for r in client.reports] == [1, 2]
    assert any("отверг отчёт" in r.getMessage() for r in caplog.records)


async def test_unauthorized_report_still_propagates() -> None:
    import httpx

    client = _RejectingCoreClient([_lease(outbox_id=1), _lease(outbox_id=2)], reject={1: 401})
    connector = _Connector(client)
    with pytest.raises(httpx.HTTPStatusError):
        await connector.step_outbox()
    assert [c["outbox_id"] for c in connector.send_calls] == [1]
