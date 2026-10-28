"""Ф10-3 (docs/architecture.md и поправка П5): толкатель реакций коннектора
`Reactor` против фейкового ядра и `FakeConnector.run()` против настоящего внутреннего ядра — задание выполнено
и отчитано, событие `reaction` дошло до ядра, исключение `push` -> `retry`, `AuthRequired` — хук без отчёта,
пауза всего цикла после `retry_after_s`, SIGTERM останавливает цикл."""

from __future__ import annotations

import asyncio
import json
import os
import signal

import httpx
import pytest
from httpx import ASGITransport

from hub.connectors import reactions as reactions_conn
from hub.connectors.base import AuthRequired, CoreClient
from hub.connectors.fake import FakeConnector, FakeSource, FaultPlan
from hub.connectors.reactions import ReactionResult, Reactor
from hub.core import connector_auth
from hub.core import reactions as reactions_mod
from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.db import next_seq
from hub.core.feed import Notifier


def _item(message_id=12, lease_gen=7, gen=3):
    return {
        "message_id": message_id,
        "channel_id": 4,
        "gen": gen,
        "lease_gen": lease_gen,
        "channel": {"ext_id": "c", "kind": "dm", "parent_ext_id": None},
        "message_ext_id": "5",
        "ops": [["👍", True]],
    }


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://hub-internal/internal/v1/reactions/12/report")
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

    async def wait_reactions(self, timeout_s: float) -> bool:
        del timeout_s
        self.wait_calls += 1
        await asyncio.sleep(0)
        if self.wait_error is not None:
            raise self.wait_error
        return bool(self.items)

    async def lease_reactions(self, *, limit: int = 1) -> list[dict]:
        self.lease_calls.append(limit)
        out, self.items = self.items[:limit], self.items[limit:]
        return out

    async def report_reaction(self, *, message_id, lease_gen, outcome, retry_after_ms=None) -> dict:
        if self.report_errors:
            raise self.report_errors.pop(0)
        self.reports.append(
            {
                "message_id": message_id,
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

    acker = Reactor(
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
        return ReactionResult("done")

    acker, _ = make(core, push)
    assert await acker.step() == 1
    assert core.lease_calls == [1]  # по одному, как у прочитанности
    assert pushed == [_item(12, 7)]
    assert core.reports == [{"message_id": 12, "lease_gen": 7, "outcome": "done", "retry_after_ms": None}]
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
    results = [None, ReactionResult("drop", reason="REACTION_INVALID")]

    async def push(item):
        return results.pop(0)

    acker, _ = make(core, push)
    await acker.step()
    await acker.step()
    assert [r["outcome"] for r in core.reports] == ["retry", "drop"]


async def test_push_timeout_becomes_retry(monkeypatch):
    monkeypatch.setattr(reactions_conn, "PUSH_TIMEOUT_S", 0.05)
    core = FakeCore()
    core.items = [_item()]

    async def push(item):
        await asyncio.sleep(5)
        return ReactionResult("done")

    acker, _ = make(core, push)
    await acker.step()
    assert [r["outcome"] for r in core.reports] == ["retry"]


async def test_retry_after_pauses_whole_loop_and_goes_to_core_in_ms():
    core = FakeCore()
    core.items = [_item()]
    clock = Clock()

    async def push(item):
        return ReactionResult("retry", retry_after_s=7, reason="FloodWaitError")

    acker, _ = make(core, push, clock)
    await acker.step()
    assert core.reports[0]["outcome"] == "retry" and core.reports[0]["retry_after_ms"] == 7000
    assert acker.paused_until == clock.t + 7

    # Цикл сначала выжидает паузу и только потом снова спрашивает ядро — для ВСЕХ сообщений.
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
        return ReactionResult("done")

    acker, _ = make(core, push)
    await acker.step()
    await acker.step()
    assert [r["message_id"] for r in core.reports] == [2]


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


async def test_base_push_reaction_default_is_drop_unsupported():
    class _NoReactions(FakeConnector):
        reactions_enabled = False

    connector = _NoReactions(FakeCore(), FakeSource())
    # умолчание каркаса — метод `ConnectorBase`, не `FakeConnector`
    from hub.connectors.base import ConnectorBase

    result = await ConnectorBase.push_reaction(connector, _item())
    assert result == ReactionResult("drop", reason="unsupported")
    assert FakeConnector.reactions_enabled is True and ConnectorBase.reactions_enabled is False


# --- E8: FakeConnector против настоящего ядра ---


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
            "kind, text, reactions_json) VALUES (?, ?, ?, ?, '42', 42, 1000, 0, 'text', 't', ?)",
            (seq, seq, acc, ch, json.dumps([{"emoji": "❤️", "count": 1, "me": False}])),
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
    db.write_sync(lambda conn: reactions_mod.enqueue(conn, mid, "👍", True, now_ms))
    db.write_sync(lambda conn: reactions_mod.enqueue(conn, mid, "❤️", True, now_ms))
    clock.value += 1.5  # задержка 1.5 с прошла
    client = CoreClient(secret=secret, transport=ASGITransport(app=app))
    return {"client": client, "message_id": mid, "db": db}


def _reaction_row(db, message_id):
    with db.reader() as conn:
        return conn.execute(
            "SELECT attempts, lease_until_ms FROM main.reaction_ack WHERE message_id = ?", (message_id,)
        ).fetchone()


def _reactions_json(db, message_id):
    with db.reader() as conn:
        row = conn.execute("SELECT reactions_json FROM main.message WHERE id = ?", (message_id,)).fetchone()
    return json.loads(row[0])


async def _until(predicate, limit_s=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit_s
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("условие не наступило")
        await asyncio.sleep(0.02)


async def test_e8_step_applies_ops_reports_done_and_event_reaches_core(core_env):
    source = FakeSource()
    # «источник» знает реакции сообщения — как их знает ядро (чужая ❤️)
    source.reaction("dm-1", ext_id="42", reactions=[{"emoji": "❤️", "count": 1, "me": False}])
    connector = FakeConnector(core_env["client"], source)
    assert await connector.step_reactions() == 1
    (job,) = source.reaction_jobs
    assert job["message_id"] == core_env["message_id"]
    assert job["message_ext_id"] == "42" and job["channel"]["ext_id"] == "dm-1"
    assert job["ops"] == [["👍", True], ["❤️", True]]
    assert _reaction_row(core_env["db"], core_env["message_id"]) is None  # отчёт `done` дошёл
    # правду о реакциях записало событие `reaction`, а не ядро само
    assert _reactions_json(core_env["db"], core_env["message_id"]) == [
        {"emoji": "❤️", "count": 2, "me": True},
        {"emoji": "👍", "count": 1, "me": True},
    ]
    await core_env["client"].aclose()


async def test_e8_run_pushes_reports_and_sigterm_stops(core_env):
    source = FakeSource()
    connector = FakeConnector(core_env["client"], source)
    run_task = asyncio.ensure_future(connector.run(source="fake", account_id=1))

    await _until(lambda: len(source.reaction_jobs) == 1)
    await _until(lambda: _reaction_row(core_env["db"], core_env["message_id"]) is None)

    os.kill(os.getpid(), signal.SIGTERM)  # обработчик поставил `run()`
    code = await asyncio.wait_for(run_task, 10)
    assert code == 0


async def test_e8_push_exception_reported_as_retry(core_env):
    source = FakeSource()
    connector = FakeConnector(core_env["client"], source, plan=FaultPlan().queue_reactions("error"))
    assert await connector.step_reactions() == 1
    attempts, lease_until = _reaction_row(core_env["db"], core_env["message_id"])
    assert attempts == 1 and lease_until is None
    assert source.reaction_jobs == []
    await core_env["client"].aclose()


async def test_e8_auth_required_no_report_lease_stays(core_env):
    source = FakeSource()
    connector = FakeConnector(core_env["client"], source, plan=FaultPlan().queue_reactions("auth_required"))

    calls = []

    async def _on_auth_required():
        calls.append(1)

    connector._on_auth_required = _on_auth_required  # noqa: SLF001 — хук каркаса, без статуса в ядре
    assert await connector.step_reactions() == 1
    assert calls == [1]
    attempts, lease_until = _reaction_row(core_env["db"], core_env["message_id"])
    assert attempts == 0 and lease_until is not None  # отчёта нет — лиз истечёт сам
    await core_env["client"].aclose()


async def test_e8_disabled_connector_never_asks_core():
    """`reactions_enabled = False` — `run()` толкатель реакций не запускает вовсе."""

    class _Client(FakeCore):
        async def push_events(self, events, cursor=None):
            return {"applied": 0, "noop": 0, "dead": 0, "max_seq": 0}

        async def set_status(self, status, *, last_error=None):
            return {"ok": True}

        async def wait_outbox(self, timeout_s):
            await asyncio.sleep(0.01)
            return False

        async def wait_read_ack(self, timeout_s):
            await asyncio.sleep(0.01)
            return False

        async def aclose(self):
            return None

    class _NoReactions(FakeConnector):
        reactions_enabled = False

    core = _Client()
    connector = _NoReactions(core, FakeSource())
    run_task = asyncio.ensure_future(connector.run(source="fake", account_id=1))
    await asyncio.sleep(0.1)
    connector._trigger_exit(0)  # noqa: SLF001 — то же, что обработчик SIGTERM
    await asyncio.wait_for(run_task, 5)
    assert core.wait_calls == 0 and core.lease_calls == []
