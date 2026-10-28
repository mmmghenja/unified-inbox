"""Мост ядра к одному терминалу: клиент управления `tmux -C attach -t =hub-tN` (раздел 2.1 + К5, К6, К8, К11).

Протокол (проба `.runs/f565-probe/probe.py` и проба исполнителя 14.10, tmux 3.4):
- вывод панели — строки `%output %<pane> <байты>`, управляющие байты и `\\` — восьмеричные `\\ooo`;
- ответ на команду — блок `%begin <t> <n> <flags>` … `%end|%error <t> <n> <flags>`; у команд этого клиента
  `flags = 1`, у присоединения (первый блок) — `0`; ответы идут строго по порядку команд;
- `%layout-change @w <контрольная>,<W>x<H>,…` — размер окна поменялся;
- `%exit` — клиент отсоединён (сессия закрыта).

Ядро читает клиента постоянно (К5, без `pause-after`); отстающих браузеров развязывает кольцо. Все команды
(ввод, размер, снимок, запуск) идут в stdin ЭТОГО клиента — порядок сохраняется (К11), а ответ снимка стоит в
потоке ровно между выводом до и после него — позиция снимка точная (К6). Содержимое строк в журнал службы не
пишется никогда, даже при ошибке разбора."""

import asyncio
import contextlib
import logging
import re
import secrets
import subprocess
from collections import deque
from collections.abc import Callable

from .config import TermConfig
from .ring import Ring
from .tmux import TmuxError, client_env, session_name

_logger = logging.getLogger("hub.term")

READ_LIMIT = 4 * 1024 * 1024  # мелочи раздела 6: строка клиента управления — до 4 МиБ
SEND_CHUNK = 1024  # байт ввода на одну команду `send-keys -H`
SNAPSHOT_HISTORY = 2000
_OCTAL = re.compile(rb"\\([0-7]{3})")
_LAYOUT_SIZE = re.compile(rb"^[0-9a-f]{4},(\d+)x(\d+),")

# К6: режимы панели, которые интерфейс восстанавливает escape-последовательностями после записи снимка.
MODE_FLAGS = (
    "cursor_flag",
    "keypad_cursor_flag",
    "keypad_flag",
    "mouse_any_flag",
    "mouse_button_flag",
    "mouse_standard_flag",
    "mouse_sgr_flag",
    "mouse_utf8_flag",
    "insert_flag",
    "wrap_flag",
    "origin_flag",
)
_SNAP_FIELDS = ("alternate_on", "cursor_x", "cursor_y", "pane_width", "pane_height",
                "scroll_region_upper", "scroll_region_lower", *MODE_FLAGS)  # fmt: skip
_SNAP_FORMAT = " ".join("#{" + name + "}" for name in _SNAP_FIELDS)


class TermGone(Exception):
    """Сессии терминала нет (оболочка вышла, закрыт) или клиент управления умер."""


def unescape(data: bytes) -> bytes:
    return _OCTAL.sub(lambda m: bytes([int(m.group(1), 8)]), data)


def _ints(text: str, count: int) -> list[int] | None:
    parts = text.split(" ")
    if len(parts) < count:
        return None
    try:
        return [int(p) if p != "" else 0 for p in parts[:count]]
    except ValueError:
        return None


class _Pending:
    """Ответ на одну строку команд. Каждая команда строки (`a ; b`) отвечает своим блоком, блоки строки идут
    подряд; ошибка команды снимает остаток строки (tmux не выполняет следующие) — строка закрывается сразу."""

    __slots__ = ("blocks", "future", "lines")

    def __init__(self, future: asyncio.Future, blocks: int) -> None:
        self.future = future
        self.blocks = blocks
        self.lines: list[bytes] = []


class Bridge:
    def __init__(
        self,
        cfg: TermConfig,
        term_id: int,
        *,
        tmux: str,
        on_change: Callable[[], None],
        on_exit: Callable[[Bridge], None],
    ) -> None:
        self.cfg = cfg
        self.term_id = term_id
        self.session = session_name(term_id)
        self.epoch = secrets.token_hex(4)  # К7: позиция = <эпоха>:<n>; новый мост — новая эпоха
        self.ring = Ring(cfg.ring_bytes)
        self.cols = 0
        self.rows = 0
        self.pane: str | None = None
        self._tmux = tmux
        self._on_change = on_change
        self._on_exit = on_exit
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._pending: deque[_Pending] = deque()
        self._block: tuple[bytes, bytes] | None = None  # (t, n) открытого блока
        self._block_ours = False
        self._block_lines: list[bytes] = []
        self._dead = False

    @property
    def alive(self) -> bool:
        return not self._dead and self._proc is not None

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            self._tmux, "-u", "-N", "-S", str(self.cfg.socket),
            "-C", "attach-session", "-t", f"={self.session}",
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            limit=READ_LIMIT, env=client_env(),
        )  # fmt: skip
        self._reader = asyncio.ensure_future(self._read_loop())
        try:
            lines, _ = await self.command("display -p '#{pane_id} #{window_width} #{window_height}'")
        except BaseException:
            # Ревью 5а: отмена (обрыв потока посреди подъёма) или таймаут — клиент управления не оставлять.
            await asyncio.shield(self.close())
            raise
        parts = lines[0].decode("ascii", errors="replace").split(" ") if lines else []
        if len(parts) != 3 or not parts[0].startswith("%"):
            await self.close()
            raise TermGone(self.session)
        self.pane = parts[0]
        self.cols, self.rows = int(parts[1]), int(parts[2])
        _logger.info("терминал %s: мост поднят (эпоха %s)", self.term_id, self.epoch)

    # --- чтение ------------------------------------------------------------------------------------------

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None  # noqa: S101
        stdout = self._proc.stdout
        try:
            while True:
                try:
                    line = await stdout.readline()
                except ValueError, asyncio.LimitOverrunError:
                    _logger.warning(
                        "терминал %s: строка клиента управления длиннее предела — мост закрыт", self.term_id
                    )
                    break
                if not line:
                    break
                if line.endswith(b"\n"):
                    line = line[:-1]
                if self._handle(line):
                    break
        except Exception:  # noqa: BLE001 — разбор не должен уронить ядро; содержимое не журналируем
            _logger.warning("терминал %s: ошибка разбора потока клиента управления", self.term_id)
        finally:
            self._die()

    def _handle(self, line: bytes) -> bool:
        """Одна строка протокола; `True` — клиент отсоединён."""
        if self._block is not None:
            if line.startswith((b"%end ", b"%error ")):
                parts = line.split(b" ")
                if len(parts) >= 3 and (parts[1], parts[2]) == self._block:
                    self._finish_block(error=line.startswith(b"%error "))
                    return False
            self._block_lines.append(line)
            return False
        if line.startswith(b"%output "):
            parts = line.split(b" ", 2)
            if len(parts) >= 2 and (self.pane is None or parts[1].decode("ascii", "replace") == self.pane):
                data = unescape(parts[2]) if len(parts) == 3 else b""
                if data:
                    self.ring.append(data)
                    self._on_change()
            return False
        if line.startswith(b"%begin "):
            parts = line.split(b" ")
            if len(parts) >= 4:
                self._block = (parts[1], parts[2])
                self._block_ours = parts[3] == b"1"
                self._block_lines = []
            return False
        if line.startswith(b"%layout-change "):
            parts = line.split(b" ")
            if len(parts) >= 3:
                match = _LAYOUT_SIZE.match(parts[2])
                if match is not None:
                    cols, rows = int(match.group(1)), int(match.group(2))
                    if (cols, rows) != (self.cols, self.rows):
                        self.cols, self.rows = cols, rows
                        self._on_change()
            return False
        return line == b"%exit" or line.startswith(b"%exit ")

    def _finish_block(self, *, error: bool) -> None:
        lines, ours = self._block_lines, self._block_ours
        self._block, self._block_lines = None, []
        if not ours or not self._pending:
            return  # блок присоединения (`flags = 0`) и прочие чужие
        pending = self._pending[0]
        if error:
            self._pending.popleft()
            if not pending.future.done():
                pending.future.set_exception(TmuxError("команда клиента управления отклонена"))
            return
        pending.lines.extend(lines)
        pending.blocks -= 1
        if pending.blocks > 0:
            return
        self._pending.popleft()
        if not pending.future.done():
            # К6: позиция кольца на момент конца ответа — вывод до команды уже в кольце, после — ещё нет.
            pending.future.set_result((pending.lines, self.ring.end))

    def _die(self) -> None:
        if self._dead:
            return
        self._dead = True
        while self._pending:
            pending = self._pending.popleft()
            if not pending.future.done():
                pending.future.set_exception(TermGone(self.session))
        _logger.info("терминал %s: мост закрыт", self.term_id)
        self._on_exit(self)

    # --- команды -----------------------------------------------------------------------------------------

    def _send(self, line: str, blocks: int = 1) -> asyncio.Future:
        """Строка команд (`blocks` — сколько в ней команд) в stdin клиента; ответ — будущее `(строки всех
        блоков, позиция кольца после последнего)`. Запись синхронная: порядок будущих в очереди = порядок
        строк в stdin = порядок ответов."""
        if self._dead or self._proc is None or self._proc.stdin is None:
            raise TermGone(self.session)
        future = asyncio.get_running_loop().create_future()
        self._pending.append(_Pending(future, blocks))
        try:
            self._proc.stdin.write(line.encode("utf-8") + b"\n")
        except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
            self._pending.pop()
            raise TermGone(self.session) from exc
        return future

    async def _drain(self) -> None:
        assert self._proc is not None and self._proc.stdin is not None  # noqa: S101
        try:
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise TermGone(self.session) from exc

    async def _wait(self, futures: list[asyncio.Future]) -> list[tuple[list[bytes], int]]:
        await self._drain()
        try:
            async with asyncio.timeout(self.cfg.command_timeout_s):
                results = await asyncio.gather(*futures, return_exceptions=True)
        except TimeoutError:
            raise TmuxError("клиент управления не ответил") from None
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return list(results)

    async def command(self, line: str, blocks: int = 1) -> tuple[list[bytes], int]:
        (result,) = await self._wait([self._send(line, blocks)])
        return result

    async def send_bytes(self, data: bytes) -> None:
        """К11: ввод байтами (`send-keys -H`) кусками, все — в stdin этого клиента подряд."""
        if not data:
            return
        target = self.pane or f"={self.session}:"
        futures = []
        for offset in range(0, len(data), SEND_CHUNK):
            chunk = data[offset : offset + SEND_CHUNK]
            futures.append(self._send(f"send-keys -t {target} -H " + " ".join(f"{b:02x}" for b in chunk)))
        await self._wait(futures)

    async def resize(self, cols: int, rows: int) -> None:
        await self.command(f"refresh-client -C {int(cols)}x{int(rows)}")

    async def snapshot(self) -> dict:
        """К6: режимы и экран одной строкой (`display ; capture-pane`) — сервер выполняет их подряд, без
        вывода панели между ними (два блока ответа подряд); позиция — конец второго блока в потоке. Снимок:
        2000 строк истории + видимый экран (в alt-screen — только экран, истории там нет), строки через
        `\\r\\n`."""
        lines, pos = await self.command(
            f"display -p -t {self.pane} '{_SNAP_FORMAT}' ; "
            f"capture-pane -p -e -S -{SNAPSHOT_HISTORY} -t {self.pane}",
            blocks=2,
        )
        if not lines:
            raise TmuxError("пустой ответ снимка")
        values = _ints(lines[0].decode("ascii", errors="replace"), len(_SNAP_FIELDS))
        if values is None:
            raise TmuxError("ответ снимка не разобран")
        fields = dict(zip(_SNAP_FIELDS, values, strict=True))
        screen = lines[1:]
        return {
            "pos": pos,
            "epoch": self.epoch,
            "cols": fields["pane_width"],
            "rows": fields["pane_height"],
            "data": b"\r\n".join(screen),
            "cursor": [fields["cursor_x"], fields["cursor_y"]],
            "alt_screen": bool(fields["alternate_on"]),
            "modes": {
                **{name: bool(fields[name]) for name in MODE_FLAGS},
                "scroll_region": [fields["scroll_region_upper"], fields["scroll_region_lower"]],
            },
        }

    async def state(self) -> tuple[bool, str]:
        """`(alternate_on, pane_current_command)` — для проверки «занят» (К8)."""
        lines, _ = await self.command(
            f"display -p -t {self.pane} '#{{alternate_on}} #{{pane_current_command}}'"
        )
        text = lines[0].decode("utf-8", errors="replace") if lines else ""
        alt, _, command = text.partition(" ")
        return alt == "1", command

    async def screen(self, lines_max: int) -> dict:
        """Текст экрана без цветов (для ключа агента): последние `lines_max` строк истории и экрана, хвостовые
        пустые строки отброшены; `command` — что сейчас идёт в панели (К12)."""
        head = "#{alternate_on} #{cursor_x} #{cursor_y} #{pane_width} #{pane_height} #{pane_current_command}"
        lines, _ = await self.command(
            f"display -p -t {self.pane} '{head}' ; capture-pane -p -J -S -{int(lines_max)} -t {self.pane}",
            blocks=2,
        )
        first = lines[0].decode("utf-8", errors="replace") if lines else ""
        parts = first.split(" ", 5)
        nums = _ints(" ".join(parts[:5]), 5) or [0, 0, 0, 0, 0]
        body = [line.decode("utf-8", errors="replace") for line in lines[1:]]
        while body and not body[-1].strip():
            body.pop()
        body = body[-lines_max:]
        return {
            "text": "\n".join(body),
            "cursor": [nums[1], nums[2]],
            "alt_screen": bool(nums[0]),
            "cols": nums[3],
            "rows": nums[4],
            "command": parts[5] if len(parts) == 6 else "",
        }

    async def paste_and_enter(self, buffer: str) -> None:
        """К8: `paste-buffer -p` (скобки вставки — если программа их включила, решает tmux) и Enter — одной
        строкой, атомарно, в общем порядке ввода."""
        await self.command(
            f"paste-buffer -p -d -b {buffer} -t {self.pane} ; send-keys -t {self.pane} Enter", blocks=2
        )

    async def close(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.stdin is not None:
            with contextlib.suppress(Exception):
                proc.stdin.close()
        try:
            async with asyncio.timeout(2):
                await proc.wait()
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
        if self._reader is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
        self._die()
