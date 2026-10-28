"""Ф17-2 (docs/architecture.md: исполнитель заявок на выход — обобщённый
`Joiner` со своими вызовами ядра (`wait_leaves`/`lease_leaves`/`report_leave`) и именем в журнале; тело
отчёта `done` — `result` без предпросмотра; `after` у выхода нет; сквозь настоящее внутреннее ядро
(`CoreClient` поверх ASGI) — `/leave/report` принимает тело и беседа уходит в архив."""

from __future__ import annotations

import asyncio
import logging

from httpx import ASGITransport

from hub.connectors.base import CoreClient
from hub.connectors.fake import FakeConnector
from hub.connectors.join import JoinResult
from hub.core import connector_auth, security
from hub.core import leave as leave_mod
from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.feed import Notifier


def _item(leave_id=5, lease_gen=2):
    return {
        "id": leave_id,
        "lease_gen": lease_gen,
        "step": "leave",
        "ext_id": "-1001",
        "chat_kind": "channel",
    }


class FakeCore:
    """Только вызовы выхода: вызовы вступления упали бы (`AttributeError`)."""

    def __init__(self) -> None:
        self.items: list[dict] = []
        self.reports: list[dict] = []
        self.waits = 0

    async def wait_leaves(self, timeout_s: float) -> bool:
        del timeout_s
        self.waits += 1
        await asyncio.sleep(0)
        return bool(self.items)

    async def lease_leaves(self, *, limit: int = 1) -> list[dict]:
        out, self.items = self.items[:limit], self.items[limit:]
        return out

    async def report_leave(self, body: dict) -> dict:
        self.reports.append(body)
        return {"ok": True}


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def mono(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += s
        await asyncio.sleep(0)


def _connector(core) -> FakeConnector:
    connector = FakeConnector.__new__(FakeConnector)
    connector._client = core  # noqa: SLF001
    connector._mono = _Clock().mono  # noqa: SLF001
    connector._sleep = _Clock().sleep  # noqa: SLF001
    return connector


async def test_leaver_uses_leave_calls_and_result_body(caplog):
    core = FakeCore()
    core.items = [_item()]
    connector = _connector(core)

    async def push(item):
        assert item["step"] == "leave"
        return JoinResult("done", result="not_member")

    connector.push_leave = push
    leaver = connector._make_leaver()  # noqa: SLF001
    with caplog.at_level(logging.INFO, logger="hub.connectors.join"):
        assert await leaver.step() == 1
    assert core.reports == [{"id": 5, "lease_gen": 2, "outcome": "done", "result": "not_member"}]
    assert "leave: шаг=leave id=5 исход not_member" in [r.getMessage() for r in caplog.records]
    assert await leaver._wait(0) is False and core.waits == 1  # noqa: SLF001


async def test_leaver_drop_and_flood_wait():
    core = FakeCore()
    core.items = [_item(1), _item(2)]
    connector = _connector(core)
    results = [
        JoinResult("drop", reason="creator"),
        JoinResult("retry", retry_after_s=7.5, reason="FloodWait"),
    ]

    async def push(item):
        return results.pop(0)

    connector.push_leave = push
    leaver = connector._make_leaver()  # noqa: SLF001
    await leaver.step()
    await leaver.step()
    assert core.reports == [
        {"id": 1, "lease_gen": 2, "outcome": "drop", "reason": "creator"},
        {"id": 2, "lease_gen": 2, "outcome": "retry", "retry_after_ms": 7500, "reason": "FloodWait"},
    ]
    assert leaver.paused_until is not None


async def test_base_push_leave_default_is_drop_unsupported():
    connector = FakeConnector.__new__(FakeConnector)
    assert await connector.push_leave(_item()) == JoinResult("drop", reason="unsupported")
    assert FakeConnector.leave_enabled is False


# --- сквозь настоящее внутреннее ядро ---------------------------------------------------------------------


async def test_through_real_core_leave(db):
    clock_value = 1_700_000_000.0
    notifier = Notifier()
    db.on_commit(notifier.notify)
    app = create_internal_app(
        db, clock=lambda: clock_value, notifier=notifier, admin_key_hash=hash_secret(ADMIN_PREFIX + "x" * 43)
    )
    acc = db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.account (source, db) VALUES ('telegram', 'main')").lastrowid
        )
    )
    channel_id = db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.channel (account_id, ext_id, kind, first_seen_ts, seq) "
                "VALUES (?, '-1001', 'channel', 0, 0)",
                (acc,),
            ).lastrowid
        )
    )
    secret = connector_auth.generate_connector_secret()
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.connector_secret (account_id, hash, created_ts) VALUES (?, ?, 0)",
            (acc, connector_auth.hash_secret(secret)),
        )
    )
    token = db.write_sync(
        lambda conn: security.create_api_token(
            conn, name="ui-test", kind="interactive", scopes=["read"], project_ids=None, expires_ts=None
        )
    )
    ui = security.Principal(token_id=token.token_id, kind="ui", scopes=frozenset({"read"}), project_ids=None)
    now_ms = int(clock_value * 1000)
    req = db.write_sync(
        lambda conn: leave_mod.create_ui(conn, principal=ui, channel_id=channel_id, now_ms=now_ms)
    )
    client = CoreClient(secret=secret, transport=ASGITransport(app=app))
    connector = _connector(client)

    async def push(item):
        assert (item["ext_id"], item["chat_kind"]) == ("-1001", "channel")
        return JoinResult("done", result="left")

    connector.push_leave = push
    leaver = connector._make_leaver()  # noqa: SLF001
    assert await client.wait_leaves(0) is True
    assert await leaver.step() == 1
    with db.reader() as conn:
        done = leave_mod.get(conn, req.id)
        archived = conn.execute("SELECT archived FROM main.channel WHERE id = ?", (channel_id,)).fetchone()[0]
    assert (done.status, done.result, archived) == ("left", "left", 1)
    await client.aclose()
