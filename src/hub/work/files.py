"""Файлы hub-work — хоста сессий хаба (Ф9-1, 9a, F9 и поправки П10, П14; задачи по «Го» убраны в Ф78-1).

- `<config_dir>/work/` (0700): `token` — bearer ядра `work:host` (0600), `token.json` — `{token_id}` без
  секрета, `config.json` — не секрет (слияние с умолчаниями), `env` — файл окружения юнита
  (`EnvironmentFile=`), его пишет `hub work setup` из сессии владельца по разрешающему списку ключей (П14).
- `<data_dir>/work/` (0700): `host.lock` — блокировка `flock` (второй `hub work run` не стартует, П10),
  `state.json` — состояние для `hub work status`, `held-<id>/` — каталоги сессий хаба (ниже), `guard.log` —
  журнал гарда (0600), `claude-ai-seen` — метка Ф87-1 «хост видел коннекторы claude.ai» (пустой файл),
  `held-turns.json` — Ф93-1 (F3) концы хода, не принятые ядром (0600).

Запись — атомарно (временный файл 0600 рядом, fsync, `os.replace`): те же функции, что у hub-agent."""

import contextlib
import glob
import json
import logging
import os
from pathlib import Path

from hub.agent.files import atomic_write, private_dir, read_json, read_secret, write_secret

_logger = logging.getLogger(__name__)

__all__ = ["atomic_write", "private_dir", "read_json", "read_secret", "write_secret"]

WORK_DIR = "work"
# Каталог дерева хаба (у прода — `~/projects/hub-prod`): плагин `deploy/opencode/hub-private.ts`.
TREE = Path(__file__).resolve().parents[3]
TOKEN_NAME = "work:host"  # noqa: S105 — имя токена в ядре, не секрет
TOKEN_SCOPES = ("work_host", "read")

DEFAULT_CONFIG: dict = {
    "claude_path": "~/.local/bin/claude",
    "quiet_hours": "23:00-08:00",
    "wait_timeout_s": 25,
    # F4: события в ядро пачкой раз в `batch_s` или по `batch_max`; ядро недоступно — буфер до `buffer_max`.
    "batch_s": 1.0,
    "batch_max": 50,
    "buffer_max": 2000,
    # Ф41-2б: постоянные сессии хаба.
    # Модель Claude Code, если у сессии своя не задана.
    "held_model": "sonnet",
    # Простой процесса дольше — усыпить (следующая реплика поднимет `--resume`).
    "held_idle_close_s": 900,
    # Ф56-1в (А3): ход идёт, а шагов в потоке нет дольше — хост докладывает ядру «завис» (карточка `stuck`).
    "held_stuck_s": 900,
    # Ф48-1: фоновая команда процесса держит сессию от усыпления не дольше (подагент — пока работает).
    "held_bg_command_hold_s": 7200,
    # Живых процессов постоянных сессий — не больше; из них серверов OpenCode — не больше.
    "held_max_parallel": 10,
    "held_max_parallel_opencode": 10,
    # Постоянные сессии на OpenCode (П11): выключено — запуск такой сессии кончается причиной в окне.
    "held_opencode": True,
    # Ф71-1: постоянные сессии на Cursor — выключатель, потолок живых, MCP хаба для подписки между ходами
    # (`hub mcp --profile …` на всё время жизни процесса) и путь к `hub` для него и для `mcp.json` плагина
    # (пусто — `hub` рядом с интерпретатором хоста).
    "held_cursor": True,
    "held_max_parallel_cursor": 5,
    "held_cursor_sidecar": True,
    "held_cursor_hub": "",
    # Ф74-8: срок тёплого кэша провайдера Cursor (с) от конца последнего хода — для подъёма по сигналам
    # (`cacheReadTokens` в расходе хода; срока Cursor не сообщает — как у Codex).
    "held_cache_s_cursor": 300,
    # Ф74-5: постоянные сессии на Codex — выключатель, потолок живых процессов `codex app-server`, путь
    # к `hub` для MCP хаба сессии (пусто — рядом с интерпретатором хоста), срок тёплого кэша (с) для подъёма
    # по сигналам (кэш Codex — `cachedInputTokens`; срок — как у Claude Code).
    "held_codex": True,
    "held_max_parallel_codex": 5,
    "held_codex_hub": "",
    "held_cache_s_codex": 300,
    # Ф74-5б: как часто читать `model/list` Codex для интерфейса (с; 0 — не читать).
    "held_models_refresh_s": 21600,
    # Ф41-4 (3.2, П1, П7, П10): входящее постоянных сессий.
    # Сигналы сами не поднимают сессию с контекстом больше и остывшим кэшем («тяжёлая и холодная»).
    "held_wake_max_tokens": 150_000,
    # Подъёмы по сигналам на весь хост — не чаще: тёплый (кэш жив, OpenCode) и холодный (кэш Claude остыл).
    "held_signal_wake_gap_s": 20,
    "held_cold_wake_gap_s": 20,  # 10.10 (владелец): было 120 — пять ролей вставали десять минут
    # Ф50-1 (П6): обычные сигналы подаются занятой сессии Claude Code, когда ход идёт дольше этого срока, и
    # не чаще раза в тот же срок; срочные (сообщение владельца, `urgent`, карточка делегату) — сразу.
    "held_midturn_after_s": 300,
    # Профиль MCP хаба, чей сервер оформляет подписку сессии при старте (`HUB_SESSION_SUB_PROFILE`).
    "held_sub_profile": "all",
    # Подъём процесса без хода ради подписки (новая роль, снятая подписка, правка фильтра) и сколько ждать её.
    "held_sub_wake": True,
    "held_sub_wait_s": 60,
    # Ф41-4б: сжатие по размеру — пороги по движкам (не больше 75 % предела контекста), порог от базы
    # сессии (П17: база + столько), срок подтверждения сжатия.
    "held_compact_tokens": 200_000,
    "held_compact_tokens_opencode": 200_000,
    "held_compact_min_growth": 40_000,
    "held_compact_wait_s": 180,
}

# П14: файл окружения юнита — ТОЛЬКО эти ключи из сессии владельца (не всё окружение).
ENV_FILE_KEYS = (
    "SSH_AUTH_SOCK",
    "PATH",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "TZ",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
    "DOCKER_HOST",
)


def config_work_dir(config_dir: Path) -> Path:
    return config_dir / WORK_DIR


def data_work_dir(data_dir: Path) -> Path:
    return data_dir / WORK_DIR


def token_path(config_dir: Path) -> Path:
    return config_work_dir(config_dir) / "token"


def token_meta_path(config_dir: Path) -> Path:
    return config_work_dir(config_dir) / "token.json"


def config_path(config_dir: Path) -> Path:
    return config_work_dir(config_dir) / "config.json"


def env_path(config_dir: Path) -> Path:
    return config_work_dir(config_dir) / "env"


def state_path(data_dir: Path) -> Path:
    return data_work_dir(data_dir) / "state.json"


def held_procs_path(data_dir: Path) -> Path:
    """Ревью Ф56, круг 2 (2): процессы постоянных сессий, запущенные этим хостом (pid, starttime, boot_id)."""
    return data_work_dir(data_dir) / "held-procs.json"


def load_held_procs(data_dir: Path) -> dict:
    try:
        data = json.loads(held_procs_path(data_dir).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def save_held_procs(data_dir: Path, data: dict) -> None:
    private_dir(data_work_dir(data_dir))
    atomic_write(held_procs_path(data_dir), json.dumps(data, ensure_ascii=False) + "\n")


def held_restart_path(data_dir: Path) -> Path:
    """Ф89-1 (П12): снимки «почему процесс сессии перезапущен и что оборвано» — пишет остановка хоста,
    читает и удаляет следующий старт (при остановке `asleep` стирает фоновые задачи у ядра)."""
    return data_work_dir(data_dir) / "held-restart.json"


def take_held_restart(data_dir: Path) -> dict:
    """Прочитать и удалить файл снимков; нет или не читается — пусто."""
    path = held_restart_path(data_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        data = {}
    with contextlib.suppress(OSError):
        path.unlink()
    return data if isinstance(data, dict) else {}


def save_held_restart(data_dir: Path, data: dict) -> None:
    private_dir(data_work_dir(data_dir))
    atomic_write(held_restart_path(data_dir), json.dumps(data, ensure_ascii=False) + "\n")


def held_turns_path(data_dir: Path) -> Path:
    """Ф93-1 (F3): отчёты конца хода сессий хаба, не принятые ядром (недоступно), — `{"turns": {"<id>":
    [тело `turn`, …]}}`. Пишется при каждом изменении очереди (пусто — файл удаляется), читает старт хоста:
    до любых новых отчётов и до `recover` ядра."""
    return data_work_dir(data_dir) / "held-turns.json"


def load_held_turns(data_dir: Path) -> dict:
    """Прочитать очередь отчётов конца хода (файл остаётся до отправки); нет или не читается — пусто."""
    try:
        data = json.loads(held_turns_path(data_dir).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def save_held_turns(data_dir: Path, data: dict) -> None:
    """Записать очередь (0600, атомарно); пустая — файл удаляется."""
    if not data.get("turns"):
        with contextlib.suppress(OSError):
            held_turns_path(data_dir).unlink()
        return
    private_dir(data_work_dir(data_dir))
    atomic_write(held_turns_path(data_dir), json.dumps(data, ensure_ascii=False) + "\n")


def claude_ai_mark_path(data_dir: Path) -> Path:
    """Ф87-1 (п. 1): хост на этой машине хоть раз видел в `mcp_status` Claude Code сервер `claude.ai …` — с
    этого момента коннекторы claude.ai ожидаются у каждого процесса. Только ставится."""
    return data_work_dir(data_dir) / "claude-ai-seen"


def has_claude_ai_mark(data_dir: Path) -> bool:
    return claude_ai_mark_path(data_dir).is_file()


def set_claude_ai_mark(data_dir: Path) -> None:
    private_dir(data_work_dir(data_dir))
    atomic_write(claude_ai_mark_path(data_dir), "")


def lock_path(data_dir: Path) -> Path:
    return data_work_dir(data_dir) / "host.lock"


def guard_log_path(data_dir: Path) -> Path:
    return data_work_dir(data_dir) / "guard.log"


# --- постоянные сессии хаба (Ф41-2б, П5) --------------------------------------------------------------------
# `<data_dir>/work/held-<id>/` (0700): каталог сессии хаба (`settings.json` с хуком гарда, конфиг и журнал
# OpenCode).
HELD_PREFIX = "held-"


def held_dir(data_dir: Path, hub_session_id: int) -> Path:
    return data_work_dir(data_dir) / f"{HELD_PREFIX}{hub_session_id}"


CURSOR_DIR = "cursor"


def cursor_root(data_dir: Path) -> Path:
    """Ф71-1 (П7): каталог плагинов сессий Cursor — закрыт для записи из сессии."""
    return data_work_dir(data_dir) / CURSOR_DIR


def cursor_plugin_dir(data_dir: Path, hub_session_id: int) -> Path:
    """Плагин сессии Cursor: каталог — имя плагина (`plugin-s<id>-<сервер>` у её MCP-серверов)."""
    return cursor_root(data_dir) / f"s{int(hub_session_id)}"


def held_settings_path(data_dir: Path, hub_session_id: int) -> Path:
    return held_dir(data_dir, hub_session_id) / "settings.json"


def load_config(config_dir: Path) -> dict:
    """Конфиг поверх умолчаний: нет файла — умолчания; битый — умолчания и строка в журнал."""
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    path = config_path(config_dir)
    if not path.exists():
        return config
    stored = read_json(path)
    if stored is None:
        _logger.warning("work: config.json не читается — работаю на умолчаниях")
        return config
    config.update(stored)
    if stored.get("held_cold_wake_gap_s") == 120:
        # 10.10: сохранённое прежнее умолчание (`hub work setup` пишет конфиг целиком) считается новым.
        config["held_cold_wake_gap_s"] = DEFAULT_CONFIG["held_cold_wake_gap_s"]
    return config


def claude_path(config: dict) -> str:
    return os.path.expanduser(str(config.get("claude_path") or DEFAULT_CONFIG["claude_path"]))


def write_default_config(config_dir: Path) -> bool:
    path = config_path(config_dir)
    if path.exists():
        return False
    private_dir(path.parent)
    atomic_write(path, json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n")
    return True


def load_state(data_dir: Path) -> dict | None:
    return read_json(state_path(data_dir))


def save_state(data_dir: Path, state: dict) -> None:
    private_dir(data_work_dir(data_dir))
    atomic_write(state_path(data_dir), json.dumps(state, ensure_ascii=False, indent=1) + "\n")


# --- окружение юнита (П14) ----------------------------------------------------------------------------------


def nvm_bin_dirs(home: Path) -> list[str]:
    """`~/.nvm/versions/node/v24*/bin` — MCP-серверы на `#!/usr/bin/env node` ищут node в `PATH`."""
    found = sorted(glob.glob(str(home / ".nvm" / "versions" / "node" / "v24*" / "bin")), reverse=True)
    return [p for p in found if os.path.isdir(p)]


def unit_environment(environ: dict, *, home: Path) -> dict[str, str]:
    """Разрешающий список ключей из окружения владельца; в `PATH` — node из nvm v24 (если его там ещё нет)."""
    env = {k: v for k, v in environ.items() if k in ENV_FILE_KEYS and isinstance(v, str) and "\n" not in v}
    parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    for directory in nvm_bin_dirs(home):
        if directory not in parts:
            parts.insert(0, directory)
    if parts:
        env["PATH"] = os.pathsep.join(parts)
    return env


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_env_file(config_dir: Path, env: dict[str, str]) -> Path:
    path = env_path(config_dir)
    private_dir(path.parent)
    lines = [
        "# Окружение юнита hub-work.service — пишет `hub work setup` из сессии владельца (разрешающий список).",
        *(f"{key}={_quote(value)}" for key, value in sorted(env.items())),
    ]
    atomic_write(path, "\n".join(lines) + "\n")
    return path
