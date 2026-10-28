"""`hub engine setup` (Ф16, П8): запускает владелец из своего терминала, как `hub work setup`.

У юнитов хаба nvm в `PATH` нет, поэтому путь к `opencode` и каталоги node берутся здесь, в окружении владельца, и
пишутся в `<config_dir>/engine.json` (0600): `{"opencode": {"path", "version", "path_extra": [...],
"disable_mcp": [...]}}`. `disable_mcp` — имена MCP-серверов из `opencode mcp list` (только имена: конфиг
OpenCode сливается с глобальным конфигом владельца, F2, и приватные серверы хаба выключают его MCP по именам).
Переопределения ролей (`roles`), если владелец их вписал, сохраняются. Секретов команда не читает: ни `auth.json`,
ни конфиг OpenCode — только вывод `opencode --version` и `opencode mcp list`.
"""

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from hub.agent.files import atomic_write, private_dir
from hub.core.engine import SETUP_FILE

from .oc_config import HUB_MCP

VERSION_TIMEOUT_S = 20.0
MCP_LIST_TIMEOUT_S = 180.0
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_VERSION_RE = re.compile(r"\b(\d+\.\d+\.\d+)\b")
# Строка сервера в `opencode mcp list` 1.18.31: «●  ✓ playwright connected» (значок, имя, состояние).
_MCP_LINE_RE = re.compile(r"^\s*●\s+\S+\s+([A-Za-z0-9._@-]+)\s+(\S+)")
_NVM_NODE_RE = re.compile(r"^(.*/\.nvm/versions/node/[^/]+)/")

Runner = Callable[..., subprocess.CompletedProcess]


class SetupError(Exception):
    """Понятная ошибка — CLI печатает и выходит с ненулевым кодом."""


def parse_mcp_list(text: str) -> list[tuple[str, str]]:
    """[(имя, состояние)] из вывода `opencode mcp list` (цвета терминала снимаются). Всё прочее в выводе
    (команды, адреса) не разбирается и не сохраняется."""
    servers: list[tuple[str, str]] = []
    for raw in _ANSI_RE.sub("", text or "").splitlines():
        m = _MCP_LINE_RE.match(raw)
        if m and m.group(1) not in (name for name, _ in servers):
            servers.append((m.group(1), m.group(2)))
    return servers


def node_dirs(binary: str, which: Callable[[str], str | None] = shutil.which) -> list[str]:
    """Каталоги для `PATH` юнитов: каталог `opencode` (как его нашёл `which`), `bin` установки nvm, откуда он
    на самом деле (у владельца `~/.local/bin/opencode` -> `~/.nvm/versions/node/v22…/…/opencode.exe`), и каталог
    `node` из текущего `PATH`. Только существующие, без повторов."""
    found: list[str] = []
    candidates = [os.path.dirname(binary)]
    m = _NVM_NODE_RE.match(os.path.realpath(binary))
    if m:
        candidates.append(os.path.join(m.group(1), "bin"))
    node = which("node")
    if node:
        candidates.append(os.path.dirname(node))
    for directory in candidates:
        if directory and os.path.isabs(directory) and os.path.isdir(directory) and directory not in found:
            found.append(directory)
    return found


def _run(runner: Runner, argv: list[str], timeout: float, cwd: str) -> subprocess.CompletedProcess:
    try:
        return runner(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            check=False,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired as exc:
        raise SetupError(f"{' '.join(argv[1:])}: нет ответа за {timeout:g} с") from exc
    except OSError as exc:
        raise SetupError(f"{argv[0]} не запускается: {exc.strerror or exc}") from exc


def run_setup(
    config_dir: Path,
    *,
    which: Callable[[str], str | None] = shutil.which,
    runner: Runner = subprocess.run,
    home: Path | None = None,
) -> str:
    """Найти `opencode` (версия, имена MCP) и `codex` (Ф74-5: путь и версия), записать `engine.json`. Нет
    одного из них — его секция остаётся прежней, в тексте строка; нет обоих — `SetupError`. Возвращает текст
    для терминала."""
    cwd = str(Path.home() if home is None else home)  # без проектного opencode.json — только глобальный
    path = Path(config_dir) / SETUP_FILE
    loaded: dict = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            loaded = data
    except OSError, ValueError:
        pass
    binary = which("opencode")
    codex = _find_codex(which, Path(cwd))
    if not binary and not codex:
        raise SetupError(
            "opencode не найден в PATH (и codex тоже) — установите OpenCode или Codex или запустите "
            "из своего терминала"
        )
    result = dict(loaded)  # Ф71-1: прочие секции (`cursor`) переживают перезапись
    lines: list[str] = []
    if binary:
        section, oc_lines = _opencode_section(
            os.path.abspath(binary), runner, cwd, which, loaded.get("opencode")
        )
        result["opencode"] = section
        lines += oc_lines
    else:
        lines.append("opencode не найден в PATH — сессии хаба на OpenCode не запустятся (секция не менялась)")
    if codex:
        section, codex_lines = _codex_section(codex, runner, cwd)
        if section is not None:
            result["codex"] = section
        lines += codex_lines
    else:
        lines.append("codex не найден — сессии хаба на Codex не запустятся (секция не менялась)")
    private_dir(Path(config_dir))
    atomic_write(path, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    lines += [
        f"Записано: {path} (0600).",
        "Повторить hub engine setup — после обновления OpenCode или Codex или нового MCP-сервера в OpenCode.",
    ]
    return "\n".join(lines)


def _opencode_section(binary: str, runner: Runner, cwd: str, which, previous) -> tuple[dict, list[str]]:
    proc = _run(runner, [binary, "--version"], VERSION_TIMEOUT_S, cwd)
    m = _VERSION_RE.search(proc.stdout or "")
    if proc.returncode != 0 or m is None:
        raise SetupError(f"opencode --version: неразборчивый ответ (код {proc.returncode})")
    version = m.group(1)
    proc = _run(runner, [binary, "mcp", "list"], MCP_LIST_TIMEOUT_S, cwd)
    if proc.returncode != 0:
        raise SetupError(f"opencode mcp list: код {proc.returncode}")
    servers = parse_mcp_list(proc.stdout + "\n" + (proc.stderr or ""))
    disable = [name for name, _ in servers if name != HUB_MCP]
    path_extra = node_dirs(binary, which)
    previous = previous if isinstance(previous, dict) else {}
    section = {"path": binary, "version": version, "path_extra": path_extra, "disable_mcp": disable}
    if isinstance(previous.get("roles"), dict):
        section["roles"] = previous["roles"]
    lines = [
        f"opencode: {binary} ({version})"
        + ("" if version.startswith(("1.18.", "1.19.")) else " — хаб проверен на 1.18.x и 1.19.x"),
        "каталоги для PATH служб: " + (", ".join(path_extra) or "—"),
        "MCP OpenCode, которые приватные серверы хаба выключат: " + (", ".join(disable) or "—"),
    ]
    if "roles" in section:
        lines.append("переопределения ролей из прежнего engine.json сохранены")
    return section, lines


def _find_codex(which, home: Path) -> str | None:
    """Ф74-5: `codex` в PATH, иначе `~/.local/bin/codex` (умолчание `choice.codex_path`)."""
    found = which("codex")
    if found:
        return os.path.abspath(found)
    candidate = home / ".local" / "bin" / "codex"
    return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None


def _codex_section(binary: str, runner: Runner, cwd: str) -> tuple[dict | None, list[str]]:
    """Секция `codex`: путь и версия (`codex --version` — «codex-cli 0.163.0»). Не отвечает — секция не
    пишется, строка в тексте (OpenCode при этом настраивается)."""
    try:
        proc = _run(runner, [binary, "--version"], VERSION_TIMEOUT_S, cwd)
    except SetupError as exc:
        return None, [f"codex: {exc} — секция не записана"]
    m = _VERSION_RE.search(proc.stdout or "")
    if proc.returncode != 0 or m is None:
        return None, [f"codex --version: неразборчивый ответ (код {proc.returncode}) — секция не записана"]
    version = m.group(1)
    line = f"codex: {binary} ({version})"
    if not version.startswith("0.163."):
        line += " — хаб проверен на 0.163.x"
    return {"path": binary, "version": version}, [line]
