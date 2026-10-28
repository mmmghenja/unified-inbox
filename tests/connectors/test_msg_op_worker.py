"""Ф14-8 (docs/architecture.md и поправка П7): толкатель правок коннектора
`MsgOpWorker` против фейкового ядра, `ConnectorBase.flush_events` (событие с правдой — в ядро раньше отчёта,
зависший толкатель не держит отчёт дольше таймаута) и `FakeConnector` против настоящего внутреннего ядра —
правка и удаление выполнены, события дошли, отчёт отдан; исключение `push` -> `retry`; SIGTERM."""

from __future__ import annotations

import asyncio
import os
import signal

import httpx
import pytest
from httpx import ASGITransport

from hub.connectors import base as base_mod
from hub.connectors import msg_ops as msg_ops_conn
from hub.connectors.base import AuthRequired, ConnectorBase, CoreClient, SendResult
from hub.connectors.fake import FakeConnector, FakeSource, FaultPlan
from hub.connectors.msg_ops import MsgOpResult, MsgOpWorker
from hub.core import connector_auth
from hub.core import msg_ops as msg_ops_mod
from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.feed import Notifier


def _item(message_id=12, lease_gen=7, op="edit"):
    item = {
        "message_id": message_id,
        "gen": 3,
        "lease_gen": lease_gen,
        "op": op,
        "channel": {"ext_id": "c", "kind": "dm", "parent_ext_id": None},
        "message_ext_id": "5",
        "thread_ext_id": None,
        "message_kind": "text",
        "has_files": False,
    }
    if op == "edit":
        item["text"] = "новый"
    return item


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://hub-internal/internal/v1/msg-ops/12/report")
    return httpx.HTTPStatusError(
        str(status), request=request, response=httpx.Response(status, request=request)
    )


class FakeCore:
    """Двойник `CoreClient` толкателя: задания — `items`, отчёты — `reports`, порядок вызовов — `log`."""

    def __init__(self) -> None:
        self.items: list[dict] = []
        self.reports: list[dict] = []
        self.lease_calls: list[int] = []
        self.wait_calls = 0
        self.wait_error: Exception | None = None
        self.report_errors: list[Exception] = []
        self.log: list[str] = []

    async def wait_msg_ops(self, timeout_s: float) -> bool:
        del timeout_s
        self.wait_calls += 1
        await asyncio.sleep(0)
        if self.wait_error is not None:
            raise self.wait_error
        return bool(self.items)

    async def lease_msg_ops(self, *, limit: int = 1) -> list[dict]:
        self.lease_calls.append(limit)
        out, self.items = self.items[:limit], self.items[limit:]
        return out

    async def report_msg_op(
        self, *, message_id, lease_gen, outcome, code=None, detail=None, retry_after_ms=None
    ) -> dict:
        self.log.append("report")
        if self.report_errors:
            raise self.report_errors.pop(0)
        self.reports.append(
            {
                "message_id": message_id,
                "lease_gen": lease_gen,
                "outcome": outcome,
                "code": code,
                "detail": detail,
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


def make(core: FakeCore, push, clock: Clock | None = None, *, is_blocked=lambda: False, flush=None):
    clock = clock or Clock()
    state = {"auth": 0, "unauth": 0, "flush": []}

    async def on_auth():
        state["auth"] += 1

    def on_unauth():
        state["unauth"] += 1

    async def default_flush(timeout_s):
        core.log.append("flush")
        state["flush"].append(timeout_s)
        return True

    worker = MsgOpWorker(
        core,
        push,
        flush=flush or default_flush,
        mono=clock.mono,
        sleep=clock.sleep,
        is_blocked=is_blocked,
        on_auth_required=on_auth,
        on_unauthorized=on_unauth,
    )
    return worker, state


# --- шаг: выполнение и отчёт ---


async def test_done_flushed_then_reported_one_at_a_time():
    core = FakeCore()
    core.items = [_item(12, 7), _item(13, 2)]
    pushed = []

    async def push(item):
        pushed.append(item)
        core.log.append("push")
        return MsgOpResult("done")

    worker, state = make(core, push)
    assert await worker.step() == 1
    assert core.lease_calls == [1]
    assert pushed == [_item(12, 7)]
    assert core.log == ["push", "flush", "report"]  # П7: буфер — до отчёта
    assert state["flush"] == [msg_ops_conn.FLUSH_TIMEOUT_S]
    assert core.reports == [
        {
            "message_id": 12,
            "lease_gen": 7,
            "outcome": "done",
            "code": None,
            "detail": None,
            "retry_after_ms": None,
        }
    ]


async def test_fail_passes_code_and_detail_bad_code_becomes_other():
    core = FakeCore()
    core.items = [_item(1), _item(2)]
    results = [
        MsgOpResult("fail", code="expired", detail="MessageEditTimeExpiredError"),
        MsgOpResult("fail", code="boom", detail="x"),
    ]

    async def push(item):
        return results.pop(0)

    worker, _ = make(core, push)
    await worker.step()
    await worker.step()
    assert [(r["outcome"], r["code"], r["detail"]) for r in core.reports] == [
        ("fail", "expired", "MessageEditTimeExpiredError"),
        ("fail", "other", "x"),
    ]


async def test_push_exception_timeout_and_bad_result_become_retry(monkeypatch):
    monkeypatch.setattr(msg_ops_conn, "PUSH_TIMEOUT_S", 0.05)
    core = FakeCore()
    core.items = [_item(1), _item(2), _item(3)]
    calls = []

    async def push(item):
        calls.append(item["message_id"])
        if item["message_id"] == 1:
            raise ConnectionResetError("сеть")
        if item["message_id"] == 2:
            await asyncio.sleep(5)
        return None

    worker, _ = make(core, push)
    for _ in range(3):
        await worker.step()
    assert [(r["outcome"], r["detail"], r["code"]) for r in core.reports] == [
        ("retry", "ConnectionResetError", None),
        ("retry", "timeout", None),
        ("retry", "bad_result", None),
    ]


async def test_retry_after_pauses_whole_loop_and_goes_to_core_in_ms():
    core = FakeCore()
    core.items = [_item()]
    clock = Clock()

    async def push(item):
        return MsgOpResult("retry", retry_after_s=7, detail="FloodWaitError")

    worker, _ = make(core, push, clock)
    await worker.step()
    assert core.reports[0]["outcome"] == "retry" and core.reports[0]["retry_after_ms"] == 7000
    assert worker.paused_until == clock.t + 7

    waits_before = core.wait_calls
    task = asyncio.ensure_future(worker.run())
    for _ in range(50):
        await asyncio.sleep(0)
        if clock.sleeps:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert clock.sleeps[0] == 7
    assert core.wait_calls - waits_before <= 1


async def test_auth_required_calls_hook_without_report_or_flush():
    core = FakeCore()
    core.items = [_item()]

    async def push(item):
        raise AuthRequired("lost")

    worker, state = make(core, push)
    await worker.step()
    assert state["auth"] == 1
    assert core.reports == [] and core.log == []


async def test_report_rejected_is_logged_and_loop_goes_on():
    core = FakeCore()
    core.items = [_item(1), _item(2)]
    core.report_errors = [_http_error(409)]

    async def push(item):
        return MsgOpResult("done")

    worker, _ = make(core, push)
    await worker.step()
    await worker.step()
    assert [r["message_id"] for r in core.reports] == [2]


async def test_flush_error_does_not_block_report():
    core = FakeCore()
    core.items = [_item()]

    async def push(item):
        return MsgOpResult("done")

    async def bad_flush(timeout_s):
        raise RuntimeError("буфер")

    worker, _ = make(core, push, flush=bad_flush)
    await worker.step()
    assert [r["outcome"] for r in core.reports] == ["done"]


async def test_core_401_stops_loop_unauthorized():
    core = FakeCore()
    core.wait_error = _http_error(401)

    async def push(item):
        raise AssertionError("не должно вызываться")

    worker, state = make(core, push)
    await asyncio.wait_for(worker.run(), 5)
    assert state["unauth"] == 1


async def test_blocked_takes_no_jobs():
    core = FakeCore()
    core.items = [_item()]

    async def push(item):
        raise AssertionError("не должно вызываться")

    worker, _ = make(core, push, is_blocked=lambda: True)
    task = asyncio.ensure_future(worker.run())
    for _ in range(20):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert core.lease_calls == []


async def test_base_push_msg_op_default_is_fail_other():
    connector = FakeConnector(FakeCore(), FakeSource())
    result = await ConnectorBase.push_msg_op(connector, _item())
    assert result == MsgOpResult("fail", code="other", detail="unsupported")
    assert FakeConnector.msg_ops_enabled is True and ConnectorBase.msg_ops_enabled is False


# --- П7: flush_events каркаса ---


class _TruthConnector(ConnectorBase):
    """Коннектор, который, как настоящие, выпускает правду через буфер (`emit`), а не напрямую."""

    msg_ops_enabled = True

    async def sync(self, reason: str = "scheduled") -> None:
        return None

    async def send(self, lease: dict) -> SendResult:
        raise AssertionError

    async def reconcile(self, lease: dict) -> SendResult:
        raise AssertionError

    async def health(self) -> None:
        return None

    async def push_msg_op(self, job: dict) -> MsgOpResult:
        self.emit(
            {"type": "delete", "ext_ids": [job["message_ext_id"]], "channel": {"ext_id": "c", "kind": "dm"}}
        )
        return MsgOpResult("done")


class _OrderCore(FakeCore):
    def __init__(self, *, push_delay: float = 0.0, hang: bool = False) -> None:
        super().__init__()
        self.push_delay = push_delay
        self.hang = hang
        self.pushed: list[dict] = []

    async def push_events(self, events, cursor=None):
        if self.hang:
            await asyncio.Event().wait()
        await asyncio.sleep(self.push_delay)
        self.pushed.extend(events)
        self.log.append("events")
        return {"applied": len(events), "noop": 0, "dead": 0, "max_seq": 0}


async def test_p7_truth_event_reaches_core_before_report():
    core = _OrderCore(push_delay=0.05)
    core.items = [_item(op="delete")]
    connector = _TruthConnector(core)
    pusher = asyncio.ensure_future(connector._pusher_loop())  # noqa: SLF001 — толкатель буфера каркаса
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        assert await connector._make_msg_op_worker().step() == 1  # noqa: SLF001
    finally:
        pusher.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pusher
    assert core.log == ["events", "report"]
    # отчёт ушёл сразу, как событие ушло, а не по таймауту ожидания (10 с)
    assert loop.time() - started < 2
    assert core.pushed[0]["type"] == "delete"


async def test_p7_hanging_pusher_does_not_hold_report_longer_than_timeout(monkeypatch):
    monkeypatch.setattr(msg_ops_conn, "FLUSH_TIMEOUT_S", 0.2)
    core = _OrderCore(hang=True)
    core.items = [_item(op="delete")]
    connector = _TruthConnector(core)
    pusher = asyncio.ensure_future(connector._pusher_loop())  # noqa: SLF001
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        await asyncio.wait_for(connector._make_msg_op_worker().step(), 5)  # noqa: SLF001
    finally:
        pusher.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pusher
    elapsed = loop.time() - started
    assert core.log == ["report"] and 0.15 <= elapsed < 2


async def test_flush_events_counts_dropped_as_gone_and_ignores_later_events(monkeypatch):
    monkeypatch.setattr(base_mod, "_EVENT_BUFFER_CAP", 2)
    connector = _TruthConnector(FakeCore())
    assert await connector.flush_events(0.01) is True  # пусто — сразу
    connector.emit({"type": "x"})
    connector.emit({"type": "y"})
    assert await connector.flush_events(0.05) is False  # толкатель не работает
    connector.emit({"type": "z"})  # переполнение: буфер выброшен — «ушло»
    assert await connector.flush_events(0.05) is True

    # события, положенные ПОСЛЕ вызова, ожидание не держат
    connector.emit({"type": "a"})
    waiter = asyncio.ensure_future(connector.flush_events(1.0))
    await asyncio.sleep(0)
    connector._event_buffer.clear()  # noqa: SLF001 — как толкатель: «a» ушло
    connector._mark_gone(1)  # noqa: SLF001
    connector.emit({"type": "b"})
    assert await asyncio.wait_for(waiter, 2) is True


# --- E12: FakeConnector против настоящего ядра ---


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
    acc = db.write_sync(
        lambda conn: conn.execute("INSERT INTO main.account (source, db) VALUES ('fake', 'main')").lastrowid
    )
    secret = connector_auth.generate_connector_secret()
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.connector_secret (account_id, hash, created_ts) VALUES (?, ?, 0)",
            (acc, connector_auth.hash_secret(secret)),
        )
    )
    client = CoreClient(secret=secret, transport=ASGITransport(app=app))
    return {"client": client, "db": db, "clock": clock}


async def _own_message(env, source: FakeSource, text: str) -> tuple[int, str]:
    """Своё сообщение у «источника» и в ядре (доставка + `sync`)."""
    ext_id, _ = source.deliver(channel_ext_id="dm-1", text=text, send_token=None, ts=1000)
    await FakeConnector(env["client"], source).sync()
    with env["db"].reader() as conn:
        mid = conn.execute("SELECT id FROM main.message WHERE ext_id = ?", (ext_id,)).fetchone()[0]
    return mid, ext_id


def _message(db, mid):
    with db.reader() as conn:
        return conn.execute(
            "SELECT text, deleted, edited_ts FROM main.message WHERE id = ?", (mid,)
        ).fetchone()


def _op_row(db, mid):
    with db.reader() as conn:
        return conn.execute(
            "SELECT state, attempts, lease_until_ms, error_code FROM main.msg_op WHERE message_id = ?", (mid,)
        ).fetchone()


def _now_ms(env) -> int:
    return int(env["clock"]() * 1000)


async def test_e12_edit_and_delete_done_events_reach_core(core_env):
    db = core_env["db"]
    source = FakeSource()
    edit_mid, _ = await _own_message(core_env, source, "было")
    del_mid, del_ext = await _own_message(core_env, source, "удалить")
    now = _now_ms(core_env)
    db.write_sync(lambda conn: msg_ops_mod.enqueue_edit(conn, edit_mid, "стало", now))
    db.write_sync(lambda conn: msg_ops_mod.enqueue_delete(conn, [del_mid], now))

    connector = FakeConnector(core_env["client"], source)
    assert await connector.step_msg_ops() == 1
    assert await connector.step_msg_ops() == 1
    assert [j["op"] for j in source.msg_op_jobs] == ["edit", "delete"]
    text, deleted, edited_ts = _message(db, edit_mid)
    assert text == "стало" and deleted == 0 and edited_ts is not None  # правду записало событие `edit`
    assert _message(db, del_mid)[1] == 1  # событие `delete`
    assert _op_row(db, edit_mid) is None and _op_row(db, del_mid) is None  # отчёты `done` дошли
    assert all(d.ext_id != del_ext for d in source.deliveries)
    await core_env["client"].aclose()


async def test_e12_stand_fail_prefix_gives_failed_expired(core_env):
    db = core_env["db"]
    source = FakeSource()
    mid, _ = await _own_message(core_env, source, "было")
    now = _now_ms(core_env)
    db.write_sync(lambda conn: msg_ops_mod.enqueue_edit(conn, mid, "!fail хочу ошибку", now))
    assert await FakeConnector(core_env["client"], source).step_msg_ops() == 1
    state, _attempts, lease_until, code = _op_row(db, mid)
    assert (state, lease_until, code) == ("failed", None, "expired")
    assert _message(db, mid)[0] == "было"
    await core_env["client"].aclose()


async def test_e12_push_exception_reported_as_retry(core_env):
    db = core_env["db"]
    source = FakeSource()
    mid, _ = await _own_message(core_env, source, "было")
    now = _now_ms(core_env)
    db.write_sync(lambda conn: msg_ops_mod.enqueue_edit(conn, mid, "стало", now))
    connector = FakeConnector(core_env["client"], source, plan=FaultPlan().queue_msg_ops("error"))
    assert await connector.step_msg_ops() == 1
    state, attempts, lease_until, _code = _op_row(db, mid)
    assert (state, attempts, lease_until) == ("queued", 1, None)
    assert source.msg_op_jobs == [] and _message(db, mid)[0] == "было"
    await core_env["client"].aclose()


async def _until(predicate, limit_s=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit_s
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("условие не наступило")
        await asyncio.sleep(0.02)


async def test_e12_run_executes_and_sigterm_stops(core_env):
    db = core_env["db"]
    source = FakeSource()
    mid, _ = await _own_message(core_env, source, "было")
    now = _now_ms(core_env)
    db.write_sync(lambda conn: msg_ops_mod.enqueue_delete(conn, [mid], now))
    connector = FakeConnector(core_env["client"], source)
    run_task = asyncio.ensure_future(connector.run(source="fake", account_id=1))

    await _until(lambda: len(source.msg_op_jobs) == 1)
    await _until(lambda: _op_row(db, mid) is None)
    assert _message(db, mid)[1] == 1

    os.kill(os.getpid(), signal.SIGTERM)  # обработчик поставил `run()`
    code = await asyncio.wait_for(run_task, 10)
    assert code == 0
