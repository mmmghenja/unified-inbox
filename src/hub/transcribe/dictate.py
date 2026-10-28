"""Цикл диктовки быстрой службы `hub transcribe run` (Ф79-1, docs/architecture.md).

`POST /speech/lease` долгим опросом -> PCM во временный wav 16 кГц моно (0600, личный `tmp` службы) ->
ОТДЕЛЬНЫЙ процесс движка (второй экземпляр `Engine` с тем же `engine.py`: голосовое на минуту не задерживает
фразу диктовки) -> `POST /speech/{job_id}/result` -> файл удалён. Срок ответа движка на кусок —
`ENGINE_TIMEOUT_S` (плюс загрузка модели на первом куске после старта процесса).

Движок поднимается при старте цикла заранее (`preload`: модель грузится ~2 с) и выгружается после
`dictate_idle_unload_s` без кусков; следующий кусок поднимает его снова (этот кусок, скорее всего, уйдёт в
SpeechKit по сроку ядра — это нормально).

Ошибка ядра (`CoreUnavailable`) — пауза и повтор; ядро без ручек `/speech` (старая версия, 404) — пауза
подольше; 401 — выход 78, как у цикла голосовых. Текст и звук не пишутся в журнал и не остаются на диске.
"""

import asyncio
import base64
import binascii
import contextlib
import logging
import os
import re
import time
import wave
from pathlib import Path

from . import files
from .core_api import CoreError, CoreUnauthorized, CoreUnavailable
from .service import CORE_RETRY_S, EXIT_UNAUTHORIZED, Engine, EngineFailed, engine_argv

_logger = logging.getLogger("hub.transcribe.dictate")

LEASE_WAIT_S = 25.0
ENGINE_TIMEOUT_S = 15.0
MISSING_RETRY_S = 60.0  # ядро не знает `/speech/lease` (не обновлено) — не долбить каждые 5 с
SAMPLE_RATE = 16000
_JOB_ID = re.compile(r"^[0-9a-f]{1,64}$")


def write_wav(path: Path, pcm: bytes) -> None:
    """LPCM 16 кГц Int16LE моно -> wav (0600, O_EXCL: чужой файл не перезаписывается)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as raw, wave.open(raw, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SAMPLE_RATE)
        f.writeframes(pcm)


class DictateWorker:
    def __init__(
        self,
        *,
        core,
        config: dict,
        data_dir: Path,
        engine: Engine | None = None,
        clock=time.monotonic,
        state: dict | None = None,
        save=None,
    ) -> None:
        self.core = core
        self.config = config
        self.engine = engine or Engine(engine_argv(config))
        self.clock = clock
        self.idle_unload_s = float(config.get("dictate_idle_unload_s") or 1800)
        self.last_job_end = clock()
        self.tmp = files.tmp_dir(data_dir)
        self.state = state if state is not None else {}
        self.stats: dict = self.state.setdefault("dictate", {})
        self._save = save

    # --- состояние --------------------------------------------------------------------------------------

    def _count(self, key: str) -> None:
        self.stats[key] = int(self.stats.get(key, 0)) + 1

    def save(self) -> None:
        self.stats["engine_alive"] = self.engine.alive
        if self._save is not None:
            self._save()

    # --- движок -----------------------------------------------------------------------------------------

    async def warm(self) -> None:
        """Модель — заранее; не вышло — не беда: первый кусок поднимет движок сам."""
        try:
            response = await self.engine.preload()
        except EngineFailed as exc:
            _logger.warning("диктовка: движок не поднялся заранее (%s)", exc)
            return
        finally:
            self.last_job_end = self.clock()
            self.save()
        if response.get("code") != 0:
            _logger.warning("диктовка: модель заранее не загрузилась (%s)", str(response.get("error"))[:200])
        else:
            _logger.info("диктовка: движок готов (%s, %s)", response.get("device"), response.get("model"))

    async def maybe_unload(self) -> None:
        if self.engine.alive and self.clock() - self.last_job_end >= self.idle_unload_s:
            await self.engine.close()
            self.save()

    def lease_wait_s(self) -> float:
        if not self.engine.alive:
            return LEASE_WAIT_S
        left = self.idle_unload_s - (self.clock() - self.last_job_end)
        return max(0.5, min(LEASE_WAIT_S, left))

    # --- цикл -------------------------------------------------------------------------------------------

    async def step(self) -> bool:
        await self.maybe_unload()
        job = await self.core.speech_lease(self.lease_wait_s())
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
        try:
            # Сначала модель (один процесс движка — один запрос за раз), потом опрос: пока грузится, ядро
            # работника не видит и шлёт куски в SpeechKit.
            warm = asyncio.create_task(self.warm())
            stopper = asyncio.create_task(stop.wait())
            done, _pending = await asyncio.wait({warm, stopper}, return_when=asyncio.FIRST_COMPLETED)
            stopper.cancel()
            if warm not in done:
                warm.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await warm
                return 0
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
                pause = 0.0
                try:
                    step.result()
                except CoreUnauthorized as exc:
                    _logger.error("диктовка: %s", exc)
                    return EXIT_UNAUTHORIZED
                except CoreUnavailable as exc:
                    _logger.warning("диктовка: ядро недоступно (%s) — пауза", exc)
                    pause = CORE_RETRY_S
                except CoreError as exc:
                    _logger.warning("диктовка: ядро ответило %s — пауза", exc)
                    pause = MISSING_RETRY_S if exc.status == 404 else CORE_RETRY_S
                if pause:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), pause)
        finally:
            await self.engine.close()
            self.save()
        return 0

    # --- один кусок -------------------------------------------------------------------------------------

    async def process(self, job: dict) -> None:
        job_id = str(job.get("job_id") or "")
        if not _JOB_ID.match(job_id):
            _logger.warning("диктовка: задание без номера — пропущено")
            return
        started = time.monotonic()
        body = await self._recognize(job, job_id)
        try:
            await self.core.speech_result(job_id, body)
        except CoreError as exc:
            # 409 — ядро уже ответило без нас (срок вышел): результат не нужен.
            _logger.info("диктовка: ответ не принят (%s)", exc)
            self._count("late")
            return
        self._count("ok" if "text" in body else "failed")
        if "text" in body:
            self.stats["last_ok_ts"] = int(time.time() * 1000)
        _logger.debug(
            "диктовка: кусок %s, %d мс",
            "ok" if "text" in body else body["error"],
            int((time.monotonic() - started) * 1000),
        )

    async def _recognize(self, job: dict, job_id: str) -> dict:
        try:
            pcm = base64.b64decode(str(job.get("audio_b64") or ""), validate=True)
        except (binascii.Error, ValueError):
            return {"error": "bad_audio"}
        if not pcm or len(pcm) % 2:
            return {"error": "bad_audio"}
        lang = job.get("lang") if isinstance(job.get("lang"), str) else None
        wav = self.tmp / f"dictate-{job_id}.wav"
        try:
            files.private_dir(self.tmp)
            write_wav(wav, pcm)
            response = await self.engine.run(wav, ENGINE_TIMEOUT_S, lang=lang)
        except EngineFailed as exc:
            _logger.warning("диктовка: %s", exc)
            return {"error": "engine_failed"}
        except OSError as exc:
            _logger.warning("диктовка: wav не записан (%s)", type(exc).__name__)
            return {"error": "io"}
        finally:
            wav.unlink(missing_ok=True)
        if response.get("code") in (0, 2):
            text = response.get("text")
            return {"text": text if isinstance(text, str) else ""}
        return {"error": "engine_error"}
