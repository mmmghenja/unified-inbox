"""Ф51-1 A (docs/architecture.md): IDLE обычного ящика — 9 минут без нового входа и без WARNING
при плановом выходе; невнятный отказ входа (`other`) после удачного входа — три повтора с паузами 30 с / 2 мин
/ 5 мин и только потом `auth`; `bad_password` и отказ при первом входе процесса — сразу."""

from __future__ import annotations

import asyncio
import imaplib
import logging
import threading
import time

import pytest

from hub.connectors.mail import gateway as gw_mod
from hub.connectors.mail.gateway import MailAuthError, MailLoginRetry

from .conftest import Mono, make_gateway
from .fakeplain import FakePlain
from .test_plain import make_plain_gateway


class _Refusing:
    """Обёртка фабрики соединений: пока в `errors` есть ошибки, очередной `LOGIN` их поднимает."""

    def __init__(self, srv: FakePlain) -> None:
        self._srv = srv
        self.errors: list[BaseException] = []
        self.logins = 0

    def __call__(self, timeout: float):
        conn = self._srv.imap_factory(timeout)
        real_login = conn.login

        def _login(user: str, password: str):
            self.logins += 1
            if self.errors:
                raise self.errors.pop(0)
            return real_login(user, password)

        conn.login = _login
        return conn


def _other() -> imaplib.IMAP4.error:
    return imaplib.IMAP4.error(b"[UNKNOWN] login is not possible now")


def _bad_password() -> imaplib.IMAP4.error:
    return imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Authentication failed.")


async def _until(pred, limit: float = 5.0) -> None:
    deadline = time.monotonic() + limit
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("условие не наступило")
        await asyncio.sleep(0.01)


def test_idle_duration_default_by_flavor(gm):
    # Ф99-3: таймер простоя прокси — 300 с без байта; заход IDLE у всех ящиков не длиннее 240 с
    assert gw_mod.PLAIN_IDLE_DURATION_S == 240.0 and gw_mod.IDLE_DURATION_S == 240.0
    assert make_plain_gateway(FakePlain()).idle_duration_s <= 240.0
    assert make_gateway(gm).idle_duration_s <= 240.0
    assert make_plain_gateway(FakePlain(), idle_duration_s=5.0).idle_duration_s == 5.0


async def test_plain_idle_planned_exit_no_relogin_no_warning(caplog):
    srv = FakePlain(idle=True)
    srv.idle_real_max_s = 0.02
    srv.on_idle_timeout = lambda _duration: None
    gw = make_plain_gateway(srv)
    events: list[str] = []
    with caplog.at_level(logging.INFO, logger="hub.connectors.mail.gateway"):
        gw.start_listener(lambda kind, _detail: events.append(kind))
        try:
            await _until(lambda: len(srv.idle_durations) >= 3)
        finally:
            await gw.close()
    assert set(srv.idle_durations) == {240.0}
    idle_conns = {name for name, cmd in srv.commands if cmd == "IDLE"}
    assert len(idle_conns) == 1  # заходы IDLE — на одном и том же соединении
    assert sum(1 for _n, cmd in srv.commands if cmd.startswith("LOGIN")) == 1
    assert events.count("connected") == 1 and "auth" not in events
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_other_after_good_login_retries_three_times_then_auth(caplog):
    srv = FakePlain()
    factory = _Refusing(srv)
    mono = Mono()
    gw = make_plain_gateway(srv, imap_factory=factory, mono=mono)
    gw.open_connection("work")  # удачный вход в этом процессе
    logins = factory.logins

    factory.errors = [_other() for _ in range(4)]
    with caplog.at_level(logging.WARNING, logger="hub.connectors.mail.gateway"):
        for attempt, pause in enumerate((30.0, 120.0, 300.0), start=1):
            with pytest.raises(MailLoginRetry) as ei:
                gw.open_connection("listener")
            assert (ei.value.attempt, ei.value.wait_s) == (attempt, pause)
            logins += 1
            assert factory.logins == logins
            # пауза ещё идёт — в сеть со входом не ходим (и другое соединение тоже)
            mono.advance(pause - 1)
            with pytest.raises(MailLoginRetry) as ei:
                gw.open_connection("work")
            assert ei.value.wait_s == pytest.approx(1.0)
            assert factory.logins == logins
            mono.advance(1)
        with pytest.raises(MailAuthError) as auth:
            gw.open_connection("listener")
    assert auth.value.kind == "other"
    assert factory.logins == logins + 1
    assert len([r for r in caplog.records if "повтор" in r.getMessage()]) == 3
    # счётчик не сбрасывается отказом: следующий невнятный отказ — снова сразу `auth`
    factory.errors = [_other()]
    with pytest.raises(MailAuthError):
        gw.open_connection("listener")


def test_good_login_resets_retry_counter():
    srv = FakePlain()
    factory = _Refusing(srv)
    mono = Mono()
    gw = make_plain_gateway(srv, imap_factory=factory, mono=mono)
    gw.open_connection("work")
    for _round in range(3):
        factory.errors = [_other(), _other()]
        for pause in (30.0, 120.0):
            with pytest.raises(MailLoginRetry) as ei:
                gw.open_connection("listener")
            assert ei.value.wait_s == pause
            mono.advance(pause)
        gw.open_connection("listener")  # удачный вход — счётчик с нуля


def test_bad_password_after_good_login_is_immediate():
    srv = FakePlain()
    factory = _Refusing(srv)
    gw = make_plain_gateway(srv, imap_factory=factory, mono=Mono())
    gw.open_connection("work")
    factory.errors = [_bad_password()]
    with pytest.raises(MailAuthError) as ei:
        gw.open_connection("listener")
    assert ei.value.kind == "bad_password"


def test_other_on_first_login_is_immediate():
    srv = FakePlain()
    factory = _Refusing(srv)
    gw = make_plain_gateway(srv, imap_factory=factory, mono=Mono())
    factory.errors = [_other()]
    with pytest.raises(MailAuthError) as ei:
        gw.open_connection("work")
    assert ei.value.kind == "other"


def _advancing_wait(mono: Mono, waits: list[float]):
    def _wait(event: threading.Event, seconds: float) -> None:
        waits.append(seconds)
        mono.advance(seconds)
        event.wait(0.001)

    return _wait


async def test_listener_retries_with_pauses_then_reports_auth():
    srv = FakePlain(idle=True)
    factory = _Refusing(srv)
    mono = Mono()
    waits: list[float] = []
    gw = make_plain_gateway(srv, imap_factory=factory, mono=mono, listener_wait=_advancing_wait(mono, waits))
    await gw.work.connect()
    factory.errors = [_other() for _ in range(4)]
    events: list[tuple[str, str | None]] = []
    gw.start_listener(lambda kind, detail: events.append((kind, detail)))
    try:
        await _until(lambda: ("auth", "other") in events)
    finally:
        await gw.close()
    assert waits == [30.0, 120.0, 300.0]
    assert [k for k, _d in events] == ["auth"]


async def test_listener_recovers_on_retry_without_auth():
    srv = FakePlain(idle=True)
    factory = _Refusing(srv)
    mono = Mono()
    waits: list[float] = []
    gw = make_plain_gateway(srv, imap_factory=factory, mono=mono, listener_wait=_advancing_wait(mono, waits))
    await gw.work.connect()
    factory.errors = [_other()]
    events: list[str] = []
    gw.start_listener(lambda kind, _detail: events.append(kind))
    try:
        await _until(lambda: "connected" in events)
    finally:
        await gw.close()
    assert waits[0] == 30.0
    assert "auth" not in events


async def test_listener_stop_breaks_retry_pause():
    srv = FakePlain(idle=True)
    factory = _Refusing(srv)
    entered = threading.Event()

    def _wait(event: threading.Event, seconds: float) -> None:
        entered.set()
        event.wait(seconds)  # настоящая пауза 30 с — её должна порвать остановка

    gw = make_plain_gateway(srv, imap_factory=factory, listener_wait=_wait)
    await gw.work.connect()
    factory.errors = [_other()]
    gw.start_listener(lambda _kind, _detail: None)
    assert await asyncio.to_thread(entered.wait, 5)
    started = time.monotonic()
    await gw.close()
    assert time.monotonic() - started < 3


def test_two_connections_refused_at_once_burn_one_retry():
    """Ревью Ф51-1 A3: слушатель и рабочее соединение переподключаются разом и получают один и тот же отказ
    — это один сбой сервера: повтор считается один раз, второму — остаток той же паузы."""
    srv = FakePlain()
    barrier = threading.Barrier(2, timeout=5)
    state = {"refuse": False, "logins": 0}

    def factory(timeout: float):
        conn = srv.imap_factory(timeout)
        real_login = conn.login

        def _login(user, password):
            if not state["refuse"]:
                return real_login(user, password)
            state["logins"] += 1
            barrier.wait()  # оба соединения уже прошли проверку паузы и стоят на LOGIN
            raise _other()

        conn.login = _login
        return conn

    gw = make_plain_gateway(srv, imap_factory=factory, mono=Mono())
    gw.open_connection("work")  # удачный вход процесса
    state["refuse"] = True
    got: list[MailLoginRetry] = []

    def _open(name):
        try:
            gw.open_connection(name)
        except MailLoginRetry as exc:
            got.append(exc)

    threads = [threading.Thread(target=_open, args=(n,)) for n in ("listener", "work")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert state["logins"] == 2 and len(got) == 2
    assert [e.attempt for e in got] == [1, 1]
    assert [e.wait_s for e in got] == [gw_mod.LOGIN_RETRY_PAUSES_S[0]] * 2
    assert gw._login_retries == 1  # noqa: SLF001


async def test_f99_work_session_noop_keeps_connection_without_new_login():
    """Ф99-3: рабочее соединение в простое получает NOOP раз в `keepalive_s`, нового входа нет."""
    srv = FakePlain()
    gw = make_plain_gateway(srv)
    gw.keepalive_s = 0.05
    try:
        await gw.work.examine()
        logins = sum(1 for _n, cmd in srv.commands if cmd.startswith("LOGIN"))
        noops = sum(1 for _n, cmd in srv.commands if cmd == "NOOP")
        await _until(lambda: sum(1 for _n, cmd in srv.commands if cmd == "NOOP") >= noops + 3)
        assert sum(1 for _n, cmd in srv.commands if cmd.startswith("LOGIN")) == logins
    finally:
        await gw.close()
    assert gw_mod.KEEPALIVE_S <= 240.0


async def test_f99_listener_drop_log_has_age_and_phase(caplog):
    srv = FakePlain(idle=True)
    srv.idle_real_max_s = 5.0
    gw = make_plain_gateway(srv)
    with caplog.at_level(logging.INFO, logger="hub.connectors.mail.gateway"):
        gw.start_listener(lambda *_a: None)
        try:
            await _until(lambda: any(cmd == "IDLE" for _n, cmd in srv.commands))
            await asyncio.sleep(0.1)
            lst = gw._listener
            lst._phase = "ждали в IDLE"
            lst._imap.sock_shutdown = True
            gw_mod._shutdown_socket(lst._imap)
            await _until(lambda: any("слушатель оборвался" in r.getMessage() for r in caplog.records))
        finally:
            await gw.close()
    line = next(r.getMessage() for r in caplog.records if "слушатель оборвался" in r.getMessage())
    assert "возраст соединения" in line and "фаза:" in line
    assert any("входов за сутки: 1" in r.getMessage() for r in caplog.records)
