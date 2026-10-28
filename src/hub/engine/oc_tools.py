"""Перевод вызова инструмента OpenCode в полезную нагрузку PreToolUse Claude (Ф16, 3.3, П7).

Гарды хаба (`hub.wake.guard.decide`, `hub.work.guard.decide`, `hub.work.policy`) написаны под события хука
Claude (`{"tool_name", "tool_input", "cwd"}`). Чтобы не переписывать их, драйвер OpenCode переводит вызов:
имя инструмента берётся из части сообщения (`part.tool`, НЕ из имени разрешения: у `write`/`apply_patch`
разрешение называется `edit`), аргументы — `state.input` целиком (F7).

Ключи аргументов встроенных инструментов — по схемам OpenCode 1.18.31 (проба P8,
`tests/engine/data/oc-tools-1.18.json`; тест сверяет таблицу со схемами). Каждый аргумент-путь доходит до
гарда (П7): `grep.include` -> `Grep.glob` (гард проверяет его как шаблон файлов), `bash.workdir` -> `cwd`
события. Что сделать с ключом вне схемы или с патчем без разобранного пути, решает вызывающий (wake —
отказ, work — карточка), поэтому перевод только помечает это: `unknown_keys`, `unparsed`.

Результат `to_claude` — словарь:
- `tool_name`, `tool_input` — как у Claude;
- `cwd` — только если вызов задал свой каталог (`bash.workdir`);
- `files` — все файлы патча (`apply_patch`/`patch`), `tool_input.file_path` — первый из них;
- `unknown_keys` — ключи встроенного инструмента вне схемы (отсортированы; нет — ключа нет);
- `unparsed: True` — обязательный путь не разобран (у `read`/`edit`/`write` нет пути, у патча — ни одного
  файла).
`guard_events` разворачивает результат в события хука (по одному на каждый файл патча).
"""

import os
import re

# Встроенные инструменты: имя Claude, перевод ключей (ключ OpenCode -> ключ Claude; `None` — ключ известен,
# но гарду не нужен и в `tool_input` не переносится), обязательный путь.
TOOLS: dict[str, dict] = {
    "bash": {
        "claude": "Bash",
        "keys": {"command": "command", "timeout": "timeout", "description": "description", "workdir": None},
    },
    "read": {
        "claude": "Read",
        "keys": {"filePath": "file_path", "offset": "offset", "limit": "limit"},
        "path": "file_path",
    },
    "edit": {
        "claude": "Edit",
        "keys": {
            "filePath": "file_path",
            "oldString": "old_string",
            "newString": "new_string",
            "replaceAll": "replace_all",
        },
        "path": "file_path",
    },
    "write": {
        "claude": "Write",
        "keys": {"filePath": "file_path", "content": "content"},
        "path": "file_path",
    },
    # Модели DeepSeek/Grok этих двух не получают (P8), но в `ids` 1.18.31 `apply_patch` есть; вход —
    # `patchText` (образец разбора — `patchFiles` гарда владельца).
    "apply_patch": {"claude": "Edit", "keys": {"patchText": None}, "patch": True},
    "patch": {"claude": "Edit", "keys": {"patchText": None}, "patch": True},
    "grep": {"claude": "Grep", "keys": {"pattern": "pattern", "path": "path", "include": "glob"}},
    "glob": {"claude": "Glob", "keys": {"pattern": "pattern", "path": "path"}},
    # В 1.18.31 `list` нет (P8); оставлен на случай старых/новых версий: смотрит каталог целиком.
    "list": {"claude": "Glob", "keys": {"path": "path", "ignore": None}, "fixed": {"pattern": "*"}},
    "webfetch": {"claude": "WebFetch", "keys": {"url": "url", "format": None, "timeout": None}},
    "websearch": {"claude": "WebSearch", "keys": {"query": "query"}, "open": True},
    "task": {
        "claude": "Task",
        "keys": {
            "description": "description",
            "prompt": "prompt",
            "subagent_type": "subagent_type",
            "task_id": "task_id",
            "command": "command",
        },
    },
    "todowrite": {"claude": "TodoWrite", "keys": {"todos": "todos"}},
    "todoread": {"claude": "TodoWrite", "keys": {}, "open": True},
    "question": {"claude": "AskUserQuestion", "keys": {"questions": "questions"}},
    "skill": {"claude": "Skill", "keys": {"name": "skill"}},
}

# Инструменты, у которых ключ вне схемы — повод для отказа/карточки (П7: файловые и bash).
FILE_TOOLS = frozenset({"bash", "read", "edit", "write", "apply_patch", "patch", "grep", "glob", "list"})

_PATCH_FILE = re.compile(r"^\*\*\*\s*(?:Add File|Update File|Delete File|Move to)\s*:\s*(.+)$", re.IGNORECASE)
_PATCH_MARKS = re.compile(r"^\*\*\*\s*(?:Begin Patch|End Patch|End of File)\s*$", re.IGNORECASE)


def patch_files(text) -> list[str]:
    """Файлы из текста патча OpenCode/Codex (`*** Add File|Update File|Delete File|Move to: <путь>`), по
    порядку, без повторов. Ревью Ф74 (1): строка берётся без отступа по краям и без учёта регистра — парсер
    Codex 0.163 принимает заголовок с ведущими пробелами или табом; лишний «файл» из строки содержимого
    гарду безопасен (судит больше), пропущенный — нет."""
    if not isinstance(text, str):
        return []
    seen: list[str] = []
    for line in text.splitlines():
        match = _PATCH_FILE.match(line.strip())
        if match is None:
            continue
        path = match.group(1).strip()
        if path and path not in seen:
            seen.append(path)
    return seen


def patch_suspect(text) -> bool:
    """Ревью Ф74 (1): в патче строка, которая без отступа начинается с `***`, но не известный маркер и не
    заголовок файла, — патч гарду неясен (карточка, `unparsed`)."""
    if not isinstance(text, str):
        return False
    for line in text.splitlines():
        bare = line.strip()
        if bare.startswith("***") and not (_PATCH_MARKS.match(bare) or _PATCH_FILE.match(bare)):
            return True
    return False


def mcp_name(tool: str, mcp_servers) -> str | None:
    """`<сервер>_<инструмент>` -> `mcp__<сервер>__<инструмент>`; сервер — самый длинный подходящий префикс
    (имена серверов бывают с `_` и `-`: `hub-all_hub_draft` -> `mcp__hub-all__hub_draft`)."""
    best = None
    for server in mcp_servers or ():
        if not isinstance(server, str) or not server:
            continue
        prefix = f"{server}_"
        if tool.startswith(prefix) and len(tool) > len(prefix) and (best is None or len(server) > len(best)):
            best = server
    if best is None:
        return None
    return f"mcp__{best}__{tool[len(best) + 1 :]}"


def to_claude(tool: str, args, *, mcp_servers=()) -> dict:
    """Перевод одного вызова (см. докстринг модуля)."""
    args = args if isinstance(args, dict) else {}
    spec = TOOLS.get(tool)
    if spec is None:
        name = mcp_name(tool, mcp_servers)
        # MCP — аргументы как есть (у гардов хаба свои проверки, например `channel_id` черновика);
        # неизвестное — имя как есть: гард не узнает его и откажет.
        return {"tool_name": name or tool, "tool_input": dict(args)}

    keys: dict = spec["keys"]
    tool_input: dict = dict(spec.get("fixed", {}))
    for key, value in args.items():
        target = keys.get(key)
        if target is not None:
            tool_input[target] = value
    result: dict = {"tool_name": spec["claude"], "tool_input": tool_input}

    if not spec.get("open"):
        unknown = sorted(k for k in args if k not in keys)
        if unknown:
            result["unknown_keys"] = unknown
    if tool == "bash":
        workdir = args.get("workdir")
        if workdir is not None:
            result["cwd"] = workdir
    if spec.get("patch"):
        files = patch_files(args.get("patchText"))
        result["files"] = files
        if files:
            tool_input["file_path"] = files[0]
        if not files or patch_suspect(args.get("patchText")):
            result["unparsed"] = True
    path_key = spec.get("path")
    if path_key and not (isinstance(tool_input.get(path_key), str) and tool_input[path_key]):
        result["unparsed"] = True
    return result


def guard_events(payload: dict, *, cwd: str, session_id: str) -> list[dict]:
    """События хука PreToolUse из результата `to_claude`.

    `cwd` — `payload["cwd"]` (bash.workdir; относительный — от каталога сессии) или каталог сессии. Патч — по
    событию `Edit` на каждый файл; нет файлов — одно событие без пути (гард решит сам, вызывающий видит
    `unparsed`)."""
    event_cwd = cwd
    own = payload.get("cwd")
    if isinstance(own, str) and own:
        # Относительный `workdir` bash — от каталога сессии, как его понимает OpenCode.
        event_cwd = os.path.normpath(os.path.join(cwd, own))
    base = {"tool_name": payload["tool_name"], "cwd": event_cwd, "session_id": session_id}
    files = payload.get("files")
    if files:
        return [{**base, "tool_input": {**payload["tool_input"], "file_path": f}} for f in files]
    return [{**base, "tool_input": dict(payload["tool_input"])}]
