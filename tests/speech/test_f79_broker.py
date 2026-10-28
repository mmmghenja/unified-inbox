"""Ф79-1, раздел A (docs/architecture.md): посредник своего whisper в памяти ядра —
нет работника, ответ работника, срок, поздний ответ, ошибка работника, очередь 8, живость, обрыв опроса."""

import asyncio
import base64
import time

import pytest

from hub.speech import local
from hub.speech.local import SpeechBroker


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


async def _work(broker: SpeechBroker, reply, *, token_id=1, wait_s=5.0):
    job = await broker.lease(wait_s, token_id=token_id)
    if job is not None and reply is not None:
        assert broker.result(job["job_id"], token_id=token_id, **reply(job)) is True
    return job


async def test_no_worker_is_none_at_once():
    broker = SpeechBroker()
    started = time.monotonic()
    assert await broker.recognize(b"\x00\x01" * 100, "ru", timeout_s=5) is None
    assert time.monotonic() - started < 0.5
    assert broker.pending == 0


async def test_worker_answers_text_and_gets_audio_and_lang():
    broker = SpeechBroker()
    pcm = b"\x01\x02" * 400
    worker = asyncio.create_task(_work(broker, lambda job: {"text": "привет", "error": None}))
    await asyncio.sleep(0.01)
    assert broker.worker_alive()
    result = await broker.recognize(pcm, "ru", timeout_s=2)
    job = await worker
    assert result is not None and result.text == "привет" and result.elapsed_ms >= 0
    assert base64.b64decode(job["audio_b64"]) == pcm and job["lang"] == "ru"
    assert broker.pending == 0


async def test_empty_text_is_a_result_not_a_failure():
    broker = SpeechBroker()
    worker = asyncio.create_task(_work(broker, lambda job: {"text": "", "error": None}))
    await asyncio.sleep(0.01)
    result = await broker.recognize(b"\x00\x00" * 10, "ru", timeout_s=2)
    await worker
    assert result is not None and result.text == ""


async def test_worker_error_is_none():
    broker = SpeechBroker()
    worker = asyncio.create_task(_work(broker, lambda job: {"text": None, "error": "engine_failed"}))
    await asyncio.sleep(0.01)
    assert await broker.recognize(b"\x00\x00" * 10, "ru", timeout_s=2) is None
    await worker


async def test_timeout_is_none_and_late_answer_is_stale():
    broker = SpeechBroker()
    worker = asyncio.create_task(_work(broker, None))  # берёт и молчит
    await asyncio.sleep(0.01)
    started = time.monotonic()
    assert await broker.recognize(b"\x00\x00" * 10, "ru", timeout_s=0.2) is None
    assert time.monotonic() - started < 1.5
    job = await worker
    assert broker.result(job["job_id"], token_id=1, text="поздно", error=None) is False
    assert broker.pending == 0


async def test_job_dropped_before_lease_is_not_handed_out():
    broker = SpeechBroker()
    broker.mark_seen()  # жив, но сейчас не ждёт
    assert await broker.recognize(b"\x00\x00" * 10, "ru", timeout_s=0.05) is None
    assert await broker.lease(0, token_id=1) is None  # снятое задание не выдаётся


async def test_answer_from_other_token_or_unknown_job_is_stale():
    broker = SpeechBroker()
    worker = asyncio.create_task(broker.lease(5, token_id=1))
    await asyncio.sleep(0.01)
    pending = asyncio.create_task(broker.recognize(b"\x00\x00" * 10, "ru", timeout_s=2))
    job = await worker
    assert broker.result(job["job_id"], token_id=2, text="чужой", error=None) is False
    assert broker.result("deadbeef", token_id=1, text="нет такого", error=None) is False
    assert broker.result(job["job_id"], token_id=1, text="свой", error=None) is True
    assert (await pending).text == "свой"
    assert broker.result(job["job_id"], token_id=1, text="второй раз", error=None) is False


async def test_queue_cap_8():
    broker = SpeechBroker()
    broker.mark_seen()
    waiting = [asyncio.create_task(broker.recognize(b"\x00\x00" * 10, "ru", timeout_s=5)) for _ in range(8)]
    await asyncio.sleep(0.01)
    assert broker.pending == local.QUEUE_MAX == 8
    started = time.monotonic()
    assert await broker.recognize(b"\x00\x00" * 10, "ru", timeout_s=5) is None  # полна — мимо, сразу
    assert time.monotonic() - started < 0.5
    broker.close()
    assert await asyncio.gather(*waiting) == [None] * 8


async def test_alive_30s_after_poll_entry_and_while_waiting():
    clock = Clock()
    broker = SpeechBroker(clock=clock)
    assert not broker.worker_alive()
    assert await broker.lease(0, token_id=1) is None
    clock.t += local.WORKER_SEEN_S
    assert broker.worker_alive()
    clock.t += 0.5
    assert not broker.worker_alive()
    poll = asyncio.create_task(broker.lease(0.3, token_id=1))
    await asyncio.sleep(0.01)
    clock.t += 1000  # ждёт в опросе — жив, сколько бы ни прошло
    assert broker.worker_alive()
    assert await poll is None


async def test_disconnected_poll_stops_and_does_not_keep_worker_alive(monkeypatch):
    monkeypatch.setattr(local, "_SLICE_S", 0.02)
    clock = Clock()
    broker = SpeechBroker(clock=clock)

    async def gone() -> bool:
        return True

    started = time.monotonic()
    assert await broker.lease(20, token_id=1, disconnected=gone) is None
    assert time.monotonic() - started < 1
    clock.t += local.WORKER_SEEN_S + 1
    assert not broker.worker_alive()


async def test_close_wakes_poll_and_marks_dead():
    broker = SpeechBroker()
    poll = asyncio.create_task(broker.lease(20, token_id=1))
    await asyncio.sleep(0.01)
    broker.close()
    assert await asyncio.wait_for(poll, 2) is None
    assert not broker.worker_alive()


@pytest.mark.parametrize("wait_s", [-5, 0])
async def test_lease_without_work_is_none(wait_s):
    broker = SpeechBroker()
    assert await broker.lease(wait_s, token_id=1) is None
