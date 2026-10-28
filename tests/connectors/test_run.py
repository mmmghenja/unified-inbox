"""Тесты 9-11 куска 1a фазы 1 (раздел D спеки `docs/architecture.md`) — машинерия
`ConnectorBase.run()`: толкатель буфера (бэкофф, переполнение -> `sync("overflow")`), детектор сна,
пульс/`AuthRequired`, статус `limited` в цикле отправки. Тест 12 (SIGTERM на живом процессе) —
`tests/connectors/test_run_process.py`.

Толкатель/планировщик/детектор сна/пульс проверяются НАПРЯМУЮ вызовом соответствующих корутин
`ConnectorBase` (а не через полный `run()` со всеми пятью задачами разом) — так тесты детерминированы и не
гоняются друг с другом за общим инжектируемым `sleep()`. Это те же самые методы, которые `run()` запускает
как задачи, так что это не отдельная реализация, а прямой вызов машинерии.
"""

import asyncio
import contextlib

import httpx
import pytest

from hub.connectors.base import AuthRequired, CoreClient
from hub.connectors.fake import FakeConnector, FakeSource
from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, generate_connector_secret, hash_secret
from hub.core.db import Db
from hub.core.feed import Notifier


async def _instant_sleep(_seconds: float) -> None:
    return None


def _connect_error() -> httpx.ConnectError:
    return httpx.ConnectError("core недоступен", request=httpx.Request("POST", "http://hub-internal/x"))


def _unauthorized_error() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://hub-internal/internal/v1/events")
    response = httpx.Response(401, request=request)
    return httpx.HTTPStatusError("401", request=request, response=response)


class _ScriptedClient:
    """Двойник `CoreClient` для толкателя: `push_events` подчиняется сценарию `fail_times`/`then_401`."""

    def __init__(self, *, fail_times: int = 0, then_401: bool = False) -> None:
        self.fail_times = fail_times
        self.then_401 = then_401
        self.calls = 0
        self.received: list[dict] = []

    async def push_events(self, events: list[dict], cursor: dict | None = None) -> dict:
        del cursor
        self.calls += 1
        if self.calls <= self.fail_times:
            raise _connect_error()
        if self.then_401 and self.calls == self.fail_times + 1:
            raise _unauthorized_error()
        self.received.extend(events)
        return {"applied": len(events), "noop": 0, "dead": 0, "max_seq": 0}

    async def set_status(self, status: str, *, last_error: str | None = None) -> dict:
        del status, last_error
        return {"ok": True}


# --- Тест 9: толкатель — бэкофф, переполнение -> ровно один sync("overflow"), 401 -> код 78 ----------------


async def test_1a_09_pusher_retries_with_growing_capped_backoff_delivers_all_in_order() -> None:
    client = _ScriptedClient(fail_times=6)
    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    connector = FakeConnector(client, FakeSource(), sleep=_fake_sleep)
    events = [{"n": i} for i in range(1200)]  # 3 пачки по <=500 — проверяем и порядок между пачками
    for ev in events:
        connector.emit(ev)
    connector._stopping = True  # noqa: SLF001 — толкатель выйдет сам, когда буфер опустеет

    await asyncio.wait_for(connector._pusher_loop(), timeout=5)  # noqa: SLF001

    assert client.received == events
    assert len(sleeps) == 6  # ровно 6 неудачных попыток до успеха
    assert sleeps == sorted(sleeps)  # бэкофф не убывает
    assert sleeps[0] < 1.5  # первая попытка — около 1 с (± джиттер 20%)
    assert all(s <= 30 * 1.25 for s in sleeps)  # потолок 30 с (с запасом на джиттер)
    assert sleeps[-1] > 20  # к 6-й неудаче бэкофф уже у потолка


async def test_1a_09_overflow_clears_buffer_and_triggers_exactly_one_sync_overflow() -> None:
    """Находка F14 (round 2 критика куска 1b): планировщик теперь СЛИВАЕТ все причины, накопившиеся в
    очереди к моменту пробуждения, в ОДНУ сверку по приоритету (`resume > start > overflow > … >
    scheduled`). В этом тесте буфер переполняется ДО того, как заведены задачи планировщика/толкателя,
    поэтому "start" (запрошенный планировщиком при входе в цикл) и "overflow" (толкатель успевает
    отработать пробную пустую пачку и попросить досверку) с высокой вероятностью совпадают на САМОМ ПЕРВОМ
    пробуждении — и `"start"` побеждает по приоритету, `"overflow"` уходит в `_pending_reasons` той же
    сверки. Это НЕ потеря: важна не строка причины, а то, что ровно ОДНА сверка закрыла переполнение
    (`need_resync` сброшен) и повторных «overflow»-циклов не было."""
    client = _ScriptedClient(fail_times=0)
    connector = FakeConnector(client, FakeSource(), sleep=_instant_sleep)

    sync_calls: list[str] = []
    pending_at_call: list[frozenset] = []
    real_sync = connector.sync

    async def _spy_sync(reason: str = "scheduled") -> None:
        sync_calls.append(reason)
        pending_at_call.append(connector._pending_reasons)  # noqa: SLF001
        await real_sync(reason)

    connector.sync = _spy_sync  # type: ignore[method-assign]

    for i in range(5001):  # ёмкость буфера 5000 -> переполнение на последнем emit()
        connector.emit({"n": i})
    assert connector._event_buffer == []  # noqa: SLF001
    assert connector._need_resync is True  # noqa: SLF001

    def _overflow_seen() -> bool:
        return any("overflow" in ({r} | p) for r, p in zip(sync_calls, pending_at_call, strict=True))

    pusher_task = asyncio.ensure_future(connector._pusher_loop())  # noqa: SLF001
    scheduler_task = asyncio.ensure_future(connector._sync_scheduler_loop())  # noqa: SLF001
    try:
        for _ in range(200):
            await asyncio.sleep(0)
            if _overflow_seen():
                break
        overflow_hits = sum(
            1 for r, p in zip(sync_calls, pending_at_call, strict=True) if "overflow" in ({r} | p)
        )
        assert overflow_hits == 1  # ровно один цикл разбирался с переполнением (сам или слитый с ним)
        assert connector._need_resync is False  # noqa: SLF001
    finally:
        connector._stopping = True  # noqa: SLF001
        connector._buffer_event.set()  # noqa: SLF001
        pusher_task.cancel()
        scheduler_task.cancel()
        for t in (pusher_task, scheduler_task):
            with contextlib.suppress(asyncio.CancelledError):
                await t


async def test_1a_09_401_from_push_events_exits_via_trigger_exit_78() -> None:
    client = _ScriptedClient(fail_times=0, then_401=True)
    connector = FakeConnector(client, FakeSource(), sleep=_instant_sleep)
    connector.emit({"n": 1})

    await asyncio.wait_for(connector._pusher_loop(), timeout=5)  # noqa: SLF001

    assert connector._exit_code == 78  # noqa: SLF001
    assert connector._stopping is True  # noqa: SLF001


# --- Тест 10: детектор сна ----------------------------------------------------------------------------------


async def test_1a_10_sleep_detector_fires_on_resume_exactly_once() -> None:
    client = _ScriptedClient()
    wall = [1_000.0]
    mono = [0.0]

    def _wall() -> float:
        return wall[0]

    def _mono() -> float:
        return mono[0]

    ticks = [5.0, 5.0, 5.0]  # обычный ход часов, потом — скачок

    async def _sleep(seconds: float) -> None:
        assert seconds == 5.0  # тик детектора — фиксированные 5 с
        if ticks:
            step = ticks.pop(0)
        else:
            step = 5.0
        wall[0] += step
        mono[0] += step

    connector = FakeConnector(client, FakeSource(), wall=_wall, mono=_mono, sleep=_sleep)
    resume_calls = 0
    real_on_resume = connector.on_resume

    async def _on_resume() -> None:
        nonlocal resume_calls
        resume_calls += 1
        await real_on_resume()
        # После срабатывания — заставим детектор увидеть ОБЫЧНЫЙ ход часов и остановиться.
        connector._stopping = True  # noqa: SLF001

    connector.on_resume = _on_resume  # type: ignore[method-assign]

    # Первый скачок настенных часов случится сразу после трёх обычных тиков — подменим следующий шаг sleep
    # на скачок +8ч при неподвижном monotonic.
    original_sleep = connector._sleep  # noqa: SLF001
    call_count = 0

    async def _sleep_with_jump(seconds: float) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 4:
            wall[0] += 8 * 3600  # часы ушли вперёд на 8 часов
            mono[0] += 5.0  # monotonic идёт как обычно (ноутбук был в сне — не считал время)
            return
        await original_sleep(seconds)

    connector._sleep = _sleep_with_jump  # noqa: SLF001

    await asyncio.wait_for(connector._sleep_detector_loop(), timeout=5)  # noqa: SLF001
    assert resume_calls == 1


async def test_1a_10_sleep_detector_ordinary_clock_progress_triggers_nothing() -> None:
    client = _ScriptedClient()
    wall = [1_000.0]
    mono = [0.0]
    ticks_left = 5

    async def _sleep(seconds: float) -> None:
        nonlocal ticks_left
        wall[0] += seconds
        mono[0] += seconds
        ticks_left -= 1
        if ticks_left <= 0:
            connector._stopping = True  # noqa: SLF001

    connector = FakeConnector(client, FakeSource(), wall=lambda: wall[0], mono=lambda: mono[0], sleep=_sleep)
    resume_calls = 0
    real_on_resume = connector.on_resume

    async def _on_resume() -> None:
        nonlocal resume_calls
        resume_calls += 1
        await real_on_resume()

    connector.on_resume = _on_resume  # type: ignore[method-assign]

    await asyncio.wait_for(connector._sleep_detector_loop(), timeout=5)  # noqa: SLF001
    assert resume_calls == 0


# --- Тест 11: AuthRequired в health() -> auth_required, выход 78 ------------------------------------------


async def test_1a_11_auth_required_in_health_stops_and_exits_78() -> None:
    class _AuthFailConnector(FakeConnector):
        async def health(self) -> None:
            raise AuthRequired("сессия протухла")

    client = _ScriptedClient()
    connector = _AuthFailConnector(client, FakeSource(), sleep=_instant_sleep)

    await asyncio.wait_for(connector._heartbeat_loop(), timeout=5)  # noqa: SLF001

    assert connector.auth_required is True
    assert connector._exit_code == 78  # noqa: SLF001
    assert connector._stopping is True  # noqa: SLF001


# --- Тест 11 (вторая половина): limited -> step_outbox не зовётся, после resume — зовётся -------------------


@pytest.fixture
def db(tmp_path):
    instance = Db(tmp_path)
    instance.start()
    yield instance
    instance.close()


ADMIN_SECRET = ADMIN_PREFIX + "run-test-admin-secret-0123456789abcdefghijkl"


def _make_account(db, *, source="fake", db_name="main") -> tuple[int, str]:
    secret = generate_connector_secret()

    def _fn(conn):
        cur = conn.execute("INSERT INTO main.account (source, db) VALUES (?, ?)", (source, db_name))
        account_id = cur.lastrowid
        from hub.core.connector_auth import hash_secret as hash_secret_

        conn.execute(
            "INSERT INTO main.connector_secret (account_id, hash, created_ts) VALUES (?, ?, 0)",
            (account_id, hash_secret_(secret)),
        )
        return account_id

    account_id = db.write_sync(_fn)
    return account_id, secret


async def test_1a_11_limited_account_outbox_loop_skips_step_outbox_until_resume(db) -> None:
    from httpx import ASGITransport

    account_id, secret = _make_account(db)
    channel_id = db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.channel (account_id, ext_id, kind, map_source) "
                "VALUES (?, 'c1', 'dm', 'auto')",
                (account_id,),
            ).lastrowid
        )
    )
    api_token_id = db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.api_token (name, kind, hash) VALUES ('t', 'ui', 'x')").lastrowid
        )
    )

    from hub.core.outbox import enqueue

    db.write_sync(
        lambda conn: enqueue(
            conn,
            channel_id=channel_id,
            text="hi",
            origin="ui",
            api_token_id=api_token_id,
            idempotency_key="k1",
            now_ms=1000,
        )
    )
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.account SET status = 'limited' WHERE id = ?", (account_id,))
    )

    notifier = Notifier()
    db.on_commit(notifier.notify)
    internal_app = create_internal_app(
        db, clock=lambda: 1.0, notifier=notifier, admin_key_hash=hash_secret(ADMIN_SECRET)
    )
    transport = ASGITransport(app=internal_app)

    core_client = CoreClient(secret=secret, transport=transport)
    connector = FakeConnector(core_client, FakeSource(), sleep=_instant_sleep)

    step_calls = 0
    real_step_outbox = connector.step_outbox

    async def _counting_step_outbox() -> int:
        nonlocal step_calls
        step_calls += 1
        return await real_step_outbox()

    connector.step_outbox = _counting_step_outbox  # type: ignore[method-assign]

    outbox_task = asyncio.ensure_future(connector._outbox_loop())  # noqa: SLF001
    try:
        for _ in range(20):
            await asyncio.sleep(0)
        assert step_calls == 0  # limited -> wait_outbox всегда ready=False, step_outbox не звался

        admin_client = httpx.AsyncClient(
            transport=transport,
            base_url="http://hub-internal",
            headers={"Authorization": f"Bearer {ADMIN_SECRET}"},
        )
        async with admin_client as ac:
            r = await ac.post(f"/internal/v1/admin/accounts/{account_id}/resume")
            assert r.status_code == 200, r.text

        for _ in range(200):
            await asyncio.sleep(0)
            if step_calls > 0:
                break
        assert step_calls > 0
    finally:
        connector._stopping = True  # noqa: SLF001
        outbox_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await outbox_task
        await core_client.aclose()
