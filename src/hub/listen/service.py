"""Цикл службы записи интервью `hub listen run` (Ф60-1, раздел 3, П9, П10, П12).

- Без интервью (или на паузе, или окончено) служба только опрашивает `GET /listen/current` раз в 2 с:
  микрофон закрыт, процессов звука нет. Интервью стало `active` — запись; ушло из `active` — запись гасится
  на ближайшем опросе.
- Запись (`Recording`): устройства по умолчанию -> эхоподавление -> два потока `ffmpeg` (`me` — источник
  эхоподавления или, если оно не поднялось, сырой микрофон и предупреждение ядру; `them` — монитор стока по
  умолчанию). Раз в 5 с устройства по умолчанию сверяются: сменились — перезапуск с новыми, строка в журнал.
  Упал `ffmpeg` или `pipewire` — перезапуск записи целиком, не больше 3 раз за минуту; дальше предупреждение
  ядру и ожидание, пока окно минуты не освободится.
- Нарезка (`segment.Segmenter`) — на каждом потоке своя. Время куска — время стены (мс от эпохи, П10): якорь
  потока = минимум «сейчас − длительность прочитанного» по всем чтениям.
- Отправка (`Sender`): очередь на поток, не больше 2 запросов одновременно на поток. 409 на кусок в полёте
  (пауза или конец пришли, пока он распознавался) — молча; 429 — кусок пропущен, строка в журнал; ключ
  SpeechKit не задан / не принят — предупреждение ядру.

Звук на диск не пишется: куски живут в памяти до отправки. Текста расшифровки служба не видит (ядро отдаёт
только длину)."""

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable

from .aio import cancel_and_wait, finish
from .core_api import CoreUnauthorized, CoreUnavailable, ListenCore, Reply
from .segment import BYTES_PER_MS, CHUNK_MAX_MS, Chunk, Segmenter

_logger = logging.getLogger("hub.listen")

EXIT_UNAUTHORIZED = 78
POLL_S = 2.0
DEVICE_CHECK_S = 5.0
RESTART_WINDOW_S = 60.0
RESTARTS_PER_WINDOW = 3
SENDERS_PER_STREAM = 2
STALL_S = (
    10.0  # ffmpeg жив, но звука нет столько секунд — поток встал (граф PipeWire под нагрузкой): перезапуск
)
# Потолок памяти звука: кусков на поток в очереди не больше QUEUE_MAX (каждый ≤ 15 с = 480 000 байт, итого
# ≤ ~14 МБ на поток); больше — самый старый выбрасывается (ядро не успевает).
QUEUE_MAX = 30

WARN_NO_AEC = "без эхоподавления: возможны повторы"
WARN_SPEECH_OFF = "ключ SpeechKit не задан — расшифровки нет"
WARN_SPEECH_KEY = "SpeechKit не принял ключ — расшифровки нет"


def warn_crash(what: str, wait_s: float) -> str:
    return f"запись звука падает ({what}) — повтор через {max(1, round(wait_s))} с"


def _wall_ms() -> int:
    return time.time_ns() // 1_000_000


class Warner:
    """Строка службы в панели интервью (`POST /listen/{id}/warn`). Вид предупреждения помнится: снять можно
    только своё (эхоподавление поднялось — снимаем «без эхоподавления», но не «ключ не задан»)."""

    def __init__(self, core, interview_id: int) -> None:
        self._core = core
        self._id = interview_id
        self.kind: str | None = None
        self.text: str | None = None
        self._cleared_once = False

    async def set(self, kind: str, text: str) -> None:
        if self.kind == kind and self.text == text:
            return
        if await self._post(text):
            self.kind, self.text = kind, text
            _logger.warning("интервью #%s: предупреждение — %s", self._id, text)

    async def clear(self, kind: str | None = None) -> None:
        """Снять своё предупреждение вида `kind` (или любое). Первый вызов за запуск службы снимает и
        оставшееся от прежнего запуска."""
        if self.kind is None:
            if self._cleared_once:
                return
        elif kind is not None and self.kind != kind:
            return
        if await self._post(""):
            self.kind, self.text = None, None
            self._cleared_once = True

    async def _post(self, text: str) -> bool:
        try:
            reply = await self._core.warn(self._id, text)
        except CoreUnavailable as exc:
            _logger.warning("интервью #%s: предупреждение не дошло до ядра (%s)", self._id, exc)
            return False
        if reply.status != 200:
            _logger.info("интервью #%s: ядро не приняло предупреждение (HTTP %s)", self._id, reply.status)
            return False
        return True


class Sender:
    """Отправка кусков одного потока: очередь, не больше `SENDERS_PER_STREAM` запросов одновременно."""

    def __init__(
        self, core, interview_id: int, who: str, warner: Warner, on_unauthorized: Callable[[], None]
    ):
        self._core = core
        self._id = interview_id
        self.who = who
        self._warner = warner
        self._on_unauthorized = on_unauthorized
        self._queue: asyncio.Queue[tuple[int, int, bytes]] = asyncio.Queue(maxsize=QUEUE_MAX)
        self._tasks = [asyncio.create_task(self._worker()) for _ in range(SENDERS_PER_STREAM)]
        self.sent = 0
        self.skipped = 0

    def put(self, start_ms: int, end_ms: int, pcm: bytes) -> None:
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
                self._queue.task_done()
            self.skipped += 1
            _logger.warning("интервью #%s, %s: ядро не успевает — старый кусок выброшен", self._id, self.who)
        self._queue.put_nowait((start_ms, end_ms, pcm))

    async def join(self) -> None:
        await self._queue.join()

    async def close(self) -> None:
        await cancel_and_wait(self._tasks)

    async def _worker(self) -> None:
        while True:
            start_ms, end_ms, pcm = await self._queue.get()
            try:
                await self._send(start_ms, end_ms, pcm)
            except CoreUnauthorized as exc:
                _logger.error("listen: %s", exc)
                self._on_unauthorized()
            except Exception:  # noqa: BLE001 — один кусок не роняет поток
                _logger.exception("интервью #%s, %s: кусок не отправлен", self._id, self.who)
            finally:
                self._queue.task_done()

    async def _send(self, start_ms: int, end_ms: int, pcm: bytes) -> None:
        try:
            reply: Reply = await self._core.chunk(
                self._id, who=self.who, start_ms=start_ms, end_ms=end_ms, pcm=pcm
            )
        except CoreUnavailable as exc:
            self.skipped += 1
            _logger.warning(
                "интервью #%s, %s: ядро недоступно (%s) — кусок пропущен", self._id, self.who, exc
            )
            return
        status = reply.status
        if status == 200:
            self.sent += 1
            await self._warner.clear("speech")
            return
        self.skipped += 1
        if status in (404, 409):
            return  # П12, мелочи: кусок был в полёте при паузе или конце — молча
        if status == 429:
            _logger.warning(
                "интервью #%s, %s: предел SpeechKit (%s) — кусок пропущен", self._id, self.who, reply.detail
            )
            return
        if status == 503 and reply.detail == "speechkit_off":
            await self._warner.set("speech", WARN_SPEECH_OFF)
            return
        if status == 502 and reply.detail == "listen_key":
            await self._warner.set("speech", WARN_SPEECH_KEY)
            return
        _logger.warning(
            "интервью #%s, %s: ядро не распознало кусок (HTTP %s %s, %d мс)",
            self._id,
            self.who,
            status,
            reply.detail,
            end_ms - start_ms,
        )


class Stream:
    """Один поток `ffmpeg` -> нарезка -> отправка. Чтение кончилось (`ffmpeg` вышел) или встало дольше
    `stall_s` — `run()` вернулся."""

    def __init__(
        self,
        who: str,
        capture,
        sender: Sender,
        *,
        wall_ms: Callable[[], int] = _wall_ms,
        stall_s: float = STALL_S,
    ) -> None:
        self.who = who
        self._stall_s = stall_s
        self.capture = capture
        self._sender = sender
        self._segmenter = Segmenter()
        self._wall_ms = wall_ms
        self._bytes = 0
        self._anchor: int | None = None  # время стены начала потока, мс

    def _emit(self, chunks: list[Chunk]) -> None:
        anchor = self._anchor or 0
        for chunk in chunks:
            if chunk.duration_ms > CHUNK_MAX_MS:  # нарезка так не режет; потолок — на всякий случай
                _logger.error("%s: кусок %d мс длиннее предела — выброшен", self.who, chunk.duration_ms)
                continue
            self._sender.put(anchor + chunk.start_ms, anchor + chunk.end_ms, chunk.pcm)

    async def run(self) -> None:
        while True:
            try:
                data = await asyncio.wait_for(self.capture.read(), self._stall_s)
            except TimeoutError:
                _logger.warning("%s: звука нет %.0f с — поток встал", self.who, self._stall_s)
                return
            if not data:
                return
            self._bytes += len(data)
            candidate = self._wall_ms() - self._bytes // BYTES_PER_MS
            self._anchor = candidate if self._anchor is None else min(self._anchor, candidate)
            self._emit(self._segmenter.feed(data))

    def flush(self) -> None:
        self._emit(self._segmenter.flush())


class RestartBudget:
    """Не больше `RESTARTS_PER_WINDOW` перезапусков за `RESTART_WINDOW_S`."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._times: deque[float] = deque()

    def wait_s(self) -> float:
        """Сколько ждать до следующего перезапуска (0 — можно сейчас)."""
        now = self._clock()
        while self._times and now - self._times[0] >= RESTART_WINDOW_S:
            self._times.popleft()
        if len(self._times) < RESTARTS_PER_WINDOW:
            return 0.0
        return RESTART_WINDOW_S - (now - self._times[0])

    def note(self) -> None:
        self._times.append(self._clock())


class Recording:
    """Запись одного интервью до отмены задачи (`paused`/`ended`/нет интервью — служба отменяет `run()`)."""

    def __init__(
        self,
        interview_id: int,
        *,
        core,
        backend,
        on_unauthorized: Callable[[], None],
        clock: Callable[[], float] = time.monotonic,
        backoff_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        wall_ms: Callable[[], int] = _wall_ms,
        device_check_s: float = DEVICE_CHECK_S,
        stall_s: float = STALL_S,
    ) -> None:
        self.interview_id = interview_id
        self._stall_s = stall_s
        self._backend = backend
        self._sleep = backoff_sleep  # только ожидания перезапуска (подменяется в тестах)
        self._wall_ms = wall_ms
        self._device_check_s = device_check_s
        self.warner = Warner(core, interview_id)
        self.senders = {
            who: Sender(core, interview_id, who, self.warner, on_unauthorized) for who in ("me", "them")
        }
        self.budget = RestartBudget(clock)
        self.starts = 0  # сколько раз запись поднималась (для тестов и журнала)
        self.devices: tuple[str, str] | None = None
        self.aec_ok: bool | None = None
        self._last_failure = "pactl"

    async def close(self) -> None:
        for sender in self.senders.values():
            await sender.close()

    async def run(self) -> None:
        while True:
            wait_s = self.budget.wait_s()
            if wait_s > 0:
                await self.warner.set("crash", warn_crash(self._last_failure, wait_s))
                await self._sleep(wait_s)
                continue
            outcome = await self._attempt()
            if outcome == "devices":
                continue  # смена устройств — перезапуск сразу, не считается падением
            self._last_failure = outcome
            self.budget.note()

    async def _clear_crash_later(self) -> None:
        """Запись простояла минуту без падений — снять предупреждение «запись звука падает»."""
        await asyncio.sleep(RESTART_WINDOW_S)
        await self.warner.clear("crash")

    async def _attempt(self) -> str:
        """Одна жизнь записи. Возвращает причину конца: `devices` (сменились устройства) или имя упавшего
        (`pactl`, `pipewire`, `ffmpeg`). Отмена задачи гасит всё и пробрасывается."""
        try:
            source, sink = await self._backend.defaults()
        except Exception as exc:  # noqa: BLE001 — pactl/pulse недоступен: перезапуск по бюджету
            _logger.warning("интервью #%s: устройства по умолчанию не узнать (%s)", self.interview_id, exc)
            await self._sleep(1.0)
            return "pactl"
        self.devices = (source, sink)
        aec = None
        streams: list[Stream] = []
        tasks: dict[asyncio.Task, str] = {}
        calm: asyncio.Task | None = None
        try:
            aec = await self._backend.start_aec(source, sink)
            self.aec_ok = aec is not None
            me_device = aec.source if aec is not None else source
            them_device = sink + ".monitor"
            _logger.info(
                "интервью #%s: запись — я: %s, собеседник: %s%s",
                self.interview_id,
                me_device,
                them_device,
                "" if aec is not None else " (без эхоподавления)",
            )
            if aec is None:
                await self.warner.set("aec", WARN_NO_AEC)
            else:
                await self.warner.clear("aec")
            self.starts += 1
            for who, device in (("me", me_device), ("them", them_device)):
                try:
                    capture = await self._backend.start_capture(device)
                except Exception as exc:  # noqa: BLE001
                    _logger.warning(
                        "интервью #%s, %s: ffmpeg не запустился (%s)", self.interview_id, who, exc
                    )
                    return "ffmpeg"
                stream = Stream(who, capture, self.senders[who], wall_ms=self._wall_ms, stall_s=self._stall_s)
                streams.append(stream)
                tasks[asyncio.create_task(stream.run())] = "ffmpeg"
            if aec is not None:
                tasks[asyncio.create_task(aec.wait())] = "pipewire"
            tasks[asyncio.create_task(self._watch_devices(source, sink))] = "devices"
            if self.warner.kind == "crash":
                calm = asyncio.create_task(self._clear_crash_later())
            done, _ = await asyncio.wait(tasks.keys(), return_when=asyncio.FIRST_COMPLETED)
            first = next(iter(done))
            reason = tasks[first]
            if reason == "devices":
                new = first.result()
                _logger.info(
                    "интервью #%s: устройства сменились (%s, %s -> %s, %s) — перезапуск записи",
                    self.interview_id,
                    source,
                    sink,
                    *new,
                )
            else:
                _logger.warning("интервью #%s: %s остановился — перезапуск записи", self.interview_id, reason)
            return reason
        finally:
            # Отмена записи (пауза, конец) не должна теряться (см. `aio.py`): прежний
            # `suppress(CancelledError)` глотал её, и цикл перезапусков крутился бесконечно.
            # Процессы гасятся при любом исходе.
            try:
                await cancel_and_wait([*tasks, calm])
            finally:
                for stream in streams:
                    stream.flush()
                await finish(self._stop_all([s.capture for s in streams], aec))

    @staticmethod
    async def _stop_all(captures, aec) -> None:
        for capture in captures:
            with contextlib.suppress(Exception):
                await capture.stop()
        if aec is not None:
            with contextlib.suppress(Exception):
                await aec.stop()

    async def _watch_devices(self, source: str, sink: str) -> tuple[str, str]:
        while True:
            await asyncio.sleep(self._device_check_s)
            try:
                current = await self._backend.defaults()
            except Exception:  # noqa: BLE001, S112 — pactl не ответил: устройства не сменились
                continue
            if current != (source, sink):
                return current


class Listener:
    """Опрос ядра и запись идущего интервью."""

    def __init__(
        self,
        *,
        core,
        backend,
        poll_s: float = POLL_S,
        recording_factory: Callable[..., Recording] | None = None,
    ) -> None:
        self._core = core
        self._backend = backend
        self._poll_s = poll_s
        self._factory = recording_factory or Recording
        self._unauthorized = asyncio.Event()
        self.recording: Recording | None = None
        self._task: asyncio.Task | None = None
        self._core_down = False

    def _on_unauthorized(self) -> None:
        self._unauthorized.set()

    async def _start(self, interview_id: int) -> None:
        _logger.info("интервью #%s идёт — запись", interview_id)
        self.recording = self._factory(
            interview_id, core=self._core, backend=self._backend, on_unauthorized=self._on_unauthorized
        )
        self._task = asyncio.create_task(self.recording.run())

    async def stop_recording(self) -> None:
        task, self._task = self._task, None
        recording, self.recording = self.recording, None
        try:
            await cancel_and_wait([task])
        finally:
            if recording is not None:
                _logger.info("интервью #%s: запись остановлена", recording.interview_id)
                await finish(recording.close())

    async def step(self) -> None:
        """Один опрос: свести запись к состоянию ядра. Ядро недоступно — состояние не меняется (после рестарта
        ядро само ставит интервью на паузу, П6)."""
        try:
            item = await self._core.current()
        except CoreUnavailable as exc:
            if not self._core_down:
                _logger.warning("ядро недоступно (%s) — жду", exc)
            self._core_down = True
            return
        if self._core_down:
            _logger.info("ядро снова отвечает")
        self._core_down = False
        if self._task is not None and self._task.done():
            # Запись умерла сама (не отменой): отказ доступа — выход 78, прочее — поднять заново.
            failure = None if self._task.cancelled() else self._task.exception()
            await self.stop_recording()
            if isinstance(failure, CoreUnauthorized):
                raise failure
            if failure is not None:
                _logger.error("запись упала: %s", failure.__class__.__name__)
        wanted = item["id"] if item is not None and item.get("status") == "active" else None
        current = self.recording.interview_id if self.recording is not None else None
        if wanted == current:
            return
        await self.stop_recording()
        if wanted is not None:
            await self._start(wanted)

    async def run(self, stop: asyncio.Event) -> int:
        try:
            while not stop.is_set():
                try:
                    await self.step()
                except CoreUnauthorized as exc:
                    _logger.error("listen: %s", exc)
                    return EXIT_UNAUTHORIZED
                if self._unauthorized.is_set():
                    return EXIT_UNAUTHORIZED
                waiters = {asyncio.create_task(stop.wait()), asyncio.create_task(self._unauthorized.wait())}
                _done, pending = await asyncio.wait(
                    waiters, timeout=self._poll_s, return_when=asyncio.FIRST_COMPLETED
                )
                await cancel_and_wait(waiters)
                if self._unauthorized.is_set():
                    return EXIT_UNAUTHORIZED
            return 0
        finally:
            await self.stop_recording()


def run(*, config_dir, base_url: str, backend=None) -> int:
    """`hub listen run`: нет токена — 78 (юнит не перезапускается)."""
    from . import files

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if files.read_token(config_dir) is None:
        _logger.error("listen: нет токена ядра — hub listen setup")
        return EXIT_UNAUTHORIZED

    async def _main() -> int:
        import signal

        from .audio import PulseBackend

        core = ListenCore(token_file=files.token_path(config_dir), base_url=base_url)
        listener = Listener(core=core, backend=backend or PulseBackend())
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        _logger.info("listen: старт, опрос ядра раз в %.0f с", POLL_S)
        try:
            return await listener.run(stop)
        finally:
            await core.aclose()

    return asyncio.run(_main())
