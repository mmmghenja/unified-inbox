"""Роли и модели OpenCode, переопределения и путь к бинарю (Ф16, раздел 1 решения владельца, 3.3).

Таблица — единственное место, где хаб знает, какая модель OpenCode за что отвечает; `GET /engine` отдаёт её
в UI как есть (только показ), а куски 16-3/16-5/16-6 берут модель и `variant` для приватного сервера своей
роли через `role_model` (с учётом переопределений `<config_dir>/engine.json`, `load_overrides`).

`engine_of_session` реэкспортируется из ядра (П15): живёт в `hub.core.engine`, чтобы ядро не зависело от
пакета драйвера, а драйвер и демоны импортировали одну функцию из своего слоя.
"""

import json
import os
import shutil
from pathlib import Path

from hub.core.engine import SETUP_FILE, engine_of_session

__all__ = [
    "ROLES",
    "EngineUnavailable",
    "claude_path",
    "codex_path",
    "cursor_model",
    "cursor_path",
    "engine_of_session",
    "load_codex",
    "load_cursor",
    "load_overrides",
    "opencode_path",
    "role_model",
]

# Роль -> модель и уровень размышления (`variant`, F17: у DeepSeek `low|high|max`). Владелец 29.09 вечером: резерв
# целиком на DeepSeek Flash (DeepSeek V4.1 Flash, проба P8), Qwen3.8 Max и Grok 4.7 — дорого.
# `work` — бывшие задачи по «Го» (убраны в Ф78-1); ключ остаётся: по нему проверяется вход OpenCode
# (`opencode_run.ROLE`), его могут называть старые `engine.json`,
# `project` — интерактивные сессии проектов владельца (их запускает он сам, агент
# `hub-project`), `explore` — разведка внутри сессий (подагенты рабочих сессий, П5),
# `facts` — служба фактов о людях (Ф24-1b, без инструментов),
# `agent` — агент владельца (`hub-human-agent`, Ф75-1): умолчание; владелец выбирает модель в интерфейсе (ядро,
# `core/owner_agent_model.py`), переопределение роли в `engine.json` — запасной путь.
ROLES: dict[str, dict[str, str]] = {
    "work": {"model": "deepseek/deepseek-flash", "variant": "max"},
    "project": {"model": "deepseek/deepseek-flash", "variant": "max"},
    "explore": {"model": "deepseek/deepseek-flash", "variant": "max"},
    "facts": {"model": "deepseek/deepseek-flash", "variant": "max"},
    "agent": {"model": "deepseek/deepseek-flash", "variant": "max"},
}


class EngineUnavailable(Exception):
    """OpenCode в хабе недоступен (нет бинаря, битые переопределения). Текст — для журнала и уведомления."""


def _load_setup(config_dir: Path) -> dict:
    path = Path(config_dir) / SETUP_FILE
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise EngineUnavailable(f"{path} не читается: {exc.strerror or exc}") from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise EngineUnavailable(f"{path}: не JSON — перезапустите hub engine setup") from None
    if not isinstance(data, dict):
        raise EngineUnavailable(f"{path}: ожидался объект — перезапустите hub engine setup")
    return data


def load_overrides(config_dir: Path) -> dict:
    """Секция `opencode` из `<config_dir>/engine.json` (пишет `hub engine setup`, П8). Нет файла — `{}`
    (умолчания). Файл есть, но не читается или не JSON-объект — `EngineUnavailable`: молча откатиться на
    умолчания значило бы запустить не ту модель или не тот бинарь."""
    path = Path(config_dir) / SETUP_FILE
    section = _load_setup(config_dir).get("opencode", {})
    if not isinstance(section, dict):
        raise EngineUnavailable(f"{path}: «opencode» должен быть объектом")
    return section


def role_model(role: str, overrides: dict | None = None) -> tuple[str, str, str]:
    """`(provider_id, model_id, variant)` роли: `ROLES`, поверх — `overrides["roles"][role]` (частично:
    можно задать только `variant`). Неизвестная роль или модель без «провайдер/» — `EngineUnavailable`."""
    if role not in ROLES:
        raise EngineUnavailable(f"неизвестная роль OpenCode: {role}")
    spec = dict(ROLES[role])
    roles = (overrides or {}).get("roles")
    if isinstance(roles, dict) and isinstance(roles.get(role), dict):
        for key in ("model", "variant"):
            value = roles[role].get(key)
            if isinstance(value, str) and value.strip():
                spec[key] = value.strip()
    provider, sep, model = spec["model"].partition("/")
    if not sep or not provider or not model:
        raise EngineUnavailable(f"модель роли {role} — «{spec['model']}», нужно «провайдер/модель»")
    return provider, model, spec["variant"]


def opencode_path(overrides: dict | None = None) -> str:
    """Абсолютный путь к `opencode`: из `engine.json` (у юнитов nvm в PATH нет, П8), иначе `shutil.which`,
    иначе `EngineUnavailable("opencode не найден")`."""
    path = (overrides or {}).get("path")
    if isinstance(path, str) and path:
        if os.path.isabs(path) and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
        raise EngineUnavailable(f"opencode не найден: {path} — перезапустите hub engine setup")
    found = shutil.which("opencode")
    if found:
        return found
    raise EngineUnavailable("opencode не найден")


# --- Cursor (Ф71-1, docs/architecture.md и П11) --------------------------------------

CURSOR_DEFAULT_MODEL = "grok-4.7"
CURSOR_DEFAULT_PATH = ".local/bin/cursor-agent"  # от домашнего каталога
# Модели с усилием суффиксом имени (`grok-4.7-high`); у прочих (`composer-2.5`) имя идёт как есть.
CURSOR_EFFORT_FAMILIES = ("grok-4.7", "cursor-grok-4.6", "claude-sonnet-5-5", "claude-opus-5-5")
# П11: у Grok усилие не ниже high.
CURSOR_EFFORTS = ("high", "xhigh")
# Слово владельца: у Claude в Cursor — усилия Claude Code (`cursor-agent --list-models` 28.10:
# `claude-{sonnet,opus}-5-5-{low,medium,high,xhigh,max}`).
CLAUDE_CODE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
CURSOR_FAMILY_EFFORTS = {
    "grok-4.7": CURSOR_EFFORTS,
    "cursor-grok-4.6": CURSOR_EFFORTS,
    "claude-sonnet-5-5": CLAUDE_CODE_EFFORTS,
    "claude-opus-5-5": CLAUDE_CODE_EFFORTS,
}
CURSOR_EFFORT_DEFAULT = "high"
CURSOR_FAST_REFUSED = "fast выключен решением владельца"


def load_cursor(config_dir: Path) -> dict:
    """Секция `cursor` из `<config_dir>/engine.json` (`path` — путь к `cursor-agent`). Нет файла или секции —
    `{}`; битый файл или секция не объект — `EngineUnavailable`."""
    section = _load_setup(config_dir).get("cursor", {})
    if not isinstance(section, dict):
        raise EngineUnavailable(f"{Path(config_dir) / SETUP_FILE}: «cursor» должен быть объектом")
    return section


def cursor_path(section: dict | None, home: Path) -> str:
    """Абсолютный путь к `cursor-agent`: `engine.json` (`cursor.path`), иначе `~/.local/bin/cursor-agent`.
    Нет файла или он не исполняемый — `EngineUnavailable`."""
    path = (section or {}).get("path")
    if not (isinstance(path, str) and path):
        path = str(Path(home) / CURSOR_DEFAULT_PATH)
    if not os.path.isabs(path) or not os.path.isfile(path) or not os.access(path, os.X_OK):
        raise EngineUnavailable(f"cursor-agent не найден: {path}")
    return path


def cursor_efforts(family: str) -> tuple[str, ...]:
    """Усилия семейства моделей Cursor: Grok — не ниже high (П11), Claude — как в Claude Code (28.10)."""
    return CURSOR_FAMILY_EFFORTS.get(family, CURSOR_EFFORTS)


def cursor_model(model: str | None, effort: str | None) -> tuple[str, str | None]:
    """`(значение --model, предупреждение или None)` для сессии Cursor. Модель — без усилия (умолчание
    `grok-4.7`); у семейств `CURSOR_EFFORT_FAMILIES` усилие — суффикс из `cursor_efforts` (пустое — `high`;
    у Grok `low`/`medium` и незнакомое поднимаются до `high` с предупреждением, П11). Любой `fast` —
    `EngineUnavailable` (Р1: «fast выключен решением владельца»)."""
    name = (model or "").strip() or CURSOR_DEFAULT_MODEL
    level = (effort or "").strip().lower()
    if name.lower().endswith("fast") or "fast" in level:
        raise EngineUnavailable(CURSOR_FAST_REFUSED)
    family = next((f for f in CURSOR_EFFORT_FAMILIES if name == f or name.startswith(f + "-")), None)
    if family is None:
        return name, None
    if name != family:
        level = level or name[len(family) + 1 :]
    warn = None
    if not level:
        level = CURSOR_EFFORT_DEFAULT
    elif level not in cursor_efforts(family):
        warn = f"усилие {level} у {family} поднято до {CURSOR_EFFORT_DEFAULT} (не ниже high — слово владельца)"
        level = CURSOR_EFFORT_DEFAULT
    return f"{family}-{level}", warn


# --- Claude Code (Ф78-2 П8: путь для разовых ходов агента и фактов; перенесено из facts/files.py) ---------

CLAUDE_DEFAULT_PATH = ".local/bin/claude"  # от домашнего каталога


def claude_path(path: str | None, home: Path) -> str:
    """Абсолютный путь к `claude`: заданный (`~` раскрывается от `home`), иначе `~/.local/bin/claude`, иначе
    `shutil.which` (у службы systemd `~/.local/bin` может не быть в `PATH`). Нет исполняемого файла —
    `EngineUnavailable`."""
    if isinstance(path, str) and path.strip():
        value = path.strip()
        if value.startswith("~/"):
            value = str(Path(home) / value[2:])
        if os.path.isabs(value) and os.path.isfile(value) and os.access(value, os.X_OK):
            return value
        raise EngineUnavailable(f"claude не найден: {value}")
    default = str(Path(home) / CLAUDE_DEFAULT_PATH)
    if os.path.isfile(default) and os.access(default, os.X_OK):
        return default
    found = shutil.which("claude")
    if found:
        return found
    raise EngineUnavailable(f"claude не найден: {default}")


# --- Codex (Ф74-5, docs/architecture.md) -----------------------------------------------------

CODEX_DEFAULT_PATH = ".local/bin/codex"  # от домашнего каталога


def load_codex(config_dir: Path) -> dict:
    """Секция `codex` из `<config_dir>/engine.json` (`path` — путь к `codex`). Нет файла или секции — `{}`;
    битый файл или секция не объект — `EngineUnavailable`. Модели и усилия — у движка (`model/list`)."""
    section = _load_setup(config_dir).get("codex", {})
    if not isinstance(section, dict):
        raise EngineUnavailable(f"{Path(config_dir) / SETUP_FILE}: «codex» должен быть объектом")
    return section


def codex_path(section: dict | None, home: Path) -> str:
    """Абсолютный путь к `codex`: `engine.json` (`codex.path`), иначе `~/.local/bin/codex`. Нет файла или он
    не исполняемый — `EngineUnavailable`."""
    path = (section or {}).get("path")
    if not (isinstance(path, str) and path):
        path = str(Path(home) / CODEX_DEFAULT_PATH)
    if not os.path.isabs(path) or not os.path.isfile(path) or not os.access(path, os.X_OK):
        raise EngineUnavailable(f"codex не найден: {path}")
    return path
