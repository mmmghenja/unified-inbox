"""Приёмка куска 1a архитектором (22.09.2026): дефекты внешнего цикла `ConnectorBase.run()`, найденные чтением
кода: каждый тест — на один такой дефект (по разбору кода исполнителя он дал бы красный). См. «Поправки»
в `docs/architecture.md`.
"""

import asyncio
import contextlib
import sys

import httpx
import pytest

from hub.connectors.base import AuthRequired
from hub.connectors.fake import FakeConnector, FakeSource
from hub.core import admin


async def _instant_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://hub-internal/internal/v1/events")
    return httpx.HTTPStatusError(
        str(status), request=request, response=httpx.Response(status, request=request)
    )


class _Client:
    """Двойник `CoreClient`: пишет вызовы; `push_events` отвечает по сценарию `push_errors`."""

    def __init__(self, *, push_errors: list[Exception] | None = None) -> None:
        self.push_errors = list(push_errors or [])
        self.received: list[dict] = []
        self.statuses: list[str] = []
        self.closed = False

    async def push_events(self, events: list[dict], cursor: dict | None = None) -> dict:
        del cursor
        if self.push_errors:
            raise self.push_errors.pop(0)
        self.received.extend(events)
        return {"applied": len(events), "noop": 0, "dead": 0, "max_seq": 0}

    async def set_status(self, status: str, *, last_error: str | None = None) -> dict:
        del last_error
        self.statuses.append(status)
        return {"ok": True}

    async def wait_outbox(self, timeout_s: float) -> bool:
        await asyncio.sleep(0)
        return False

    async def aclose(self) -> None:
        self.closed = True


async def test_1a_acc_rejected_batch_is_dropped_not_retried_forever() -> None:
    """422 на пачку: повтор той же пачки даёт то же самое — толкатель вставал навсегда, живые события за
    этой пачкой не доходили никогда. Теперь пачка выбрасывается, следующие доходят, сверка запрошена."""
    client = _Client(push_errors=[_http_error(422)])
    connector = FakeConnector(client, FakeSource(), sleep=_instant_sleep)
    connector.emit({"n": 1})

    pusher = asyncio.ensure_future(connector._pusher_loop())  # noqa: SLF001
    try:
        for _ in range(100):
            await asyncio.sleep(0)
            if not connector._event_buffer and not connector._need_resync:  # noqa: SLF001
                break
        connector.emit({"n": 2})
        for _ in range(100):
            await asyncio.sleep(0)
            if client.received:
                break
        assert client.received == [{"n": 2}]
        assert connector._sync_queue.get_nowait() == "overflow"  # noqa: SLF001
    finally:
        connector._stopping = True  # noqa: SLF001
        connector._buffer_event.set()  # noqa: SLF001
        pusher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pusher


async def test_1a_acc_auth_required_from_sync_sets_status_and_exits_78() -> None:
    """Потеря входа посреди сверки раньше только логировалась: коннектор жил дальше слепым."""

    class _Conn(FakeConnector):
        async def sync(self, reason: str = "scheduled") -> None:
            raise AuthRequired("сессия отозвана")

    client = _Client()
    connector = _Conn(client, FakeSource(), sleep=_instant_sleep)

    await connector._run_one_sync("scheduled")  # noqa: SLF001

    assert client.statuses == ["auth_required"]
    assert connector._exit_code == 78  # noqa: SLF001


async def test_1a_acc_auth_required_from_send_exits_78() -> None:
    class _Conn(FakeConnector):
        async def send(self, lease: dict):
            raise AuthRequired("сессия отозвана")

    client = _Client()
    connector = _Conn(client, FakeSource(), sleep=_instant_sleep)

    await connector._process_lease({"mode": "send", "outbox_id": 1, "lease_gen": 1})  # noqa: SLF001

    assert client.statuses == ["auth_required"]
    assert connector._exit_code == 78  # noqa: SLF001


async def test_1a_acc_auth_required_from_start_source_exits_78_not_traceback() -> None:
    """`start_source()` с `AuthRequired` (1b: сессия Telegram не авторизована) раньше вылетал из `run()`
    исключением -> код 1 -> systemd перезапускал по кругу вместо остановки на 78."""

    class _Conn(FakeConnector):
        async def start_source(self) -> None:
            raise AuthRequired("не авторизован")

    client = _Client()
    connector = _Conn(client, FakeSource(), sleep=_instant_sleep)

    code = await asyncio.wait_for(connector.run(source="fake", account_id=1), timeout=5)

    assert code == 78
    assert client.statuses == ["auth_required"]
    assert client.closed


async def test_1a_acc_hanging_start_source_yields_to_sigterm() -> None:
    """Зависшее подключение к источнику (прокси лежит) раньше держало процесс до SIGKILL: сигнал ставил флаг,
    но `run()` сидел в `await start_source()`."""
    started = asyncio.Event()

    class _Conn(FakeConnector):
        async def start_source(self) -> None:
            started.set()
            await asyncio.Event().wait()

    client = _Client()
    connector = _Conn(client, FakeSource(), sleep=_instant_sleep)
    run_task = asyncio.ensure_future(connector.run(source="fake", account_id=1))
    await asyncio.wait_for(started.wait(), timeout=5)

    connector._trigger_exit(0)  # то же, что делает обработчик SIGTERM  # noqa: SLF001

    assert await asyncio.wait_for(run_task, timeout=5) == 0
    assert client.closed


async def test_1a_acc_hanging_on_resume_does_not_freeze_sleep_detector(monkeypatch) -> None:
    """Зависший `on_resume()` навсегда останавливал детектор сна — следующие пробуждения не ловились."""
    monkeypatch.setattr("hub.connectors.base._SEND_TIMEOUT_S", 0.01)
    wall = [1000.0]
    mono = [0.0]

    class _Conn(FakeConnector):
        async def on_resume(self) -> None:
            await asyncio.Event().wait()

    connector = _Conn(
        _Client(), FakeSource(), wall=lambda: wall[0], mono=lambda: mono[0], sleep=_instant_sleep
    )
    detector = asyncio.ensure_future(connector._sleep_detector_loop())  # noqa: SLF001
    await asyncio.sleep(0)  # детектор снял исходные показания часов
    wall[0] += 8 * 3600  # «проспали» 8 часов: monotonic стоит
    try:
        reason = await asyncio.wait_for(connector._sync_queue.get(), timeout=5)  # noqa: SLF001
        assert reason == "resume"
    finally:
        connector._stopping = True  # noqa: SLF001
        detector.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await detector


@pytest.mark.parametrize(("status", "expected"), [(None, 0), (500, 0), (401, 78)])
def test_1a_acc_hub_connector_exit_code_when_core_not_ready(monkeypatch, tmp_path, status, expected) -> None:
    """Гонка при загрузке ноутбука: коннектор стартовал раньше, чем core открыл сокет. 78 юнит НЕ
    перезапускает (RestartPreventExitStatus) — 78 только на 401 (секрет отозван). Ф99 (28.10): сеть и 5xx —
    коннектор сам ждёт ядро с повтором (`_connector_wait_account`), а не выходит с 1 (systemd упирался в
    предел перезапусков); SIGTERM во время ожидания — выход 0."""
    import os
    import signal

    from hub import cli

    monkeypatch.setenv("HUB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HUB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(cli, "_CONNECTOR_WAIT_STEPS_S", (0.01,))
    key_dir = tmp_path / "config" / "connectors"
    key_dir.mkdir(parents=True)
    (key_dir / "7.key").write_text("hubc_x", encoding="utf-8")
    calls = []

    async def _get_account(self):
        calls.append(1)
        if len(calls) == 3:
            os.kill(os.getpid(), signal.SIGTERM)  # ждёт и повторяет — останавливаем, как systemd
        request = httpx.Request("GET", "http://hub-internal/internal/v1/account")
        if status is None:
            raise httpx.ConnectError("нет сокета", request=request)
        raise httpx.HTTPStatusError(
            str(status), request=request, response=httpx.Response(status, request=request)
        )

    monkeypatch.setattr("hub.connectors.base.CoreClient.get_account", _get_account)
    monkeypatch.setattr(sys, "argv", ["hub", "connector", "7"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == expected
    assert len(calls) == (1 if status == 401 else 3)


def test_1a_acc_heartbeat_does_not_erase_limited_reason(tmp_path) -> None:
    """Пульс `ok` у `limited` аккаунта затирал `last_error` (`PeerFlood`) — причина ограничения пропадала."""
    from hub.core.db import Db

    db = Db(tmp_path)
    db.start()
    try:

        def _fn(conn):
            cur = conn.execute("INSERT INTO main.account (source, db) VALUES ('fake', 'main')")
            account_id = cur.lastrowid
            admin.set_account_status(conn, account_id, status="limited", last_error="PeerFlood", now_ms=1)
            admin.set_account_status(conn, account_id, status="ok", last_error=None, now_ms=2)
            return conn.execute(
                "SELECT status, last_error, last_ok_ts FROM main.account WHERE id = ?", (account_id,)
            ).fetchone()

        assert db.write_sync(_fn) == ("limited", "PeerFlood", 2)
    finally:
        db.close()


# --- после opus-критика (22.09.2026) ---------------------------------------------------------------------


async def test_1a_crit1_wait_outbox_request_timeout_exceeds_long_poll() -> None:
    """Умолчание httpx — 5 с, long-poll — 25 с: каждый wait падал ReadTimeout, отправка ждала до ~35 с."""
    from hub.connectors.base import CoreClient

    seen: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.path] = request.extensions["timeout"]
        if request.url.path.endswith("/outbox/wait"):
            return httpx.Response(200, json={"ready": False})
        return httpx.Response(200, json={"ok": True})

    client = CoreClient(transport=httpx.MockTransport(_handler), secret="hubc_x")
    await client.wait_outbox(25)
    await client.set_status("ok")
    await client.aclose()

    assert seen["/internal/v1/outbox/wait"]["read"] > 25
    assert seen["/internal/v1/account/status"]["read"] > 5


async def test_1a_crit3_unserializable_event_is_dropped_pipeline_keeps_going() -> None:
    import datetime

    client = _Client()
    connector = FakeConnector(client, FakeSource(), sleep=_instant_sleep)
    connector.emit({"ts": datetime.datetime(2026, 9, 22)})  # сырой объект источника — ошибка коннектора
    connector.emit({"n": 1})
    connector._stopping = True  # noqa: SLF001

    await asyncio.wait_for(connector._pusher_loop(), timeout=5)  # noqa: SLF001

    assert client.received == [{"n": 1}]
    assert connector._sync_queue.get_nowait() == "overflow"  # noqa: SLF001


async def test_1a_crit7_overflow_while_batch_in_flight_keeps_new_events() -> None:
    """Пока пачка в полёте, буфер переполнился и очистился; удаление «по длине пачки» стирало новые."""
    gate = asyncio.Event()

    class _SlowClient(_Client):
        async def push_events(self, events, cursor=None):
            if not self.received and not gate.is_set():
                await gate.wait()
            return await super().push_events(events, cursor)

    client = _SlowClient()
    connector = FakeConnector(client, FakeSource(), sleep=_instant_sleep)
    for i in range(10):
        connector.emit({"old": i})
    pusher = asyncio.ensure_future(connector._pusher_loop())  # noqa: SLF001
    await asyncio.sleep(0)  # пачка old-* ушла в полёт
    for i in range(5001):  # переполнение: буфер очищен, дальше новые
        connector.emit({"x": i})
    for i in range(3):
        connector.emit({"new": i})
    gate.set()
    connector._stopping = True  # noqa: SLF001
    await asyncio.wait_for(pusher, timeout=5)

    assert [e for e in client.received if "new" in e] == [{"new": 0}, {"new": 1}, {"new": 2}]


class _LeaseClient(_Client):
    def __init__(self, leases: list[dict]) -> None:
        super().__init__()
        self._leases = leases
        self.reports: list[dict] = []

    async def lease(self, **_kw) -> list[dict]:
        return self._leases

    async def report(self, **kw) -> dict:
        self.reports.append(kw)
        return {"ok": True}


def _leases(n: int, *, reconcile_at: int | None = None) -> list[dict]:
    return [
        {"outbox_id": i, "lease_gen": 1, "mode": "reconcile" if i == reconcile_at else "send"}
        for i in range(n)
    ]


async def test_1a_crit8_flood_stops_the_rest_of_the_batch() -> None:
    sent: list[int] = []

    class _Conn(FakeConnector):
        async def send(self, lease):
            from hub.connectors.base import SendResult

            sent.append(lease["outbox_id"])
            return SendResult("flood", retry_after_ms=30_000)

    client = _LeaseClient(_leases(4, reconcile_at=2))
    connector = _Conn(client, FakeSource(), sleep=_instant_sleep)

    await connector.step_outbox()

    assert sent == [0]
    # остаток: send-лизы возвращены как flood с тем же сроком, reconcile-лиз не тронут (истечёт сам)
    assert [(r["outbox_id"], r["outcome"], r["retry_after_ms"]) for r in client.reports] == [
        (0, "flood", 30_000),
        (1, "flood", 30_000),
        (3, "flood", 30_000),
    ]


async def test_1a_crit8_mark_limited_stops_the_rest_of_the_batch() -> None:
    sent: list[int] = []

    class _Conn(FakeConnector):
        async def send(self, lease):
            from hub.connectors.base import SendResult

            sent.append(lease["outbox_id"])
            await self.mark_limited("PeerFlood")
            return SendResult("temporary")

    client = _LeaseClient(_leases(3))
    connector = _Conn(client, FakeSource(), sleep=_instant_sleep)

    await connector.step_outbox()

    assert sent == [0]
    assert client.statuses == ["limited"]
    assert [r["outcome"] for r in client.reports] == ["temporary", "flood", "flood"]


async def test_f69_peer_flood_stop_postpones_the_rest_without_account_pause() -> None:
    """Ф69-1 П5: после PeerFlood (`stop_batch(STOP_PEER_FLOOD)`) остаток пачки — `temporary` + `postponed`
    (попытка не тратится, `flood_until` аккаунта не ставится), а не `flood`; reconcile-лиз не тронут."""
    sent: list[int] = []

    class _Conn(FakeConnector):
        async def send(self, lease):
            from hub.connectors.base import STOP_PEER_FLOOD, SendResult

            sent.append(lease["outbox_id"])
            self.stop_batch(STOP_PEER_FLOOD)
            return SendResult("permanent", error="PeerFlood")

    client = _LeaseClient(_leases(4, reconcile_at=2))
    connector = _Conn(client, FakeSource(), sleep=_instant_sleep)

    await connector.step_outbox()

    assert sent == [0]
    assert client.statuses == []  # коннектор `limited` больше не ставит
    assert [
        (r["outbox_id"], r["outcome"], r.get("error"), r.get("retry_after_ms"), r.get("postponed", False))
        for r in client.reports
    ] == [
        (0, "permanent", "PeerFlood", None, False),
        (1, "temporary", None, 60_000, True),
        (3, "temporary", None, 60_000, True),
    ]

    # следующая пачка без PeerFlood — прежний режим (`flood` у остатка после FLOOD_WAIT)
    class _Flood(FakeConnector):
        async def send(self, lease):
            from hub.connectors.base import SendResult

            return SendResult("flood", retry_after_ms=30_000)

    client.reports.clear()
    connector.__class__ = _Flood
    await connector.step_outbox()
    assert [r["outcome"] for r in client.reports] == ["flood", "flood", "flood"]


async def test_1a_crit4_heartbeat_fires_right_after_resume() -> None:
    """Пульс ждал свои 60 с monotonic после пробуждения — hub-watch успевал крикнуть «коннектор молчит»."""
    wall = [1000.0]
    beats: list[float] = []

    class _Conn(FakeConnector):
        async def health(self) -> None:
            beats.append(wall[0])

    never = asyncio.Event()

    async def _sleep(seconds: float) -> None:
        if seconds >= 60:  # интервал пульса — «никогда» в рамках теста
            await never.wait()
        await asyncio.sleep(0)

    connector = _Conn(_Client(), FakeSource(), wall=lambda: wall[0], mono=lambda: 0.0, sleep=_sleep)
    heartbeat = asyncio.ensure_future(connector._heartbeat_loop())  # noqa: SLF001
    detector = asyncio.ensure_future(connector._sleep_detector_loop())  # noqa: SLF001
    try:
        for _ in range(20):
            await asyncio.sleep(0)
        assert len(beats) == 1  # стартовый пульс
        wall[0] += 8 * 3600
        for _ in range(50):
            await asyncio.sleep(0)
            if len(beats) == 2:
                break
        assert len(beats) == 2
    finally:
        connector._stopping = True  # noqa: SLF001
        for t in (heartbeat, detector):
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t


async def test_1a_crit_clock_back_requests_sync_without_reconnect() -> None:
    wall = [1000.0]
    resumed: list[bool] = []

    class _Conn(FakeConnector):
        async def on_resume(self) -> None:
            resumed.append(True)

    connector = _Conn(_Client(), FakeSource(), wall=lambda: wall[0], mono=lambda: 0.0, sleep=_instant_sleep)
    detector = asyncio.ensure_future(connector._sleep_detector_loop())  # noqa: SLF001
    await asyncio.sleep(0)
    wall[0] -= 3600
    try:
        assert await asyncio.wait_for(connector._sync_queue.get(), timeout=5) == "clock"  # noqa: SLF001
        assert resumed == []
    finally:
        connector._stopping = True  # noqa: SLF001
        detector.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await detector


@pytest.mark.parametrize("sequence", [["no_network", "ok"], ["auth_required", "ok"], ["stale", "ok"]])
def test_1a_crit2_limited_survives_any_connector_status(tmp_path, sequence) -> None:
    """limited -> no_network (сон/прокси) -> ok молча возобновлял отправку под спам-ограничением."""
    from hub.core.db import Db

    db = Db(tmp_path)
    db.start()
    try:

        def _fn(conn):
            account_id = conn.execute(
                "INSERT INTO main.account (source, db) VALUES ('fake', 'main')"
            ).lastrowid
            admin.set_account_status(conn, account_id, status="limited", last_error="PeerFlood", now_ms=1)
            for status in sequence:
                admin.set_account_status(conn, account_id, status=status, last_error="x", now_ms=2)
            before = conn.execute("SELECT status, last_error FROM main.account WHERE id = ?", (account_id,))
            state = before.fetchone()
            admin.resume_account(conn, account_id)
            after = conn.execute("SELECT status FROM main.account WHERE id = ?", (account_id,)).fetchone()
            return state, after

        state, after = db.write_sync(_fn)
        assert state == ("limited", "PeerFlood")
        assert after == ("ok",)
    finally:
        db.close()


def test_1a_crit2_invalid_status_still_rejected_while_limited(tmp_path) -> None:
    import sqlite3

    from hub.core.db import Db

    db = Db(tmp_path)
    db.start()
    try:

        def _fn(conn):
            account_id = conn.execute(
                "INSERT INTO main.account (source, db) VALUES ('fake', 'main')"
            ).lastrowid
            admin.set_account_status(conn, account_id, status="limited", last_error=None, now_ms=1)
            admin.set_account_status(conn, account_id, status="bogus", last_error=None, now_ms=2)

        with pytest.raises(sqlite3.IntegrityError):
            db.write_sync(_fn)
    finally:
        db.close()


def test_1a_crit6_work_account_folder_label_not_stored_in_main(tmp_path) -> None:
    from hub.core import mapping
    from hub.core.db import Db

    db = Db(tmp_path)
    db.start()
    try:

        def _fn(conn):
            account_id = conn.execute(
                "INSERT INTO main.account (source, db) VALUES ('slack', 'work')"
            ).lastrowid
            project_id = conn.execute("INSERT INTO main.project (name) VALUES ('P')").lastrowid
            match = {"attr": "tg_folders", "has": 5, "label": "папка «Сделка»"}
            mapping.add_rule(
                conn, account_id=account_id, project_id=project_id, match=match, priority=0, now_ms=1
            )
            return conn.execute("SELECT match_json FROM main.map_rule").fetchone()[0]

        assert "Сделка" not in db.write_sync(_fn)
    finally:
        db.close()
