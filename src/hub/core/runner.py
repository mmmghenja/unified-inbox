"""Процесс `hub core`: два `uvicorn.Server` в ОДНОМ event loop — публичное приложение на `127.0.0.1:8787` и
внутреннее на unix-сокете `<data_dir>/internal.sock`. Общие `Db`, `Notifier`, `clock`. Спецификация:
docs/architecture.md. Ф10-7: у публичного приложения может быть
второй сокет — удалённый слушатель `127.0.0.1:<remote_port>` для туннеля с телефона (тот же `_Server`).
Ф14-9 (docs/architecture.md: есть пара `<config_dir>/tls/` — удалённый
сокет уходит во второй `_Server` с TLS (тот же `public_app`, `lifespan="off"`); пара битая — удалённый не
поднимается.

Защита от второго экземпляра — ДВЕ линии: `flock` на `<data_dir>/core.lock` (основная) и проверка живости
файла сокета (снимает частный случай — «мёртвый» файл от упавшего процесса не должен мешать новому старту,
а живой сокет — это тоже второй core, даже если по какой-то причине lock-файл не сработал).
"""

import asyncio
import contextlib
import fcntl
import logging
import os
import secrets
import signal
import socket
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import uvicorn

from ..diag import install_task_dump
from ..term import journal as term_journal
from ..term.config import TermConfig
from ..term.hub import TermHub
from . import backup as backup_mod
from . import card_route as card_route_mod
from . import chan_subs as chan_subs_mod
from . import decisions as decisions_mod
from . import draft_channels, held_files, hub_sessions, owner_queue, people_stats, retention
from . import human_requests as human_requests_mod
from . import interview as interview_mod
from . import machines as machines_mod
from . import media as media_mod
from . import outbox as outbox_mod
from . import peer_msgs as peer_msgs_mod
from . import people as people_mod
from . import presses as presses_mod
from . import reminders as reminders_mod
from . import session_host as session_host_mod
from . import subs as subs_mod
from . import tls as tls_mod
from . import uploads as uploads_mod
from .activity import Activity
from .app_internal import create_internal_app
from .app_public import DEFAULT_PORT, create_public_app
from .connector_auth import ADMIN_PREFIX, hash_secret, write_secret_file
from .db import Db, DbClosed
from .feed import Notifier
from .remote import RemoteState
from .tg_lookup import TgLookup

_logger = logging.getLogger(__name__)

SOCKET_NAME = "internal.sock"
LOCK_NAME = "core.lock"
ADMIN_KEY_NAME = "admin.key"

# Раздел F.3 спеки phase2-02-core-threads: тик ~60 с по НАСТЕННЫМ часам (`clock()`), не долгий
# `sleep(6 ч)` — monotonic-таймер asyncio стоит во время сна ноутбука, а важно именно настенное время.
_RETENTION_TICK_S = 60.0
_RETENTION_INTERVAL_MS = 6 * 60 * 60 * 1000
_RETENTION_INITIAL_DELAY_MS = 5 * 60 * 1000
_RETENTION_STOP_WAIT_S = 30.0  # < TimeoutStopSec юнита core (180 с): прогон выходит по флагу между порциями
_GRACEFUL_SHUTDOWN_S = 5  # Ф31-1 решение 6: uvicorn обрывает запросы, не закончившиеся за 5 с после остановки


async def _retention_loop(
    db: Db,
    clock: Callable[[], float],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    run_once=retention.run_once,
    stop_event: threading.Event | None = None,
    inflight: dict | None = None,
) -> None:
    """Планировщик ретенции work-БД: тик каждые `_RETENTION_TICK_S`, реальный прогон
    (`retention.run_once`, тяжёлая синхронная работа — в отдельном потоке, не блокирует event loop) —
    только если с прошлого успешного прошло `>= _RETENTION_INTERVAL_MS`; первый прогон процесса — не раньше
    `_RETENTION_INITIAL_DELAY_MS` после старта этого цикла (не грузить писатель сразу при подъёме ядра).
    `sleep`/`run_once` инжектируются — тот же приём, что `wall()/mono()/sleep()` у `ConnectorBase.run()`,
    тесты двигают `FakeClock` вместо ожидания реальных 60 с/5 мин/6 ч.

    Поправки после opus-критика кода (п.8/P1): `stop_event` — `threading.Event` останова, ПЕРЕДАЁТСЯ в
    `run_once`, который сам проверяет его между порциями (таблицы/vacuum) и выходит пораньше — если прогон
    уже идёт на крупной базе, `runner._serve_forever` не ждёт его до конца при остановке `hub core`.
    `runner._serve_forever` дополнительно ставит `stop_event` ПЕРЕД `task.cancel()` (нужен для мгновенного
    прерывания простаивающего тика, иначе остановка core ждала бы полный `_RETENTION_TICK_S`) — само по себе
    `.cancel()` не останавливает уже запущенный `asyncio.to_thread(run_once, …)` (обрывает только `await`
    вызывающей корутины, сам поток бежит дальше в фоне), поэтому попытка записи из уже «отменённого», но ещё
    бегущего `run_once` на закрытый `Db` обязана быть безопасной сама по себе (см. `Db._submit`, находка
    критика `test_run1`) — `stop_event` лишь СОКРАЩАЕТ окно этой гонки, не отменяет её необходимость."""
    stop_event = stop_event if stop_event is not None else threading.Event()
    started_ms = int(clock() * 1000)
    while not stop_event.is_set():
        await sleep(_RETENTION_TICK_S)
        if stop_event.is_set():
            break
        now_ms = int(clock() * 1000)
        if now_ms - started_ms < _RETENTION_INITIAL_DELAY_MS:
            continue
        try:
            with db.reader() as conn:
                last_run_ts = retention.get_last_run_ts(conn)
        except Exception:  # noqa: BLE001 — сбой чтения не должен убить планировщик, попробуем на следующем тике
            _logger.exception("retention: не удалось прочитать last_run_ts")
            continue
        # Поправки п.5 (R5): `0 <=` — если настенные часы позади прошлого прогона (транзитный скачок вперёд
        # и обратно, тест ret6), разница отрицательна и «< INTERVAL» была бы истинной ВСЕГДА — планировщик
        # застревал бы на месте той же длины, что и сам скачок, СИММЕТРИЧНО находке R5 в `run_once`. Опорное
        # время (R3) уже защищает содержимое — планировщик не обязан ждать так же.
        if last_run_ts is not None and 0 <= now_ms - last_run_ts < _RETENTION_INTERVAL_MS:
            continue
        # Приёмка 2b (архитектор): поток прогона — в `inflight["future"]`, а ждём его через `shield`: отмена
        # этой корутины при остановке ядра не бросает поток, `_serve_forever` дожидается его до `db.close()`.
        future = asyncio.ensure_future(asyncio.to_thread(run_once, db, now_ms, stop_event))
        if inflight is not None:
            inflight["future"] = future
        try:
            await asyncio.shield(future)
        except DbClosed:
            return
        except Exception:  # noqa: BLE001 — прогон не должен убить планировщик, попробуем на следующем тике
            _logger.exception("retention: прогон упал")


# Ф10-4 A7: очистка неотправленных загрузок — раз в час по настенным часам (тот же тик, что у ретенции).
_UPLOADS_TICK_S = 60.0
_UPLOADS_INTERVAL_MS = 60 * 60 * 1000


async def _uploads_cleanup_loop(
    db: Db,
    clock: Callable[[], float],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Ф10-4 A7 + П2: раз в час — `uploads.cleanup` (загрузки старше 24 ч без отправки: строка и файл по
    правилу ссылок) одной транзакцией писателя. Первый прогон — на первом тике после старта. Сбой прогона
    журналируется, цикл живёт дальше; закрытый `Db` — выход.

    Ф12-5 A4: в той же транзакции — уборка бесед-заготовок UI без живой строки старше суток
    (`draft_channels.cleanup_idle`, до очистки загрузок: загрузка тоже считается событием беседы, П8)."""
    last_run_ms: int | None = None
    while stop_event is None or not stop_event.is_set():
        await sleep(_UPLOADS_TICK_S)
        now_ms = int(clock() * 1000)
        if last_run_ms is not None and 0 <= now_ms - last_run_ms < _UPLOADS_INTERVAL_MS:
            continue
        try:
            archived, removed = await db.write(lambda conn, now_ms=now_ms: _hourly_cleanup(conn, now_ms))
        except DbClosed:
            return
        except Exception:  # noqa: BLE001 — сбой очистки не должен убить цикл, попробуем через час
            _logger.exception("uploads: очистка упала")
        else:
            if removed:
                _logger.info("uploads: удалено неотправленных загрузок: %s", removed)
            if archived:
                _logger.info("новые беседы: в архив без отправки за сутки: %s", archived)
        last_run_ms = now_ms


def _hourly_cleanup(conn, now_ms: int) -> tuple[int, int]:
    """Одна транзакция часового прохода: `(заготовок в архив, загрузок удалено)`."""
    # Ф34-3 П5: неподтверждённые черновики по номеру телефона старше 7 дней — отмена (заготовка сессии
    # уходит в архив, номер стирается); до уборки простаивающих заготовок.
    for outbox_id in draft_channels.expired_phone_drafts(conn, now_ms=now_ms):
        outbox_mod.cancel(conn, outbox_id, now_ms=now_ms)
    archived = draft_channels.cleanup_idle(conn, now_ms=now_ms)
    # Ф41-3в (4.1): байты файлов постоянных сессий — неприложенные, доставленные и снятые старше суток.
    held_files.cleanup(conn, now_ms=now_ms)
    # Ф44-1c (4.1): нажатия кнопок ботов — сроки, старые карточки и строки, обнуление у стёртых сообщений.
    presses_mod.cleanup(conn, now_ms=now_ms)
    # Ф54-1 (П1): заявки «нужен человек» — байты скриншотов и старые строки (сроки — минутный цикл).
    human_requests_mod.cleanup(conn, now_ms=now_ms)
    # Ф56-5 (2.3): журнал запусков в терминале — 30 дней.
    term_journal.cleanup(conn, now_ms=now_ms)
    return archived, uploads_mod.cleanup(conn, now_ms=now_ms)


# Ф21-1 B, D: люди — связывание раз в 10 минут (первое — через минуту после старта, на первом тике) и пересчёт
# статистики раз в час (первый — на том же тике, после связывания). Тик — по настенным часам, как у ретенции.
_PEOPLE_TICK_S = 60.0
_PEOPLE_LINK_INTERVAL_MS = 10 * 60 * 1000
_PEOPLE_STATS_INTERVAL_MS = 60 * 60 * 1000
_PEOPLE_STOP_WAIT_S = 10.0  # пересчёт выходит по флагу между беседами; дольше не ждём


def _due(last_ms: int | None, now_ms: int, interval_ms: int) -> bool:
    return last_ms is None or not 0 <= now_ms - last_ms < interval_ms


async def _people_loop(
    db: Db,
    clock: Callable[[], float],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    stop_event: threading.Event | None = None,
    inflight: dict | None = None,
    link=people_mod.link_all,
    recompute=people_stats.recompute,
) -> None:
    """Ф21-1: связывание (`people.link_all`, одна транзакция писателя) и часовой пересчёт статистики
    (`people_stats.recompute` — чтение своим `db.reader()` в потоке, флаг остановки `stop_event` проверяется
    между беседами, П10). Сбой — в журнал, цикл живёт дальше; закрытый `Db` — выход. Поток пересчёта — в
    `inflight["future"]`: `_serve_forever` дожидается его до `db.close()`, как прогона ретенции."""
    stop_event = stop_event if stop_event is not None else threading.Event()
    last_link: int | None = None
    last_stats: int | None = None
    while not stop_event.is_set():
        await sleep(_PEOPLE_TICK_S)
        if stop_event.is_set():
            break
        now_ms = int(clock() * 1000)
        if _due(last_link, now_ms, _PEOPLE_LINK_INTERVAL_MS):
            last_link = now_ms
            try:
                counts = await db.write(lambda conn, now_ms=now_ms: link(conn, now_ms))
            except DbClosed:
                return
            except Exception:  # noqa: BLE001 — сбой не должен убить цикл, попробуем через 10 минут
                _logger.exception("люди: связывание упало")
            else:
                if any(counts.values()):
                    _logger.info("люди: связывание %s", counts)
        if stop_event.is_set():
            break
        if _due(last_stats, now_ms, _PEOPLE_STATS_INTERVAL_MS):
            last_stats = now_ms
            future = asyncio.ensure_future(asyncio.to_thread(recompute, db, now_ms, stop_event))
            if inflight is not None:
                inflight["future"] = future
            try:
                await asyncio.shield(future)
            except DbClosed:
                return
            except Exception:  # noqa: BLE001 — сбой не должен убить цикл, попробуем через час
                _logger.exception("люди: пересчёт статистики упал")


# Ф13-1 B2: завершение подписок мёртвых сессий и отозванных токенов — раз в 60 с по настенным часам.
_SUBS_TICK_S = 60.0


def _cleanup_all(
    conn,
    now_ms: int,
    sub_waiters: subs_mod.SubWaiters | None = None,
    chan_sub_waiters: chan_subs_mod.ChanWaiters | None = None,
) -> list[tuple[str, str]]:
    """Ф14-5 A2: подписки на проект и на беседы (`c<id>`) — одним проходом, одной транзакцией. Ф16-4 (П1):
    учёт идущих ожиданий — для живости подписок OpenCode (ожидание идёт — подписка жива)."""
    # Ф41-2а (2.3): хост постоянных сессий не подтверждал состояние дольше `HOST_STALE_MS` — сессии хаба
    # `running`/`idle` переводятся в `asleep`. Подписки при этом не трогаются: спящая сессия жива.
    for hub_session_id in hub_sessions.stale_to_asleep(conn, now_ms=now_ms):
        _logger.info("сессия хаба #%s: хост молчит — asleep", hub_session_id)
    # Ф33-1: личности другой машины без связи дольше суток реестру больше не нужны (их подписки сняты).
    session_host_mod.current().prune(now_ms=now_ms)
    sub_waiting = sub_waiters.waiting if sub_waiters is not None else None
    chan_waiting = (
        (lambda rec: chan_sub_waiters.waiting(chan_subs_mod.rec_key(rec), rec.session_id))
        if chan_sub_waiters is not None
        else None
    )
    ended = [
        (str(sub_id), reason) for sub_id, reason in subs_mod.cleanup(conn, now_ms=now_ms, waiting=sub_waiting)
    ]
    ended += [
        (f"c{sub_id}", reason)
        for sub_id, reason in chan_subs_mod.cleanup(conn, now_ms=now_ms, waiting=chan_waiting)
    ]
    # Ф66-1 (C, П5, П9, П10): «сразу да» на карточки, которые появляются при чтении очереди (`heavy`,
    # `paused`), состояния `broken`/`dead` и сработавшие напоминания с дошедшим сигналом.
    answered = card_route_mod.sweep(conn, now_ms=now_ms, waiting=sub_waiting)
    if answered:
        _logger.info("карточки по таблице маршрута: «сразу да» — %s", answered)
    # Ф56-1в: карточки «стоит без дела» отменены (владелец 22.10) — оставшиеся закрываются.
    retired = owner_queue.retire_stalls(conn, now_ms=now_ms)
    if retired:
        _logger.info("карточки «стоит без дела» сняты: %s", retired)
    # Ф65-1 (Ж2): поручения агенту владельца без шага 5 минут — обратно владельцу.
    hung = owner_queue.return_hung(conn, now_ms=now_ms)
    if hung:
        _logger.info("поручения агенту владельца зависли — владельцу вернулись %s", hung)
    return ended


async def _subs_cleanup_loop(
    db: Db,
    clock: Callable[[], float],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    stop_event: asyncio.Event | None = None,
    sub_waiters: subs_mod.SubWaiters | None = None,
    chan_sub_waiters: chan_subs_mod.ChanWaiters | None = None,
) -> None:
    """Ф13-1 B2: раз в минуту — `subs.cleanup` (процесс сессии умер или подменён — `session_gone`, токен
    отозван — `token_revoked`) одной транзакцией писателя. Сбой журналируется, цикл живёт дальше; закрытый
    `Db` — выход. В журнал — только номера подписок и причины."""
    while stop_event is None or not stop_event.is_set():
        await sleep(_SUBS_TICK_S)
        now_ms = int(clock() * 1000)
        try:
            ended = await db.write(
                lambda conn, now_ms=now_ms: _cleanup_all(conn, now_ms, sub_waiters, chan_sub_waiters)
            )
        except DbClosed:
            return
        except Exception:  # noqa: BLE001 — сбой не должен убить цикл, попробуем через минуту
            _logger.exception("subs: проверка подписок упала")
        else:
            for sub_id, reason in ended:
                _logger.info("subs: подписка #%s завершена: %s", sub_id, reason)


# Ф34-2 решение 4: срабатывание напоминаний — раз в 60 с по настенным часам.
_REMINDERS_TICK_S = 60.0


async def _reminders_loop(
    db: Db,
    clock: Callable[[], float],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Ф34-2 решение 4 + П11: раз в минуту — должные напоминания (`reminders.fire_due`; пропущенные за простой
    ядра срабатывают на первом тике). Сначала читатель: писать только когда есть что — пустая запись раз в
    минуту будила бы все long-poll (`on_commit`). Запись сама будит ожидания подписок и `/stream`. Сбой — в
    журнал, цикл живёт дальше; закрытый `Db` — выход. В журнал — только числа."""
    while stop_event is None or not stop_event.is_set():
        await sleep(_REMINDERS_TICK_S)
        now_ms = int(clock() * 1000)
        try:
            # Ф35-2: сообщения агентов с прошедшим сроком — тем же правилом «сначала читатель».
            with db.reader() as conn:
                stale = peer_msgs_mod.has_expired(conn, now_ms)
            if stale:
                expired = await db.write(lambda conn, now_ms=now_ms: peer_msgs_mod.expire(conn, now_ms))
                _logger.info("сообщения агентов: просрочено %d", expired)
        except DbClosed:
            return
        except Exception:  # noqa: BLE001 — сбой не должен убить цикл, попробуем через минуту
            _logger.exception("сообщения агентов: просрочка упала")
        try:
            with db.reader() as conn:
                work = reminders_mod.has_work(conn, now_ms)
            if not work:
                continue
            counts = await db.write(lambda conn, now_ms=now_ms: reminders_mod.fire_due(conn, now_ms))
        except DbClosed:
            return
        except Exception:  # noqa: BLE001 — сбой не должен убить цикл, попробуем через минуту
            _logger.exception("напоминания: срабатывание упало")
        else:
            if any(counts.values()):
                _logger.info("напоминания: %s", counts)


# Ф54-1 (П1): срок заявок «нужен человек» — раз в 60 с по настенным часам.
_HUMAN_TICK_S = 60.0


async def _human_loop(
    db: Db,
    clock: Callable[[], float],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Ф54-1 (П1): раз в минуту — заявки «нужен человек» с вышедшим сроком (`human_requests.sweep`): автор
    получает сигнал исхода «срок вышел» и без единого запроса к ручкам. Сначала читатель: писать только когда
    есть что (пустая запись раз в минуту будила бы все long-poll). Запись сама будит ожидания подписок и
    `/stream`. Сбой — в журнал, цикл живёт дальше; закрытый `Db` — выход. В журнал — только числа."""
    while stop_event is None or not stop_event.is_set():
        await sleep(_HUMAN_TICK_S)
        now_ms = int(clock() * 1000)
        try:
            with db.reader() as conn:
                due = human_requests_mod.has_due(conn, now_ms)
            if not due:
                continue
            expired = await db.write(lambda conn, now_ms=now_ms: human_requests_mod.sweep(conn, now_ms))
        except DbClosed:
            return
        except Exception:  # noqa: BLE001 — сбой не должен убить цикл, попробуем через минуту
            _logger.exception("заявки «нужен человек»: снятие по сроку упало")
        else:
            if expired:
                _logger.info("заявки «нужен человек»: срок вышел у %d", expired)


# Ф60-1 (П6): потолок интервью 3 часа и «тишина 20 мин» — раз в 30 с.
_INTERVIEW_TICK_S = 30.0


async def _interview_loop(
    interviews,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Ф60-1 (П6): раз в 30 с — открытое интервью дольше 3 часов кончается (с итогом), `active` без новых
    строк 20 минут встаёт на паузу (`InterviewRuntime.sweep`: сначала читатель, пишет только когда есть
    что). Сбой — в журнал, цикл живёт дальше; закрытый `Db` — выход. В журнал — только номера."""
    while stop_event is None or not stop_event.is_set():
        await sleep(_INTERVIEW_TICK_S)
        try:
            await interviews.sweep()
        except DbClosed:
            return
        except Exception:  # noqa: BLE001 — сбой не должен убить цикл, попробуем через 30 с
            _logger.exception("интервью: проверка потолка и тишины упала")


class _Server(uvicorn.Server):
    """uvicorn.Server без собственных обработчиков сигналов. Штатный `capture_signals` ставит `signal.signal`
    поверх обработчиков цикла и на выходе ПЕРЕВЫБРАСЫВАЕТ сигнал; у нас два сервера в одном цикле — второй
    перекрыл бы первого, а перевыброшенный SIGTERM убил бы процесс до `Db.close()`. Сигналами владеет
    `_serve_forever`: один обработчик гасит оба сервера, затем дожидается очереди записи."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


def _read_or_create_admin_key(config_dir: Path) -> str:
    """Вернуть sha256(секрет). Файл создаётся при первом старте (0600); core держит в памяти только хеш —
    сам секрет читает и пишет только этот момент запуска."""
    path = config_dir / ADMIN_KEY_NAME
    if path.exists():
        secret = path.read_text(encoding="utf-8").strip()
    else:
        secret = ADMIN_PREFIX + secrets.token_urlsafe(32)
        write_secret_file(path, secret)
    return hash_secret(secret)


def _acquire_lock(data_dir: Path) -> int:
    """`flock` на `<data_dir>/core.lock` — второй `hub core` на этом каталоге данных отказывается стартовать.
    Файловый дескриптор держим открытым на всё время жизни процесса (закрытие снимает блокировку)."""
    lock_path = data_dir / LOCK_NAME
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise RuntimeError(f"hub core уже запущен на {data_dir} (lock {lock_path})") from exc
    return fd


def _socket_is_alive(socket_path: Path) -> bool:
    """True, если по этому файлу сокета кто-то реально слушает — не просто «файл существует»."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(str(socket_path))
    except OSError:
        return False
    else:
        return True
    finally:
        probe.close()


def _bind_tcp(host: str, port: int) -> socket.socket:
    """Забиндить TCP-сокет слушателя (так же, как `uvicorn.Config.bind_socket`, но без `sys.exit` при
    занятом порте — П3 Ф10-7: занятый удалённый порт не должен ронять ядро). `listen` делает сам asyncio."""
    assert host == "127.0.0.1", "публичное приложение слушает только 127.0.0.1"  # noqa: S101
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
    except BaseException:
        sock.close()
        raise
    sock.set_inheritable(True)
    return sock


def _bind_remote(host: str, port: int) -> socket.socket | None:
    """Удалённый слушатель (туннель с телефона, Ф10-7). Порт занят -> `None` и громкая строка в журнале: ядро
    работает без него (`listening:false` в `/remote`), телефон — не повод ронять ленту."""
    try:
        return _bind_tcp(host, port)
    except OSError as exc:
        _logger.error(
            "удалённый слушатель %s:%s НЕ поднят (%s): вход с телефона недоступен, ядро работает без него",
            host,
            port,
            exc.strerror or exc.__class__.__name__,
        )
        return None


def _prepare_socket_path(data_dir: Path) -> Path:
    """Убрать файл сокета, только если он «мёртвый» (никто не слушает) — живой сокет — это второй core,
    старт которого запрещён (спека, раздел «Процесс hub core»)."""
    socket_path = data_dir / SOCKET_NAME
    if socket_path.exists():
        if _socket_is_alive(socket_path):
            raise RuntimeError(f"внутренний сокет {socket_path} уже слушает — второй core запрещён")
        socket_path.unlink()
    return socket_path


async def _serve_forever(
    data_dir: Path,
    config_dir: Path,
    *,
    host: str,
    port: int,
    clock: Callable[[], float],
    ui_dir: Path | None,
    remote_port: int = 0,
) -> None:
    assert host == "127.0.0.1", "публичное приложение слушает только 127.0.0.1"  # noqa: S101
    # Ф10-7, П2: удалённый слушатель — только по явному `--remote-port`; совпадение с локальным — отказ.
    if remote_port and remote_port == port:
        raise ValueError(f"--remote-port {remote_port} совпадает с --port")

    lock_fd = _acquire_lock(data_dir)
    try:
        socket_path = _prepare_socket_path(data_dir)
        admin_key_hash = _read_or_create_admin_key(config_dir)

        # Открытая копия БД от бэкапа, убитого мимо `finally` (SIGKILL, питание), не должна пережить
        # следующий старт — и не должна ждать следующего УСПЕШНОГО бэкапа (спека куска 7, «Поправки»).
        backup_mod.cleanup_plaintext(data_dir)
        # Ф8-5 П5: недописанные загрузки медиа (`media/**/.tmp*`, `.tmp-conn`) от прошлого процесса.
        media_mod.cleanup_tmp(data_dir)

        db = Db(data_dir, clock=clock)
        db.start()
        # Поправки после opus-критика кода (п.8/P2): `retention_task` — ДО `try`. Раньше присваивался
        # внутри, а безусловный `finally` ниже трогал его в любом случае — исключение МЕЖДУ `db.start()` и
        # этим присваиванием (например, `create_public_app`/`bind_socket`) давало `UnboundLocalError` в
        # `finally`, маскируя исходную причину падения И пропуская `db.close()` (находка критика, тест run2).
        retention_task: asyncio.Task | None = None
        retention_inflight: dict = {}
        # Ф21-1: цикл людей — так же до `try` (см. выше), со своим флагом остановки и потоком пересчёта.
        people_task: asyncio.Task | None = None
        people_stop = threading.Event()
        people_inflight: dict = {}
        public_sockets: list[socket.socket] = []
        remote_sockets: list[socket.socket] = []
        try:
            # Ф8-5 A1/П15: точка отсчёта медиа — первый старт ядра с миграцией `media`, часами `Db`, один раз.
            db.write_sync(lambda conn: media_mod.ensure_start_ts(conn, int(db.clock() * 1000)))
            # Ф66-1 (A.4, П1, П7, П8): таблица маршрута карточек проектам без неё — по прежним настройкам.
            moved = db.write_sync(lambda conn: card_route_mod.migrate(conn, now_ms=int(db.clock() * 1000)))
            if moved:
                _logger.info("таблица маршрута карточек перенесена из прежних настроек: проекты %s", moved)
            # Ф76-1 (П4): журнал решений — первая загрузка из прежней истории карточек, один раз (флаг в
            # `main.meta`); повтор не задваивает (`source_key`).
            loaded = db.write_sync(lambda conn: decisions_mod.backfill(conn, now_ms=int(db.clock() * 1000)))
            if loaded:
                _logger.info("журнал решений: первая загрузка из истории карточек, строк %s", loaded)
            # Ф60-1 (П6): идущее интервью после рестарта ядра — на паузе («ядро перезапущено»): служба гасит
            # запись, владелец жмёт «Продолжить».
            paused = db.write_sync(
                lambda conn: interview_mod.pause_on_start(conn, now_ms=int(db.clock() * 1000))
            )
            if paused:
                _logger.info("интервью на паузе после старта ядра: %s", paused)
            # Ф77-1 (A2): своя машина в реестре — «машина ядра» (у прежней после переезда отметка снята).
            db.write_sync(lambda conn: machines_mod.register_core(conn, now_ms=int(db.clock() * 1000)))
            notifier = Notifier()
            db.on_commit(notifier.notify)

            # Ф10-7, П1: одно состояние удалённого доступа на оба приложения (как `notifier`).
            remote_state = RemoteState(port=remote_port)
            with db.reader() as conn:
                remote_state.load(conn)
            # Ф13-1: один учёт идущих ожиданий подписок на оба приложения (`waiting` в `/admin/subs`).
            sub_waiters = subs_mod.SubWaiters()
            chan_sub_waiters = chan_subs_mod.ChanWaiters()  # Ф14-5 П8
            activity = Activity()  # Ф20-3 A2: один на оба приложения
            tg_lookup = TgLookup()  # Ф29-1 A1: один на оба приложения
            notifier.on_close(tg_lookup.close)  # Ф32-2 п.4: остановка ядра будит и его ожидания
            # Ф56-5: терминалы хаба — сервер tmux транзиентной службой (К1), мосты в памяти ядра.
            term_hub = TermHub(TermConfig.from_env(data_dir))

            public_app = create_public_app(
                db,
                clock=clock,
                port=port,
                notifier=notifier,
                ui_dir=ui_dir,
                remote_port=remote_port,
                remote=remote_state,
                sub_waiters=sub_waiters,
                chan_sub_waiters=chan_sub_waiters,
                activity=activity,
                tg_lookup=tg_lookup,
                term=term_hub,
            )
            interviews = public_app.state.interviews  # Ф60-1: одна память интервью на оба приложения
            internal_app = create_internal_app(
                db,
                clock=clock,
                notifier=notifier,
                admin_key_hash=admin_key_hash,
                remote=remote_state,
                sub_waiters=sub_waiters,
                chan_sub_waiters=chan_sub_waiters,
                activity=activity,
                tg_lookup=tg_lookup,
                term=term_hub,
                interviews=interviews,
            )
            # `/admin/backup` читает recipient из config_dir — атрибут выставляется здесь, а не в
            # create_internal_app(), чтобы не трогать app_internal.py (кусок 7 его не затрагивает, см. отчёт).
            internal_app.state.config_dir = config_dir
            # Ф16-1: `PUT /engine` проверяет готовность OpenCode по `<config_dir>/engine.json` (П13).
            public_app.state.config_dir = config_dir

            # Ф10-7, решение 1 и П3: оба TCP-сокета биндит сам runner, потом ОДИН `_Server` с двумя сокетами
            # (lifespan — один раз, `should_exit` — один). Занят локальный порт -> ядро не стартует (OSError);
            # занят удалённый -> старт без него. `proxy_headers=False`: туннель приходит с 127.0.0.1, и
            # `X-Forwarded-*` любого, кто достучался до сервера, иначе переписали бы client/scheme.
            # Ф14-9 решение 4: с парой https удалённый сокет — у своего `_Server` (у uvicorn `Config.ssl` один
            # на все сокеты сервера); битая пара — удалённый не поднимается вовсе (решение 2).
            public_sockets.append(_bind_tcp(host, port))
            tls_context = None
            if remote_port:
                tls_context, remote_state.tls = tls_mod.load(config_dir)
                if remote_state.tls is not None and remote_state.tls.error is not None:
                    _logger.error(
                        "удалённый слушатель %s:%s НЕ поднят: https-пара в %s не грузится (%s) — вход с "
                        "телефона недоступен, ядро работает без него",
                        host,
                        remote_port,
                        tls_mod.tls_dir(config_dir),
                        remote_state.tls.error,
                    )
                    remote_socket = None
                else:
                    remote_socket = _bind_remote(host, remote_port)
                if remote_socket is not None:
                    if tls_context is not None:
                        remote_sockets.append(remote_socket)
                    else:
                        public_sockets.append(remote_socket)
                    remote_state.listening = True

            # Ф31-1 решение 6: запас на случай ожидания, не знающего `notifier.close()` (оно гасит долгие
            # ожидания сразу) — через 5 с uvicorn обрывает оставшиеся запросы.
            public_config = uvicorn.Config(
                public_app,
                host=host,
                port=port,
                log_level="warning",
                proxy_headers=False,
                timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_S,
            )
            remote_server: _Server | None = None
            if remote_sockets:
                # Lifespan приложения — один раз, у локального сервера; здесь `lifespan="off"`. Свой контекст
                # вместо `ssl_certfile` uvicorn: минимум TLS 1.2 и ошибки загрузки пойманы до старта.
                remote_config = uvicorn.Config(
                    public_app,
                    host=host,
                    port=remote_port,
                    log_level="warning",
                    proxy_headers=False,
                    lifespan="off",
                    timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_S,
                )
                remote_config.load()
                remote_config.ssl = tls_context
                remote_server = _Server(remote_config)
            internal_config = uvicorn.Config(
                internal_app,
                uds=str(socket_path),
                log_level="warning",
                proxy_headers=False,
                timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_S,
            )

            # Биндим внутренний сокет здесь (синхронно, до входа в serve()), чтобы chmod 0600 случился
            # СРАЗУ после создания файла, а не после того, как uvicorn сам решит его забиндить где-то
            # внутри своего startup(). `Config.bind_socket()` — тот же метод, которым это делает сам uvicorn.
            internal_socket = internal_config.bind_socket()
            os.chmod(socket_path, 0o600)  # noqa: S103 — намеренно уже, не 0o644 по умолчанию у ОС

            public_server = _Server(public_config)
            internal_server = _Server(internal_config)

            loop = asyncio.get_running_loop()
            notifier.attach(loop)
            install_task_dump(loop, name="core")
            retention_stop = threading.Event()
            # Ф8-4 A1/П9: ретенция снята — планировщик не стартует, пока константа не включена правкой кода.
            if retention.RETENTION_ENABLED:
                retention_task = asyncio.ensure_future(
                    _retention_loop(db, clock, stop_event=retention_stop, inflight=retention_inflight)
                )

            people_task = asyncio.ensure_future(
                _people_loop(db, clock, stop_event=people_stop, inflight=people_inflight)
            )
            stop_event = asyncio.Event()
            uploads_task = asyncio.ensure_future(_uploads_cleanup_loop(db, clock, stop_event=stop_event))
            subs_task = asyncio.ensure_future(
                _subs_cleanup_loop(
                    db,
                    clock,
                    stop_event=stop_event,
                    sub_waiters=sub_waiters,
                    chan_sub_waiters=chan_sub_waiters,
                )
            )

            reminders_task = asyncio.ensure_future(_reminders_loop(db, clock, stop_event=stop_event))
            human_task = asyncio.ensure_future(_human_loop(db, clock, stop_event=stop_event))
            interview_task = asyncio.ensure_future(_interview_loop(interviews, stop_event=stop_event))

            def _on_signal() -> None:
                stop_event.set()

            for sig in (signal.SIGTERM, signal.SIGINT):
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(sig, _on_signal)

            servers = [public_server, internal_server]
            serves = [
                public_server.serve(sockets=public_sockets),
                internal_server.serve(sockets=[internal_socket]),
            ]
            if remote_server is not None:
                servers.append(remote_server)
                serves.append(remote_server.serve(sockets=remote_sockets))
            serve_task = asyncio.ensure_future(asyncio.gather(*serves))
            stop_task = asyncio.ensure_future(stop_event.wait())
            try:
                done, _pending = await asyncio.wait(
                    {serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if stop_task in done and not serve_task.done():
                    # Ф31-1 П5: сначала разбудить и закрыть долгие ожидания (`/stream`, long-poll) — они
                    # отвечают пусто, ничего не выдавая; иначе uvicorn ждал бы их конца.
                    notifier.close()
                    for server in servers:
                        server.should_exit = True
                    await serve_task
            finally:
                if not stop_task.done():
                    stop_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await stop_task
                uploads_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await uploads_task
                subs_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await subs_task
                reminders_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await reminders_task
                human_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await human_task
                interview_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await interview_task
                # Ф56-5: клиенты управления — дочерние процессы ядра; сервер tmux (служба) остаётся жить.
                with contextlib.suppress(Exception):
                    await term_hub.aclose()
        finally:
            # uvicorn закрывает их сам, но не если до `serve` не дошло
            for sock in public_sockets + remote_sockets:
                with contextlib.suppress(OSError):
                    sock.close()
            # Раздел F.3, Поправки п.8 (P1/P2): `retention_stop.set()` — ПЕРВЫМ, до отмены: если `run_once`
            # сейчас в процессе (крупная база), она заметит между порциями и выйдет сама, не дожидаясь конца
            # всего прогона. `.cancel()` — следом, чтобы НЕМЕДЛЕННО прервать простаивающий тик (иначе
            # остановка core ждала бы полный `_RETENTION_TICK_S`, тест `test_sigterm_stops_core_cleanly`
            # укладывается в 15 с) — сам `.cancel()` не останавливает уже запущенный `asyncio.to_thread`
            # (обрывает только `await` вызывающей корутины), но поток, оставшийся позади, теперь безопасен:
            # `Db._submit` на закрытом `Db` больше не падает и не виснет (находка критика `test_run1`), а
            # `retention_stop` заставляет его закончиться быстро. `retention_task` — `None`, если исключение
            # случилось раньше его создания (`test_run2`) — тогда ждать нечего.
            if retention_task is not None:
                retention_stop.set()
                retention_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await retention_task
            # Приёмка 2b (архитектор): поток прогона, уже начатый до остановки, — дождаться (он сам выходит
            # по `retention_stop` между порциями), и только потом закрывать `Db`.
            inflight_future = retention_inflight.get("future")
            if inflight_future is not None and not inflight_future.done():
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(inflight_future), timeout=_RETENTION_STOP_WAIT_S)
            # Ф21-1 П10: пересчёт людей — флаг первым, затем отмена тика, затем дождаться потока (он выходит
            # между беседами без записи).
            people_stop.set()
            if people_task is not None:
                people_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await people_task
            people_future = people_inflight.get("future")
            if people_future is not None and not people_future.done():
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(people_future), timeout=_PEOPLE_STOP_WAIT_S)
            db.close()  # дожидается очереди записи, как требует спека
    finally:
        os.close(lock_fd)


def run_core(
    data_dir: Path,
    config_dir: Path,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    clock: Callable[[], float] = time.time,
    ui_dir: Path | None = None,
    remote_port: int = 0,
) -> None:
    """Точка входа `hub core` (синхронная — вызывается из `cli.py`). `remote_port` — удалённый слушатель
    для туннеля с телефона (Ф10-7; 0 — не слушать, П2)."""
    asyncio.run(
        _serve_forever(
            data_dir,
            config_dir,
            host=host,
            port=port,
            clock=clock,
            ui_dir=ui_dir,
            remote_port=remote_port,
        )
    )
