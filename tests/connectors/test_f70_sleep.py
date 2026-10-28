"""Ф70-1 (docs/architecture.md и E): каркас коннектора спит вместе с днём проекта —
`stop_receiving`/`start_receiving`, сверка при сне не идёт, после пробуждения — `resume`, пульс при сне не
ходит в источник и шлёт `ok`; источник без поддержки сна не засыпает. Стенд: `FakeConnector.run()` против
настоящего внутреннего ядра — день закрыт: входящее не доходит, ручная отправка уходит;
день открыт — догон."""

import asyncio
import contextlib
import logging

import pytest
from httpx import ASGITransport

from hub.connectors.base import CoreClient
from hub.connectors.fake import FakeConnector, FakeSource
from hub.core import connector_auth, mapping, workday
from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.feed import Notifier
from hub.core.outbox import enqueue

SLEEP = {"project_id": 7, "since_ts": 1000}


class _Client:
    def __init__(self) -> None:
        self.statuses: list[str] = []

    async def set_status(self, status: str, *, last_error: str | None = None) -> dict:
        del last_error
        self.statuses.append(status)
        return {"ok": True}


class _Spy(FakeConnector):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[str] = []

    async def stop_receiving(self) -> None:
        self.calls.append("stop")

    async def start_receiving(self) -> None:
        self.calls.append("start")

    async def health(self) -> None:
        self.calls.append("health")

    async def sync(self, reason: str = "scheduled") -> None:
        self.calls.append(f"sync:{reason}")


def test_core_sleep_sources_match_connector_classes() -> None:
    """Ревью п.1: ядро помечает спящим только аккаунты источников, чей коннектор умеет спать."""
    from hub.connectors.mail.connector import MailConnector
    from hub.connectors.mail.plain import PlainMailConnector
    from hub.connectors.telegram.connector import TelegramConnector
    from hub.core.conn_sleep import SLEEP_SOURCES

    classes: tuple[type, ...] = (MailConnector, PlainMailConnector, TelegramConnector)
    for cls in classes:
        assert cls.sleep_enabled is (cls.source in SLEEP_SOURCES), cls.__name__
    assert FakeConnector.sleep_enabled is True
    assert "fake" in SLEEP_SOURCES  # `account.source` поддельного (`_CONNECTOR_REGISTRY` в cli.py)
    assert {"telegram", "whatsapp", "feed"}.isdisjoint(SLEEP_SOURCES)


def _drain(connector) -> set[str]:
    out = set()
    while not connector._sync_queue.empty():  # noqa: SLF001
        out.add(connector._sync_queue.get_nowait())  # noqa: SLF001
    return out


async def test_sleep_and_wake_call_receiving_hooks_and_resume(caplog) -> None:
    connector = _Spy(_Client(), FakeSource())
    caplog.set_level(logging.INFO, logger="hub.connectors.base")
    await connector._apply_sleep(SLEEP)  # noqa: SLF001
    await connector._apply_sleep(SLEEP)  # noqa: SLF001 — повтор без смены ничего не зовёт
    assert connector.sleeping is True
    assert connector.calls == ["stop"]
    assert _drain(connector) == set()
    await connector._apply_sleep(None)  # noqa: SLF001
    assert connector.sleeping is False
    assert connector.calls == ["stop", "start"]
    assert _drain(connector) == {"resume"}
    text = caplog.text
    assert "сон: день проекта 7 закрыт" in text and "сон снят, догон" in text


async def test_unsupported_source_does_not_sleep(caplog) -> None:
    class _NoSleep(_Spy):
        sleep_enabled = False

    connector = _NoSleep(_Client(), FakeSource())
    caplog.set_level(logging.INFO, logger="hub.connectors.base")
    await connector._apply_sleep(SLEEP)  # noqa: SLF001
    await connector._apply_sleep(SLEEP)  # noqa: SLF001
    assert connector.sleeping is False
    assert connector.calls == []
    assert caplog.text.count("сон не поддерживается") == 1
    assert connector._sleep_known is True  # noqa: SLF001 — long-poll ждёт смены, а не крутится


async def test_heartbeat_while_asleep_skips_health_and_reports_ok() -> None:
    client = _Client()
    ticks: list[float] = []

    async def _sleep(seconds: float) -> None:
        ticks.append(seconds)
        if len(ticks) >= 2:
            connector._stopping = True  # noqa: SLF001
        await asyncio.sleep(0)

    connector = _Spy(client, FakeSource(), sleep=_sleep)
    await connector._apply_sleep(SLEEP)  # noqa: SLF001
    await asyncio.wait_for(connector._heartbeat_loop(), 5)  # noqa: SLF001
    assert "health" not in connector.calls
    assert client.statuses == ["ok", "ok"]


async def test_scheduler_does_not_sync_while_asleep_then_resumes() -> None:
    async def _sleep(_seconds: float) -> None:
        await asyncio.sleep(3600)  # плановой сверки в тесте нет — только заказанные

    connector = _Spy(_Client(), FakeSource(), sleep=_sleep)
    await connector._apply_sleep(SLEEP)  # noqa: SLF001
    task = asyncio.ensure_future(connector._sync_scheduler_loop())  # noqa: SLF001
    try:
        connector._request_sync("event_error")  # noqa: SLF001
        for _ in range(50):
            await asyncio.sleep(0)
        assert [c for c in connector.calls if c.startswith("sync")] == []
        await connector._apply_sleep(None)  # noqa: SLF001
        for _ in range(200):
            await asyncio.sleep(0)
            if any(c.startswith("sync") for c in connector.calls):
                break
        # «start», «event_error» и «resume» слились в одну сверку — главная причина `resume`.
        assert [c for c in connector.calls if c.startswith("sync")] == ["sync:resume"]
    finally:
        connector._stopping = True  # noqa: SLF001
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_sleep_detector_does_not_reconnect_while_asleep() -> None:
    wall = [1000.0]
    mono = [0.0]
    steps = [5.0, 3600.0, 5.0]

    async def _sleep(_seconds: float) -> None:
        step = steps.pop(0) if steps else 5.0
        wall[0] += step
        mono[0] += 5.0
        if not steps:
            connector._stopping = True  # noqa: SLF001
        await asyncio.sleep(0)

    connector = _Spy(_Client(), FakeSource(), wall=lambda: wall[0], mono=lambda: mono[0], sleep=_sleep)
    resumes: list[int] = []

    async def _on_resume() -> None:
        resumes.append(1)

    connector.on_resume = _on_resume  # type: ignore[method-assign]
    await connector._apply_sleep(SLEEP)  # noqa: SLF001
    await asyncio.wait_for(connector._sleep_detector_loop(), 5)  # noqa: SLF001
    assert resumes == []
    assert _drain(connector) == {"resume"}  # сверка — после снятия сна
    # Ревью п.4: ноутбук спал, пока спал канал — `on_resume` при выходе канала из сна, один раз.
    await connector._apply_sleep(None)  # noqa: SLF001
    assert resumes == [1]
    await connector._apply_sleep(SLEEP)  # noqa: SLF001
    await connector._apply_sleep(None)  # noqa: SLF001 — ноутбук больше не спал
    assert resumes == [1]


# --- стенд: FakeConnector.run() против настоящего внутреннего ядра ------------------------------------------


class _Clock:
    value = 1_760_000_000.0

    def __call__(self) -> float:
        return self.value


@pytest.fixture
def stand(db):
    notifier = Notifier()
    db.on_commit(notifier.notify)
    app = create_internal_app(
        db, clock=_Clock(), notifier=notifier, admin_key_hash=hash_secret(ADMIN_PREFIX + "y" * 43)
    )

    def _fn(conn):
        acc = conn.execute("INSERT INTO main.account (source, db) VALUES ('fake', 'main')").lastrowid
        project_id = conn.execute("INSERT INTO main.project (name) VALUES ('P')").lastrowid
        mapping.add_rule(
            conn, account_id=acc, project_id=project_id, match={"all": True}, priority=None, now_ms=0
        )
        ch = conn.execute(
            "INSERT INTO main.channel (account_id, ext_id, kind, first_seen_ts, project_id, map_source) "
            "VALUES (?, 'dm-1', 'dm', 0, ?, 'auto')",
            (acc, project_id),
        ).lastrowid
        token = conn.execute(
            "INSERT INTO main.api_token (name, kind, hash) VALUES ('t', 'ui', 'x')"
        ).lastrowid
        workday.set_workday(conn, project_id, True, now_ms=0)
        return acc, project_id, ch, token

    acc, project_id, ch, token = db.write_sync(_fn)
    secret = connector_auth.generate_connector_secret()
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.connector_secret (account_id, hash, created_ts) VALUES (?, ?, 0)",
            (acc, connector_auth.hash_secret(secret)),
        )
    )
    client = CoreClient(secret=secret, transport=ASGITransport(app=app))
    return {"db": db, "client": client, "project": project_id, "channel": ch, "token": token}


def _day(db, project_id, open_: bool) -> None:
    audit = {"actor": "ui", "api_token_id": None, "client_name": None, "request_id": "r"}
    db.write_sync(lambda conn: workday.set_day(conn, project_id, open_, now_ms=1, audit=audit))


def _texts(db) -> list[str]:
    with db.reader() as conn:
        rows = conn.execute("SELECT text FROM main.message WHERE is_me = 0 ORDER BY id").fetchall()
    return [r[0] for r in rows]


async def _until(predicate, limit_s=8.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit_s
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("условие не наступило")
        await asyncio.sleep(0.02)


async def test_stand_closed_day_holds_incoming_manual_send_goes_open_day_catches_up(stand) -> None:
    db = stand["db"]
    _day(db, stand["project"], False)  # день закрыт ещё до старта коннектора
    source = FakeSource()
    source.incoming("dm-1", ext_id="m1", text="пока спал", ts=1_000)
    connector = FakeConnector(stand["client"], source)
    run_task = asyncio.ensure_future(connector.run(source="fake", account_id=1))
    try:
        await _until(lambda: connector.sleeping)
        await asyncio.sleep(0.3)
        assert _texts(db) == []  # сверка `start` при сне не прошла — входящее не доходит

        db.write_sync(
            lambda conn: enqueue(
                conn, channel_id=stand["channel"], text="ручная", origin="ui", api_token_id=stand["token"],
                idempotency_key=None, now_ms=1,
            )
        )  # fmt: skip
        await _until(lambda: source.delivered_count("ручная") == 1)  # ручная отправка при сне уходит

        _day(db, stand["project"], True)
        await _until(lambda: not connector.sleeping)
        await _until(lambda: _texts(db) == ["пока спал"])  # догон после «Начать день»
    finally:
        connector._trigger_exit(0)  # noqa: SLF001 — то же, что SIGTERM
        await asyncio.wait_for(run_task, 10)
