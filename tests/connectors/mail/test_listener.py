"""Слушатель IDLE и жизненный цикл (спека 5a, тест 8 и поправки М10, М12, М13): настоящие потоки шлюза,
`FakeGmail` на месте соединения `imaplib`."""

from __future__ import annotations

import asyncio
import imaplib
import os
import signal
import threading
import time

import pytest

from .conftest import Mono, make_connector
from .helpers import incoming, messages


class Waits:
    """`listener_wait`: пишет запрошенные паузы, реально ждёт чуть-чуть (прерывается `event`)."""

    def __init__(self) -> None:
        self.values: list[float] = []

    def __call__(self, event: threading.Event, seconds: float) -> None:
        self.values.append(seconds)
        event.wait(0.01)


async def _until(pred, limit: float = 5.0) -> None:
    deadline = time.monotonic() + limit
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("условие не наступило")
        await asyncio.sleep(0.01)


def _queued(connector) -> list[str]:
    out = []
    q = connector._sync_queue  # noqa: SLF001
    while not q.empty():
        out.append(q.get_nowait())
    return out


async def _wait_reason(connector, reason: str, limit: float = 5.0) -> list[str]:
    """Сигнал слушателя идёт из его потока через `call_soon_threadsafe` — ждать, пока причина дойдёт."""
    seen: list[str] = []
    deadline = time.monotonic() + limit
    while reason not in seen:
        seen += _queued(connector)
        if reason in seen:
            break
        if time.monotonic() > deadline:
            raise AssertionError(f"причина {reason!r} не пришла: {seen}")
        await asyncio.sleep(0.01)
    return seen


def _listener_conns(gm) -> set[str]:
    return {name for name, cmd in gm.commands if cmd == "IDLE"}


@pytest.fixture
async def made():
    items = []
    yield items
    for gw in items:
        await gw.close()


async def test_8_exists_triggers_catch_up_on_work_connection(admin_client, internal_transport, gm, db, made):
    connector, gw, _client, _aid = await make_connector(admin_client, internal_transport, gm)
    made.append(gw)
    await connector.start_source()
    await connector.sync("start")
    await _until(gm.idle_started.is_set)
    _queued(connector)

    msg = incoming(gm)
    gm.idle_queue.put(("EXISTS", [b"1"]))
    await _until(lambda: not connector._sync_queue.empty())  # noqa: SLF001
    await _wait_reason(connector, "live")
    await connector.sync("live")
    assert [r["ext_id"] for r in messages(db)] == [msg.message_id]

    listener = _listener_conns(gm)
    fetchers = {name for name, cmd in gm.commands if "BODY.PEEK[]" in cmd}
    assert fetchers and not (fetchers & listener)  # догон — не на соединении слушателя


async def test_m12_fetch_flags_in_idle_is_signal(admin_client, internal_transport, gm, made):
    connector, gw, _client, _aid = await make_connector(admin_client, internal_transport, gm)
    made.append(gw)
    await connector.start_source()
    await _until(gm.idle_started.is_set)
    _queued(connector)
    gm.idle_queue.put(("FETCH", [b"3 (FLAGS (\\Seen))"]))
    await _until(lambda: not connector._sync_queue.empty())  # noqa: SLF001
    assert await _wait_reason(connector, "live") == ["live"]


async def test_8_listener_drop_reconnects_with_backoff(admin_client, internal_transport, gm, made):
    waits = Waits()
    connector, gw, _client, _aid = await make_connector(
        admin_client, internal_transport, gm, gw_kwargs={"listener_wait": waits, "rng": lambda: 0.5}
    )
    made.append(gw)
    gm.connect_errors = [OSError("down"), OSError("down"), OSError("down")]
    await connector.start_source()  # рабочее соединение съест первую ошибку — старт без связи не падает
    await _until(gm.idle_started.is_set)
    assert waits.values[:2] == [1.0, 2.0]  # 1…60 с, удвоение (джиттер нулевой при rng=0.5)

    n_before = len(_listener_conns(gm))
    gm.idle_started.clear()
    gm.idle_queue.put(imaplib.IMAP4.abort("socket error: EOF"))
    await _until(lambda: len(_listener_conns(gm)) > n_before)
    await _wait_reason(connector, "reconnect")


async def test_8_backoff_caps_at_60s_and_throttle_waits_30min(admin_client, internal_transport, gm, made):
    waits = Waits()
    connector, gw, _client, _aid = await make_connector(
        admin_client, internal_transport, gm, gw_kwargs={"listener_wait": waits, "rng": lambda: 0.5}
    )
    made.append(gw)
    gm.connect_errors = [OSError("x")] * 9
    gw.start_listener(lambda *_a: None)
    await _until(lambda: len(waits.values) >= 9)
    assert waits.values[:9] == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0]
    await gw.stop_listener()

    waits.values.clear()
    gm.login_error = b"[ALERT] Too many simultaneous connections. (Failure)"
    gw.start_listener(lambda *_a: None)
    await _until(lambda: len(waits.values) >= 1)
    assert waits.values[0] == 30 * 60.0
    await gw.stop_listener()


async def test_8_forced_reconnect_every_23h(admin_client, internal_transport, gm, made):
    mono = Mono()
    gm.idle_real_max_s = 0.005
    gm.on_idle_timeout = mono.advance
    connector, gw, _client, _aid = await make_connector(
        admin_client, internal_transport, gm, gw_kwargs={"mono": mono}
    )
    made.append(gw)
    await connector.start_source()
    await _until(lambda: len(_listener_conns(gm)) >= 2, limit=20)
    assert max(gm.idle_durations) <= 240.0  # Ф99-3: таймер простоя прокси 300 с
    first_conn_idles = [d for d in gm.idle_durations[:400]]
    assert sum(first_conn_idles[:345]) >= 23 * 3600 - 1  # второе подключение — через 23 ч
    await _wait_reason(connector, "reconnect")


async def test_8_on_resume_reconnects_everything(admin_client, internal_transport, gm, made):
    connector, gw, _client, _aid = await make_connector(admin_client, internal_transport, gm)
    made.append(gw)
    await connector.start_source()
    await connector.sync("start")
    await _until(gm.idle_started.is_set)
    conns_before = len(gm.connections)
    listeners_before = len(_listener_conns(gm))

    await connector.on_resume()
    await _until(lambda: len(_listener_conns(gm)) > listeners_before)
    await _wait_reason(connector, "reconnect")
    await connector.sync("resume")
    # рабочее соединение пересоздано (старое убито), слушатель — тоже
    assert len(gm.connections) >= conns_before + 2
    assert all(c.killed or c.closed for c in gm.connections[:conns_before])


async def test_8_listener_auth_failure_leads_to_auth_required(admin_client, internal_transport, gm, made):
    connector, gw, _client, _aid = await make_connector(admin_client, internal_transport, gm)
    made.append(gw)
    await connector.start_source()
    await _until(gm.idle_started.is_set)
    gm.login_error = b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)"
    gm.idle_queue.put(imaplib.IMAP4.abort("socket error: EOF"))
    await _until(lambda: connector._auth_failed is not None)  # noqa: SLF001
    from hub.connectors.base import AuthRequired

    with pytest.raises(AuthRequired):
        await connector.health()


async def test_start_source_auth_error_is_auth_required(admin_client, internal_transport, gm, made):
    from hub.connectors.base import AuthRequired

    connector, gw, _client, _aid = await make_connector(
        admin_client, internal_transport, gm, gw_kwargs={"password": "wrong"}
    )
    made.append(gw)
    with pytest.raises(AuthRequired):
        await connector.start_source()


async def test_start_source_not_gmail_is_auth_required(admin_client, internal_transport, gm, made):
    from hub.connectors.base import AuthRequired

    gm.capabilities.remove("X-GM-EXT-1")
    connector, gw, _client, _aid = await make_connector(admin_client, internal_transport, gm)
    made.append(gw)
    with pytest.raises(AuthRequired):
        await connector.start_source()


# --- М13: health --------------------------------------------------------------------------------------------


async def test_m13_health_no_network_after_2min_then_ok(admin_client, internal_transport, gm, made):
    wall_value = [1_790_000_000.0]

    def wall() -> float:
        return wall_value[0]

    connector, gw, client, account_id = await make_connector(
        admin_client, internal_transport, gm, gw_kwargs={"wall": wall}, wall=wall
    )
    made.append(gw)
    gm.connect_errors = [OSError("down")] * 1000
    await connector.start_source()
    await connector.health()  # только что упал — ещё не 2 мин
    wall_value[0] += 121
    with pytest.raises(ConnectionError):
        await connector.health()
    r = await admin_client.get("/internal/v1/admin/status")
    assert next(a for a in r.json()["accounts"] if a["id"] == account_id)["status"] == "no_network"

    gm.connect_errors = []
    await _until(gm.idle_started.is_set)
    await connector.health()  # слушатель подключён — здоров
    before = len(gm.commands)
    await connector.health()
    assert len(gm.commands) - before <= 2  # health по рабочему соединению не ходит (разве что IDLE/DONE)
    assert not any("SEARCH" in c or "FETCH" in c for _n, c in gm.commands[before:])


# --- М10: SIGTERM при висящем IDLE --------------------------------------------------------------------------


async def test_m10_sigterm_with_hanging_idle_exits_fast(admin_client, internal_transport, gm):
    gm.idle_real_max_s = 3600.0  # IDLE висит «вечно»
    connector, gw, _client, account_id = await make_connector(admin_client, internal_transport, gm)
    connector._sleep = asyncio.sleep  # noqa: SLF001 — настоящие паузы каркаса
    task = asyncio.ensure_future(connector.run(source="mail", account_id=account_id))
    await _until(gm.idle_started.is_set, limit=10)
    listener_thread = gw._listener._thread  # noqa: SLF001
    assert listener_thread.is_alive()
    started = time.monotonic()
    os.kill(os.getpid(), signal.SIGTERM)
    code = await asyncio.wait_for(task, timeout=10)
    assert code == 0
    assert time.monotonic() - started < 10
    assert not listener_thread.is_alive()
    assert listener_thread.daemon


# --- рабочие соединения: простой и обрыв ----------------------------------------------------------------


async def test_session_retries_once_on_reused_dead_connection(gm, made):
    from .conftest import make_gateway

    gw = make_gateway(gm)
    made.append(gw)
    incoming(gm)
    assert await gw.sender.search_thread(gm.messages[0].thrid) == [1]
    first = gm.connections[-1]
    first.closed = True  # Gmail закрыл простаивавшее соединение
    assert await gw.sender.search_thread(gm.messages[0].thrid) == [1]  # прозрачный повтор на новом
    assert gm.connections[-1] is not first


async def test_session_fresh_connection_failure_not_retried(gm, made):
    from hub.connectors.mail.gateway import MailUnavailable

    from .conftest import make_gateway

    gw = make_gateway(gm)
    made.append(gw)

    orig = gm.search
    calls = []

    def _boom(args):
        calls.append(args)
        raise imaplib.IMAP4.abort("socket error: EOF")

    gm.search = _boom
    with pytest.raises(MailUnavailable):
        await gw.sender.search_thread(1)
    assert len(calls) == 1  # только что открытое соединение — без повтора
    gm.search = orig


async def test_session_reconnects_after_20min_idle(gm, made):
    from .conftest import make_gateway

    mono = Mono()
    gw = make_gateway(gm, mono=mono)
    made.append(gw)
    await gw.work.examine()
    n = len(gm.connections)
    mono.advance(19 * 60)
    await gw.work.examine()
    assert len(gm.connections) == n
    mono.advance(21 * 60)
    await gw.work.examine()
    assert len(gm.connections) == n + 1
    assert gm.connections[n - 1].closed  # старое закрыто до нового (лимит соединений Gmail)
