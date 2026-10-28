"""Профили hub-mcp: имя, файл токена и файл описания. Спека 7a, B3.

`<config_dir>/mcp/` — каталог 0700; `<профиль>.token` — сам bearer-токен (0600, запись атомарно через
временный файл в том же каталоге); `<профиль>.json` — `{token_id, projects, scopes}` без секрета. Значение
токена этот модуль никуда не печатает и не логирует.
"""

import json
import os
import re
from pathlib import Path

PROFILE_RE = re.compile(r"[a-z0-9-]{1,32}")
MCP_DIR_NAME = "mcp"

# Ф13-1 D: подписка сессии на проект — всем профилям, КРОМЕ `orchestrator` (его список — ровно выше).
SUB_TOOLS = ("hub_subscribe", "hub_unsubscribe", "hub_subscriptions")
# Ф15-1 B: реакция сессии — тоже не оркестратору (его списка выше она не касается).
REACT_TOOLS = ("hub_react",)
# Ф17-1 B: вступление в группы и каналы — тоже не оркестратору; Ф17-2 B: выход тоже.
JOIN_TOOLS = ("hub_join", "hub_joins", "hub_leave")
# Ф29-1 B: поиск и чтение Telegram без вступления — тоже не оркестратору.
TG_LOOKUP_TOOLS = ("hub_tg_search", "hub_tg_peek")
# Ф34-2 П2: напоминания ставит и ведёт подписанная сессия — тоже не оркестратору.
REMIND_TOOLS = ("hub_remind", "hub_reminders", "hub_remind_done")
# Ф35-1b: переписка агентов — тоже подписанной сессии: не оркестратору.
# Ф50-4: `hub_agent_state` — состояние роли своего проекта, тем же сессиям.
PEER_TOOLS = ("hub_agents", "hub_agent_send", "hub_agent_inbox", "hub_role", "hub_agent_state")
# Ф37-3: отметка «разобрано» — подписанной сессии проекта с ролью: не оркестратору.
HANDLED_TOOLS = ("hub_handled",)
# Ф38-2 B: заявка на правило раскладки — тоже не оркестратору (как `JOIN_TOOLS`).
RULE_REQUEST_TOOLS = ("hub_propose_rule", "hub_rule_requests")
# Ф41-5б (5.3): заявки агентов владельцу (напарник; список своих заявок) — тоже не оркестратору.
AGENT_REQUEST_TOOLS = ("hub_request_session", "hub_agent_requests")
# Ф44-1c: нажатие кнопок Telegram-ботов — тоже не оркестратору (как `JOIN_TOOLS`).
PRESS_TOOLS = ("hub_press", "hub_presses")
# Ф52-1 C: учёт нагрузки ноутбука — сессиям, которые запускают тяжёлое сами: не оркестратору.
LOAD_TOOLS = ("hub_load",)
# Ф54-1: заявка «нужен человек» — подписанной сессии проекта с ролью: не оркестратору.
HUMAN_TOOLS = ("hub_human", "hub_humans", "hub_human_cancel")
# Ф56-1б (А4б): вопрос владельцу от сессии с ролью — подписанной сессии: не оркестратору.
ASK_TOOLS = ("hub_ask_owner",)
# Ф63-1: правила проекта для входящих — сессии с подпиской-ролью: не оркестратору.
INBOUND_RULE_TOOLS = ("hub_inbound_rules",)
# Ф62-6 F: группы WhatsApp (создать, участники, ссылка, свои заявки) — тоже не оркестратору.
GROUP_TOOLS = ("hub_group_create", "hub_group_members", "hub_group_link", "hub_group_ops")


class ProfileError(ValueError):
    """Неверное имя профиля."""


def validate_profile(name: str | None) -> str:
    if name is None or PROFILE_RE.fullmatch(name) is None:
        raise ProfileError(f"имя профиля — [a-z0-9-]{{1,32}}, получено {name!r}")
    return name


def profile_dir(config_dir: Path) -> Path:
    """`<config_dir>/mcp`, 0700 (создаётся при необходимости, права подтягиваются, если были шире)."""
    path = config_dir / MCP_DIR_NAME
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_mode & 0o077:
        path.chmod(0o700)
    return path


def token_path(config_dir: Path, profile: str) -> Path:
    return config_dir / MCP_DIR_NAME / f"{validate_profile(profile)}.token"


def meta_path(config_dir: Path, profile: str) -> Path:
    return config_dir / MCP_DIR_NAME / f"{validate_profile(profile)}.json"


def read_token(config_dir: Path, profile: str) -> str | None:
    """Токен профиля или `None` (нет файла, нечитаем, пуст, неверное имя профиля) — сервер при этом всё равно
    работает и объясняет в каждом инструменте, как настроить профиль (B3)."""
    try:
        value = token_path(config_dir, profile).read_text(encoding="utf-8").strip()
    except OSError, UnicodeDecodeError, ProfileError:
        return None
    return value or None


def read_meta(config_dir: Path, profile: str) -> dict | None:
    try:
        meta = json.loads(meta_path(config_dir, profile).read_text(encoding="utf-8"))
    except OSError, UnicodeDecodeError, ValueError, ProfileError:
        return None
    return meta if isinstance(meta, dict) else None


def _atomic_write(path: Path, data: str) -> None:
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


def write_meta(config_dir: Path, profile: str, meta: dict) -> None:
    """Ф14-3 C: только описание профиля (атомарно, 0600) — `hub mcp attach` меняет `scopes`, токен тот же."""
    validate_profile(profile)
    profile_dir(config_dir)
    _atomic_write(meta_path(config_dir, profile), json.dumps(meta, ensure_ascii=False, indent=2) + "\n")


def write_profile(config_dir: Path, profile: str, *, token: str, meta: dict) -> None:
    """Записать токен и описание профиля. Сначала токен, потом описание: после сбоя между двумя записями в
    описании останется id прежнего токена — следующий `hub mcp setup` отзовёт именно его (он ещё живой), а
    файл токена всё это время указывает на действующий токен."""
    validate_profile(profile)
    profile_dir(config_dir)
    _atomic_write(token_path(config_dir, profile), token + "\n")
    _atomic_write(meta_path(config_dir, profile), json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
