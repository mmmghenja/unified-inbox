"""Повтор упавшей сверки каркаса (`ConnectorBase._schedule_sync_redo`): 16.10 после включения стартовая
сверка Telegram упала на `httpx.ReadTimeout` к ядру, повтора не было до плановой через 600±180 с. Теперь
временная ошибка — повтор той же причины через 30, 60, 120, 240, 300 с, не больше 5 подряд; успех
сбрасывает счётчик; `AuthRequired`, прочие ошибки и сон повтора не ставят.

Часы виртуальные: инжектированный `sleep` записывает паузы; плановая пауза планировщика (420–780 с) не
кончается никогда — сверки в тестах только заказанные или повторные."""

import asyncio
import contextlib
import logging

import httpx

from hub.connectors.base import AuthRequired
from hub.connectors.fake import FakeConnector, FakeSource


class _Client:
    def __init__(self) -> None:
        self.statuses: list[str] = []

    async def set_status(self, status: str, *, last_error: str | None = None) -> dict:
        del last_error
        self.statuses.append(status)
        return {"ok": True}


def _read_timeout() -> httpx.ReadTimeout:
    return httpx.ReadTimeout("ядро не ответило", request=httpx.Request("GET", "http://hub-internal/x"))


class _Clock:
    """Виртуальный `sleep`: паузы повтора (≤ 300 с) и опрос (5 с) записываются и проходят сразу; плановая
    пауза планировщика висит. `gate` — паузу повтора держит, пока тест не отпустит."""

    def __init__(self) -> None:
        self.redo: list[float] = []
        self.gate: asyncio.Event | None = None
        self.on_redo = None

    async def sleep(self, seconds: float) -> None:
        if seconds >= 400:  # noqa: PLR2004 — плановая пауза 600±180
            await asyncio.Event().wait()
        if seconds > 5:  # noqa: PLR2004 — 5 с — опрос созревшего повтора
            self.redo.append(seconds)
            if self.on_redo is not None:
                self.on_redo()
            if self.gate is not None:
                await self.gate.wait()
        await asyncio.sleep(0)


def _connector(clock: _Clock, script: list) -> tuple[FakeConnector, list[str]]:
    """`script` — по одному исходу на вызов `sync()`: исключение — бросить, `None` — успех; кончился —
    успех."""
    connector = FakeConnector(_Client(), FakeSource(), sleep=clock.sleep)
    calls: list[str] = []

    async def _sync(reason: str = "scheduled") -> None:
        calls.append(reason)
        outcome = script.pop(0) if script else None
        if callable(outcome):
            outcome = outcome()
        if outcome is not None:
            raise outcome

    connector.sync = _sync  # type: ignore[method-assign]
    return connector, calls


async def _spin(n: int = 300) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


@contextlib.asynccontextmanager
async def _scheduler(connector: FakeConnector):
    task = asyncio.ensure_future(connector._sync_scheduler_loop())  # noqa: SLF001
    try:
        yield task
    finally:
        connector._stopping = True  # noqa: SLF001
        connector._sync_wake.set()  # noqa: SLF001
        redo = connector._sync_redo_task  # noqa: SLF001
        for t in (task, redo):
            if t is not None:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t


async def test_read_timeout_on_start_then_success_repeats_start_once_after_30s(caplog) -> None:
    clock = _Clock()
    connector, calls = _connector(clock, [_read_timeout()])
    caplog.set_level(logging.INFO, logger="hub.connectors.base")
    async with _scheduler(connector):
        await _spin()
        assert calls == ["start", "start"]
        assert clock.redo == [30.0]
        await _spin()
        assert calls == ["start", "start"]  # третьей нет — дальше только плановая
    assert connector._sync_redo_count == 0  # noqa: SLF001
    assert "sync(start) повтор через 30 с (попытка 1)" in caplog.text


async def test_persistent_failures_grow_pauses_and_stop_after_five(caplog) -> None:
    clock = _Clock()
    connector, calls = _connector(clock, [_read_timeout() for _ in range(20)])
    async with _scheduler(connector):
        await _spin(600)
    assert calls == ["start"] * 6  # первая + 5 повторов
    assert clock.redo == [30.0, 60.0, 120.0, 240.0, 300.0]
    assert "повторы исчерпаны" in caplog.text


async def test_success_resets_counter() -> None:
    clock = _Clock()
    connector, calls = _connector(clock, [OSError("сеть"), TimeoutError(), None, _read_timeout()])
    async with _scheduler(connector):
        await _spin()
        assert calls == ["start", "start", "start"]
        assert clock.redo == [30.0, 60.0]
        connector._request_sync("event_error")  # noqa: SLF001
        await _spin()
    assert calls == ["start", "start", "start", "event_error", "event_error"]
    assert clock.redo == [30.0, 60.0, 30.0]  # после успеха — снова с 30 с, причина та же


async def test_auth_required_and_other_errors_do_not_repeat() -> None:
    for exc in (AuthRequired("вход не принят"), ValueError("дефект")):
        clock = _Clock()
        connector, calls = _connector(clock, [exc])
        async with _scheduler(connector):
            await _spin()
        assert calls == ["start"], exc
        assert clock.redo == [], exc
        assert connector._sync_redo_task is None  # noqa: SLF001


async def test_no_repeat_while_asleep() -> None:
    # Уснули посреди упавшей сверки — повтор не заказывается (после сна будет `resume`).
    clock = _Clock()
    connector = None

    def _fall_asleep() -> Exception:
        connector._sleeping = True  # noqa: SLF001
        connector._awake.clear()  # noqa: SLF001
        return _read_timeout()

    connector, calls = _connector(clock, [_fall_asleep])
    async with _scheduler(connector):
        await _spin()
    assert calls == ["start"]
    assert clock.redo == []

    # Уснули, пока повтор ждал паузу, — повтор не ставится.
    clock = _Clock()
    connector, calls = _connector(clock, [_read_timeout()])

    def _sleep_now() -> None:
        connector._sleeping = True  # noqa: SLF001
        connector._awake.clear()  # noqa: SLF001

    clock.on_redo = _sleep_now
    async with _scheduler(connector):
        await _spin()
        assert connector._sync_queue.empty()  # noqa: SLF001
    assert calls == ["start"]
    assert clock.redo == [30.0]


async def test_narrow_pass_does_not_swallow_pending_repeat() -> None:
    # Пока повтор `start` ждёт паузу, прошла узкая сверка `join` — повтор `start` всё равно идёт.
    clock = _Clock()
    clock.gate = asyncio.Event()
    connector, calls = _connector(clock, [_read_timeout()])
    async with _scheduler(connector):
        await _spin()
        assert calls == ["start"]
        connector._request_sync("join")  # noqa: SLF001
        await _spin()
        assert calls == ["start", "join"]
        clock.gate.set()
        await _spin()
    assert calls == ["start", "join", "start"]


async def test_stopping_cancels_repeat() -> None:
    clock = _Clock()
    clock.gate = asyncio.Event()
    connector, calls = _connector(clock, [_read_timeout()])
    async with _scheduler(connector):
        await _spin()
        connector._stopping = True  # noqa: SLF001
        clock.gate.set()
        await _spin()
        assert connector._sync_queue.empty()  # noqa: SLF001
    assert calls == ["start"]


async def test_pass_with_same_reason_replaces_pending_repeat() -> None:
    # Пока повтор `start` ждёт паузу, сверка `start` пришла другим путём и тоже упала: старый повтор снят,
    # новый — со следующей паузой (60 с), а не сразу после первой.
    clock = _Clock()
    clock.gate = asyncio.Event()
    connector, calls = _connector(clock, [_read_timeout(), _read_timeout()])
    async with _scheduler(connector):
        await _spin()
        connector._request_sync("start")  # noqa: SLF001
        await _spin()
        assert calls == ["start", "start"]
        assert clock.redo == [30.0, 60.0]
        clock.gate.set()
        await _spin()
    assert calls == ["start", "start", "start"]
