"""Ф21-1 (docs/architecture.md, D, обязательный тест I.16): цикл ядра `runner._people_loop` —
связывание раз в 10 минут и пересчёт раз в час по часам-инъекции, сбой — в журнал и цикл жив, закрытый `Db` —
выход, флаг остановки — выход."""

import logging
import threading

from hub.core import runner
from hub.core.db import Db

from .people_helpers import account, channel, contact, msg, person_of, stats_of


def _ticker(clock, stop: threading.Event, ticks: int):
    count = {"n": 0}

    async def fake_sleep(_seconds):
        count["n"] += 1
        clock.advance_ms(60 * 1000)  # тик = минута настенных часов
        if count["n"] > ticks:
            stop.set()

    return fake_sleep


async def test_i16_schedule_by_injected_clock(db, clock):
    links, stats = [], []
    stop = threading.Event()

    def link(_conn, now_ms):
        links.append(now_ms)
        return {"created": 0}

    def recompute(_db, now_ms, _stop):
        stats.append(now_ms)
        return 0

    await runner._people_loop(
        db, clock, sleep=_ticker(clock, stop, 125), stop_event=stop, link=link, recompute=recompute
    )
    start = links[0]
    assert start == 1_700_000_060_000  # первый — через минуту после старта
    assert [t - start for t in links] == [i * 10 * 60 * 1000 for i in range(13)]
    assert [t - start for t in stats] == [0, 60 * 60 * 1000, 2 * 60 * 60 * 1000]


async def test_i16_real_pass_links_and_counts(db, clock):
    tg = account(db, "telegram")
    p = contact(db, tg, "100", "Пётр")
    dm = channel(db, tg, "100")
    msg(db, dm, ts=int(clock() * 1000) - 1000, sender=p)
    stop = threading.Event()
    inflight: dict = {}
    await runner._people_loop(db, clock, sleep=_ticker(clock, stop, 1), stop_event=stop, inflight=inflight)
    assert person_of(db, p) is not None
    assert stats_of(db, p)["in"]["total"] == 1
    assert inflight["future"].done()


async def test_i16_failure_logged_loop_alive(db, clock, caplog):
    calls = {"link": 0, "stats": 0}
    stop = threading.Event()

    def link(_conn, _now_ms):
        calls["link"] += 1
        raise RuntimeError("сбой связывания")

    def recompute(_db, _now_ms, _stop):
        calls["stats"] += 1
        raise RuntimeError("сбой пересчёта")

    with caplog.at_level(logging.ERROR, logger="hub.core.runner"):
        await runner._people_loop(
            db, clock, sleep=_ticker(clock, stop, 65), stop_event=stop, link=link, recompute=recompute
        )
    assert calls == {"link": 7, "stats": 2}
    messages = [r.getMessage() for r in caplog.records]
    assert "люди: связывание упало" in messages and "люди: пересчёт статистики упал" in messages


async def test_i16_closed_db_exits(tmp_path, clock):
    closed = Db(tmp_path)
    closed.start()
    closed.close()
    stop = threading.Event()
    await runner._people_loop(closed, clock, sleep=_ticker(clock, stop, 10_000), stop_event=stop)
    assert not stop.is_set()  # вышел сам по `DbClosed`, а не по флагу


async def test_i16_stop_flag_exits_before_work(db, clock):
    stop = threading.Event()
    stop.set()
    calls = []
    await runner._people_loop(db, clock, stop_event=stop, link=lambda *a: calls.append(a))
    assert calls == []
