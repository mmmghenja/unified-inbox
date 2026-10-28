"""Ф14-4 B1 (`docs/architecture.md`): подготовка голосового к отправке в Telegram.

Запись браузера приходит webm (Chrome) или ogg (Firefox), кодек opus. Telegram показывает голосовое (волна и
длительность) только у ogg/opus — webm переупаковывается в ogg БЕЗ перекодирования (`ffmpeg ... -c:a copy -f
ogg`, таймаут 30 с); ogg — как есть. Длительность — `ffprobe` (таймаут 10 с), целые секунды вверх, сбой — 0.

Подпроцессы — `asyncio.create_subprocess_exec` без оболочки; нет ffmpeg, выход не 0 или таймаут — «не
удалось» (`None`), вызывающий шлёт файлом. Временный каталог с ogg удаляет `prepared` при любом исходе.
В журнал — без имён и путей файлов.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
REMUX_TIMEOUT_S = 30.0
PROBE_TIMEOUT_S = 10.0
OGG_MIME = "audio/ogg"


class ProcessFailed(Exception):
    """Подпроцесс не запустился, завершился не с 0 или не уложился в таймаут."""


async def run_process(args: list[str], limit_s: float) -> bytes:
    """Запуск без оболочки, stdin закрыт; вернёт stdout. Сбой запуска, код не 0, таймаут -> `ProcessFailed`
    (по таймауту процесс убивается и дожидается)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        raise ProcessFailed(type(exc).__name__) from exc
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), limit_s)
    except TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise ProcessFailed("timeout") from exc
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise
    if proc.returncode != 0:
        raise ProcessFailed(f"exit {proc.returncode}")
    return out


async def remux_to_ogg(src: Path, dst: Path) -> bool:
    """webm -> ogg без перекодирования (первая звуковая дорожка). `True` — файл готов."""
    args = [FFMPEG, "-nostdin", "-v", "error", "-i", str(src), "-map", "0:a:0", "-c:a", "copy", "-f", "ogg"]
    try:
        await run_process([*args, str(dst)], REMUX_TIMEOUT_S)
    except ProcessFailed:
        return False
    return _nonempty(dst)


def _nonempty(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def _work_dir(tmp_dir: Path) -> Path:
    tmp_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(dir=tmp_dir, prefix="voice-"))


async def probe_duration(path: Path) -> int:
    """Длительность в целых секундах (вверх); любой сбой — 0."""
    args = [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]
    try:
        out = await run_process(args, PROBE_TIMEOUT_S)
        value = float(out.decode("ascii", "replace").strip())
    except (ProcessFailed, ValueError):
        return 0
    if not math.isfinite(value) or value <= 0:
        return 0
    return math.ceil(value)


@contextlib.asynccontextmanager
async def prepared(src: Path, mime: str | None, tmp_dir: Path) -> AsyncIterator[tuple[Path, int] | None]:
    """`(путь к ogg, длительность)` или `None` (ремукс не удался — слать файлом). ogg (`audio/ogg`) — без
    ремукса; иное — ремукс во временный каталог внутри `tmp_dir`, удаляемый при выходе при любом исходе."""
    if mime == OGG_MIME:
        yield src, await probe_duration(src)
        return
    work = _work_dir(tmp_dir)
    try:
        dst = work / "voice.ogg"
        if not await remux_to_ogg(src, dst):
            yield None
            return
        yield dst, await probe_duration(dst)
    finally:
        shutil.rmtree(work, ignore_errors=True)
