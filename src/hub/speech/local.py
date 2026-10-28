"""Посредник распознавания на своём whisper (Ф79-1, docs/architecture.md).

Ядро само ничего не распознаёт и к ноутбуку не ходит: быстрый работник (служба `hub transcribe run` с
`device=cuda`) сам приходит долгим опросом `POST /speech/lease`, берёт кусок звука и отвечает
`POST /speech/{job_id}/result`. Всё — только в памяти процесса ядра (без БД): рестарт ядра очередь обнуляет,
это нормально — кусок диктовки живёт секунды, ждущий его запрос уйдёт в SpeechKit.

- `recognize(pcm, lang, timeout_s=…)` -> `LocalResult` или `None` (работника нет, очередь полна, ошибка
  работника, вышел срок). Снятое задание больше не выдаётся; поздний ответ — `stale` (409, тихо).
- Работник «жив», пока ждёт в опросе, и ещё `WORKER_SEEN_S` после входа в последний опрос. Обрыв соединения
  метку не продлевает: ожидание кусками по секунде, и каждый кусок спрашивает, жив ли клиент.

Звук и текст не пишутся ни в журнал, ни на диск.
"""

import asyncio
import base64
import logging
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

_logger = logging.getLogger(__name__)

WORKER_SEEN_S = 30.0
QUEUE_MAX = 8
LEASE_WAIT_MAX_S = 25.0
_SLICE_S = 1.0  # ожидание в опросе — кусками: обрыв клиента замечается за секунду
ERROR_MAX = 100
TEXT_MAX = 20_000  # кусок ≤ 30 с — текста в разы меньше


@dataclass(frozen=True)
class LocalResult:
    text: str
    elapsed_ms: int


@dataclass
class _Job:
    id: str
    pcm: bytes
    lang: str
    future: asyncio.Future
    started: float
    taken_by: int | None = None
    taken: bool = False


class SpeechBroker:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._queue: deque[_Job] = deque()
        self._jobs: dict[str, _Job] = {}
        self._seen: float | None = None
        self._waiting = 0
        self._wake: asyncio.Event | None = None
        self.closed = False

    # --- живость работника -----------------------------------------------------------------------------

    def worker_alive(self) -> bool:
        if self.closed:
            return False
        if self._waiting > 0:
            return True
        return self._seen is not None and self._clock() - self._seen <= WORKER_SEEN_S

    def mark_seen(self) -> None:
        self._seen = self._clock()

    @property
    def pending(self) -> int:
        """Живые задания: в очереди и выданные, ещё без ответа."""
        return len(self._jobs)

    def _event(self) -> asyncio.Event:
        if self._wake is None:
            self._wake = asyncio.Event()
        return self._wake

    def _notify(self) -> None:
        event, self._wake = self._wake, None
        if event is not None:
            event.set()

    def close(self) -> None:
        """Остановка ядра: ждущие в опросе выходят пустыми, ждущие результата — `None`."""
        self.closed = True
        for job in list(self._jobs.values()):
            if not job.future.done():
                job.future.set_result(None)
        self._queue.clear()
        self._jobs.clear()
        self._notify()

    # --- сторона ядра (диктовка, интервью) ---------------------------------------------------------------

    async def recognize(self, pcm: bytes, lang: str, *, timeout_s: float) -> LocalResult | None:
        if not self.worker_alive():
            return None
        if len(self._jobs) >= QUEUE_MAX:
            _logger.info("whisper: очередь полна (%d) — мимо", len(self._jobs))
            return None
        loop = asyncio.get_running_loop()
        job = _Job(
            id=uuid.uuid4().hex, pcm=pcm, lang=lang, future=loop.create_future(), started=self._clock()
        )
        self._queue.append(job)
        self._jobs[job.id] = job
        self._notify()
        outcome = None
        try:
            async with asyncio.timeout(timeout_s):
                outcome = await job.future
        except TimeoutError:
            outcome = None
        finally:
            self._drop(job)
        if not isinstance(outcome, dict) or outcome.get("error") is not None:
            return None
        text = outcome.get("text")
        if not isinstance(text, str):
            return None
        return LocalResult(text=text, elapsed_ms=int((self._clock() - job.started) * 1000))

    def _drop(self, job: _Job) -> None:
        self._jobs.pop(job.id, None)
        try:
            self._queue.remove(job)
        except ValueError:
            pass

    # --- сторона работника ----------------------------------------------------------------------------------

    def _take(self, token_id: int | None) -> _Job | None:
        while self._queue:
            job = self._queue.popleft()
            if job.id not in self._jobs or job.future.done():
                continue
            job.taken = True
            job.taken_by = token_id
            return job
        return None

    async def lease(
        self,
        wait_s: float,
        *,
        token_id: int | None = None,
        disconnected: Callable[[], Awaitable[bool]] | None = None,
    ) -> dict | None:
        """Долгий опрос работника: задание `{job_id, lang, audio_b64}` или `None` за `wait_s` (≤ 25 с)."""
        wait_s = max(0.0, min(float(wait_s), LEASE_WAIT_MAX_S))
        self.mark_seen()
        self._waiting += 1
        try:
            deadline = self._clock() + wait_s
            while not self.closed:
                event = self._event()
                job = self._take(token_id)
                if job is not None:
                    return {
                        "job_id": job.id,
                        "lang": job.lang,
                        "audio_b64": base64.b64encode(job.pcm).decode("ascii"),
                    }
                left = deadline - self._clock()
                if left <= 0:
                    return None
                try:
                    async with asyncio.timeout(min(left, _SLICE_S)):
                        await event.wait()
                except TimeoutError:
                    pass
                if disconnected is not None and await disconnected():
                    return None
            return None
        finally:
            self._waiting -= 1

    def result(self, job_id: str, *, token_id: int | None, text: str | None, error: str | None) -> bool:
        """Ответ работника. `False` — задания нет (снято, не выдавалось, уже отвечено) или выдано другому."""
        self.mark_seen()
        job = self._jobs.get(job_id)
        if job is None or not job.taken or job.future.done() or job.taken_by != token_id:
            return False
        if error is not None:
            job.future.set_result({"error": error[:ERROR_MAX]})
        else:
            job.future.set_result({"text": text or ""})
        return True


def broker_of(app_state) -> SpeechBroker | None:
    broker = getattr(app_state, "speech_broker", None)
    return broker if isinstance(broker, SpeechBroker) else None
