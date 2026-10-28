"""Цикл службы `hub transcribe run` (Ф14-6, docs/architecture.md):
подделка ядра (лиз/скачивание/продление/отчёт) и подделка движка тем же протоколом (`fake_engine.py`);
ffmpeg — настоящий (перекодирование в wav 16 кГц моно)."""

import asyncio
import math
import os
import shutil
import struct
import sys
import wave
from pathlib import Path

import pytest

from hub.transcribe import files, service
from hub.transcribe.core_api import CoreError, CoreUnauthorized, CoreUnavailable

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="нет ffmpeg")

_FAKE_ENGINE = Path(__file__).with_name("fake_engine.py")


def _tone_wav(path: Path, seconds: float, rate: int = 44100) -> bytes:
    with wave.open(str(path), "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(rate)
        frames = b"".join(
            struct.pack("<hh", v, v)
            for v in (int(8000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(int(seconds * rate)))
        )
        f.writeframes(frames)
    return path.read_bytes()


class FakeCore:
    def __init__(self, jobs=(), *, payload: bytes = b"", download_error: Exception | None = None) -> None:
        self.jobs = list(jobs)
        self.payload = payload
        self.download_error = download_error
        self.reports: list[tuple[int, dict]] = []
        self.extends: list[tuple[int, int, float]] = []
        self.lease_waits: list[float] = []
        self.lease_fast: list[bool] = []
        self.report_error: Exception | None = None
        self.seen_files: list[Path] = []

    async def lease(self, wait_s: float, *, fast: bool = False):
        self.lease_waits.append(wait_s)
        self.lease_fast.append(fast)
        if not self.jobs:
            return None
        job = self.jobs.pop(0)
        if isinstance(job, Exception):
            raise job
        return job

    async def download(self, media_id: int, dest: Path) -> int:
        if self.download_error is not None:
            raise self.download_error
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(self.payload)
        self.seen_files.append(dest)
        return len(self.payload)

    async def extend(self, media_id: int, lease_gen: int, duration_s: float):
        self.extends.append((media_id, lease_gen, duration_s))
        return {"lease_expires_ts": 1}

    async def report(self, media_id: int, body: dict):
        if self.report_error is not None:
            raise self.report_error
        self.reports.append((media_id, body))
        return {"ok": True}


def _job(media_id: int = 7, *, max_s: int = 600, manual: bool = False) -> dict:
    return {
        "media_id": media_id,
        "message_id": 100,
        "lease_gen": 3,
        "kind": "voice",
        "mime": "audio/ogg",
        "detected": None,
        "duration_s": None,
        "size": 1000,
        "manual": manual,
        "max_s": max_s,
    }


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _worker(
    tmp_path: Path, core: FakeCore, mode: str = "ok", *, clock=None, idle: float = 300
) -> service.Worker:
    data_dir = tmp_path / "data"
    files.clean_tmp(data_dir)
    config = {**files.DEFAULT_CONFIG, "idle_unload_s": idle}
    engine = service.Engine([sys.executable, str(_FAKE_ENGINE), mode, str(tmp_path / "pids")])
    return service.Worker(core=core, config=config, data_dir=data_dir, engine=engine, clock=clock or Clock())


def _pids(tmp_path: Path) -> list[str]:
    path = tmp_path / "pids"
    return path.read_text().split() if path.exists() else []


async def test_job_done_reports_result_extends_lease_and_removes_files(tmp_path) -> None:
    payload = _tone_wav(tmp_path / "src.wav", 2.0)
    core = FakeCore([_job()], payload=payload)
    worker = _worker(tmp_path, core)
    try:
        assert await worker.step() is True
    finally:
        await worker.engine.kill()
    [(media_id, body)] = core.reports
    assert media_id == 7
    assert body["outcome"] == "done" and body["lease_gen"] == 3
    assert body["text"] == "проверка 7-3.wav" and body["lang"] == "ru" and body["lang_prob"] == 0.91
    assert body["elapsed_ms"] == 1250 and body["device"] == "cuda" and body["model"] == "large-v3-turbo"
    [(_, gen, duration)] = core.extends
    assert gen == 3 and duration == pytest.approx(2.0, abs=0.05)  # П8: от длительности wav, не события
    assert list(files.tmp_dir(worker.data_dir).iterdir()) == []  # П10: файлы задания удалены
    state = files.load_state(worker.data_dir)
    assert state["counters"] == {"done": 1} and state["last_ok_ts"] and state["engine_alive"] is True
    assert "проверка" not in json_dump(state)  # текста расшифровки в состоянии нет


def json_dump(value) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


async def test_manual_flag_goes_to_engine(tmp_path) -> None:
    """14-6b: ручной повтор (`manual` в задании) передаётся движку — тот при 0 с речи идёт без VAD."""
    payload = _tone_wav(tmp_path / "a.wav", 1.0)
    core = FakeCore([_job(7, manual=True), _job(8)], payload=payload)
    worker = _worker(tmp_path, core)
    try:
        assert await worker.step() and await worker.step()
    finally:
        await worker.engine.close()
    assert [body["model"] for _m, body in core.reports] == ["large-v3-turbo manual", "large-v3-turbo"]


async def test_doubtful_result_is_done_with_problem(tmp_path) -> None:
    core = FakeCore([_job()], payload=_tone_wav(tmp_path / "src.wav", 1.0))
    worker = _worker(tmp_path, core, "doubtful")
    try:
        await worker.step()
    finally:
        await worker.engine.kill()
    body = core.reports[0][1]
    assert body["outcome"] == "done" and body["problem"] == "пусто" and body["text"] == ""
    assert body["lang"] is None


async def test_engine_error_code_is_failed(tmp_path) -> None:
    core = FakeCore([_job()], payload=_tone_wav(tmp_path / "src.wav", 1.0))
    worker = _worker(tmp_path, core, "error")
    try:
        await worker.step()
    finally:
        await worker.engine.kill()
    body = core.reports[0][1]
    assert body == {"lease_gen": 3, "outcome": "failed", "error": "RuntimeError: сломалось"}
    assert files.load_state(worker.data_dir)["errors"][-1]["where"] == "media 7"


async def test_engine_crash_is_failed_and_next_job_starts_new_process(tmp_path) -> None:
    payload = _tone_wav(tmp_path / "src.wav", 1.0)
    core = FakeCore([_job(7), _job(8)], payload=payload)
    worker = _worker(tmp_path, core, "crash")
    try:
        await worker.step()
        assert worker.engine.alive is False
        await worker.step()
    finally:
        await worker.engine.kill()
    assert [b["outcome"] for _, b in core.reports] == ["failed", "failed"]
    assert core.reports[0][1]["error"] == "движок упал"
    pids = _pids(tmp_path)
    assert len(pids) == 2 and pids[0] != pids[1]


async def test_engine_garbage_is_failed(tmp_path) -> None:
    core = FakeCore([_job()], payload=_tone_wav(tmp_path / "src.wav", 1.0))
    worker = _worker(tmp_path, core, "garbage")
    try:
        await worker.step()
    finally:
        await worker.engine.kill()
    assert core.reports[0][1]["error"] == "движок ответил не по протоколу"
    assert worker.engine.alive is False


async def test_engine_hang_is_killed_by_timeout(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service, "ENGINE_MIN_TIMEOUT_S", 0.5)
    monkeypatch.setattr(service, "ENGINE_LOAD_S", 0.0)
    core = FakeCore([_job()], payload=_tone_wav(tmp_path / "src.wav", 0.2))
    worker = _worker(tmp_path, core, "hang")
    proc_holder = {}
    original_start = worker.engine.start

    async def start():
        await original_start()
        proc_holder["proc"] = worker.engine.proc

    worker.engine.start = start
    await asyncio.wait_for(worker.step(), 10)
    body = core.reports[0][1]
    assert body["outcome"] == "failed" and body["error"].startswith("движок не ответил за")
    assert worker.engine.alive is False
    assert proc_holder["proc"].returncode is not None  # процесс убит, а не брошен


async def test_first_job_timeout_includes_model_load(tmp_path, monkeypatch) -> None:
    seen = []

    async def fake_wait_for(awaitable, limit_s):
        seen.append(limit_s)
        return await awaitable

    core = FakeCore([_job(7), _job(8)], payload=_tone_wav(tmp_path / "src.wav", 1.0))
    worker = _worker(tmp_path, core)
    monkeypatch.setattr(service.asyncio, "wait_for", fake_wait_for)
    try:
        await worker.step()
        await worker.step()
    finally:
        await worker.engine.kill()
    engine_timeouts = [t for t in seen if t >= service.ENGINE_MIN_TIMEOUT_S and t != service.FFMPEG_TIMEOUT_S]
    assert engine_timeouts == [
        service.ENGINE_MIN_TIMEOUT_S + service.ENGINE_LOAD_S,
        service.ENGINE_MIN_TIMEOUT_S,
    ]


async def test_idle_unload_closes_engine_and_next_job_reloads(tmp_path) -> None:
    clock = Clock()
    core = FakeCore([_job(7)], payload=_tone_wav(tmp_path / "src.wav", 1.0))
    worker = _worker(tmp_path, core, clock=clock, idle=20)
    try:
        await worker.step()
        assert worker.engine.alive
        clock.t += 5
        assert await worker.step() is False
        assert worker.engine.alive  # 5 с < 20 с — держим
        assert core.lease_waits[-1] == pytest.approx(15)  # ждём лиз не дольше, чем до выгрузки
        proc = worker.engine.proc
        clock.t += 16
        await worker.step()
        assert worker.engine.alive is False and proc.returncode == 0  # закрыт по stdin, сам вышел
        assert files.load_state(worker.data_dir)["engine_alive"] is False
        assert core.lease_waits[-1] == service.LEASE_WAIT_S
        core.jobs.append(_job(8))
        await worker.step()
        assert worker.engine.alive
    finally:
        await worker.engine.kill()
    assert [b["outcome"] for _, b in core.reports] == ["done", "done"]
    assert len(set(_pids(tmp_path))) == 2


async def test_download_404_is_skip_other_errors_failed(tmp_path) -> None:
    core = FakeCore([_job()], download_error=CoreError(404))
    worker = _worker(tmp_path, core)
    await worker.step()
    assert core.reports[0][1] == {"lease_gen": 3, "outcome": "skip", "error": "файла нет в хабе"}
    assert worker.engine.alive is False  # движок не поднимался
    core2 = FakeCore([_job()], download_error=CoreError(403))
    worker2 = _worker(tmp_path / "b", core2)
    await worker2.step()
    assert core2.reports[0][1]["outcome"] == "failed"


async def test_not_audio_is_skip_by_ffmpeg(tmp_path) -> None:
    core = FakeCore([_job()], payload="это не звук, а текст".encode() * 10)
    worker = _worker(tmp_path, core)
    await worker.step()
    assert core.reports[0][1] == {"lease_gen": 3, "outcome": "skip", "error": "нет звука (ffmpeg)"}
    assert worker.engine.alive is False
    assert list(files.tmp_dir(worker.data_dir).iterdir()) == []


async def test_ffmpeg_missing_is_skip(tmp_path) -> None:
    core = FakeCore([_job()], payload=_tone_wav(tmp_path / "src.wav", 1.0))
    worker = _worker(tmp_path, core)
    worker.config["ffmpeg"] = str(tmp_path / "нет-ffmpeg")
    await worker.step()
    assert core.reports[0][1]["outcome"] == "skip"
    assert core.reports[0][1]["error"].startswith("ffmpeg не запускается")


async def test_video_round_audio_track_is_extracted(tmp_path) -> None:
    """Кружок — mp4 с видео: ffmpeg берёт только звук (`-vn`)."""
    src = tmp_path / "src.wav"
    _tone_wav(src, 1.0)
    mp4 = tmp_path / "round.mp4"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=64x64:d=1",
        "-i",
        str(src),
        "-shortest",
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        str(mp4),
    )
    if await proc.wait() != 0:
        pytest.skip("ffmpeg без libx264/aac")
    core = FakeCore([{**_job(), "kind": "video", "mime": "video/mp4"}], payload=mp4.read_bytes())
    worker = _worker(tmp_path, core)
    try:
        await worker.step()
    finally:
        await worker.engine.kill()
    assert core.reports[0][1]["outcome"] == "done"
    assert core.extends[0][2] == pytest.approx(1.0, abs=0.1)


async def test_too_long_is_skip_with_reason(tmp_path) -> None:
    payload = _tone_wav(tmp_path / "src.wav", 2.0)
    core = FakeCore([_job(7, max_s=1), _job(8, max_s=1, manual=True)], payload=payload)
    worker = _worker(tmp_path, core)
    await worker.step()
    await worker.step()
    assert [b.get("error") for _, b in core.reports] == ["длиннее 10 мин — по кнопке", "длиннее 60 мин"]
    assert core.extends == [] and worker.engine.alive is False


async def test_report_conflict_is_logged_not_fatal(tmp_path) -> None:
    core = FakeCore([_job(7)], payload=_tone_wav(tmp_path / "src.wav", 1.0))
    core.report_error = CoreError(409)
    worker = _worker(tmp_path, core)
    try:
        assert await worker.step() is True
    finally:
        await worker.engine.kill()
    assert files.load_state(worker.data_dir)["counters"] == {}


async def test_run_exits_78_on_unauthorized_and_cleans_tmp(tmp_path) -> None:
    core = FakeCore([CoreUnauthorized("отозван")])
    worker = _worker(tmp_path, core)
    leftover = files.tmp_dir(worker.data_dir) / "1-1.src"
    leftover.write_bytes(b"x")
    assert await worker.run(asyncio.Event()) == service.EXIT_UNAUTHORIZED
    assert list(files.tmp_dir(worker.data_dir).iterdir()) == []


async def test_run_pauses_on_core_unavailable_and_stops(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service, "CORE_RETRY_S", 0.05)
    stop = asyncio.Event()

    class StoppingCore(FakeCore):
        async def lease(self, wait_s: float, *, fast: bool = False):
            if len(self.lease_waits) >= 2:
                stop.set()  # третий лиз — уже после двух пауз
            return await super().lease(wait_s, fast=fast)

    core = StoppingCore([CoreUnavailable("ConnectError"), CoreError(500)])
    worker = _worker(tmp_path, core)
    assert await asyncio.wait_for(worker.run(stop), 5) == 0
    errors = files.load_state(worker.data_dir)["errors"]
    assert [e["where"] for e in errors] == ["core", "core"]


def test_engine_argv_and_too_long_reason() -> None:
    argv = service.engine_argv(
        {"engine_python": "~/venv/bin/python3", "model_dirs": ["~/m1", "/m2"], "compute_type": "int8"}
    )
    assert argv[0] == os.path.expanduser("~/venv/bin/python3") and argv[1] == str(service.ENGINE_PATH)
    assert argv[2:] == [
        "--model-dir",
        os.path.expanduser("~/m1"),
        "--model-dir",
        "/m2",
        "--compute-type",
        "int8",
        "--device",
        "cuda",
    ]
    assert service.too_long_reason(601, 600, False) == "длиннее 10 мин — по кнопке"
    assert service.too_long_reason(600, 600, False) is None
    assert service.too_long_reason(3601, 3600, True) == "длиннее 60 мин"
    assert service.too_long_reason(5000, None, False) is None
