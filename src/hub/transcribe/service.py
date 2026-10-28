"""Цикл службы `hub transcribe run` (Ф14-6, docs/architecture.md).

lease -> `GET /media/{id}` в личный `tmp` (0600) -> ffmpeg в wav 16 кГц моно (у кружка — без видео; ffmpeg
упал или звука нет -> `skip`) -> длительность по wav (длиннее потолка задания -> `skip`, иначе продление лиза
по ней, П8) -> задание движку -> `report`. Файлы задания удаляются сразу после него.

Движок — дочерний процесс (`engine.py` интерпретатором диктовки): стартует на первом задании, живёт, пока есть
задания; `idle_unload_s` без заданий — stdin закрыт, по таймауту kill (видеопамять свободна). Не ответил за
`max(120 с, 2 × длительность)` (плюс загрузка модели на первом задании) — kill, `failed` с причиной; упал —
`failed`; следующее задание поднимет новый процесс.

Ф79-1 (docs/architecture.md): на cuda служба — быстрый работник: лизит голосовые
с `fast=True` и рядом ведёт цикл диктовки (`transcribe/dictate.py`) вторым процессом движка — `run_service`.

Журнал — только `media_id`, коды и классы ошибок; текст расшифровки не пишется никуда, кроме `report`.
"""

import asyncio
import contextlib
import json
import logging
import time
import wave
from pathlib import Path

from . import files
from .core_api import CoreError, CoreUnauthorized, CoreUnavailable

_logger = logging.getLogger("hub.transcribe")

ENGINE_PATH = Path(__file__).with_name("engine.py")
LEASE_WAIT_S = 25.0
ENGINE_MIN_TIMEOUT_S = 120.0
ENGINE_LOAD_S = 120.0  # первое задание движка ещё и грузит модель
ENGINE_CLOSE_S = 10.0
FFMPEG_TIMEOUT_S = 300.0
CORE_RETRY_S = 5.0
MAX_ERRORS = 20
EXIT_UNAUTHORIZED = 78


class EngineFailed(Exception):
    """Движок упал, завис или ответил не протоколом — `failed` с этой причиной."""


class Engine:
    """Процесс движка: одна строка JSON на задание."""

    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.proc: asyncio.subprocess.Process | None = None
        self.jobs = 0

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,
        )
        self.jobs = 0
        _logger.info("движок запущен (pid %s)", self.proc.pid)

    async def run(
        self, wav: Path, timeout_s: float, *, manual: bool = False, lang: str | None = None
    ) -> dict:
        request = {"wav": str(wav), "manual": bool(manual)}  # 14-6b: ручной повтор — без VAD при тишине
        if lang:
            request["lang"] = lang  # Ф79-1: подсказка языка (диктовка, интервью)
        return await self._ask(request, timeout_s)

    async def preload(self) -> dict:
        """Ф79-1: поднять процесс и загрузить модель заранее (движок диктовки)."""
        return await self._ask({"preload": True}, 0.0)

    async def _ask(self, request: dict, timeout_s: float) -> dict:
        if not self.alive:
            await self.start()
        assert self.proc is not None and self.proc.stdin is not None and self.proc.stdout is not None
        if self.jobs == 0:
            timeout_s += ENGINE_LOAD_S
        try:
            self.proc.stdin.write((json.dumps(request) + "\n").encode())
            await self.proc.stdin.drain()
            line = await asyncio.wait_for(self.proc.stdout.readline(), timeout_s)
        except TimeoutError:
            await self.kill()
            raise EngineFailed(f"движок не ответил за {timeout_s:.0f} с") from None
        except (BrokenPipeError, ConnectionResetError):
            await self.kill()
            raise EngineFailed("движок упал") from None
        if not line:
            await self.kill()
            raise EngineFailed("движок упал")
        self.jobs += 1
        try:
            response = json.loads(line)
        except ValueError:
            await self.kill()
            raise EngineFailed("движок ответил не по протоколу") from None
        if not isinstance(response, dict):
            await self.kill()
            raise EngineFailed("движок ответил не по протоколу")
        return response

    async def kill(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()

    async def close(self, grace_s: float = ENGINE_CLOSE_S) -> None:
        """Выгрузка по простою: stdin EOF — движок выходит сам; не вышел за `grace_s` — kill."""
        proc = self.proc
        if proc is None:
            return
        if proc.returncode is None and proc.stdin is not None:
            with contextlib.suppress(Exception):
                proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), grace_s)
            except TimeoutError:
                await self.kill()
        self.proc = None
        _logger.info("движок закрыт (простой)")


def engine_argv(config: dict) -> list[str]:
    argv = [files.expand(config["engine_python"]), str(ENGINE_PATH)]
    for path in config.get("model_dirs") or []:
        argv += ["--model-dir", files.expand(path)]
    argv += ["--compute-type", str(config.get("compute_type") or "int8_float16")]
    argv += ["--device", str(config.get("device") or "cuda")]
    return argv


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as f:
        rate = f.getframerate() or 1
        return f.getnframes() / rate


def too_long_reason(duration_s: float, max_s: int | float | None, manual: bool) -> str | None:
    """П8: длительность по факту больше потолка задания — `skip` с причиной."""
    if isinstance(max_s, int | float) and duration_s > max_s:
        return "длиннее 60 мин" if manual else "длиннее 10 мин — по кнопке"
    return None


class Worker:
    def __init__(
        self, *, core, config: dict, data_dir: Path, engine: Engine | None = None, clock=time.monotonic
    ) -> None:
        self.core = core
        self.config = config
        self.data_dir = data_dir
        self.engine = engine or Engine(engine_argv(config))
        self.clock = clock
        # Ф79-1, раздел D: на cuda — быстрый работник (голосовые ему первым, `fast=True` в лизе).
        self.fast = files.is_fast(config)
        self.idle_unload_s = float(config.get("idle_unload_s") or 300)
        self.last_job_end = clock()
        self.state: dict = files.load_state(data_dir) or {}
        self.state.setdefault("errors", [])
        self.state.setdefault("counters", {})
        self.tmp = files.tmp_dir(data_dir)

    # --- состояние для `hub transcribe status` -----------------------------------------------------------

    def _count(self, key: str) -> None:
        counters = self.state["counters"]
        counters[key] = int(counters.get(key, 0)) + 1

    def _error(self, where: str, text: str) -> None:
        errors = self.state["errors"]
        errors.append({"ts": int(time.time() * 1000), "where": where, "error": text[:200]})
        del errors[:-MAX_ERRORS]

    def save(self) -> None:
        self.state["engine_alive"] = self.engine.alive
        self.state["engine_pid"] = self.engine.proc.pid if self.engine.alive and self.engine.proc else None
        self.state["updated_ts"] = int(time.time() * 1000)
        try:
            files.save_state(self.data_dir, self.state)
        except OSError as exc:
            _logger.warning("state.json не записан (%s)", type(exc).__name__)

    # --- цикл --------------------------------------------------------------------------------------------

    async def maybe_unload(self) -> None:
        if self.engine.alive and self.clock() - self.last_job_end >= self.idle_unload_s:
            await self.engine.close()
            self.save()

    def lease_wait_s(self) -> float:
        if not self.engine.alive:
            return LEASE_WAIT_S
        left = self.idle_unload_s - (self.clock() - self.last_job_end)
        return max(0.5, min(LEASE_WAIT_S, left))

    async def step(self) -> bool:
        """Один оборот: выгрузка по простою, лиз, задание. `True` — задание было."""
        await self.maybe_unload()
        job = await self.core.lease(self.lease_wait_s(), fast=self.fast)
        if job is None:
            await self.maybe_unload()
            return False
        try:
            await self.process(job)
        finally:
            self.last_job_end = self.clock()
            self.save()
        return True

    async def run(self, stop: asyncio.Event) -> int:
        files.clean_tmp(self.data_dir)
        self.save()
        try:
            while not stop.is_set():
                step = asyncio.create_task(self.step())
                stopper = asyncio.create_task(stop.wait())
                done, _pending = await asyncio.wait({step, stopper}, return_when=asyncio.FIRST_COMPLETED)
                if step not in done:
                    step.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await step
                    break
                stopper.cancel()
                try:
                    step.result()
                except CoreUnauthorized as exc:
                    _logger.error("transcribe: %s", exc)
                    return EXIT_UNAUTHORIZED
                except CoreUnavailable as exc:
                    self._error("core", str(exc))
                    _logger.warning("ядро недоступно (%s) — пауза", exc)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), CORE_RETRY_S)
                except CoreError as exc:
                    self._error("core", str(exc))
                    _logger.warning("ядро ответило %s — пауза", exc)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), CORE_RETRY_S)
        finally:
            await self.engine.close()
            files.clean_tmp(self.data_dir)
            self.save()
        return 0

    # --- одно задание -----------------------------------------------------------------------------------

    async def process(self, job: dict) -> None:
        media_id = int(job["media_id"])
        lease_gen = job["lease_gen"]
        source = self.tmp / f"{media_id}-{lease_gen}.src"
        wav = self.tmp / f"{media_id}-{lease_gen}.wav"
        try:
            outcome = await self._transcribe(job, source, wav)
        finally:
            source.unlink(missing_ok=True)
            wav.unlink(missing_ok=True)
        body = {"lease_gen": lease_gen, **outcome}
        try:
            await self.core.report(media_id, body)
        except CoreError as exc:
            # 409 — лиз устарел (просрочен и перехвачен): результат не нужен.
            _logger.warning("media %s: отчёт не принят (%s)", media_id, exc)
            return
        self._count(outcome["outcome"])
        if outcome["outcome"] != "done":
            self._error(f"media {media_id}", outcome.get("error") or outcome["outcome"])
        else:
            self.state["last_ok_ts"] = int(time.time() * 1000)
        _logger.info("media %s: %s", media_id, outcome["outcome"])

    async def _transcribe(self, job: dict, source: Path, wav: Path) -> dict:
        media_id = int(job["media_id"])
        try:
            await self.core.download(media_id, source)
        except CoreError as exc:
            if exc.status == 404:
                return {"outcome": "skip", "error": "файла нет в хабе"}  # П10: не три повтора
            return {"outcome": "failed", "error": f"скачать не удалось: {exc}"}
        reason = await self._ffmpeg(source, wav)
        if reason is not None:
            return {"outcome": "skip", "error": reason}
        try:
            duration = wav_duration(wav)
        except (OSError, EOFError, wave.Error):
            return {"outcome": "skip", "error": "нет звука"}
        if duration <= 0:
            return {"outcome": "skip", "error": "нет звука"}
        too_long = too_long_reason(duration, job.get("max_s"), bool(job.get("manual")))
        if too_long is not None:
            return {"outcome": "skip", "error": too_long}
        # П8: срок лиза — от настоящей длительности (у Slack её в событии нет).
        try:
            await self.core.extend(media_id, job["lease_gen"], duration)
        except CoreError as exc:
            _logger.warning("media %s: продлить лиз не удалось (%s)", media_id, exc)
        try:
            response = await self.engine.run(
                wav, max(ENGINE_MIN_TIMEOUT_S, 2 * duration), manual=bool(job.get("manual"))
            )
        except EngineFailed as exc:
            return {"outcome": "failed", "error": str(exc)}
        code = response.get("code")
        if code in (0, 2):
            elapsed = response.get("elapsed")
            lang = response.get("lang")
            return {
                "outcome": "done",
                "text": response.get("text") if isinstance(response.get("text"), str) else "",
                "lang": lang if lang in ("ru", "en") else None,
                "lang_prob": response.get("lang_prob"),
                "problem": response.get("problem"),
                "model": response.get("model"),
                "device": response.get("device"),
                "elapsed_ms": int(elapsed * 1000) if isinstance(elapsed, int | float) else None,
            }
        return {"outcome": "failed", "error": str(response.get("error") or "ошибка движка")[:200]}

    async def _ffmpeg(self, source: Path, wav: Path) -> str | None:
        """`ffmpeg -nostdin -i <файл> -vn -ac 1 -ar 16000 -f wav`; упал или нет звука — причина `skip`."""
        argv = [
            str(self.config.get("ffmpeg") or "ffmpeg"),
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            str(wav),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return f"ffmpeg не запускается ({type(exc).__name__})"
        try:
            _out, _err = await asyncio.wait_for(proc.communicate(), FFMPEG_TIMEOUT_S)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            return "ffmpeg не уложился по времени"
        if proc.returncode != 0 or _size(wav) <= 44:
            return "нет звука (ffmpeg)"
        return None


def _size(path: Path) -> int:
    """Размер файла (0 — нет файла); вызывается после завершения ffmpeg, файл маленький и локальный."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


async def run_service(stop: asyncio.Event, worker: Worker, dictate=None) -> int:
    """Ф79-1: цикл голосовых и (у быстрого работника) цикл диктовки параллельно. Один кончился (остановка, 78,
    исключение) — останавливается и второй; 78 любого — 78 службы, исключение — наружу (systemd поднимет)."""
    if dictate is None:
        return await worker.run(stop)
    inner = asyncio.Event()

    async def _relay() -> None:
        await stop.wait()
        inner.set()

    relay = asyncio.create_task(_relay())
    # Голосовые — первыми: их `run` чистит личный `tmp` до первого ожидания — раньше, чем пишет диктовка.
    tasks = [asyncio.create_task(worker.run(inner)), asyncio.create_task(dictate.run(inner))]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        inner.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        relay.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await relay
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return EXIT_UNAUTHORIZED if EXIT_UNAUTHORIZED in results else 0
