"""Ф8-7 (docs/architecture.md и поправка П4): толкатель прочитанности
коннектора `ReadAcker` против фейкового ядра и `FakeConnector.run()` против настоящего внутреннего ядра —
задание выполнено и отчитано, исключение `push` -> `retry`, пауза всего цикла после `retry_after_s`,
SIGTERM останавливает цикл."""

from __future__ import annotations

import asyncio
import os
import signal

import httpx
import pytest
from httpx import ASGITransport

from hub.connectors import read_ack as read_ack_conn
from hub.connectors.base import AuthRequired, CoreClient
from hub.connectors.fake import FakeConnector, FakeSource, FaultPlan
from hub.connectors.read_ack import ReadAcker, ReadAckResult
from hub.core import connector_auth
from hub.core import read_ack as read_ack_mod
from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.db import next_seq
from hub.core.feed import Notifier


def _item(channel_id=12, lease_gen=7, gen=3):
    return {
        "channel_id": channel_id,
        "gen": gen,
        "lease_gen": lease_gen,
        "channel": {"ext_id": "c", "kind": "dm", "parent_ext_id": None},
        "message": {"ext_id": "5", "ts": 1000, "ext_sort": 5, "thread_ext_id": None, "is_me": False},
    }


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://hub-internal/internal/v1/read-ack/report")
    return httpx.HTTPStatusError(
        str(status), request=request, response=httpx.Response(status, request=request)
    )


class FakeCore:
    """Двойник `CoreClient` для толкателя: задания из `items`, отчёты — в `reports`."""

    def __init__(self) -> None:
        self.items: list[dict] = []
        self.reports: list[dict] = []
        self.lease_calls: list[int] = []
        self.wait_calls = 0
        self.wait_error: Exception | None = None
        self.report_errors: list[Exception] = []

    async def wait_read_ack(self, timeout_s: float) -> bool:
        del timeout_s
        self.wait_calls += 1
        await asyncio.sleep(0)
        if self.wait_error is not None:
            raise self.wait_error
        return bool(self.items)

    async def lease_read_ack(self, *, limit: int = 1) -> list[dict]:
        self.lease_calls.append(limit)
        out, self.items = self.items[:limit], self.items[limit:]
        return out

    async def report_read_ack(self, *, channel_id, lease_gen, outcome, retry_after_ms=None) -> dict:
        if self.report_errors:
            raise self.report_errors.pop(0)
        self.reports.append(
            {
                "channel_id": channel_id,
                "lease_gen": lease_gen,
                "outcome": outcome,
                "retry_after_ms": retry_after_ms,
            }
        )
        return {"ok": True}


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def mono(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s
        await asyncio.sleep(0)


def make(core: FakeCore, push, clock: Clock | None = None, *, is_blocked=lambda: False):
    clock = clock or Clock()
    state = {"auth": 0, "unauth": 0}

    async def on_auth():
        state["auth"] += 1

    def on_unauth():
        state["unauth"] += 1

    acker = ReadAcker(
        core,
        push,
        mono=clock.mono,
        sleep=clock.sleep,
        is_blocked=is_blocked,
        on_auth_required=on_auth,
        on_unauthorized=on_unauth,
    )
    return acker, state


# --- шаг: выполнение и отчёт ---


async def test_done_is_reported_with_lease_gen_one_at_a_time():
    core = FakeCore()
    core.items = [_item(12, 7), _item(13, 2)]
    pushed = []

    async def push(item):
        pushed.append(item)
        return ReadAckResult("done")

    acker, _ = make(core, push)
    assert await acker.step() == 1
    assert core.lease_calls == [1]  # П7: по одному
    assert pushed == [_item(12, 7)]
    assert core.reports == [{"channel_id": 12, "lease_gen": 7, "outcome": "done", "retry_after_ms": None}]
    assert acker.paused_until is None


async def test_push_exception_becomes_retry():
    core = FakeCore()
    core.items = [_item()]

    async def push(item):
        raise ConnectionResetError("сеть")

    acker, _ = make(core, push)
    await acker.step()
    assert [r["outcome"] for r in core.reports] == ["retry"]
    assert core.reports[0]["retry_after_ms"] is None


async def test_bad_result_becomes_retry_and_drop_passes_through():
    core = FakeCore()
    core.items = [_item(1), _item(2)]
    results = [None, ReadAckResult("drop", reason="channel_not_found")]

    async def push(item):
        return results.pop(0)

    acker, _ = make(core, push)
    await acker.step()
    await acker.step()
    assert [r["outcome"] for r in core.reports] == ["retry", "drop"]


async def test_push_timeout_becomes_retry(monkeypatch):
    monkeypatch.setattr(read_ack_conn, "PUSH_TIMEOUT_S", 0.05)
    core = FakeCore()
    core.items = [_item()]

    async def push(item):
        await asyncio.sleep(5)
        return ReadAckResult("done")

    acker, _ = make(core, push)
    await acker.step()
    assert [r["outcome"] for r in core.reports] == ["retry"]


async def test_retry_after_pauses_whole_loop_and_goes_to_core_in_ms():
    core = FakeCore()
    core.items = [_item()]
    clock = Clock()

    async def push(item):
        return ReadAckResult("retry", retry_after_s=7, reason="FloodWaitError")

    acker, _ = make(core, push, clock)
    await acker.step()
    assert core.reports[0]["outcome"] == "retry" and core.reports[0]["retry_after_ms"] == 7000
    assert acker.paused_until == clock.t + 7

    # П4: цикл сначала выжидает паузу и только потом снова спрашивает ядро — для ВСЕХ бесед.
    waits_before = core.wait_calls
    task = asyncio.ensure_future(acker.run())
    for _ in range(50):
        await asyncio.sleep(0)
        if clock.sleeps:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert clock.sleeps[0] == 7
    assert core.wait_calls - waits_before <= 1  # до конца паузы ядро не спрашивали (после сна — можно)


async def test_auth_required_calls_hook_without_report():
    core = FakeCore()
    core.items = [_item()]

    async def push(item):
        raise AuthRequired("lost")

    acker, state = make(core, push)
    await acker.step()
    assert state["auth"] == 1
    assert core.reports == []


async def test_report_rejected_is_logged_and_loop_goes_on():
    core = FakeCore()
    core.items = [_item(1), _item(2)]
    core.report_errors = [_http_error(409)]

    async def push(item):
        return ReadAckResult("done")

    acker, _ = make(core, push)
    await acker.step()
    await acker.step()
    assert [r["channel_id"] for r in core.reports] == [2]


async def test_core_401_stops_loop_unauthorized():
    core = FakeCore()
    core.wait_error = _http_error(401)

    async def push(item):
        raise AssertionError("не должно вызываться")

    acker, state = make(core, push)
    await asyncio.wait_for(acker.run(), 5)
    assert state["unauth"] == 1


async def test_blocked_takes_no_jobs():
    core = FakeCore()
    core.items = [_item()]

    async def push(item):
        raise AssertionError("не должно вызываться")

    acker, _ = make(core, push, is_blocked=lambda: True)
    task = asyncio.ensure_future(acker.run())
    for _ in range(20):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert core.lease_calls == []


# --- каркас: ConnectorBase ---


async def test_base_push_read_default_is_drop_unsupported():
    class _NoReadAck(FakeConnector):
        read_ack_enabled = False

    connector = _NoReadAck(FakeCore(), FakeSource())
    # умолчание каркаса — метод `ConnectorBase`, не `FakeConnector`
    from hub.connectors.base import ConnectorBase

    result = await ConnectorBase.push_read(connector, _item())
    assert result == ReadAckResult("drop", reason="unsupported")
    assert FakeConnector.read_ack_enabled is True and ConnectorBase.read_ack_enabled is False


# --- E14: FakeConnector.run() против настоящего ядра ---


class _Clock:
    def __init__(self) -> None:
        self.value = 1_700_000_000.0

    def __call__(self) -> float:
        return self.value


@pytest.fixture
def core_env(db):
    clock = _Clock()
    notifier = Notifier()
    db.on_commit(notifier.notify)
    app = create_internal_app(
        db, clock=clock, notifier=notifier, admin_key_hash=hash_secret(ADMIN_PREFIX + "x" * 43)
    )

    def _fn(conn):
        acc = conn.execute("INSERT INTO main.account (source, db) VALUES ('fake', 'main')").lastrowid
        ch = conn.execute(
            "INSERT INTO main.channel (account_id, ext_id, kind, first_seen_ts) VALUES (?, 'dm-1', 'dm', 0)",
            (acc,),
        ).lastrowid
        seq = next_seq(conn)
        mid = conn.execute(
            "INSERT INTO main.message (seq, first_seq, account_id, channel_id, ext_id, ext_sort, ts, is_me, "
            "kind, text) VALUES (?, ?, ?, ?, '42', 42, 1000, 0, 'text', 't')",
            (seq, seq, acc, ch),
        ).lastrowid
        return acc, ch, mid

    acc, ch, mid = db.write_sync(_fn)
    secret = connector_auth.generate_connector_secret()
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.connector_secret (account_id, hash, created_ts) VALUES (?, ?, 0)",
            (acc, connector_auth.hash_secret(secret)),
        )
    )
    now_ms = int(clock() * 1000)
    db.write_sync(lambda conn: read_ack_mod.enqueue(conn, ch, mid, now_ms))
    clock.value += 5  # задержка 5 с прошла
    client = CoreClient(secret=secret, transport=ASGITransport(app=app))
    return {"client": client, "channel_id": ch, "db": db}


def _read_ack_row(db, channel_id):
    with db.reader() as conn:
        return conn.execute(
            "SELECT attempts, lease_until_ms FROM main.read_ack WHERE channel_id = ?", (channel_id,)
        ).fetchone()


async def _until(predicate, limit_s=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit_s
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("условие не наступило")
        await asyncio.sleep(0.02)


async def test_e14_run_pushes_reports_and_sigterm_stops(core_env):
    source = FakeSource()
    connector = FakeConnector(core_env["client"], source)
    run_task = asyncio.ensure_future(connector.run(source="fake", account_id=1))

    await _until(lambda: len(source.read_acks) == 1)
    (item,) = source.read_acks
    assert item["channel_id"] == core_env["channel_id"]
    assert item["message"]["ext_id"] == "42" and item["channel"]["ext_id"] == "dm-1"
    await _until(lambda: _read_ack_row(core_env["db"], core_env["channel_id"]) is None)  # отчёт `done` дошёл

    os.kill(os.getpid(), signal.SIGTERM)  # обработчик поставил `run()`
    code = await asyncio.wait_for(run_task, 10)
    assert code == 0


async def test_e14_push_exception_reported_as_retry(core_env):
    source = FakeSource()
    connector = FakeConnector(core_env["client"], source, plan=FaultPlan().queue_read_ack("error"))
    assert await connector.step_read_ack() == 1
    attempts, lease_until = _read_ack_row(core_env["db"], core_env["channel_id"])
    assert attempts == 1 and lease_until is None
    assert source.read_acks == []
    await core_env["client"].aclose()


async def test_e14_disabled_connector_never_asks_core():
    """`read_ack_enabled = False` — `run()` толкатель не запускает вовсе."""

    class _Client(FakeCore):
        async def push_events(self, events, cursor=None):
            return {"applied": 0, "noop": 0, "dead": 0, "max_seq": 0}

        async def set_status(self, status, *, last_error=None):
            return {"ok": True}

        async def wait_outbox(self, timeout_s):
            await asyncio.sleep(0.01)
            return False

        async def aclose(self):
            return None

    class _NoReadAck(FakeConnector):
        read_ack_enabled = False

    core = _Client()
    connector = _NoReadAck(core, FakeSource())
    run_task = asyncio.ensure_future(connector.run(source="fake", account_id=1))
    await asyncio.sleep(0.1)
    connector._trigger_exit(0)  # noqa: SLF001 — то же, что обработчик SIGTERM
    await asyncio.wait_for(run_task, 5)
    assert core.wait_calls == 0 and core.lease_calls == []
