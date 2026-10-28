"""Ф71-1: вызовы Cursor в терминах хука Claude (docs/architecture.md).

Гард и правила хаба (`hub.work.guard.decide`, `hub.work.policy`) написаны под события хука Claude
(`{"tool_name", "tool_input", "cwd"}`). Хуки Cursor (`preToolUse`, `beforeMCPExecution`, `subagentStart`)
переводятся сюда ТОЛЬКО по белому списку (вторая проба 19.10 — других имён не было):

- `Shell` {command, cwd, timeout} -> `Bash` {command}; `cwd` — каталог события (пустой — каталог сессии,
  относительный — от него; это делает `oc_tools.guard_events`);
- `Read` {file_path} -> `Read`; `Write` {file_path, content} -> `Write` (и новый файл, и правка — всегда
  полное
  содержимое);
- `Delete` {file_path} -> `Bash` `rm -- '<абсолютный путь>'`: гард проверяет цель записи `rm` (те же правила,
  что у записи: правила проекта, `~/.claude`, каталоги хаба, вне папки проекта), а карточка владельцу читается
  как команда удаления;
- `Grep` {pattern, file_path?, glob?, output_mode} -> `Grep` {pattern, path, glob}: гард проверяет `glob` как
  шаблон файлов (так же идёт и `Glob` модели);
- `beforeMCPExecution` {mcp_server_name, tool_name, tool_input — строка JSON} -> `mcp__<сервер>__<инструмент>`
  с разобранным входом; `preToolUse` вызова `MCP:<инструмент>` пропускается (`skip`): имени сервера в нём нет,
  решает `beforeMCPExecution`;
- `subagentStart` -> `Task` (вход как есть).

Что не узнано, НЕ пропускается: незнакомое имя — `unknown_tool`, ключ вне списка — `unknown_keys`, нет пути
или команды — `unparsed`, вход MCP не разобран — `unparsed`. Решает вызывающий (хост: карточка владельцу).

`stream_call` — имя и вход вызова из потока событий (`tool_call`) для ленты окна сессии.
"""

import json
import os
import shlex

PRE = "preToolUse"
MCP = "beforeMCPExecution"
SUBAGENT = "subagentStart"
HOOK_EVENTS = (PRE, MCP, SUBAGENT)
MCP_PREFIX = "MCP:"

# Ключи входа, которые известны и гарду не нужны (не пути): с ними вызов не «незнакомый».
_EXTRA = {
    "Shell": {"timeout", "description", "is_background", "run_in_background"},
    "Read": {"offset", "limit"},
    "Write": set(),
    "Delete": set(),
    "Grep": {
        "output_mode",
        "-i",
        "-n",
        "-A",
        "-B",
        "-C",
        "case_insensitive",
        "head_limit",
        "multiline",
        "type",
        "offset",
    },
}


def _unknown(name: str, args: dict, known: set[str]) -> list[str]:
    return sorted(k for k in args if k not in known | _EXTRA.get(name, set()))


def _text(value) -> str | None:
    return value if isinstance(value, str) and value else None


def from_hook(event: str, data: dict, *, cwd: str) -> dict:
    """Вход хука Cursor -> нагрузка хука Claude (см. докстринг модуля). `cwd` — каталог процесса сессии:
    от него считается относительный путь `Delete`. Помимо `tool_name`/`tool_input`: `cwd` (каталог `Shell`),
    `skip` (решать нечего — разрешить), `unknown_tool`, `unknown_keys`, `unparsed`."""
    data = data if isinstance(data, dict) else {}
    if event == SUBAGENT:
        return {"tool_name": "Task", "tool_input": {k: v for k, v in data.items() if k == "subagent_type"}}
    if event == MCP:
        server = _text(data.get("mcp_server_name"))
        tool = _text(data.get("tool_name"))
        raw = data.get("tool_input")
        try:
            args = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            args = None
        if server is None or tool is None:
            return {"tool_name": f"MCP:{tool or '?'}", "tool_input": {}, "unparsed": True}
        name = f"mcp__{server}__{tool}"
        if not isinstance(args, dict):
            return {"tool_name": name, "tool_input": {}, "unparsed": True}
        return {"tool_name": name, "tool_input": dict(args)}
    if event != PRE:
        return {"tool_name": str(event or "?"), "tool_input": {}, "unknown_tool": True}
    name = str(data.get("tool_name") or "")
    args = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    if name.startswith(MCP_PREFIX):
        return {"tool_name": name, "tool_input": dict(args), "skip": True}
    result: dict
    if name == "Shell":
        result = {"tool_name": "Bash", "tool_input": {}}
        command = _text(args.get("command"))
        if command is None:
            result["unparsed"] = True
        else:
            result["tool_input"]["command"] = command
        own = args.get("cwd")
        if isinstance(own, str) and own:
            result["cwd"] = own
        known = {"command", "cwd"}
    elif name in ("Read", "Write"):
        path = _text(args.get("file_path"))
        result = {
            "tool_name": name,
            "tool_input": {k: args[k] for k in ("file_path", "content") if k in args},
        }
        if path is None:
            result["unparsed"] = True
        known = {"file_path", "content"} if name == "Write" else {"file_path"}
    elif name == "Delete":
        path = _text(args.get("file_path"))
        result = {"tool_name": "Bash", "tool_input": {}}
        if path is None:
            result["unparsed"] = True
        else:
            full = os.path.normpath(os.path.join(cwd, os.path.expanduser(path)))
            result["tool_input"]["command"] = f"rm -- {shlex.quote(full)}"
        known = {"file_path"}
    elif name == "Grep":
        tool_input: dict = {}
        if isinstance(args.get("pattern"), str):
            tool_input["pattern"] = args["pattern"]
        if _text(args.get("file_path")) is not None:
            tool_input["path"] = args["file_path"]
        if _text(args.get("glob")) is not None:
            tool_input["glob"] = args["glob"]
        result = {"tool_name": "Grep", "tool_input": tool_input}
        known = {"pattern", "file_path", "glob"}
    elif name == "Task":
        # Подагент Cursor: его вызовы идут через те же хуки (проба 19.10); хосту нужны тип и модель.
        tool_input = {k: args[k] for k in ("subagent_type", "model") if isinstance(args.get(k), str)}
        return {"tool_name": "Task", "tool_input": tool_input}
    else:
        return {"tool_name": name or "?", "tool_input": dict(args), "unknown_tool": True}
    unknown = _unknown(name, args, known)
    if unknown:
        result["unknown_keys"] = unknown
    return result


# --- поток событий -----------------------------------------------------------------------------------------

_STREAM_NAMES = {
    "shellToolCall": "Bash",
    "readToolCall": "Read",
    "editToolCall": "Write",
    "deleteToolCall": "Delete",
    "grepToolCall": "Grep",
    "globToolCall": "Glob",
}


def stream_call(tool_call: dict) -> tuple[str, dict, dict | None]:
    """`tool_call` события потока -> (имя в терминах Claude, вход для ленты, `result` или `None`)."""
    tool_call = tool_call if isinstance(tool_call, dict) else {}
    kind, body = next(
        ((k, v) for k, v in tool_call.items() if k.endswith("ToolCall") and isinstance(v, dict)), ("?", {})
    )
    args = body.get("args") if isinstance(body.get("args"), dict) else {}
    result = body.get("result") if isinstance(body.get("result"), dict) else None
    if kind == "shellToolCall":
        tool_input = {"command": args.get("command")}
        if _text(args.get("workingDirectory")):
            tool_input["cwd"] = args["workingDirectory"]
        return "Bash", tool_input, result
    if kind in ("readToolCall", "editToolCall", "deleteToolCall"):
        return _STREAM_NAMES[kind], {"file_path": args.get("path")}, result
    if kind == "grepToolCall":
        keys = {"pattern": "pattern", "path": "path", "glob": "glob"}
        return "Grep", {dst: args[src] for src, dst in keys.items() if src in args}, result
    if kind == "globToolCall":
        tool_input = {"pattern": args.get("globPattern")}
        if _text(args.get("targetDirectory")):
            tool_input["path"] = args["targetDirectory"]
        return "Glob", tool_input, result
    if kind == "mcpToolCall":
        server = _text(args.get("providerIdentifier")) or _text(args.get("serverIdentifier")) or "?"
        tool = _text(args.get("toolName")) or "?"
        inner = args.get("args") if isinstance(args.get("args"), dict) else {}
        return f"mcp__{server}__{tool}", dict(inner), result
    name = kind[: -len("ToolCall")] if kind.endswith("ToolCall") else kind
    shown = {k: v for k, v in args.items() if k != "toolCallId"}
    return (name[:1].upper() + name[1:]) or "?", shown, result


def result_text(name: str, result: dict | None) -> tuple[str, bool, dict | None]:
    """`result` вызова потока -> (текст `tool_result`, ошибка ли, `tool_use_result` для diff или `None`)."""
    if not isinstance(result, dict) or not result:
        return "", False, None
    if isinstance(result.get("success"), dict):
        ok = result["success"]
        if name == "Bash":
            out = ok.get("interleavedOutput")
            if not isinstance(out, str):
                out = "".join(str(ok.get(k) or "") for k in ("stdout", "stderr"))
            code = ok.get("exitCode")
            failed = isinstance(code, int) and not isinstance(code, bool) and code != 0
            return out, failed, None
        if name == "Write" and isinstance(ok.get("diffString"), str):
            use = {"patch": ok["diffString"]}
            if isinstance(ok.get("path"), str):
                use["filePath"] = ok["path"]
            return str(ok.get("message") or ""), False, use
        if isinstance(ok.get("content"), str):
            return ok["content"], False, None
        content = ok.get("content")
        if isinstance(content, list):
            texts = []
            for item in content:
                text = (item.get("text") if isinstance(item, dict) else None) or {}
                text = text.get("text") if isinstance(text, dict) else text
                if isinstance(text, str):
                    texts.append(text)
            return "\n".join(texts), bool(ok.get("isError")), None
        return json.dumps(ok, ensure_ascii=False)[:4000], False, None
    for key in ("rejected", "error"):
        bad = result.get(key)
        if isinstance(bad, dict):
            text = bad.get("reason") or bad.get("errorMessage") or bad.get("error") or key
            return str(text), True, None
    return json.dumps(result, ensure_ascii=False)[:4000], False, None
