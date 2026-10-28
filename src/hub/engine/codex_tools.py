"""Ф74-5: вызовы Codex в терминах хука Claude (docs/architecture.md).

Гард и правила хаба (`hub.work.guard.decide`, `hub.work.policy`) написаны под события хука Claude
(`{"tool_name", "tool_input", "cwd"}`). Хук `PreToolUse` Codex 0.163.0 (проба 21.10, `.runs/codex-probe/`)
переводится сюда ТОЛЬКО по белому списку:

- `Bash` {command} -> `Bash` {command}. Параметр `workdir` вызова в хук НЕ приходит (проба 5, 21.10: `pwd` с
  `workdir=/tmp` — во входе хука только `{"command": "pwd"}`), поэтому хост команду ПРИВЯЗЫВАЕТ к папке сессии
  (`bound_command`): ушла в другую папку — не исполняется. Обращение к терминалу (`/dev/tty`, `/dev/pts`) —
  `tty`: ввод мимо хука (`write_stdin`) хост не пропускает (П-Codex-1, отступление 2 спеки);
- `apply_patch` {command: текст патча} -> `Edit` на каждый файл патча (`files`; разбор —
  `oc_tools.patch_files`);
- `mcp__<сервер>__<инструмент>` — как есть (имена MCP у Codex и Claude совпадают);
- `view_image` {path} -> `Read` {file_path};
- `list_mcp_resources` / `list_mcp_resource_templates` / `read_mcp_resource` -> `ListMcpResourcesTool` /
  `ReadMcpResourceTool`;
- `spawn_agent` (`Agent`) -> `Task` (модель подагента проверяет хост);
- `update_plan`, `send_input`, `resume_agent`, `wait_agent`, `close_agent`, `list_agents`,
  `request_user_input`
  — решать нечего (`skip`): план, переписка с подагентами (их инструменты идут через тот же хук), вопрос (его
  карточку делает запрос `item/tool/requestUserInput`).

Что не узнано, НЕ пропускается: незнакомое имя — `unknown_tool`, ключ вне списка — `unknown_keys`, нет команды
или пути — `unparsed`.

`stream_item` — имя и вход вызова из элемента потока (`commandExecution`, `fileChange`, `mcpToolCall`, …) для
ленты окна сессии; `result_text` — текст результата."""

import json
import os
import re
import shlex

from . import oc_tools

PRE = "PreToolUse"
SUBAGENT = "SubagentStart"
HOOK_EVENTS = (PRE, SUBAGENT)
MCP_PREFIX = "mcp__"

SKIP_TOOLS = frozenset(
    {
        "update_plan",
        "send_input",
        "send_message",
        "resume_agent",
        "wait_agent",
        "wait",
        "close_agent",
        "list_agents",
        "interrupt_agent",
        "followup_task",
        "request_user_input",
    }
)
SUBAGENT_TOOLS = frozenset({"spawn_agent", "Agent"})
_TTY_RE = re.compile(r"/dev/(?:tty|pts)\b")
# Ввод в запущенную команду (`write_stdin`) хук не видит: допустимы только управляющие символы (Ctrl-C,
# Ctrl-D, Enter) — остановить или дождаться команды; печатный текст — обход гарда.
_PRINTABLE_RE = re.compile(r"[^\x00-\x20\x7f]")

BOUND_HEAD = "# hub: команда привязана к папке сессии\n"
# Ф74-5б: метка хода в окружении команды — после «Прервать» хост снимает оставшиеся команды хода по ней
# (Codex на `turn/interrupt` кончает ход, а запущенную команду оставляет идти до её конца).
CMD_MARK_ENV = "HUB_CODEX_CMD"
BOUND_REFUSED = (
    "гард хаба: команда сессии хаба выполняется только в папке сессии ({cwd}) — без параметра workdir; "
    "в другую папку переходи командой cd внутри самой команды"
)


def _text(value) -> str | None:
    return value if isinstance(value, str) and value else None


def from_hook(data: dict, *, cwd: str) -> dict:
    """Вход хука `PreToolUse` -> нагрузка хука Claude (см. докстринг модуля). Помимо `tool_name`/`tool_input`:
    `files` (патч), `skip`, `tty`, `unknown_tool`, `unknown_keys`, `unparsed`."""
    data = data if isinstance(data, dict) else {}
    name = str(data.get("tool_name") or "")
    raw = data.get("tool_input")
    args = raw if isinstance(raw, dict) else {}
    if name.startswith(MCP_PREFIX) and name.count("__") >= 2:
        if not isinstance(raw, dict):
            return {"tool_name": name, "tool_input": {}, "unparsed": True}
        return {"tool_name": name, "tool_input": dict(args)}
    if name in SKIP_TOOLS:
        return {"tool_name": name, "tool_input": dict(args), "skip": True}
    result: dict
    known: set[str]
    if name == "Bash":
        result = {"tool_name": "Bash", "tool_input": {}}
        command = _text(args.get("command"))
        if command is None:
            result["unparsed"] = True
        else:
            result["tool_input"]["command"] = command
            if _TTY_RE.search(command):
                result["tty"] = True
        known = {"command"}
    elif name == "apply_patch":
        # Пути патча Codex — от папки хода (у `apply_patch` своего `workdir` нет): гард судит полные.
        files = [
            f if os.path.isabs(f) or not cwd else os.path.join(cwd, f)
            for f in oc_tools.patch_files(args.get("command"))
        ]
        result = {"tool_name": "Edit", "tool_input": {}, "files": files, "patch": True}
        if files:
            result["tool_input"]["file_path"] = files[0]
        if not files or oc_tools.patch_suspect(args.get("command")):
            result["unparsed"] = True
        known = {"command"}
    elif name == "view_image":
        path = _text(args.get("path"))
        result = {"tool_name": "Read", "tool_input": {"file_path": path} if path else {}}
        if path is None:
            result["unparsed"] = True
        known = {"path", "detail"}
    elif name in ("list_mcp_resources", "list_mcp_resource_templates"):
        result = {"tool_name": "ListMcpResourcesTool", "tool_input": dict(args)}
        known = set(args)
    elif name == "read_mcp_resource":
        result = {"tool_name": "ReadMcpResourceTool", "tool_input": dict(args)}
        known = set(args)
    elif name in SUBAGENT_TOOLS:
        tool_input = {k: args[k] for k in ("model", "agent_type", "reasoning_effort") if k in args}
        return {"tool_name": "Task", "tool_input": tool_input}
    else:
        return {"tool_name": name or "?", "tool_input": dict(args), "unknown_tool": True}
    unknown = sorted(k for k in args if k not in known)
    if unknown:
        result["unknown_keys"] = unknown
    return result


def bound_command(command: str, cwd_real: str, mark: str | None = None) -> str:
    """Команда `Bash`, привязанная к папке сессии: исполняется, только если оболочка стоит в `cwd_real`
    (Codex мог подать `workdir`, которого хук не видит), и без ввода с терминала (`write_stdin` хук не
    проходит — команда читает пустой ввод, как `Bash` у Claude). `mark` — метка хода в окружении команды
    (`CMD_MARK_ENV`), по ней хост снимает команды прерванного хода."""
    message = BOUND_REFUSED.format(cwd=cwd_real)
    export = f"export {CMD_MARK_ENV}={shlex.quote(mark)}; " if mark else ""
    return (
        BOUND_HEAD
        + f'if [ "$(pwd -P)" != {shlex.quote(cwd_real)} ]; then '
        + f"echo {shlex.quote(message)} >&2; exit 126; fi\n"
        + export
        + "exec </dev/null\n"
        + command
    )


def unbound(command: str) -> str:
    """Команда без привязки `bound_command` (для ленты и карточек)."""
    if not command.startswith(BOUND_HEAD):
        return command
    rest = command[len(BOUND_HEAD) :]
    parts = rest.split("\n", 2)
    return parts[2] if len(parts) == 3 else rest


def stdin_bypass(text) -> bool:
    """Ввод `write_stdin` с печатным текстом (проба 5: `hello\\n` дошёл до `cat` мимо хука)."""
    return isinstance(text, str) and bool(_PRINTABLE_RE.search(text))


# --- поток событий -----------------------------------------------------------------------------------------


def shell_command(item: dict) -> str:
    """Команда элемента `commandExecution`: из `commandActions` (без обёртки `/bin/bash -lc`), иначе разбором
    строки `command`."""
    actions = item.get("commandActions")
    if isinstance(actions, list) and len(actions) == 1 and isinstance(actions[0], dict):
        text = _text(actions[0].get("command"))
        if text is not None:
            return unbound(text)
    raw = str(item.get("command") or "")
    try:
        words = shlex.split(raw)
    except ValueError:
        return unbound(raw)
    if len(words) == 3 and words[0].endswith("sh") and words[1] in ("-lc", "-c"):
        return unbound(words[2])
    return unbound(raw)


TOOL_ITEMS = frozenset({"commandExecution", "fileChange", "mcpToolCall"})


def stream_item(item: dict) -> tuple[str, dict] | None:
    """Элемент потока -> (имя в терминах Claude, вход для ленты) или `None` (не вызов инструмента)."""
    kind = item.get("type")
    if kind == "commandExecution":
        tool_input = {"command": shell_command(item)}
        if _text(item.get("cwd")):
            tool_input["cwd"] = item["cwd"]
        return "Bash", tool_input
    if kind == "fileChange":
        changes = [c for c in item.get("changes") or [] if isinstance(c, dict)]
        paths = [str(c.get("path") or "") for c in changes if c.get("path")]
        added = all(((c.get("kind") or {}).get("type") == "add") for c in changes) and bool(changes)
        tool_input: dict = {"file_path": paths[0] if paths else ""}
        if len(paths) > 1:
            tool_input["files"] = paths
        return ("Write" if added else "Edit"), tool_input
    if kind == "mcpToolCall":
        server = _text(item.get("server")) or "?"
        tool = _text(item.get("tool")) or "?"
        args = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
        return f"mcp__{server}__{tool}", dict(args)
    if kind == "imageView":
        return "Read", {"file_path": str(item.get("path") or "")}
    if kind == "webSearch":
        return "WebSearch", {"query": str(item.get("query") or "")}
    if kind == "collabAgentToolCall":
        tool = str(item.get("tool") or "")
        shown = {"tool": tool}
        if _text(item.get("model")):
            shown["model"] = item["model"]
        if _text(item.get("prompt")):
            shown["prompt"] = item["prompt"][:2000]
        return "Task" if tool == "spawnAgent" else f"Agent:{tool or '?'}", shown
    return None


def result_text(item: dict, *, output: str = "", patch: str | None = None) -> tuple[str, bool, dict | None]:
    """Законченный элемент -> (текст `tool_result`, ошибка ли, `tool_use_result` для diff или `None`).
    `output` — накопленный вывод команды (`item/commandExecution/outputDelta`), `patch` — текст патча из хука
    (у `fileChange` Codex 0.163.0 `diff` бывает пустым, проба 2)."""
    kind = item.get("type")
    status = item.get("status")
    failed_status = status in ("failed", "declined")
    if kind == "commandExecution":
        out = item.get("aggregatedOutput")
        text = out if isinstance(out, str) and out else output
        code = item.get("exitCode")
        failed = failed_status or (isinstance(code, int) and not isinstance(code, bool) and code != 0)
        return text or "", failed, None
    if kind == "fileChange":
        changes = [c for c in item.get("changes") or [] if isinstance(c, dict)]
        diffs = [str(c.get("diff") or "") for c in changes]
        diff = "\n".join(d for d in diffs if d)
        if not diff and patch:
            diff = patch
        use = None
        if diff:
            use = {"patch": diff}
            if changes and changes[0].get("path"):
                use["filePath"] = str(changes[0]["path"])
        paths = ", ".join(str(c.get("path") or "") for c in changes)
        return (f"изменено: {paths}" if not failed_status else f"не применено: {paths}"), failed_status, use
    if kind == "mcpToolCall":
        error = item.get("error")
        if isinstance(error, dict) or isinstance(error, str):
            message = error.get("message") if isinstance(error, dict) else error
            return str(message or "ошибка MCP"), True, None
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        texts = []
        for block in result.get("content") or []:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                texts.append(block["text"])
        if not texts and result.get("structuredContent") is not None:
            texts.append(json.dumps(result["structuredContent"], ensure_ascii=False)[:4000])
        return "\n".join(texts), failed_status or bool(result.get("isError")), None
    if kind == "webSearch":
        return str(item.get("query") or ""), False, None
    if kind == "collabAgentToolCall":
        states = item.get("agentsStates") if isinstance(item.get("agentsStates"), dict) else {}
        lines = [
            f"{tid}: {(st or {}).get('status')}"
            + (f" — {st['message']}" if (st or {}).get("message") else "")
            for tid, st in states.items()
            if isinstance(st, dict)
        ]
        return "\n".join(lines), failed_status, None
    return "", failed_status, None
