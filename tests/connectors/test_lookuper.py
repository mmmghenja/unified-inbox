"""Ф29-1 (docs/architecture.md): цикл исполнителя поиска и чтения `Lookuper`
против фейкового ядра и `ConnectorBase` по умолчанию против настоящего внутреннего ядра — ответ `ok`/`error`,
`AuthRequired` -> `error auth_required` и выход по потере входа, сбой ядра — пауза и снова, 401 — стоп."""

from __future__ import annotations

import asyncio

import httpx
from httpx import ASGITransport

from hub.connectors.base import AuthRequired, CoreClient
from hub.connectors.fake import FakeConnector, FakeSource
from hub.connectors.lookup import Lookuper, LookupResult
from hub.core import connector_auth
from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.feed import Notifier
from hub.core.tg_lookup import TgLookup


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://hub-internal/internal/v1/lookup/next")
    return httpx.HTTPStatusError(
        str(status), request=request, response=httpx.Response(status, request=request)
    )


class FakeCore:
    def __init__(self) -> None:
        self.items: list[dict | Exception] = []
        self.results: list[dict] = []
        self.result_errors: list[Exception] = []

    async def lookup_next(self, timeout_s: float) -> dict | None:
        del timeout_s
        await asyncio.sleep(0)
        if not self.items:
            return None
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def lookup_result(self, body: dict) -> dict:
        if self.result_errors:
            raise self.result_errors.pop(0)
        self.results.append(body)
        return {"ok": True}


class Harness:
    def __init__(self, run) -> None:
        self.core = FakeCore()
        self.now = 100.0
        self.sleeps: list[float] = []
        self.auth_calls = 0
        self.unauthorized = 0
        self.lookuper = Lookuper(
            self.core,  # type: ignore[arg-type]
            run,
            mono=lambda: self.now,
            sleep=self._sleep,
            is_blocked=lambda: False,
            on_auth_required=self._auth,
            on_unauthorized=self._unauth,
        )

    async def _sleep(self, s: float) -> None:
        self.sleeps.append(s)
        await asyncio.sleep(0)

    async def _auth(self) -> None:
        self.auth_calls += 1

    def _unauth(self) -> None:
        self.unauthorized += 1


async def test_ok_and_error_results_go_to_core():
    async def run(item):
        if item["kind"] == "search":
            return LookupResult("ok", payload={"results": []})
        return LookupResult("error", reason="flood", retry_after_s=12.5)

    h = Harness(run)
    h.core.items = [{"id": 1, "kind": "search", "params": {}}, {"id": 2, "kind": "peek", "params": {}}]
    assert await h.lookuper.step() is True
    assert await h.lookuper.step() is True
    assert await h.lookuper.step() is False
    assert h.core.results == [
        {"id": 1, "outcome": "ok", "payload": {"results": []}},
        {"id": 2, "outcome": "error", "reason": "flood", "retry_after_s": 12.5},
    ]


async def test_auth_required_reports_then_exits():
    async def run(item):
        raise AuthRequired("lost")

    h = Harness(run)
    h.core.items = [{"id": 3, "kind": "peek", "params": {}}]
    await h.lookuper.step()
    assert h.core.results == [{"id": 3, "outcome": "error", "reason": "auth_required"}]
    assert h.auth_calls == 1


async def test_crash_and_bad_result_are_errors_and_loop_lives():
    calls = []

    async def run(item):
        calls.append(item["id"])
        if item["id"] == 4:
            raise RuntimeError("boom")
        return "не результат"

    h = Harness(run)
    h.core.items = [{"id": 4, "kind": "search", "params": {}}, {"id": 5, "kind": "search", "params": {}}]
    await h.lookuper.step()
    await h.lookuper.step()
    assert [r["reason"] for r in h.core.results] == ["RuntimeError", "bad_result"]


async def test_rejected_result_is_logged_not_raised():
    async def run(item):
        return LookupResult("ok", payload={})

    h = Harness(run)
    h.core.items = [{"id": 6, "kind": "search", "params": {}}]
    h.core.result_errors = [_http_error(404)]
    await h.lookuper.step()  # 404 — ядро уже не ждёт: без исключения


async def test_core_unreachable_backoff_then_unauthorized_stops():
    async def run(item):
        return LookupResult("ok", payload={})

    h = Harness(run)
    h.core.items = [httpx.ConnectError("нет сокета"), _http_error(401)]
    await asyncio.wait_for(h.lookuper.run(), 1)
    assert h.sleeps == [1.0]
    assert h.unauthorized == 1


# --- каркас по умолчанию против настоящего ядра -----------------------------------------------------------


async def test_base_connector_without_support_answers_unsupported(db):
    """`ConnectorBase.run_lookup` по умолчанию — `error unsupported`; ядро отдаёт сессии 422 с текстом."""
    lookup = TgLookup(answer_timeout_s=5)
    notifier = Notifier()
    app = create_internal_app(
        db,
        clock=lambda: 1_700_000_000.0,
        notifier=notifier,
        admin_key_hash=hash_secret(ADMIN_PREFIX + "x" * 43),
        tg_lookup=lookup,
    )
    account_id = db.write_sync(
        lambda conn: conn.execute("INSERT INTO main.account (source, db) VALUES ('fake', 'main')").lastrowid
    )
    secret = connector_auth.generate_connector_secret()
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.connector_secret (account_id, hash, created_ts) VALUES (?, ?, 0)",
            (account_id, connector_auth.hash_secret(secret)),
        )
    )
    async with CoreClient(transport=ASGITransport(app=app), secret=secret) as client:
        connector = FakeConnector(client, FakeSource())
        assert connector.lookup_enabled is False
        ticket = lookup.submit(account_id, "search", {"query": "abc", "limit": 3}, now_ms=1_700_000_000_000)
        assert await connector._make_lookuper().step() is True  # noqa: SLF001
        try:
            await lookup.wait(ticket)
        except Exception as exc:  # noqa: BLE001
            assert getattr(exc, "status", None) == 422
            assert "не умеет" in exc.text
        else:
            raise AssertionError("ожидался отказ")
