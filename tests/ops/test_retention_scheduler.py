"""Планировщик ретенции в `hub core` (спека `docs/architecture.md`):
тик ~60 с по настенным часам, первый прогон не раньше 5 мин после старта, дальше — не чаще раза в 6 ч.
`sleep`/`run_once` инжектируются (тот же приём, что `wall()/mono()/sleep()` у `ConnectorBase.run()`) —
никакого реального ожидания, `FakeClock` двигается вручную на каждый тик."""

import asyncio

import pytest

from hub.core import retention
from hub.core.db import Db
from hub.core.runner import _retention_loop


class FakeClock:
    def __init__(self, start_ms: int = 0) -> None:
        self.ms = start_ms

    def __call__(self) -> float:
        return self.ms / 1000

    def advance_ms(self, ms: int) -> None:
        self.ms += ms


@pytest.fixture
def db(tmp_path):
    instance = Db(tmp_path)
    instance.start()
    yield instance
    instance.close()


async def _run_ticks(db, clock, *, ticks: int, tick_ms: int = 60_000, run_once=retention.run_once):
    """Прогоняет `_retention_loop` ровно `ticks` тиков (последний обрывает цикл `asyncio.CancelledError`,
    как обрывает его `retention_task.cancel()` в `runner.py`), двигая `clock` на `tick_ms` на каждом тике."""
    state = {"n": 0}

    async def fake_sleep(_seconds: float) -> None:
        # Тик за тиком: первые `ticks` вызовов двигают часы и ДАЮТ _retention_loop обработать тик; тик
        # `ticks + 1` рвёт цикл, не двигая часы дальше — иначе последний обработанный тик недодавали бы
        # (продвинули часы, но `_retention_loop` до своей проверки уже не дошёл бы).
        state["n"] += 1
        if state["n"] > ticks:
            raise asyncio.CancelledError
        clock.advance_ms(tick_ms)

    with pytest.raises(asyncio.CancelledError):
        await _retention_loop(db, clock, sleep=fake_sleep, run_once=run_once)


async def test_no_run_before_initial_delay(db) -> None:
    clock = FakeClock(0)
    calls: list[int] = []

    def spy_run_once(db_arg, now_ms, stop_event=None):
        del stop_event
        calls.append(now_ms)

    # 4 тика по 60 с = 240 с < 5 мин (300 с) — ни одного прогона.
    await _run_ticks(db, clock, ticks=4, run_once=spy_run_once)
    assert calls == []


async def test_first_run_at_initial_delay_then_gated_by_six_hours(db) -> None:
    clock = FakeClock(0)
    calls: list[int] = []

    def counting_run_once(db_arg, now_ms, stop_event=None):
        del stop_event
        calls.append(now_ms)
        return retention.run_once(db_arg, now_ms)  # настоящий run_once — пишет retention_last_run_ts

    # Тик 5 (300_000 мс = 5 мин) — первый прогон. Тики 6..364 — под 6-часовым порогом, ни одного нового.
    # Тик 365 (300_000 + 360*60_000 = 300_000 + 21_600_000 = 21_900_000 мс) — прошло ровно 6 ч, второй прогон.
    await _run_ticks(db, clock, ticks=365, run_once=counting_run_once)
    assert calls == [300_000, 21_900_000]


async def test_scheduler_survives_run_once_exception(db) -> None:
    """Раздел F.3: сбой прогона не должен убить планировщик — на следующем тике пробуем снова."""
    clock = FakeClock(0)
    calls: list[int] = []

    def flaky_run_once(db_arg, now_ms, stop_event=None):
        del stop_event
        calls.append(now_ms)
        raise RuntimeError("синтетический сбой прогона")

    # тик 5 (первый прогон, падает) + тик 6 (под порогом 6ч, но last_run_ts не записан из-за падения ->
    # планировщик пробует ЕЩЁ РАЗ на следующем тике, а не ждёт 6 ч).
    await _run_ticks(db, clock, ticks=6, run_once=flaky_run_once)
    assert calls == [300_000, 360_000]
