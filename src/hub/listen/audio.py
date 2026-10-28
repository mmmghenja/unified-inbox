"""Звук службы записи (Ф60-1, раздел 3 и П12): устройства по умолчанию (`pactl`), эхоподавление PipeWire
дочерним процессом (`pipewire -c <конфиг>`), потоки `ffmpeg -f pulse … -f s16le -` в канал.

Звук на диск не пишется: `ffmpeg` отдаёт LPCM в канал службы, конфиг эхоподавления — во временном каталоге
службы (под `PrivateTmp` юнита — свой `/tmp`), удаляется при остановке.

Цикл службы (`service.py`) работает с `PulseBackend` через тот же набор методов, что у подделок в тестах:
`defaults()`, `start_aec(source, sink)`, `start_capture(device)`."""

import asyncio
import contextlib
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .aio import cancel_and_wait, finish
from .segment import SAMPLE_RATE

_logger = logging.getLogger("hub.listen")

PACTL_TIMEOUT_S = 5.0
AEC_READY_S = 6.0  # источник эхоподавления должен появиться у pipewire-pulse за это время
AEC_POLL_S = 0.25
STOP_GRACE_S = 2.0
STDERR_LINES_MAX = 20  # строк stderr дочернего процесса в журнал (дальше — молча)
READ_BYTES = 3200  # 100 мс LPCM 16 кГц моно


class AudioError(Exception):
    pass


def aec_node_prefix(pid: int, n: int) -> str:
    """`hub_listen_aec_<pid>_<n>`: pid службы и номер запуска — у перезапуска свои имена (старый узел может
    ещё не исчезнуть)."""
    return f"hub_listen_aec_{pid}_{n}"


def _q(value: str) -> str:
    """Строка в конфиге PipeWire (SPA JSON) — в кавычках, без кавычек и обратных косых внутри."""
    return '"' + value.replace("\\", "").replace('"', "") + '"'


def aec_config(prefix: str, source: str, sink: str) -> str:
    """Конфиг клиента PipeWire с одним модулем эхоподавления (образец — проба `.runs/f60-probe/aec.conf`).

    - `monitor.mode = true`: опорный сигнал — монитор стока по умолчанию; своего стока-устройства модуль не
      заводит (узел `<prefix>_sink` — поток захвата с монитора), звук звонка идёт как шёл.
    - `capture.props.target.object` — источник по умолчанию (микрофон), `sink.props.target.object` и
      `playback.props.target.object` — сток по умолчанию (П12; в режиме монитора узла воспроизведения нет).
    - `priority.session/driver = 1`: источник эхоподавления не становится микрофоном по умолчанию.
    - `core.daemon` не задан: процесс — клиент общего PipeWire, своего сокета не открывает."""
    return (
        "context.properties = { log.level = 2 }\n"
        "context.spa-libs = {\n"
        "  audio.convert.* = audioconvert/libspa-audioconvert\n"
        "  support.*       = support/libspa-support\n"
        "}\n"
        "context.modules = [\n"
        "  { name = libpipewire-module-protocol-native }\n"
        "  { name = libpipewire-module-client-node }\n"
        "  { name = libpipewire-module-adapter }\n"
        "  { name = libpipewire-module-echo-cancel\n"
        "    args = {\n"
        "      library.name = aec/libspa-aec-webrtc\n"
        "      monitor.mode = true\n"
        f"      capture.props = {{ node.name = {_q(prefix + '_capture')} target.object = {_q(source)} }}\n"
        f"      source.props = {{ node.name = {_q(prefix + '_source')} "
        'node.description = "hub: эхоподавление интервью (не выбирать)" '
        "priority.session = 1 priority.driver = 1 }\n"
        f"      sink.props = {{ node.name = {_q(prefix + '_sink')} target.object = {_q(sink)} "
        'node.description = "hub: опора эхоподавления" priority.session = 1 priority.driver = 1 }\n'
        f"      playback.props = {{ node.name = {_q(prefix + '_playback')} target.object = {_q(sink)} }}\n"
        "    }\n"
        "  }\n"
        "]\n"
    )


def ffmpeg_argv(device: str) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-f",
        "pulse",
        "-i",
        device,
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-f",
        "s16le",
        "-",
    ]


async def _drain_stderr(stream: asyncio.StreamReader | None, label: str) -> None:
    if stream is None:
        return
    shown = 0
    while True:  # порциями, а не readline: строка без перевода не копится в памяти и не роняет чтение
        data = await stream.read(4096)
        if not data:
            return
        if shown < STDERR_LINES_MAX:
            shown += 1
            _logger.info("%s: %s", label, data.decode(errors="replace").rstrip()[:300])


async def _stop_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), STOP_GRACE_S)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()


@dataclass
class _Child:
    """Дочерний процесс с журналом stderr. `wait()` — до выхода, `stop()` — гасит (TERM, через 2 с KILL)."""

    proc: asyncio.subprocess.Process
    label: str
    _drain: asyncio.Task | None = None

    def __post_init__(self) -> None:
        self._drain = asyncio.create_task(_drain_stderr(self.proc.stderr, self.label))

    @property
    def pid(self) -> int:
        return self.proc.pid

    async def wait(self) -> int:
        return await self.proc.wait()

    async def stop(self) -> None:
        await _stop_process(self.proc)
        await cancel_and_wait([self._drain])


@dataclass
class AecProc:
    child: _Child
    source: str  # имя источника эхоподавления у pulse
    tmpdir: Path

    async def wait(self) -> int:
        return await self.child.wait()

    async def stop(self) -> None:
        try:
            await self.child.stop()
        finally:
            shutil.rmtree(self.tmpdir, ignore_errors=True)


@dataclass
class Capture:
    child: _Child
    device: str

    async def read(self) -> bytes:
        """Очередная порция LPCM; `b""` — поток кончился (ffmpeg вышел)."""
        stdout = self.child.proc.stdout
        if stdout is None:
            return b""
        return await stdout.read(READ_BYTES)

    async def wait(self) -> int:
        return await self.child.wait()

    async def stop(self) -> None:
        await self.child.stop()


@dataclass
class PulseBackend:
    """Настоящий звук ноутбука: `pactl`, `pipewire`, `ffmpeg` из PATH."""

    pid: int = field(default_factory=os.getpid)
    _runs: int = 0

    async def _pactl(self, *args: str) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                "pactl",
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            raise AudioError(f"pactl не запускается ({type(exc).__name__})") from None
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), PACTL_TIMEOUT_S)
        except TimeoutError:
            await _stop_process(proc)
            raise AudioError("pactl не ответил") from None
        if proc.returncode != 0:
            raise AudioError(f"pactl {args[0]}: код {proc.returncode}")
        return out.decode(errors="replace")

    async def defaults(self) -> tuple[str, str]:
        """(источник по умолчанию, сток по умолчанию)."""
        source = (await self._pactl("get-default-source")).strip()
        sink = (await self._pactl("get-default-sink")).strip()
        if not source or not sink:
            raise AudioError("pactl не назвал устройства по умолчанию")
        return source, sink

    async def _sources(self) -> set[str]:
        names = set()
        for line in (await self._pactl("list", "short", "sources")).splitlines():
            parts = line.split("\t")
            if len(parts) > 1:
                names.add(parts[1])
        return names

    async def _spawn(self, argv: list[str], label: str, *, stdout) -> _Child:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.DEVNULL, stdout=stdout, stderr=asyncio.subprocess.PIPE
            )
        except OSError as exc:
            raise AudioError(f"{argv[0]} не запускается ({type(exc).__name__})") from None
        return _Child(proc, label)

    async def start_aec(self, source: str, sink: str) -> AecProc | None:
        """Эхоподавление; не поднялось за `AEC_READY_S` — `None` (служба пишет сырой микрофон)."""
        self._runs += 1
        prefix = aec_node_prefix(self.pid, self._runs)
        tmpdir = Path(tempfile.mkdtemp(prefix="hub-listen-"))
        conf = tmpdir / "aec.conf"
        fd = os.open(conf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(aec_config(prefix, source, sink))
        try:
            child = await self._spawn(
                ["pipewire", "-c", str(conf)], "pipewire", stdout=asyncio.subprocess.DEVNULL
            )
        except AudioError as exc:
            _logger.warning("эхоподавление: %s", exc)
            shutil.rmtree(tmpdir, ignore_errors=True)
            return None
        aec = AecProc(child=child, source=prefix + "_source", tmpdir=tmpdir)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + AEC_READY_S
        try:
            while loop.time() < deadline:
                if child.proc.returncode is not None:
                    _logger.warning("эхоподавление: pipewire вышел с кодом %s", child.proc.returncode)
                    break
                try:
                    if aec.source in await self._sources():
                        return aec
                except AudioError as exc:
                    _logger.warning("эхоподавление: %s", exc)
                    break
                await asyncio.sleep(AEC_POLL_S)
            else:
                _logger.warning("эхоподавление: источник %s не появился за %.0f с", aec.source, AEC_READY_S)
        except BaseException:
            # Ревью Ф60-1 (2): пауза или конец посреди подъёма — вызывающий ещё не знает про процесс,
            # гасим его здесь, иначе pipewire держал бы микрофон до перезапуска службы.
            await finish(aec.stop())
            raise
        await aec.stop()
        return None

    async def start_capture(self, device: str) -> Capture:
        child = await self._spawn(ffmpeg_argv(device), "ffmpeg", stdout=asyncio.subprocess.PIPE)
        return Capture(child=child, device=device)
