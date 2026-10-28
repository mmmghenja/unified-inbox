"""Ф60-1, часть B: цикл службы `hub listen run` с подменным звуком и подменным ядром (раздел 3, П9–П12).

Звука нет: «процессы» — подделки с теми же методами, что у `audio.PulseBackend`."""

import asyncio
import functools
import logging
import math
from array import array

import pytest

from hub.listen import service
from hub.listen.core_api import CoreUnauthorized, CoreUnavailable, Reply
from hub.listen.segment import SAMPLE_RATE


def _speech_pcm(ms: int, amp: int = 8000) -> bytes:
    return array(
        "h",
        (int(amp * math.sin(2 * math.pi * 310 * i / SAMPLE_RATE)) for i in range(ms * SAMPLE_RATE // 1000)),
    ).tobytes()


def _quiet_pcm(ms: int) -> bytes:
    return array("h", ((i % 7) - 3 for i in range(ms * SAMPLE_RATE // 1000))).tobytes()


class FakeCapture:
    def __init__(self, device: str, *, dead: bool = False) -> None:
        self.device = device
        self.stopped = False
        self._q: asyncio.Queue[bytes] = asyncio.Queue()
        if dead:
            self._q.put_nowait(b"")

    def feed(self, data: bytes) -> None:
        for i in range(0, len(data), 3200):
            self._q.put_nowait(data[i : i + 3200])

    def die(self) -> None:
        self._q.put_nowait(b"")

    async def read(self) -> bytes:
        return await self._q.get()

    async def wait(self) -> int:
        return 0

    async def stop(self) -> None:
        self.stopped = True
        self._q.put_nowait(b"")


class FakeAec:
    def __init__(self, source: str) -> None:
        self.source = source
        self.stopped = False
        self.exited = asyncio.Event()

    async def wait(self) -> int:
        await self.exited.wait()
        return 1

    async def stop(self) -> None:
        self.stopped = True


class FakeBackend:
    def __init__(self) -> None:
        self.devices = ("mic_a", "spk_a")
        self.aec_ok = True
        self.dead_captures = False
        self.aecs: list[FakeAec] = []
        self.captures: list[FakeCapture] = []
        self.aec_calls: list[tuple[str, str]] = []

    async def defaults(self) -> tuple[str, str]:
        return self.devices

    async def start_aec(self, source: str, sink: str):
        self.aec_calls.append((source, sink))
        if not self.aec_ok:
            return None
        aec = FakeAec(f"hub_listen_aec_1_{len(self.aecs) + 1}_source")
        self.aecs.append(aec)
        return aec

    async def start_capture(self, device: str) -> FakeCapture:
        capture = FakeCapture(device, dead=self.dead_captures)
        self.captures.append(capture)
        return capture

    def live(self) -> list:
        return [c for c in self.captures if not c.stopped] + [a for a in self.aecs if not a.stopped]


class FakeCore:
    def __init__(self) -> None:
        self.item: dict | None = None
        self.chunks: list[dict] = []
        self.warns: list[tuple[int, str]] = []
        self.chunk_reply = Reply(200)
        self.chunk_gate: asyncio.Event | None = None
        self.in_flight = 0
        self.max_in_flight = 0
        self.current_exc: Exception | None = None
        self.chunk_exc: Exception | None = None

    async def current(self):
        if self.current_exc is not None:
            raise self.current_exc
        return self.item

    async def chunk(self, interview_id, *, who, start_ms, end_ms, pcm):
        if self.chunk_exc is not None:
            raise self.chunk_exc
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.chunk_gate is not None:
                await self.chunk_gate.wait()
            self.chunks.append(
                {"id": interview_id, "who": who, "start_ms": start_ms, "end_ms": end_ms, "bytes": len(pcm)}
            )
            return self.chunk_reply
        finally:
            self.in_flight -= 1

    async def warn(self, interview_id, text):
        self.warns.append((interview_id, text))
        return Reply(200)


async def _until(cond, limit_s: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit_s
    while not cond():
        if loop.time() > deadline:
            raise AssertionError("не дождались")
        await asyncio.sleep(0.005)


def _listener(core, backend, **rec_kwargs):
    factory = functools.partial(service.Recording, **rec_kwargs) if rec_kwargs else None
    return service.Listener(core=core, backend=backend, poll_s=0.01, recording_factory=factory)


async def test_no_interview_no_sound_processes():
    core, backend = FakeCore(), FakeBackend()
    listener = _listener(core, backend)
    await listener.step()
    core.item = {"id": 5, "status": "paused", "them_lang": "ru-RU"}
    await listener.step()
    await asyncio.sleep(0.05)
    assert backend.aec_calls == [] and backend.captures == [] and listener.recording is None


async def test_active_records_me_from_aec_them_from_monitor_and_pause_kills_everything():
    core, backend = FakeCore(), FakeBackend()
    listener = _listener(core, backend, wall_ms=lambda: 1_700_000_000_000)
    core.item = {"id": 7, "status": "active", "them_lang": "en-US"}
    await listener.step()
    await _until(lambda: len(backend.captures) == 2)
    assert backend.aec_calls == [("mic_a", "spk_a")]
    me, them = backend.captures
    assert me.device == backend.aecs[0].source and them.device == "spk_a.monitor"
    # Эхоподавление поднялось — предупреждение прежнего запуска снимается (пустая строка), нового нет.
    await _until(lambda: core.warns == [(7, "")])

    them.feed(_quiet_pcm(600) + _speech_pcm(1500) + _quiet_pcm(1200))
    await _until(lambda: len(core.chunks) == 1)
    sent = core.chunks[0]
    assert sent["id"] == 7 and sent["who"] == "them"
    # П10: время стены, мс от эпохи (якорь потока = «сейчас» − прочитанное к моменту куска; часы в тесте
    # стоят, поэтому начало речи — за ~2–3 с до «сейчас»).
    assert 1_700_000_000_000 - 3000 <= sent["start_ms"] <= 1_700_000_000_000 - 2000
    assert sent["bytes"] == (sent["end_ms"] - sent["start_ms"]) * 32

    core.item = {"id": 7, "status": "paused", "them_lang": "en-US"}
    await listener.step()
    assert listener.recording is None
    assert backend.live() == []  # оба ffmpeg и pipewire погашены

    core.item = {"id": 7, "status": "active", "them_lang": "en-US"}
    await listener.step()
    await _until(lambda: len(backend.captures) == 4)
    assert len(backend.live()) == 3

    core.item = None  # окончено: ядро отдаёт null
    await listener.step()
    assert backend.live() == [] and listener.recording is None


async def test_ended_status_and_other_interview_switch():
    core, backend = FakeCore(), FakeBackend()
    listener = _listener(core, backend)
    core.item = {"id": 1, "status": "active", "them_lang": "ru-RU"}
    await listener.step()
    await _until(lambda: len(backend.captures) == 2)
    core.item = {"id": 2, "status": "active", "them_lang": "ru-RU"}
    await listener.step()
    await _until(lambda: len(backend.captures) == 4)
    assert listener.recording.interview_id == 2
    assert all(c.stopped for c in backend.captures[:2]) and backend.aecs[0].stopped
    core.item = {"id": 2, "status": "ended", "them_lang": "ru-RU"}
    await listener.step()
    assert backend.live() == []


async def test_no_aec_falls_back_to_raw_mic_and_warns():
    core, backend = FakeCore(), FakeBackend()
    backend.aec_ok = False
    listener = _listener(core, backend)
    core.item = {"id": 3, "status": "active", "them_lang": "ru-RU"}
    await listener.step()
    await _until(lambda: len(backend.captures) == 2)
    assert [c.device for c in backend.captures] == ["mic_a", "spk_a.monitor"]
    await _until(lambda: core.warns == [(3, service.WARN_NO_AEC)])
    await listener.stop_recording()


async def test_409_on_chunk_in_flight_is_swallowed(caplog):
    core, backend = FakeCore(), FakeBackend()
    core.chunk_reply = Reply(409, "interview_not_active")
    listener = _listener(core, backend)
    core.item = {"id": 4, "status": "active", "them_lang": "ru-RU"}
    await listener.step()
    await _until(lambda: len(backend.captures) == 2)
    await _until(lambda: core.warns == [(4, "")])
    with caplog.at_level(logging.INFO, logger="hub.listen"):
        backend.captures[0].feed(_quiet_pcm(300) + _speech_pcm(800) + _quiet_pcm(1000))
        await _until(lambda: len(core.chunks) == 1)
        await asyncio.sleep(0.02)
    assert core.warns == [(4, "")]
    assert not [r for r in caplog.records if "кусок" in r.getMessage() or r.levelno >= logging.WARNING]
    await listener.stop_recording()


async def test_speechkit_off_warns_and_429_only_logs():
    core, backend = FakeCore(), FakeBackend()
    listener = _listener(core, backend)
    core.item = {"id": 6, "status": "active", "them_lang": "ru-RU"}
    await listener.step()
    await _until(lambda: len(backend.captures) == 2)
    core.chunk_reply = Reply(429, "listen_rate")
    backend.captures[1].feed(_quiet_pcm(300) + _speech_pcm(800) + _quiet_pcm(1000))
    await _until(lambda: len(core.chunks) == 1)
    core.chunk_reply = Reply(503, "speechkit_off")
    backend.captures[1].feed(_speech_pcm(800) + _quiet_pcm(1000))
    await _until(lambda: (6, service.WARN_SPEECH_OFF) in core.warns)
    # Ключ появился — первый принятый кусок снимает своё предупреждение.
    core.chunk_reply = Reply(200)
    backend.captures[1].feed(_speech_pcm(800) + _quiet_pcm(1000))
    await _until(lambda: core.warns[-1] == (6, ""))
    await listener.stop_recording()


async def test_at_most_two_requests_per_stream():
    core = FakeCore()
    core.chunk_gate = asyncio.Event()
    sender = service.Sender(core, 9, "me", service.Warner(core, 9), lambda: None)
    for i in range(5):
        sender.put(i * 1000, i * 1000 + 500, b"\0\0" * 8000)
    await asyncio.sleep(0.05)
    assert core.in_flight == 2
    core.chunk_gate.set()
    await asyncio.wait_for(sender.join(), 2)
    assert core.max_in_flight == 2 and len(core.chunks) == 5
    assert sorted(c["start_ms"] for c in core.chunks) == [0, 1000, 2000, 3000, 4000]
    await sender.close()


async def test_device_change_restarts_with_new_devices():
    core, backend = FakeCore(), FakeBackend()
    listener = _listener(core, backend, device_check_s=0.01)
    core.item = {"id": 8, "status": "active", "them_lang": "ru-RU"}
    await listener.step()
    await _until(lambda: len(backend.captures) == 2)
    backend.devices = ("bt_headset_mic", "bt_headset_out")  # включили гарнитуру / BT перешёл в HFP
    await _until(lambda: len(backend.captures) == 4)
    assert backend.aec_calls[-1] == ("bt_headset_mic", "bt_headset_out")
    assert [c.device for c in backend.captures[2:]] == [backend.aecs[1].source, "bt_headset_out.monitor"]
    assert all(c.stopped for c in backend.captures[:2]) and backend.aecs[0].stopped
    assert listener.recording.budget.wait_s() == 0  # смена устройств — не падение
    await listener.stop_recording()
    assert backend.live() == []


async def test_pipewire_crash_restarts_recording():
    core, backend = FakeCore(), FakeBackend()
    listener = _listener(core, backend)
    core.item = {"id": 10, "status": "active", "them_lang": "ru-RU"}
    await listener.step()
    await _until(lambda: len(backend.captures) == 2)
    backend.aecs[0].exited.set()  # pipewire упал
    await _until(lambda: len(backend.captures) == 4)
    assert backend.aecs[0].stopped and all(c.stopped for c in backend.captures[:2])
    await listener.stop_recording()


async def test_crash_loop_three_restarts_a_minute_then_warn():
    """Конечная синтетика: подменное ожидание перезапуска после первого вызова стоит до отмены — цикл не
    крутится (14.10 прежний вариант с мгновенным ожиданием и потерянной отменой крутил перезапуски
    бесконечно и съел 19,5 ГБ)."""
    core, backend = FakeCore(), FakeBackend()
    backend.dead_captures = True  # ffmpeg выходит сразу
    clock = [1000.0]
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += seconds
        await asyncio.Event().wait()  # дальше не идём: ровно один круг бюджета

    rec = service.Recording(
        11,
        core=core,
        backend=backend,
        on_unauthorized=lambda: None,
        clock=lambda: clock[0],
        backoff_sleep=fake_sleep,
    )
    task = asyncio.create_task(rec.run())
    try:
        await _until(lambda: bool(slept))
    finally:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert rec.starts == 3 and len(backend.captures) == 6 and slept == [pytest.approx(60.0)]
    first_warn = next(i for i, (_n, text) in enumerate(core.warns) if "падает" in text)
    assert core.warns[first_warn] == (11, service.warn_crash("ffmpeg", 60.0))
    await rec.close()
    assert backend.live() == []


async def test_unauthorized_exits_78():
    core, backend = FakeCore(), FakeBackend()
    core.current_exc = CoreUnauthorized("отозван")
    listener = _listener(core, backend)
    assert await asyncio.wait_for(listener.run(asyncio.Event()), 2) == service.EXIT_UNAUTHORIZED


async def test_unauthorized_on_chunk_exits_78_and_stops_sound():
    core, backend = FakeCore(), FakeBackend()
    core.item = {"id": 12, "status": "active", "them_lang": "ru-RU"}
    core.chunk_exc = CoreUnauthorized("отозван")
    listener = _listener(core, backend)
    task = asyncio.create_task(listener.run(asyncio.Event()))
    await _until(lambda: len(backend.captures) == 2)
    backend.captures[0].feed(_quiet_pcm(300) + _speech_pcm(800) + _quiet_pcm(1000))
    assert await asyncio.wait_for(task, 2) == service.EXIT_UNAUTHORIZED
    assert backend.live() == []


async def test_core_unavailable_keeps_state_and_stop_event_ends_cleanly():
    core, backend = FakeCore(), FakeBackend()
    core.item = {"id": 13, "status": "active", "them_lang": "ru-RU"}
    listener = _listener(core, backend)
    stop = asyncio.Event()
    task = asyncio.create_task(listener.run(stop))
    await _until(lambda: len(backend.captures) == 2)
    core.current_exc = CoreUnavailable("ConnectError")
    await asyncio.sleep(0.05)
    assert listener.recording is not None and len(backend.live()) == 3
    stop.set()
    assert await asyncio.wait_for(task, 2) == 0
    assert backend.live() == []


async def test_stalled_stream_restarts_recording():
    """ffmpeg жив, но звука нет дольше `stall_s` — запись поднимается заново."""
    core, backend = FakeCore(), FakeBackend()
    listener = _listener(core, backend, stall_s=0.05)
    core.item = {"id": 14, "status": "active", "them_lang": "ru-RU"}
    await listener.step()
    await _until(lambda: len(backend.captures) >= 4)
    assert all(c.stopped for c in backend.captures[:2]) and backend.aecs[0].stopped
    await listener.stop_recording()
    assert backend.live() == []


async def test_cancel_during_cleanup_is_not_swallowed():
    """Отмена записи, пришедшая, пока запись гасит свои задачи, доходит до конца (прежний
    `suppress(CancelledError): await task` её глотал — цикл перезапусков не останавливался)."""
    from hub.listen import aio

    child_done = asyncio.Event()

    async def slow_child() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)  # гасится не сразу, как настоящий процесс
            child_done.set()
            raise

    child = asyncio.create_task(slow_child())
    await asyncio.sleep(0)
    outer = asyncio.create_task(aio.cancel_and_wait([child]))
    await asyncio.sleep(0.01)  # outer ждёт child
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(outer, 2)
    assert child_done.is_set() and child.done()


async def test_pause_while_restarting_stops_for_good():
    """Пауза приходит, пока запись гасит упавший поток: запись не поднимается снова."""
    core, backend = FakeCore(), FakeBackend()
    listener = _listener(core, backend)
    core.item = {"id": 15, "status": "active", "them_lang": "ru-RU"}
    await listener.step()
    await _until(lambda: len(backend.captures) == 2)
    backend.captures[0].die()
    core.item = {"id": 15, "status": "paused", "them_lang": "ru-RU"}
    await listener.step()
    await asyncio.sleep(0.05)
    assert listener.recording is None and backend.live() == []
    count = len(backend.captures)
    await asyncio.sleep(0.05)
    assert len(backend.captures) == count


async def test_recording_died_from_unauthorized_warn_exits_78():
    """Отказ доступа на предупреждении (не на куске) роняет задачу записи — служба выходит с 78."""
    core, backend = FakeCore(), FakeBackend()
    backend.aec_ok = False

    async def warn_denied(interview_id, text):
        raise CoreUnauthorized("отозван")

    core.warn = warn_denied
    core.item = {"id": 16, "status": "active", "them_lang": "ru-RU"}
    listener = _listener(core, backend)
    assert await asyncio.wait_for(listener.run(asyncio.Event()), 2) == service.EXIT_UNAUTHORIZED
    assert backend.live() == []
