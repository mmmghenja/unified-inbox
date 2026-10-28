"""Сервер tmux терминала и разовые команды к нему (Ф56-5, К1).

Сервер запускается ТОЛЬКО здесь (`ensure`): боевой путь — транзиентная служба `systemd-run --user
--unit=<unit> --collect tmux -D …` (окружение менеджера пользователя, вне песочницы ядра); без systemd (тесты)
— обычный отвязанный процесс. Все прочие вызовы tmux идут с `-N`: клиент не поднимет сервер сам (иначе он
родился бы внутри песочницы ядра)."""

import asyncio
import contextlib
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .config import CONF_TEXT, TermConfig

_logger = logging.getLogger("hub.term")

SESSION_PREFIX = "hub-t"
_SESSION_RE = re.compile(r"^hub-t([1-9][0-9]{0,8})$")
OWNER_UI = "ui"
OWNER_OPTION = "@hub_owner"
_LIST_FORMAT = "\t".join(
    (
        "#{session_name}",
        "#{pane_id}",
        "#{session_created}",
        "#{" + OWNER_OPTION + "}",
        "#{window_width}",
        "#{window_height}",
        "#{pane_current_command}",
        "#{pane_current_path}",
    )
)


class TmuxError(Exception):
    """Команда tmux не удалась (текст — для журнала службы, без содержимого терминала)."""


@dataclass(frozen=True)
class TermInfo:
    id: int
    pane: str
    created_ts: int
    owner: str
    cols: int
    rows: int
    command: str
    path: str

    @property
    def session(self) -> str:
        return session_name(self.id)


def session_name(term_id: int) -> str:
    return f"{SESSION_PREFIX}{term_id}"


def owner_of_token(token_id: int) -> str:
    return f"token:{token_id}"


def client_env() -> dict[str, str]:
    """Окружение клиентов tmux: без переменных ядра (`HUB_*`). В оболочку оно и так не попадает
    (`update-environment ""`), это вторая линия."""
    return {k: v for k, v in os.environ.items() if not k.startswith("HUB_")}


class TmuxServer:
    def __init__(self, cfg: TermConfig) -> None:
        self.cfg = cfg
        self._lock = asyncio.Lock()
        self._proc: subprocess.Popen | None = None  # сервер без systemd (тесты)
        self._tmux = shutil.which(cfg.tmux) or cfg.tmux

    @property
    def tmux(self) -> str:
        return self._tmux

    def base(self) -> list[str]:
        return [self._tmux, "-u", "-N", "-S", str(self.cfg.socket)]

    async def run(
        self, *args: str, stdin: bytes | None = None, wait_s: float | None = None
    ) -> tuple[int, bytes]:
        """`(код, stdout)`; stderr — только в журнал отладки и без содержимого (длина)."""
        proc = await asyncio.create_subprocess_exec(
            *self.base(),
            *args,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=client_env(),
        )
        try:
            async with asyncio.timeout(wait_s or self.cfg.command_timeout_s):
                out, err = await proc.communicate(stdin)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise TmuxError(f"tmux {args[0]}: нет ответа") from None
        if proc.returncode != 0:
            _logger.debug("tmux %s: код %s, stderr %s байт", args[0], proc.returncode, len(err))
        return proc.returncode, out

    async def alive(self) -> bool:
        if not self.cfg.socket.exists():
            return False
        code, _ = await self.run("show-options", "-s", "escape-time")
        return code == 0

    async def ensure(self) -> None:
        """Сервер жив — ничего; нет — запустить (К1: служба уже есть и сокет отвечает — не запускать)."""
        async with self._lock:
            problem = self.cfg.problem()
            if problem is not None:
                raise TmuxError(problem)
            if await self.alive():
                return
            self._prepare_files()
            if self.cfg.use_systemd:
                await self._start_systemd()
            else:
                self._start_plain()
            deadline = time.monotonic() + self.cfg.start_timeout_s
            while time.monotonic() < deadline:
                if await self.alive():
                    _logger.info(
                        "терминал: сервер tmux запущен (%s)",
                        self.cfg.unit if self.cfg.use_systemd else "процесс",
                    )
                    return
                await asyncio.sleep(0.05)
            raise TmuxError("сервер tmux не поднялся")

    def _prepare_files(self) -> None:
        for directory in {self.cfg.socket.parent, self.cfg.conf.parent}:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if directory.stat().st_mode & 0o077:
                directory.chmod(0o700)
        tmp = self.cfg.conf.with_name(self.cfg.conf.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(CONF_TEXT)
        os.replace(tmp, self.cfg.conf)
        # Мёртвый сокет от прежнего сервера: новый сервер на занятом пути не встанет.
        with contextlib.suppress(FileNotFoundError):
            if self.cfg.socket.is_socket():
                self.cfg.socket.unlink()

    def _server_argv(self) -> list[str]:
        return [self._tmux, "-u", "-D", "-S", str(self.cfg.socket), "-f", str(self.cfg.conf)]

    def _start_plain(self) -> None:
        env = client_env()
        env.update(dict(self.cfg.extra_env))
        self._proc = subprocess.Popen(  # noqa: S603 — путь tmux и аргументы свои
            self._server_argv(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
            cwd=str(self.cfg.cwd),
        )

    async def _systemctl(self, *args: str) -> int:
        systemctl = shutil.which("systemctl") or "/usr/bin/systemctl"
        proc = await asyncio.create_subprocess_exec(
            systemctl,
            "--user",
            *args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        async with asyncio.timeout(30):
            return await proc.wait()

    async def _start_systemd(self) -> None:
        unit = f"{self.cfg.unit}.service"
        if await self._systemctl("is-active", "--quiet", unit) == 0:
            # Служба жива, а сокет не отвечает (сокет удалён, сервер завис) — остановить и поднять заново.
            _logger.warning("терминал: служба %s жива, но сокет не отвечает — перезапуск", unit)
            await self._systemctl("stop", unit)
        await self._systemctl("reset-failed", unit)
        systemd_run = shutil.which("systemd-run") or "/usr/bin/systemd-run"
        proc = await asyncio.create_subprocess_exec(
            systemd_run,
            "--user",
            f"--unit={self.cfg.unit}",
            "--collect",
            "--quiet",
            "--description=hub: терминал (tmux)",
            f"--working-directory={self.cfg.cwd}",
            "--",
            *self._server_argv(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        async with asyncio.timeout(30):
            _, err = await proc.communicate()
        if proc.returncode != 0:
            raise TmuxError(f"systemd-run: код {proc.returncode}: {err.decode(errors='replace')[:300]}")

    async def kill_server(self) -> None:
        """Только для тестов и стендов: погасить свой сервер."""
        await self.run("kill-server")
        if self._proc is not None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                await asyncio.to_thread(self._proc.wait, 5)
            self._proc = None

    async def list_terms(self) -> list[TermInfo]:
        """Живые терминалы хаба на сокете. Сервера нет — пусто (и сервер не поднимается)."""
        if not self.cfg.socket.exists():
            return []
        code, out = await self.run("list-panes", "-a", "-F", _LIST_FORMAT)
        if code != 0:
            return []
        result: dict[int, TermInfo] = {}
        for raw in out.decode("utf-8", errors="replace").splitlines():
            parts = raw.split("\t", 7)
            if len(parts) != 8:
                continue
            match = _SESSION_RE.match(parts[0])
            if match is None:
                continue
            term_id = int(match.group(1))
            if term_id in result:  # одна панель на терминал; лишние (если кто-то разделил окно) — мимо
                continue
            try:
                created_ts = int(parts[2]) * 1000
                cols, rows = int(parts[4]), int(parts[5])
            except ValueError:
                continue
            result[term_id] = TermInfo(
                id=term_id,
                pane=parts[1],
                created_ts=created_ts,
                owner=parts[3] or OWNER_UI,
                cols=cols,
                rows=rows,
                command=parts[6],
                path=parts[7],
            )
        return [result[key] for key in sorted(result)]

    async def new_session(self, term_id: int, *, owner: str, cols: int, rows: int) -> None:
        """Сессия и метка владельца — одной последовательностью команд (`;`): без владельца не бывает."""
        name = session_name(term_id)
        code, _ = await self.run(
            "new-session", "-d", "-s", name, "-x", str(cols), "-y", str(rows), "-c", str(self.cfg.cwd),
            "--", *self.cfg.shell,
            ";", "set-option", "-t", f"={name}:", OWNER_OPTION, owner,
        )  # fmt: skip
        if code != 0:
            raise TmuxError(f"new-session {name}: код {code}")

    async def kill_session(self, term_id: int) -> bool:
        code, _ = await self.run("kill-session", "-t", f"={session_name(term_id)}")
        return code == 0

    async def has_session(self, term_id: int) -> bool:
        if not self.cfg.socket.exists():
            return False
        code, _ = await self.run("has-session", "-t", f"={session_name(term_id)}")
        return code == 0

    async def load_buffer(self, name: str, data: bytes) -> None:
        """Буфер вставки из stdin отдельного клиента: `load-buffer -` у клиента управления недоступен (его
        stdin — поток команд; проба: «Bad file descriptor»). Буфер ничего не печатает в панель — порядок
        ввода держит клиент управления (`paste-buffer` идёт через него)."""
        code, _ = await self.run("load-buffer", "-b", name, "-", stdin=data)
        if code != 0:
            raise TmuxError(f"load-buffer: код {code}")

    async def delete_buffer(self, name: str) -> None:
        await self.run("delete-buffer", "-b", name)


def short_path(path: str, home: Path | None = None) -> str:
    home_str = str(home or Path.home())
    if path == home_str:
        return "~"
    if path.startswith(home_str + "/"):
        return "~" + path[len(home_str) :]
    return path


def title_of(info: TermInfo) -> str:
    """`#{pane_current_command}` в `#{pane_current_path}` (раздел 2.5)."""
    return f"{info.command} в {short_path(info.path)}"
