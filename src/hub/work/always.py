"""«Всегда в этой сессии» на карточке разрешения сессии хаба (Ф99-7.2, `docs/architecture.md`).

Отдельный от `auto_free` набор сессии: действует и в ручном режиме, без перезапуска процесса. Ключ — строка:
- `mcp:<точное имя инструмента MCP>` — карточка пишущего MCP (`mcp_write`); хост отвечает «разрешить» сам
  (Claude Code — на запрос движка, OpenCode/Cursor/Codex — в `_oc_decide`);
- `dir:<абсолютный каталог>` — карточка записи вне папки проекта (`outside_workdir`) инструментом файлов:
  точный каталог записи, гард пропускает файлы прямо в нём (через сокет набора, `guard.ALWAYS_DIR`).

Всё прочее — без кнопки (проверка здесь, её зовёт ЯДРО): кнопки этапа 1 (push, server, deploy, kube, helm,
gh, trello), чужие хуки и правила проекта, сбой проверки, неразобранная команда, вложенный агент, вопросы
(и с вариантами), инструменты самого хаба (отправки — черновики и жёсткие правила). Движку «always» не
шлётся никогда — только разовое «разрешить». Только stdlib и модули хоста без импортов хаба."""

import os

from . import guard, policy

MCP = "mcp:"
DIR = guard.ALWAYS_DIR
# Набор едет гарду одной строкой сокета (`guard.FREE_REPLY_MAX` = 16 КБ): 30 ключей по 400 знаков — с запасом.
MAX = 30
KEY_MAX = 400
RULES = (policy.MCP_WRITE, "outside_workdir")
_PATH_KEYS = ("file_path", "notebook_path")


def hub_tool(tool: str) -> bool:
    """Инструмент MCP самого хаба (сервер `hub…` или имя `hub_*`) — у него «всегда» нет."""
    call = policy.split_mcp(tool)
    if call is None:
        return False
    server, name = call
    return server.startswith("hub") or name.startswith("hub_")


def key_of(*, kind, rule, tool, tool_input, truncated=False) -> str | None:
    """Ключ «всегда» для карточки; `None` — у карточки кнопки нет."""
    if kind != "permission" or not isinstance(tool, str) or not tool:
        return None
    if rule == policy.MCP_WRITE:
        if policy.split_mcp(tool) is None or hub_tool(tool):
            return None
        key = MCP + tool
    elif rule == "outside_workdir":
        if tool not in guard.EDIT_TOOLS or truncated or not isinstance(tool_input, dict):
            return None
        raw = next((tool_input[k] for k in _PATH_KEYS if isinstance(tool_input.get(k), str)), None)
        if raw is None or not os.path.isabs(raw):
            return None
        folder = os.path.dirname(os.path.normpath(raw))
        if folder in ("/", "") or "\n" in folder or "\x00" in folder:
            return None
        # Ревью Ф99: не домашний каталог и не его предки («всегда» на `~/notes.md` открыло бы настройки
        # оболочки и git там же), не файл с именем на «.».
        home = os.path.realpath(os.path.expanduser("~"))
        real = os.path.realpath(folder)
        if home == real or home.startswith(real.rstrip("/") + "/") or os.path.basename(raw).startswith("."):
            return None
        key = DIR + folder
    else:
        return None
    return key if len(key) <= KEY_MAX else None


def settings_paths(home: str, folders) -> list[str]:
    """Файлы настроек Claude Code, чьи PreToolUse-хуки действуют на сессию: владельца и папок проекта/процесса."""
    names = ("settings.json", "settings.local.json")
    out = [os.path.join(str(base), ".claude", name) for base in (home, *folders) if base for name in names]
    return list(dict.fromkeys(out))


def hooked(tool: str, paths) -> bool:
    """Ревью Ф99: на инструмент `tool` висит PreToolUse-хук из настроек `paths` (не хаба — свой хук хаб подаёт
    через `--settings`, в файлах его нет). Тогда «всегда» запрос движка не закрывает: тип `rule` приходит и
    когда вместе с правилом спросил хук (проба 5а), а его вопрос решает владелец. Сбой чтения или кривой
    matcher — считается хуком (карточка)."""
    import json  # noqa: PLC0415
    import re  # noqa: PLC0415

    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            return True
        entries = ((data.get("hooks") or {}).get("PreToolUse") or []) if isinstance(data, dict) else []
        for entry in entries if isinstance(entries, list) else []:
            matcher = entry.get("matcher") if isinstance(entry, dict) else None
            if matcher in (None, "", "*"):
                return True
            try:
                if isinstance(matcher, str) and re.fullmatch(matcher, tool):
                    return True
            except re.error:
                return True
    return False


def valid(key) -> bool:
    """Ключ набора (как его хранит ядро и отдаёт хосту)."""
    if not isinstance(key, str) or len(key) > KEY_MAX:
        return False
    if key.startswith(MCP):
        tool = key[len(MCP) :]
        return policy.split_mcp(tool) is not None and not hub_tool(tool)
    return guard.always_dir_entry(key)
