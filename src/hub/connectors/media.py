"""Загрузчик медиа коннектора (Ф8-5, `docs/architecture.md`/П4/П15).

Отдельная задача коннектора, по образцу цикла отправки: `wait_media` -> `lease_media(limit=1)` -> источник
качает файл во временный файл `<data_dir>/media/.tmp-conn/<account_id>/` -> `PUT content` с `Content-Length`
-> временный файл удаляется. Одна загрузка за раз; своя задача — сверку и отправку не держит (у них свои
задачи и свои лимиты, общего замка нет).

Как качать — решает коннектор (`fetch(job, sink)`): Telegram — `download_media`, Slack — `GET` с Bearer.
Здесь — только очередь, временный файл, потолок `max_bytes`, отчёт ядру и пауза после FloodWait.
Журнал — только `media_id` и класс ошибки: без имён файлов, URL и текстов (П15).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal, NamedTuple

from .base import (
    AuthRequired,
    CoreClient,
    _is_core_unreachable,
    _is_rejected,
    _is_unauthorized,
    _own_task_cancelling,
)

_logger = logging.getLogger(__name__)

WAIT_MEDIA_S = 25.0
_BACKOFF_BASE_S = 1.0
_BACKOFF_CAP_S = 30.0
_BLOCKED_SLEEP_S = 60.0
# Лиз ядра — 10 мин (A2.4): загрузка должна кончиться раньше, иначе строку перехватят.
FETCH_TIMEOUT_S = 480.0
_PUT_TIMEOUT_S = 300.0


def cleanup_parts(tmp_dir: str | Path) -> int:
    """Ф32-2 п.5а + П4: старт коннектора — убрать осиротевшие `*.part` своего каталога аккаунта (загрузки
    медиа и файлы отправки `outbox-*.part` процесса, убитого посреди работы; ядро чистит `.tmp-conn` только
    на своём старте). Только обычные файлы самого каталога: без рекурсии, ссылки не трогаются и не
    разыменовываются. Второго процесса того же аккаунта нет. Возвращает число удалённых."""
    root = Path(tmp_dir)
    if root.is_symlink() or not root.is_dir():
        return 0
    removed = 0
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if not entry.name.endswith(".part") or not entry.is_file(follow_symlinks=False):
                    continue
                with contextlib.suppress(OSError):
                    os.unlink(entry.path)
                    removed += 1
    except OSError as exc:
        _logger.warning("media: чистка временного каталога не прошла: %s", type(exc).__name__)
    if removed:
        _logger.info("media: убрано недокачанных временных файлов: %d", removed)
    return removed


class MediaResult(NamedTuple):
    """Итог `fetch`. `ready` — файл записан в `sink`, дальше `PUT`; прочее уходит в `report`.
    `retry_after_s` — FloodWait: ядру (попыткой не считается, П15) и пауза загрузчика на это время.
    `size` — размер, узнанный у источника (для `too_big`, П6)."""

    outcome: Literal["ready", "gone", "temporary", "too_big", "auth"]
    error: str | None = None
    retry_after_s: float | None = None
    size: int | None = None


class MediaTooBig(Exception):
    """Файл превысил `max_bytes` задания — запись оборвана (П6: ядро решит, `deferred` или `too_big`)."""


class MediaSink:
    """Пишущий конец временного файла для источника: `write`/`flush` (как файл — Telethon пишет в него
    `download_media(message, file=sink)`), `reset()` — начать заново (повтор после
    `FileReferenceExpired`), `written` — сколько записано. Больше `max_bytes` -> `MediaTooBig`."""

    def __init__(self, f, max_bytes: int | None) -> None:
        self._f = f
        self._max_bytes = max_bytes
        self.written = 0

    def write(self, data: bytes) -> int:
        n = len(data)
        if self._max_bytes is not None and self.written + n > self._max_bytes:
            raise MediaTooBig
        self._f.write(data)
        self.written += n
        return n

    def flush(self) -> None:
        self._f.flush()

    def reset(self) -> None:
        self._f.seek(0)
        self._f.truncate()
        self.written = 0


FetchFn = Callable[[dict, MediaSink], Awaitable[MediaResult]]


class MediaDownloader:
    """Цикл загрузчика. `fetch` — метод коннектора; `mono`/`sleep` — часы коннектора (тесты подменяют);
    `is_blocked()` — вход в источник потерян (`auth_required`) — задания не берутся;
    `on_auth_required()` — `fetch` поднял `AuthRequired`; `on_unauthorized()` — ядро ответило 401 (секрет
    отозван) — выход 78, как у остальных циклов каркаса."""

    def __init__(
        self,
        client: CoreClient,
        fetch: FetchFn,
        *,
        tmp_dir: str | Path,
        mono: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
        is_blocked: Callable[[], bool],
        on_auth_required: Callable[[], Awaitable[None]],
        on_unauthorized: Callable[[], None],
    ) -> None:
        self._client = client
        self._fetch = fetch
        self._tmp_dir = Path(tmp_dir)
        self._mono = mono
        self._sleep = sleep
        self._is_blocked = is_blocked
        self._on_auth_required = on_auth_required
        self._on_unauthorized = on_unauthorized
        self._paused_until: float | None = None
        self._stopped = False

    @property
    def paused_until(self) -> float | None:
        return self._paused_until

    async def run(self) -> None:
        backoff = _BACKOFF_BASE_S
        while not self._stopped:
            if self._is_blocked():
                await self._sleep(_BLOCKED_SLEEP_S)
                continue
            if self._paused_until is not None:
                left = self._paused_until - self._mono()
                if left > 0:
                    await self._sleep(left)
                    continue
                self._paused_until = None
            try:
                ready = await self._client.wait_media(WAIT_MEDIA_S)
                if ready:
                    await self.step()
            except asyncio.CancelledError:
                if _own_task_cancelling():
                    raise
                _logger.warning("media: чужая отмена в цикле загрузчика — повтор")
                continue
            except Exception as exc:  # noqa: BLE001 — цикл загрузчика не падает
                if _is_unauthorized(exc):
                    self._stopped = True
                    self._on_unauthorized()
                    return
                if not _is_core_unreachable(exc):
                    _logger.warning("media: запрос к ядру не прошёл: %s", type(exc).__name__)
                await self._sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_CAP_S)
                continue
            backoff = _BACKOFF_BASE_S

    async def step(self) -> int:
        """Один шаг: взять одно задание и обработать. Число обработанных заданий."""
        jobs = await self._client.lease_media(limit=1)
        for job in jobs:
            await self.process(job)
        return len(jobs)

    async def process(self, job: dict) -> None:
        media_id = job["media_id"]
        lease_gen = job["lease_gen"]
        max_bytes = job.get("max_bytes")
        size = job.get("size")
        if isinstance(max_bytes, int) and isinstance(size, int) and size > max_bytes:
            await self._report(media_id, lease_gen, MediaResult("too_big", size=size))
            return

        self._tmp_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self._tmp_dir / f"{int(media_id)}.{int(lease_gen)}.part"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
        f = os.fdopen(fd, "w+b")
        try:
            sink = MediaSink(f, max_bytes if isinstance(max_bytes, int) else None)
            try:
                result = await asyncio.wait_for(self._fetch(job, sink), timeout=FETCH_TIMEOUT_S)
            except MediaTooBig:
                result = MediaResult("too_big", size=size if isinstance(size, int) else None)
            except AuthRequired:
                # Вход в источник потерян: строка останется `fetching`, лиз истечёт сам (как у outbox).
                await self._on_auth_required()
                return
            except TimeoutError:
                result = MediaResult("temporary", error="timeout")
            except asyncio.CancelledError:
                if _own_task_cancelling():
                    raise
                result = MediaResult("temporary", error="cancelled")
            except Exception as exc:  # noqa: BLE001 — сбой одной загрузки не роняет загрузчик
                _logger.warning("media: загрузка media_id=%s упала: %s", media_id, type(exc).__name__)
                result = MediaResult("temporary", error=type(exc).__name__)

            if result.retry_after_s is not None:
                self._paused_until = self._mono() + result.retry_after_s

            if result.outcome != "ready":
                f.close()
                await self._report(media_id, lease_gen, result)
                return

            f.flush()
            os.fsync(f.fileno())
            written = sink.written
            f.close()
            await self._put(media_id, lease_gen, str(path), written)
        finally:
            with contextlib.suppress(Exception):
                f.close()
            with contextlib.suppress(OSError):
                path.unlink()

    async def _put(self, media_id: int, lease_gen: int, path: str, size: int) -> None:
        try:
            await self._client.put_media_content(
                media_id=media_id, lease_gen=lease_gen, path=path, size=size, timeout_s=_PUT_TIMEOUT_S
            )
        except Exception as exc:
            self._core_refused(exc, "PUT", media_id)

    async def _report(self, media_id: int, lease_gen: int, result: MediaResult) -> None:
        if result.outcome != "gone":
            _logger.info("media: media_id=%s итог %s (%s)", media_id, result.outcome, result.error)
        try:
            await self._client.report_media(
                media_id=media_id,
                lease_gen=lease_gen,
                outcome=result.outcome,
                error=result.error,
                retry_after_s=result.retry_after_s,
                size=result.size,
            )
        except Exception as exc:
            self._core_refused(exc, "report", media_id)

    def _core_refused(self, exc: Exception, what: str, media_id: int) -> None:
        """409 (лиз перехвачен), 413/507 и прочие 4xx — ядро решило по строке само, дальше по очереди.
        401 — наверх (выход 78). Недоступность ядра — лиз истечёт, ядро выдаст строку снова."""
        if _is_rejected(exc):
            _logger.warning(
                "media: ядро отвергло %s media_id=%s (HTTP %s)", what, media_id, exc.response.status_code
            )
            return
        if _is_core_unreachable(exc):
            _logger.warning("media: %s media_id=%s — ядро недоступно: %s", what, media_id, type(exc).__name__)
            return
        raise exc
