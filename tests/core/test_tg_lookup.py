"""Ф29-1 (docs/architecture.md): посредник поиска и чтения Telegram в памяти
ядра — суточные лимиты, интервал между выдачами, пауза FloodWait, очередь, таймаут ответа. Время — подменное
(миллисекунды передаются явно)."""

import pytest

from hub.core import tg_lookup as lookup_mod
from hub.core.tg_lookup import DAY_MS, GAP_MS, LookupFailed, NotFound, TgLookup

T0 = 1_700_000_000_000
ACC = 7


class Clock:
    def __init__(self, ms: int = T0) -> None:
        self.ms = ms

    def __call__(self) -> int:
        return self.ms


async def _serve(lk: TgLookup, clock: Clock, *, payload=None) -> dict:
    """Коннектор: взять заявку, ответить `ok`, сдвинуть часы на интервал."""
    item = await lk.next(ACC, 0, clock)
    assert item is not None
    lk.result(
        ACC, item["id"], outcome="ok", payload=payload or {}, reason=None, retry_after_s=None, now_ms=clock()
    )
    clock.ms += GAP_MS
    return item


@pytest.mark.parametrize(("kind", "limit"), [("search", 60), ("peek", 100)])
async def test_daily_limit_429_with_time_and_window_slides(kind, limit):
    lk, clock = TgLookup(), Clock()
    first_ms = clock.ms
    for _ in range(limit):
        ticket = lk.submit(ACC, kind, {}, now_ms=clock())
        await _serve(lk, clock)
        assert await lk.wait(ticket) == {}
    with pytest.raises(LookupFailed) as exc:
        lk.submit(ACC, kind, {}, now_ms=clock())
    assert exc.value.status == 429
    assert f"лимит: {limit}" in exc.value.text and "следующий после" in exc.value.text
    assert lookup_mod._hhmm(first_ms + DAY_MS) in exc.value.text  # noqa: SLF001
    # Другой вид — свой лимит.
    other = "peek" if kind == "search" else "search"
    lk.submit(ACC, other, {}, now_ms=clock())
    # Через сутки после первой заявки — снова можно.
    clock.ms = first_ms + DAY_MS + 1
    lk.submit(ACC, kind, {}, now_ms=clock())


async def test_gap_between_issues_is_3s():
    lk, clock = TgLookup(), Clock()
    lk.submit(ACC, "search", {"query": "a"}, now_ms=clock())
    lk.submit(ACC, "search", {"query": "b"}, now_ms=clock())
    first = await lk.next(ACC, 0, clock)
    assert first["params"] == {"query": "a"} and first["kind"] == "search"
    clock.ms += GAP_MS - 1
    assert await lk.next(ACC, 0, clock) is None
    clock.ms += 1
    second = await lk.next(ACC, 0, clock)
    assert second["params"] == {"query": "b"}


async def test_flood_pauses_account_and_rejects_queued_and_new():
    lk, clock = TgLookup(), Clock()
    first = lk.submit(ACC, "peek", {}, now_ms=clock())
    queued = lk.submit(ACC, "peek", {}, now_ms=clock())
    item = await lk.next(ACC, 0, clock)
    lk.result(
        ACC, item["id"], outcome="error", payload=None, reason="flood", retry_after_s=30, now_ms=clock()
    )
    for ticket in (first, queued):
        with pytest.raises(LookupFailed) as exc:
            await lk.wait(ticket)
        assert exc.value.status == 429 and "подождать 30 с" in exc.value.text
    clock.ms += 10_000
    with pytest.raises(LookupFailed) as exc:
        lk.submit(ACC, "search", {}, now_ms=clock())
    assert exc.value.status == 429 and "подождать 20 с" in exc.value.text
    # Другой аккаунт паузу не делит.
    lk.submit(ACC + 1, "search", {}, now_ms=clock())
    clock.ms += 20_001
    lk.submit(ACC, "search", {}, now_ms=clock())
    # Отбитая из очереди заявка до Telegram не дошла — в суточный лимит не считается.
    assert len(lk._stamps[(ACC, "peek")]) == 1  # noqa: SLF001


async def test_queue_over_5_is_429():
    lk, clock = TgLookup(), Clock()
    for _ in range(lookup_mod.QUEUE_MAX):
        lk.submit(ACC, "search", {}, now_ms=clock())
    with pytest.raises(LookupFailed) as exc:
        lk.submit(ACC, "search", {}, now_ms=clock())
    assert exc.value.status == 429 and "очередь" in exc.value.text


async def test_timeout_504_unissued_is_dropped_and_refunded():
    lk, clock = TgLookup(answer_timeout_s=0.01), Clock()
    ticket = lk.submit(ACC, "search", {}, now_ms=clock())
    with pytest.raises(LookupFailed) as exc:
        await lk.wait(ticket)
    assert exc.value.status == 504 and "не ответил" in exc.value.text
    assert await lk.next(ACC, 0, clock) is None
    assert len(lk._stamps[(ACC, "search")]) == 0  # noqa: SLF001


async def test_timeout_after_issue_late_result_is_404():
    lk, clock = TgLookup(answer_timeout_s=0.01), Clock()
    ticket = lk.submit(ACC, "search", {}, now_ms=clock())
    item = await lk.next(ACC, 0, clock)
    with pytest.raises(LookupFailed) as exc:
        await lk.wait(ticket)
    assert exc.value.status == 504
    with pytest.raises(NotFound):
        lk.result(ACC, item["id"], outcome="ok", payload={}, reason=None, retry_after_s=None, now_ms=clock())
    # Выданная заявка дошла до Telegram — в лимите остаётся.
    assert len(lk._stamps[(ACC, "search")]) == 1  # noqa: SLF001


async def test_result_of_other_account_or_unknown_is_404():
    lk, clock = TgLookup(), Clock()
    lk.submit(ACC, "search", {}, now_ms=clock())
    item = await lk.next(ACC, 0, clock)
    with pytest.raises(NotFound):
        lk.result(
            ACC + 1, item["id"], outcome="ok", payload={}, reason=None, retry_after_s=None, now_ms=clock()
        )
    with pytest.raises(NotFound):
        lk.result(ACC, 999, outcome="ok", payload={}, reason=None, retry_after_s=None, now_ms=clock())


@pytest.mark.parametrize(
    ("reason", "status", "text"),
    [
        ("not_found", 404, "такого канала или группы нет"),
        ("not_group", 422, "это человек или бот"),
        ("private", 422, "закрытый — читать можно только после вступления (hub_join)"),
        ("temporary", 503, "Telegram не отвечает, повторите позже"),
        ("query_too_short", 422, "короткий запрос"),
        ("CHAT_SOMETHING", 502, "Telegram отказал: CHAT_SOMETHING"),
    ],
)
async def test_error_reasons_to_texts(reason, status, text):
    lk, clock = TgLookup(), Clock()
    ticket = lk.submit(ACC, "peek", {}, now_ms=clock())
    item = await lk.next(ACC, 0, clock)
    lk.result(
        ACC, item["id"], outcome="error", payload=None, reason=reason, retry_after_s=None, now_ms=clock()
    )
    with pytest.raises(LookupFailed) as exc:
        await lk.wait(ticket)
    assert (exc.value.status, exc.value.text) == (status, text)


async def test_next_wakes_on_submit():
    import asyncio

    lk, clock = TgLookup(), Clock()
    waiter = asyncio.ensure_future(lk.next(ACC, 5, clock))
    await asyncio.sleep(0)
    lk.submit(ACC, "search", {"query": "x"}, now_ms=clock())
    item = await asyncio.wait_for(waiter, 1)
    assert item["params"] == {"query": "x"}


# --- Ф32-2 п.4 + П3: остановка ядра -------------------------------------------------------------------------


async def test_f322_close_wakes_hanging_next_empty_and_waiters_get_503():
    import asyncio

    lk, clock = TgLookup(), Clock()
    other = ACC + 1
    # аккаунт ACC: коннектор висит в `next` на пустой очереди; аккаунт `other`: сессия ждёт ответа в очереди
    hanging = asyncio.ensure_future(lk.next(ACC, 25, clock))
    queued = lk.submit(other, "search", {"query": "abc"}, now_ms=clock())
    waiter = asyncio.ensure_future(lk.wait(queued))
    await asyncio.sleep(0.05)
    assert not hanging.done() and not waiter.done()

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    lk.close()
    assert await asyncio.wait_for(hanging, 2) is None
    with pytest.raises(LookupFailed) as exc:
        await asyncio.wait_for(waiter, 2)
    assert loop.time() - t0 < 2
    assert exc.value.status == 503 and "останавливается" in exc.value.text
    # заявка не ушла в «выдана»: коннектор её не получил и после закрытия, суточный лимит возвращён
    assert queued.issued_ms is None
    assert await lk.next(other, 0, clock) is None
    assert lk._inflight == {} and not lk._stamps[(other, "search")]  # noqa: SLF001
    # новые заявки после закрытия — тот же 503
    with pytest.raises(LookupFailed) as exc:
        lk.submit(other, "search", {"query": "abc"}, now_ms=clock())
    assert exc.value.status == 503


async def test_f322_close_checks_before_issue_even_with_queued_ticket():
    """`next` проверяет остановку ДО выдачи: заявка в очереди есть, но флаг уже стоит — пусто."""
    lk, clock = TgLookup(), Clock()
    ticket = lk.submit(ACC, "peek", {}, now_ms=clock())
    lk._closed = True  # noqa: SLF001 — флаг без `close()`: очередь не разобрана, заявка лежит
    assert await lk.next(ACC, 5, clock) is None
    assert ticket.issued_ms is None and lk._inflight == {}  # noqa: SLF001
    ticket.future.cancel()


async def test_f322_close_fails_issued_ticket_and_late_result_is_not_found():
    lk, clock = TgLookup(), Clock()
    ticket = lk.submit(ACC, "peek", {}, now_ms=clock())
    item = await lk.next(ACC, 0, clock)
    assert item["id"] == ticket.id
    lk.close()
    with pytest.raises(LookupFailed) as exc:
        await lk.wait(ticket)
    assert exc.value.status == 503
    with pytest.raises(NotFound):
        lk.result(ACC, item["id"], outcome="ok", payload={}, reason=None, retry_after_s=None, now_ms=clock())


async def test_f322_notifier_close_runs_hooks():
    from hub.core.feed import Notifier

    lk, notifier = TgLookup(), Notifier()
    notifier.on_close(lk.close)
    assert not lk.closed
    notifier.close()
    assert lk.closed and notifier.closed
