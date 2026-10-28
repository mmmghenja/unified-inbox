"""Настройки терминала (Ф56-5, К1). Боевые значения — служба `hub-term`, сокет `<data_dir>/term/tmux.sock`;
стенды и тесты задают свои (`HUB_TERM_UNIT`, `HUB_TERM_SOCKET` или параметрами) — боевую службу не задевают.
"""

import os
import pwd
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_UNIT = "hub-term"
ENV_UNIT = "HUB_TERM_UNIT"
ENV_SOCKET = "HUB_TERM_SOCKET"
ENV_SYSTEMD = "HUB_TERM_SYSTEMD"
_UNIT_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
# Мелочи раздела 6: путь сокета unix — меньше 108 байт.
SOCKET_PATH_MAX = 107


def _login_shell() -> tuple[str, ...]:
    shell = pwd.getpwuid(os.getuid()).pw_shell or "/bin/bash"
    return (shell, "-l")


@dataclass(frozen=True)
class TermConfig:
    """`socket` — сокет сервера tmux; `conf` — его конфиг (пишет ядро); `unit` — имя транзиентной службы;
    `use_systemd` — запускать сервер через `systemd-run --user` (боевое) или обычным процессом (тесты);
    `shell` — команда оболочки (по умолчанию `$SHELL -l` из passwd); `cwd` — папка новой оболочки;
    `extra_env` — добавки окружения сервера только при запуске без systemd (тесты: `HISTFILE=/dev/null`)."""

    socket: Path
    conf: Path
    unit: str = DEFAULT_UNIT
    use_systemd: bool = True
    tmux: str = "tmux"
    shell: tuple[str, ...] = field(default_factory=_login_shell)
    cwd: Path = field(default_factory=Path.home)
    extra_env: tuple[tuple[str, str], ...] = ()
    ring_bytes: int = 1 << 20
    max_ui: int = 4
    max_agent: int = 2
    start_timeout_s: float = 5.0
    command_timeout_s: float = 10.0

    def problem(self) -> str | None:
        """Почему сервер на этих настройках не запустить. Проверка ленивая (перед запуском сервера), а не в
        конструкторе: длинный путь каталога данных не должен ронять старт ядра — только терминал."""
        if not _UNIT_RE.match(self.unit):
            return f"имя службы терминала недопустимо: {self.unit!r}"
        if len(os.fsencode(str(self.socket))) > SOCKET_PATH_MAX:
            return f"путь сокета терминала длиннее {SOCKET_PATH_MAX} байт: {self.socket}"
        return None

    @classmethod
    def from_env(cls, data_dir: Path, env: dict[str, str] | None = None) -> TermConfig:
        env = os.environ if env is None else env
        term_dir = data_dir / "term"
        socket = Path(env.get(ENV_SOCKET) or term_dir / "tmux.sock")
        return cls(
            socket=socket,
            conf=term_dir / "tmux.conf",
            unit=env.get(ENV_UNIT) or DEFAULT_UNIT,
            use_systemd=env.get(ENV_SYSTEMD, "1") != "0",
        )


# Конфиг сервера tmux (раздел 2 + К1). Префикс не трогаем: ввод идёт `send-keys -H` прямо в панель.
CONF_TEXT = """\
# Пишет ядро хаба (Ф56-5). Правки вручную перезапишутся при следующем запуске сервера терминала.
set -g status off
set -g history-limit 20000
set -g default-terminal "tmux-256color"
set -s escape-time 0
set -g window-size latest
set -g remain-on-exit off
set -g detach-on-destroy on
set -g update-environment ""
set -g set-titles off
"""
