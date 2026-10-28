"""Файлы дежурного агента. Спека 7b, B1 и поправки К4 («каталог агента»), мелочи (битый state); Ф57-1 —
модели нет (ключи модели в старом конфиге не читаются).

- `<config_dir>/agent/` (0700): `token` — bearer ядра (0600), `token.json` — `{token_id}` без секрета (чтобы
  повторный `hub agent setup` отозвал прежний), `config.json` — не секрет (окна, тихие часы, что не
  разбирается…).
- `<data_dir>/agent/` (0700): `state.json` — курсор, счётчики, ошибки; пишет только hub-agent.
  Юнит hub-agent может писать ТОЛЬКО сюда (`ReadWritePaths=%h/.local/share/hub/agent`).

Значение токена этот модуль никуда не печатает и не логирует. Запись — атомарно (временный файл
0600 рядом, fsync, `os.replace`).
"""

import json
import logging
import os
from pathlib import Path

_logger = logging.getLogger(__name__)

AGENT_DIR = "agent"
TOKEN_FILE = "token"  # noqa: S105 — имя файла, не секрет
TOKEN_META_FILE = "token.json"  # noqa: S105 — имя файла, не секрет
CONFIG_FILE = "config.json"
STATE_FILE = "state.json"
TOKEN_NAME = "agent:triage"  # noqa: S105 — имя токена в ядре, не секрет
TOKEN_SCOPES = ("read", "triage")

DEFAULT_CONFIG: dict = {
    # Тик (карточки созревших бесед) — раз в tick_s, опрос `/sync` — раз в poll_s (П14).
    "tick_s": 15,
    "poll_s": 5,
    # Ф8-3 B2/B5: беседа «реагирую» — карточка по правилам после `react_silence_s` тишины, но не позже
    # `react_max_wait_s` от первого сообщения; уведомление — не чаще `react_notify_every_s` на беседу и только
    # о сообщениях не старше `react_notify_max_age_s` (старше — одна сводная строка).
    # Владелец 12.10.2026: было 120 и 600 — дежурная сессия проекта получала сигнал через 2–3 минуты
    # («висит больше минуты, никто не разбирает»); тик — раз в `tick_s`, так что на деле 15–30 с.
    "react_silence_s": 15,
    "react_max_wait_s": 120,
    "react_notify_every_s": 180,
    "react_notify_max_age_s": 600,
    "quiet_hours": "23:00-08:00",
    "skip_projects": [],
    "skip_channels": [],
    # Решение владельца: названия каналов, которые не разбираются (без учёта регистра, с `#` или без).
    # «#youtrack» в Slack Globex — лента всех изменений задач YouTrack: шум.
    "skip_channel_titles": ["#youtrack"],
    # П1: предохранитель — непрочитанное старше N дней не разбирается (новый аккаунт без маркеров прочтения).
    "max_age_days": 3,
}

# Ключи прежних версий, которые в боевом `config.json` уже лежат: DeepSeek (7b), локальная модель Ollama и
# её окна созревания (7b-2; Ф57-1 — модели нет). При чтении молча выкидываются.
LEGACY_CONFIG_KEYS = (
    "provider",
    "model",
    "base_url",
    "proxy",
    "prices",
    "peak_hours_utc",
    "daily_budget_usd",
    "llm_url",
    "llm_model",
    "llm_num_ctx",
    "llm_keep_alive",
    "llm_timeout_s",
    "llm_max_prompt_chars",
    "max_calls_per_hour",
    "notify_labels",
    "mature_s",
    "mature_priority_s",
    "mature_count",
)


def private_dir(path: Path) -> Path:
    """Каталог 0700 (создаётся при необходимости, права подтягиваются, если были шире)."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_mode & 0o077:
        path.chmod(0o700)
    return path


def config_agent_dir(config_dir: Path) -> Path:
    return config_dir / AGENT_DIR


def data_agent_dir(data_dir: Path) -> Path:
    return data_dir / AGENT_DIR


def token_path(config_dir: Path) -> Path:
    return config_agent_dir(config_dir) / TOKEN_FILE


def token_meta_path(config_dir: Path) -> Path:
    return config_agent_dir(config_dir) / TOKEN_META_FILE


def config_path(config_dir: Path) -> Path:
    return config_agent_dir(config_dir) / CONFIG_FILE


def state_path(data_dir: Path) -> Path:
    return data_agent_dir(data_dir) / STATE_FILE


def atomic_write(path: Path, data: str) -> None:
    """Временный файл 0600 рядом (O_EXCL), fsync, `os.replace` — читатель видит либо старое, либо новое."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)  # умаск процесса мог ослабить права при open()
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def read_secret(path: Path) -> str | None:
    """Токен из файла или `None` (нет файла, нечитаем, пуст)."""
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError, UnicodeDecodeError:
        return None
    return value or None


def write_secret(path: Path, value: str) -> None:
    private_dir(path.parent)
    atomic_write(path, value + "\n")


def read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError, UnicodeDecodeError, ValueError:
        return None
    return value if isinstance(value, dict) else None


def load_config(config_dir: Path) -> dict:
    """Конфиг агента поверх умолчаний: нет файла — умолчания; битый файл — умолчания и строка в журнал.
    Ключи прежних версий (`LEGACY_CONFIG_KEYS`: DeepSeek, локальная модель) из файла игнорируются."""
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    path = config_path(config_dir)
    if not path.exists():
        return config
    stored = read_json(path)
    if stored is None:
        _logger.warning("agent: config.json не читается — работаю на умолчаниях")
        return config
    config.update(stored)
    for key in LEGACY_CONFIG_KEYS:
        config.pop(key, None)
    return config


def write_default_config(config_dir: Path) -> bool:
    """Записать конфиг по умолчанию, если его нет. `True` — записан."""
    path = config_path(config_dir)
    if path.exists():
        return False
    private_dir(path.parent)
    atomic_write(path, json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n")
    return True


def load_state(data_dir: Path) -> dict | None:
    """Состояние агента; нет файла или он битый — `None` (вызывающий пишет строку в журнал и берёт
    `max_seq`, мелочи поправок)."""
    return read_json(state_path(data_dir))


def save_state(data_dir: Path, state: dict) -> None:
    private_dir(data_agent_dir(data_dir))
    atomic_write(state_path(data_dir), json.dumps(state, ensure_ascii=False, indent=1) + "\n")
