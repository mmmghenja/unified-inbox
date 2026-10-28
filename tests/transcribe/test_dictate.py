"""Цикл диктовки быстрой службы (Ф79-1, docs/architecture.md): подделка ядра
(`speech_lease`/`speech_result`) и подделка движка тем же протоколом (`fake_engine.py`, с `preload` и `lang`).
Плюс: оба цикла вместе (`run_service`), кто быстрый (`device`), лиз голосовых с `fast`, клиент ядра."""

import asyncio
import base64
import sys
import wave
from pathlib import Path

import httpx
import pytest

from hub.transcribe import dictate, files, service
from hub.transcribe import main as tmain
from hub.transcribe.core_api import CoreError, CoreUnauthorized, CoreUnavailable, TranscribeCore

_FAKE_ENGINE = Path(__file__).with_name("fake_engine.py")
PCM = b"\x10\x00" * 16000  # 1 с


class FakeCore:
    def __init__(self, jobs=(), *, engine=None) -> None:
        self.jobs = list(jobs)
        self.results: list[tuple[str, dict]] = []
        self.result_error: Exception | None = None
        self.engine = engine
        self.alive_at_lease: list[bool] = []
        self.waits: list[float] = []
        self.got = asyncio.Event()  # пришёл ответ работника
        self.drained = asyncio.Event()  # подготовленные задания розданы

    async def speech_lease(self, wait_s: float):
        self.waits.append(wait_s)
        if self.engine is not None:
            self.alive_at_lease.append(self.engine.alive)
        if not self.jobs:
            await asyncio.sleep(0.01)
            return None
        job = self.jobs.pop(0)
        if not self.jobs:
            self.drained.set()
        if isinstance(job, Exception):
            raise job
        return job

    async def speech_result(self, job_id: str, body: dict) -> None:
        if self.result_error is not None:
            raise self.result_error
        self.results.append((job_id, body))
        self.got.set()


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _job(job_id: str = "ab12", *, pcm: bytes = PCM, lang: str = "ru") -> dict:
    return {"job_id": job_id, "lang": lang, "audio_b64": base64.b64encode(pcm).decode()}


def _engine(tmp_path: Path, mode: str = "ok") -> service.Engine:
    return service.Engine([sys.executable, str(_FAKE_ENGINE), mode, str(tmp_path / "pids")])


def _dw(tmp_path: Path, core: FakeCore, mode: str = "ok", *, clock=None, idle: float = 1800):
    data_dir = tmp_path / "data"
    files.clean_tmp(data_dir)
    config = {**files.DEFAULT_CONFIG, "dictate_idle_unload_s": idle}
    engine = _engine(tmp_path, mode)
    core.engine = engine
    return dictate.DictateWorker(
        core=core, config=config, data_dir=data_dir, engine=engine, clock=clock or Clock()
    )


def _pids(tmp_path: Path) -> list[str]:
    path = tmp_path / "pids"
    return path.read_text().split() if path.exists() else []


async def test_job_text_with_lang_hint_and_wav_removed(tmp_path) -> None:
    core = FakeCore([_job("ab12")])
    worker = _dw(tmp_path, core)
    try:
        assert await worker.step() is True
    finally:
        await worker.engine.close()
    assert core.results == [("ab12", {"text": "проверка dictate-ab12.wav [ru]"})]
    assert list(files.tmp_dir(tmp_path / "data").iterdir()) == []
    assert worker.stats["ok"] == 1 and "last_ok_ts" in worker.stats


def test_write_wav_is_16k_mono_private(tmp_path) -> None:
    path = tmp_path / "x.wav"
    dictate.write_wav(path, PCM)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    with wave.open(str(path), "rb") as f:
        assert (f.getframerate(), f.getnchannels(), f.getsampwidth(), f.getnframes()) == (16000, 1, 2, 16000)
    with pytest.raises(FileExistsError):
        dictate.write_wav(path, PCM)  # O_EXCL: чужой файл не перезаписывается


async def test_silence_is_empty_text(tmp_path) -> None:
    core = FakeCore([_job()])
    worker = _dw(tmp_path, core, "doubtful")
    try:
        await worker.step()
    finally:
        await worker.engine.close()
    assert core.results == [("ab12", {"text": ""})]


@pytest.mark.parametrize(
    ("mode", "error"), [("error", "engine_error"), ("crash", "engine_failed"), ("garbage", "engine_failed")]
)
async def test_engine_failures_answer_error(tmp_path, mode, error) -> None:
    core = FakeCore([_job()])
    worker = _dw(tmp_path, core, mode)
    try:
        await worker.step()
    finally:
        await worker.engine.close()
    assert core.results == [("ab12", {"error": error})]
    assert worker.stats["failed"] == 1


async def test_engine_hang_is_killed_after_15s_budget(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(dictate, "ENGINE_TIMEOUT_S", 0.3)
    monkeypatch.setattr(service, "ENGINE_LOAD_S", 0.0)
    core = FakeCore([_job()])
    worker = _dw(tmp_path, core, "hang")
    try:
        await asyncio.wait_for(worker.step(), 5)
    finally:
        await worker.engine.close()
    assert core.results == [("ab12", {"error": "engine_failed"})]
    assert not worker.engine.alive


@pytest.mark.parametrize(
    "job", [{"job_id": "ab12", "audio_b64": "не base64!"}, {"job_id": "ab12", "audio_b64": "AA=="}]
)
async def test_bad_audio_without_engine(tmp_path, job) -> None:
    core = FakeCore([job])
    worker = _dw(tmp_path, core)
    await worker.step()
    assert core.results == [("ab12", {"error": "bad_audio"})]
    assert _pids(tmp_path) == []


async def test_job_without_valid_id_is_skipped(tmp_path) -> None:
    core = FakeCore([{"job_id": "../x", "audio_b64": base64.b64encode(PCM).decode()}])
    worker = _dw(tmp_path, core)
    await worker.step()
    assert core.results == [] and _pids(tmp_path) == []


async def test_late_answer_409_is_not_fatal(tmp_path) -> None:
    core = FakeCore([_job()])
    core.result_error = CoreError(409)
    worker = _dw(tmp_path, core)
    try:
        assert await worker.step() is True
    finally:
        await worker.engine.close()
    assert worker.stats["late"] == 1


async def test_run_preloads_before_first_lease_and_stops(tmp_path) -> None:
    stop = asyncio.Event()
    core = FakeCore([_job()])
    worker = _dw(tmp_path, core)

    async def _stop_after_result() -> None:
        await core.got.wait()
        stop.set()

    stopper = asyncio.create_task(_stop_after_result())
    assert await asyncio.wait_for(worker.run(stop), 10) == 0
    await stopper
    assert core.alive_at_lease[0] is True  # модель загружена до первого опроса
    assert len(_pids(tmp_path)) == 1  # загрузка заранее — не задание, процесс один
    assert not worker.engine.alive  # остановка закрывает движок


async def test_preload_failure_is_not_fatal(tmp_path) -> None:
    stop = asyncio.Event()
    core = FakeCore([_job()])
    worker = _dw(tmp_path, core, "preload_error")

    async def _stop_after_result() -> None:
        await core.got.wait()
        stop.set()

    stopper = asyncio.create_task(_stop_after_result())
    assert await asyncio.wait_for(worker.run(stop), 10) == 0
    await stopper
    assert core.results == [("ab12", {"text": "проверка dictate-ab12.wav [ru]"})]


async def test_idle_unload_and_next_job_restarts_engine(tmp_path) -> None:
    clock = Clock()
    core = FakeCore([_job("a1")])
    worker = _dw(tmp_path, core, clock=clock, idle=1800)
    try:
        await worker.step()
        assert worker.engine.alive
        assert core.waits[-1] == dictate.LEASE_WAIT_S
        clock.t += 1790
        await worker.step()  # пусто; опрос не дольше остатка простоя
        assert core.waits[-1] == pytest.approx(10.0)
        clock.t += 11
        await worker.step()
        assert not worker.engine.alive  # выгружен
        core.jobs.append(_job("b2"))
        await worker.step()
        assert worker.engine.alive
    finally:
        await worker.engine.close()
    assert len(set(_pids(tmp_path))) == 2
    assert [r[0] for r in core.results] == ["a1", "b2"]


async def test_run_exits_78_on_unauthorized(tmp_path) -> None:
    core = FakeCore([CoreUnauthorized("отозван")])
    worker = _dw(tmp_path, core)
    assert await asyncio.wait_for(worker.run(asyncio.Event()), 10) == service.EXIT_UNAUTHORIZED


async def test_run_pauses_on_core_errors_and_old_core(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(dictate, "CORE_RETRY_S", 0.05)
    monkeypatch.setattr(dictate, "MISSING_RETRY_S", 0.1)
    stop = asyncio.Event()
    core = FakeCore([CoreUnavailable("ConnectError"), CoreError(404), CoreError(500)])
    worker = _dw(tmp_path, core)

    async def _stop_when_drained() -> None:
        await core.drained.wait()
        stop.set()

    stopper = asyncio.create_task(_stop_when_drained())
    assert await asyncio.wait_for(worker.run(stop), 10) == 0
    await stopper


# --- оба цикла вместе, кто быстрый ------------------------------------------------------------------------


class VoiceCore:
    """Цикл голосовых: пустые лизы (с флагом `fast`), 78 по запросу."""

    def __init__(self, *, unauthorized: bool = False) -> None:
        self.fast: list[bool] = []
        self.unauthorized = unauthorized

    async def lease(self, wait_s: float, *, fast: bool = False):
        self.fast.append(fast)
        if self.unauthorized:
            raise CoreUnauthorized("отозван")
        await asyncio.sleep(0.01)
        return None


def _voice(tmp_path: Path, core, device: str = "cuda") -> service.Worker:
    data_dir = tmp_path / "data"
    files.clean_tmp(data_dir)
    config = {**files.DEFAULT_CONFIG, "device": device}
    return service.Worker(core=core, config=config, data_dir=data_dir, engine=_engine(tmp_path))


async def test_run_service_both_loops_and_stop(tmp_path) -> None:
    stop = asyncio.Event()
    vcore = VoiceCore()
    voice = _voice(tmp_path, vcore)
    dcore = FakeCore([_job()])
    dw = _dw(tmp_path, dcore)

    async def _stop() -> None:
        await dcore.got.wait()
        stop.set()

    stopper = asyncio.create_task(_stop())
    assert await asyncio.wait_for(service.run_service(stop, voice, dw), 10) == 0
    await stopper
    assert vcore.fast and all(vcore.fast)  # cuda: голосовые лизятся быстрым
    assert dcore.results


async def test_run_service_78_from_one_loop_stops_both(tmp_path) -> None:
    voice = _voice(tmp_path, VoiceCore())
    dw = _dw(tmp_path, FakeCore([CoreUnauthorized("отозван")]))
    code = await asyncio.wait_for(service.run_service(asyncio.Event(), voice, dw), 10)
    assert code == service.EXIT_UNAUTHORIZED


async def test_cpu_worker_leases_slow(tmp_path) -> None:
    vcore = VoiceCore()
    voice = _voice(tmp_path, vcore, device="cpu")
    assert voice.fast is False
    await voice.step()
    assert vcore.fast == [False]


@pytest.mark.parametrize(
    ("device", "dictate_on", "built"), [("cuda", True, True), ("cuda", False, False), ("cpu", True, False)]
)
def test_build_dictate_only_on_cuda(tmp_path, device, dictate_on, built) -> None:
    config = {**files.DEFAULT_CONFIG, "device": device, "dictate": dictate_on}
    voice = service.Worker(core=None, config=config, data_dir=tmp_path / "d", engine=_engine(tmp_path))
    dw = tmain.build_dictate(core=None, config=config, data_dir=tmp_path / "d", worker=voice)
    assert (dw is not None) is built
    if dw is not None:
        assert dw.idle_unload_s == 1800 and dw.engine is not voice.engine  # свой процесс движка


def test_config_defaults_and_unknown_keys(tmp_path) -> None:
    files.write_config(tmp_path, {"device": "cpu", "dictate_idle_unload_s": 60, "lishnee": 1})
    config = files.load_config(tmp_path)
    assert config["device"] == "cpu" and config["dictate"] is True and config["dictate_idle_unload_s"] == 60
    assert "lishnee" not in config and not files.is_fast(config)


def test_status_text_shows_dictation(tmp_path) -> None:
    data_dir = tmp_path / "data"
    files.save_state(
        data_dir,
        {"engine_alive": False, "dictate": {"engine_alive": True, "ok": 5, "failed": 1, "late": 2}},
    )
    out = tmain.status_text(data_dir, None)
    assert "диктовка (Ф79-1): движок жив, кусков 5 (не вышло 1, поздно 2)" in out


# --- клиент ядра -----------------------------------------------------------------------------------------


async def test_core_client_speech_routes_and_fast_flag(tmp_path) -> None:
    seen: list[tuple[str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.content))
        if request.url.path == "/api/v1/speech/lease":
            return httpx.Response(200, json=_job())
        if request.url.path.endswith("/result"):
            return (
                httpx.Response(409, json={"detail": "stale"})
                if b"late" in request.content
                else httpx.Response(204)
            )
        return httpx.Response(204)

    token = tmp_path / "token"
    token.write_text("t1\n")
    core = TranscribeCore(
        token_file=token, base_url="http://core/api/v1", transport=httpx.MockTransport(handler)
    )
    try:
        assert (await core.speech_lease(25))["job_id"] == "ab12"
        assert await core.speech_result("ab12", {"text": "ok"}) is None
        with pytest.raises(CoreError) as exc:
            await core.speech_result("ab12", {"text": "late"})
        assert exc.value.status == 409
        assert await core.lease(1) is None
        assert await core.lease(1, fast=True) is None
    finally:
        await core.aclose()
    assert seen[0] == ("/api/v1/speech/lease", b'{"wait_s":25}')
    assert seen[1][0] == "/api/v1/speech/ab12/result"
    assert seen[3] == ("/api/v1/transcripts/lease", b'{"wait_s":1}')  # старое ядро не получит лишнего поля
    assert seen[4] == ("/api/v1/transcripts/lease", b'{"wait_s":1,"fast":true}')
