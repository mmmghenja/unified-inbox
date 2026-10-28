"""Процесс `claude -p` рабочей сессии и протокол stream-json (Ф9-1, 9a, F3–F6; поправки П4, П8, П9, П12, П14,
«Мелочи»). Формат управляющих сообщений — по Agent SDK 0.3.220 и фактчеку ФЧ1
(`.runs/orch-factcheck/host.py`).

- `build_argv` — точный набор флагов F3 + П4 (`--permission-mode bypassPermissions`,
  `--allow-dangerously-skip-permissions`, `--permission-prompt-tool stdio`), без `--strict-mcp-config`,
  `--restricted`, `--setting-sources` (решение владельца «Без песочницы»).
- `build_env` — ЗАПРЕТИТЕЛЬНЫЙ список (П14): окружение хоста без `ANTHROPIC_*`, `CLAUDE*` и без `DISPLAY`,
  `WAYLAND_DISPLAY`, `XAUTHORITY` (окна на экране владельца), плюс `HUB_WORK=1`, `HUB_WORK_TASK=<id>`. Разрешающий
  `engine/claude_run.py::build_env` здесь НЕ годится — выкинул бы `SSH_AUTH_SOCK`.
- `Process` — один писатель stdin и один читатель stdout; строки stdout — с большим пределом (не 64 КБ
  `StreamReader` по умолчанию); процесс — в своей группе; «Стоп» гасит всё дерево потомков (П12: обход
  `/proc`, не только группа — dev-серверы `run_in_background` уходят в свои группы).
- `summarize_*` — сжатие событий для журнала задачи (F4, П8): вывод инструментов в БД не хранится."""

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

_logger = logging.getLogger("hub.work")

LINE_LIMIT = 64 * 1024 * 1024
KILL_GRACE_S = 10.0
SAY_NAMESPACE = uuid.UUID("5f0c7e0a-9a1a-4c6e-9b1e-68756277726b")  # uuid реплики = uuid5(ns, "say-<id>")
ENV_DENY_PREFIXES = ("ANTHROPIC", "CLAUDE")
ENV_DENY_KEYS = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY")
BASH_SUMMARY_MAX = 300
TEXT_MAX = 8000
ERROR_HEAD_MAX = 200
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")  # значения `claude --effort` (2.1.295)


def build_argv(
    *,
    claude: str,
    settings: Path,
    model: str,
    system_prompt: str,
    session_flag: str,
    session_id: str,
    partial: bool = False,
    effort: str | None = None,
) -> list[str]:
    """`effort` — `--effort <уровень>` (только из `CLAUDE_EFFORTS`; иное значение флаг не ставит).
    `partial` (Ф41-3, 3.2) — `--include-partial-messages`: только постоянным сессиям хаба (текст по мере
    набора); прочим запускам флаг не ставится."""
    if session_flag not in ("--session-id", "--resume"):
        raise ValueError(session_flag)
    return [
        claude,
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        *(["--include-partial-messages"] if partial else []),
        "--permission-prompt-tool",
        "stdio",
        "--permission-mode",
        "bypassPermissions",
        "--allow-dangerously-skip-permissions",
        "--settings",
        str(settings),
        "--model",
        str(model),
        *(["--effort", effort] if effort in CLAUDE_EFFORTS else []),
        "--append-system-prompt",
        system_prompt,
        session_flag,
        session_id,
    ]


def build_env(environ: dict, *, claude: str, task_id: int) -> dict:
    env = {k: v for k, v in environ.items() if not k.startswith(ENV_DENY_PREFIXES) and k not in ENV_DENY_KEYS}
    claude_dir = os.path.dirname(claude)
    parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    if claude_dir and claude_dir not in parts:
        parts.append(claude_dir)
    env["PATH"] = os.pathsep.join(parts) if parts else claude_dir
    env["HUB_WORK"] = "1"
    env["HUB_WORK_TASK"] = str(task_id)
    return env


def build_held_env(
    environ: dict,
    *,
    claude: str,
    hub_session_id: int,
    sub_profile: str | None = None,
    launch_key: str | None = None,
) -> dict:
    """Ф41-2б (4.1; 41-2а, 3.5): окружение процесса постоянной сессии хаба — как у задачи, но без
    `HUB_WORK`/`HUB_WORK_TASK` и с `HUB_SESSION=<id>` (личность сессии для MCP хаба). Ф41-4 П10:
    `HUB_SESSION_SUB_PROFILE` — профиль MCP, чей сервер оформляет подписку сессии при старте."""
    env = build_env(environ, claude=claude, task_id=0)
    env.pop("HUB_WORK", None)
    env.pop("HUB_WORK_TASK", None)
    env["HUB_SESSION"] = str(hub_session_id)
    env.pop("HUB_SESSION_SUB_PROFILE", None)
    if sub_profile:
        env["HUB_SESSION_SUB_PROFILE"] = sub_profile
    # Ф41-5 П1: ключ запуска — личность авто-сессии для ядра (ядро знает только его SHA-256).
    env.pop("HUB_SESSION_KEY", None)
    if launch_key:
        env["HUB_SESSION_KEY"] = launch_key
    return env


def with_path_extra(env: dict, extra) -> dict:
    """`PATH` с каталогами `path_extra` из `engine.json` впереди (node установки OpenCode, Ф16 П8)."""
    parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    for directory in reversed([p for p in extra or [] if isinstance(p, str) and os.path.isabs(p)]):
        if directory not in parts:
            parts.insert(0, directory)
    env["PATH"] = os.pathsep.join(parts)
    return env


def say_uuid(say_id: int) -> str:
    return str(uuid.uuid5(SAY_NAMESPACE, f"say-{say_id}"))


# Ф41-2б П10: у реплик постоянных сессий своё пространство uuid (id `held_say` пересекаются с id `work_say`).
HELD_SAY_NAMESPACE = uuid.UUID("7c1d2f4e-3b5a-4d8f-a6c1-68656c647361")


def held_say_uuid(say_id: int, resent_ts: int | None = None) -> str:
    """`resent_ts` (Ф48-2, A) — реплика прервана и отправлена снова: у повтора свой uuid (прежний уже есть в
    разговоре движка)."""
    name = f"held-say-{say_id}" if resent_ts is None else f"held-say-{say_id}-{resent_ts}"
    return str(uuid.uuid5(HELD_SAY_NAMESPACE, name))


def held_say_mid_uuid(say_id: int, resent_ts: int | None, attempt: int) -> str:
    """Ф55-1 (2.2): `uuid` строки с репликами, поданной посреди хода (`say_id` — первая реплика строки).
    У каждой подачи свой: прежнюю строку движок мог выбросить (`cancelled`), а реплика подаётся снова."""
    return str(uuid.uuid5(HELD_SAY_NAMESPACE, f"held-say-{say_id}-{resent_ts or 0}-mid-{attempt}"))


# Ф41-4 (3.1): сигнал хаба во вводе постоянной сессии — свой корень uuid, с репликами владельца не пересекается.
HELD_SIGNAL_NAMESPACE = uuid.UUID("5b0e7a52-9c1d-4f36-8e2a-7369676e616c")


def held_signal_uuid(hub_session_id: int, stamp_ms: int, n: int) -> str:
    return str(uuid.uuid5(HELD_SIGNAL_NAMESPACE, f"held-signal-{hub_session_id}-{stamp_ms}-{n}"))


def proc_start(pid: int | None) -> int | None:
    """`starttime` процесса из `/proc/<pid>/stat` (поле 22) — вторая половина личности процесса."""
    if not pid:
        return None
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
            fields = f.read().rsplit(")", 1)[-1].split()
        value = int(fields[19])
    except OSError, ValueError, IndexError:
        return None
    return value if value > 0 else None


def proc_ppid(pid: int | None) -> int | None:
    """Родитель процесса из `/proc/<pid>/stat` (поле 4); процесса нет — `None`."""
    if not pid:
        return None
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
            return int(f.read().rsplit(")", 1)[-1].split()[1])
    except OSError, ValueError, IndexError:
        return None


def proc_cmdline(pid: int | None) -> list[str]:
    """Аргументы процесса из `/proc/<pid>/cmdline`; процесса нет или не читается — пусто."""
    if not pid:
        return []
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def boot_id() -> str | None:
    """Личность загрузки машины: после перезагрузки pid и starttime могут совпасть с чужим процессом."""
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def user_line(
    text: str,
    *,
    message_uuid: str,
    priority: str | None = None,
    blocks: list[dict] | None = None,
    synthetic: bool = False,
) -> dict:
    """Строка `user` во ввод процесса. Ф41-3в: `blocks` — блоки файлов реплики (картинка, документ; у
    OpenCode — части `file`), идут после текста; пустой текст при блоках не подаётся (пустой текстовый блок
    модель не принимает). Ф41-4 (3.1): `synthetic` — сигнал хаба, а не реплика владельца: только OpenCode (его
    часть текста получает `synthetic: true`); процессу Claude Code ключ не подаётся."""
    content: list[dict] = [{"type": "text", "text": text}] if text or not blocks else []
    line = {
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": [*content, *(blocks or [])]},
        "parent_tool_use_id": None,
        "uuid": message_uuid,
    }
    if priority is not None:
        line["priority"] = priority
    if synthetic:
        line["synthetic"] = True
    return line


# --- сжатие событий (F4, П8) --------------------------------------------------------------------------------

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|AUTH)[A-Z0-9_]*)\s*[=:]\s*\S+"),
    re.compile(r"(?i)\b(bearer|token|password|passwd|secret|api[_-]?key)\b\s*[:=]?\s*\S+"),
    # Живая приёмка 9a: длинная «base64-строка» — только отдельным словом (не кусок пути или имени файла) и с
    # цифрой и буквой; иначе под `***` уходили пути worktree и имена тестов.
    re.compile(
        r"(?<![\w/.\-])(?=[A-Za-z0-9+/_-]*\d)(?=[A-Za-z0-9+/_-]*[A-Za-z])[A-Za-z0-9+/_-]{32,}={0,2}(?![\w/.\-])"
    ),
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
)


def mask_secrets(text: str) -> str:
    """П8: похожее на секреты — `KEY=…`, `token …`, `password …`, длинные base64/hex — под `***`."""
    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(lambda m: (m.group(1) + "=***") if m.lastindex else "***", out)
    return out


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def rel_path(path, workdir: str | None):
    """Ф41-3 (2.2, 2.6 п.4): путь внутри рабочей папки сессии — относительный от неё; иначе как есть."""
    if not workdir or not isinstance(path, str) or not os.path.isabs(path):
        return path
    try:
        rel = os.path.relpath(os.path.normpath(path), os.path.normpath(workdir))
    except ValueError:
        return path
    return path if rel == os.pardir or rel.startswith(os.pardir + os.sep) else rel


def summarize_tool_use(name: str, tool_input, workdir: str | None = None) -> str:
    """F4: имя + сжатый вход: Bash — команда ≤ 300 символов, Read/Edit/Write/Grep/Glob — путь, MCP — имя.
    `workdir` (Ф41-3, только постоянные сессии) — путь внутри рабочей папки пишется относительным; без него
    строки прежние."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if name == "Bash":
        return _one_line(mask_secrets(str(tool_input.get("command") or "")), BASH_SUMMARY_MAX)
    for key in ("file_path", "notebook_path", "path"):
        if isinstance(tool_input.get(key), str):
            extra = f" · {tool_input['pattern']}" if isinstance(tool_input.get("pattern"), str) else ""
            return _one_line(rel_path(tool_input[key], workdir) + extra, BASH_SUMMARY_MAX)
    if isinstance(tool_input.get("pattern"), str):
        return _one_line(tool_input["pattern"], BASH_SUMMARY_MAX)
    return name


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


def summarize_tool_result(block: dict) -> tuple[str, bool]:
    """П8: только `is_error` и длина; при ошибке — первые 200 символов с маскировкой секретов."""
    is_error = bool(block.get("is_error"))
    text = _result_text(block.get("content"))
    if is_error:
        return f"ошибка ({len(text)} симв.): " + _one_line(mask_secrets(text), ERROR_HEAD_MAX), True
    return f"готово ({len(text)} симв.)", False


_BLANK_RUN = re.compile(r"\n{4,}")  # три и больше пустых строки подряд


def summarize_text(text: str) -> str:
    r"""Ф10-1 C: текст модели с переносами (markdown в UI): `\r\n`/`\r` -> `\n`, хвостовые пробелы строк
    убраны, 3+ пустых строки подряд -> одна пустая, предел `TEXT_MAX` с «…». Маскировки нет (как было)."""
    norm = str(text).replace("\r\n", "\n").replace("\r", "\n")
    norm = "\n".join(line.rstrip() for line in norm.split("\n"))
    norm = _BLANK_RUN.sub("\n\n", norm).strip()
    return norm if len(norm) <= TEXT_MAX else norm[: TEXT_MAX - 1] + "…"


# --- подробности событий постоянной сессии (Ф41-3, 2.2) -----------------------------------------------------
#
# Только для постоянных сессий хаба (`BaseRunner.rich`): `brief` — строке ленты, `detail` — по клику. Строки
# проходят `mask_secrets`, пути внутри рабочей папки — относительные. Содержимое прочитанных файлов (`Read`) в
# события не пишется. Нет нужного поля в потоке движка — подробностей нет, событие уходит как раньше (П11).

EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
COMMAND_DETAIL_MAX = 20_000
INPUT_DETAIL_BYTES = 20_000
PATCH_MAX = 40_000
BASH_OUTPUT_MAX = 8000
OUTPUT_MAX = 4000
NEW_FILE_LINES_MAX = 400
DESCRIPTION_MAX = 1000
BRIEF_FILE_MAX = 300
# Пределы ядра (`held_work.BRIEF_MAX` / `DETAIL_MAX`) — в байтах после сериализации; резать обязан хост.
DETAIL_BYTES = 58_000


def _mask_deep(value, depth: int = 0):
    if isinstance(value, str):
        return mask_secrets(value)
    if depth >= 8:
        return value
    if isinstance(value, dict):
        return {str(k): _mask_deep(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_deep(v, depth + 1) for v in value]
    return value


def _json_bytes(value) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def fit_detail(detail: dict | None) -> dict | None:
    """Подробности — в пределе ядра по байтам: длинное текстовое поле (`patch`, `text`, `command`) режется с
    пометкой `cut`; не вышло — подробностей нет."""
    if detail is None:
        return None
    for _ in range(6):
        size = _json_bytes(detail)
        if size <= DETAIL_BYTES:
            return detail
        field = next((k for k in ("patch", "text", "command") if isinstance(detail.get(k), str)), None)
        if field is None:
            return None
        keep = int(len(detail[field]) * DETAIL_BYTES / size * 0.9)
        tail = field == "text" and detail.get("tail")
        detail = {**detail, field: detail[field][-keep:] if tail else detail[field][:keep], "cut": True}
    return None


def tool_input_detail(name: str, tool_input) -> dict | None:
    """`detail` события `tool`: Bash — команда целиком (до 20 000 символов) и описание, только если команда
    длиннее строки события или есть описание; правки и чтение файлов — нет; прочие — вход объектом (до 20 000
    байт, иначе `{"_cut": true}`)."""
    if not isinstance(tool_input, dict) or not tool_input or name in EDIT_TOOLS or name == "Read":
        return None
    if name == "Bash":
        command = mask_secrets(str(tool_input.get("command") or ""))
        description = tool_input.get("description")
        description = mask_secrets(description.strip()) if isinstance(description, str) else ""
        if not description and command.strip() == _one_line(command, BASH_SUMMARY_MAX):
            return None
        detail: dict = {"type": "input", "command": command[:COMMAND_DETAIL_MAX]}
        if description:
            detail["description"] = description[:DESCRIPTION_MAX]
        return fit_detail(detail)
    masked = _mask_deep(tool_input)
    if _json_bytes(masked) > INPUT_DETAIL_BYTES:
        masked = {"_cut": True}
    return {"type": "input", "input": masked}


def _patch_from_hunks(hunks) -> list[str] | None:
    """`structuredPatch` Claude Code -> строки обычного unified diff (`@@ -a,b +c,d @@` и строки)."""
    lines: list[str] = []
    for hunk in hunks:
        if not isinstance(hunk, dict) or not isinstance(hunk.get("lines"), list):
            return None
        try:
            head = [int(hunk[k]) for k in ("oldStart", "oldLines", "newStart", "newLines")]
        except KeyError, TypeError, ValueError:
            return None
        lines.append(f"@@ -{head[0]},{head[1]} +{head[2]},{head[3]} @@")
        lines.extend(str(line) for line in hunk["lines"])
    return lines or None


def _patch_from_text(patch: str) -> list[str] | None:
    """Unified diff строкой (OpenCode, `metadata.filediff.patch` / `metadata.diff`): шапка файла (`Index:`,
    `---`, `+++`) отбрасывается — в ленте остаются заголовки `@@` и строки."""
    lines = patch.replace("\r\n", "\n").split("\n")
    start = next((i for i, line in enumerate(lines) if line.startswith("@@")), None)
    if start is None:
        return None
    body: list[str] = []
    header = False  # шапка следующего файла в патче на несколько файлов: от `Index:` до ближайшего `@@`
    for line in lines[start:]:
        if line.startswith("@@"):
            header = False
        elif line.startswith("Index: "):
            header = True
        if not header:
            body.append(line)
    while body and not body[-1]:
        body.pop()
    return body or None


def _diff_details(name: str, tool_input, use_result, workdir: str | None) -> tuple[dict | None, dict | None]:
    if not isinstance(use_result, dict):
        return None, None
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    lines = None
    cut = False
    new_lines = None
    hunks = use_result.get("structuredPatch")
    content = use_result.get("content")
    if isinstance(use_result.get("patch"), str):
        lines = _patch_from_text(use_result["patch"])
    elif isinstance(hunks, list) and hunks:
        lines = _patch_from_hunks(hunks)
    elif name == "Write" and isinstance(content, str) and use_result.get("type") != "update":
        # Новый файл: `structuredPatch` нет (или пуст), есть содержимое — все строки «+», не больше 400.
        body = content.replace("\r\n", "\n").split("\n")
        if body and not body[-1]:
            body.pop()
        new_lines = len(body)
        cut = new_lines > NEW_FILE_LINES_MAX
        lines = [f"@@ -0,0 +1,{new_lines} @@", *("+" + line for line in body[:NEW_FILE_LINES_MAX])]
    if not lines:
        return None, None
    added = sum(1 for line in lines if line.startswith("+")) if new_lines is None else new_lines
    removed = sum(1 for line in lines if line.startswith("-"))
    path = next(
        (
            v
            for v in (
                use_result.get("filePath"),
                tool_input.get("file_path"),
                tool_input.get("notebook_path"),
                tool_input.get("path"),
            )
            if isinstance(v, str) and v
        ),
        "",
    )
    file = _one_line(rel_path(path, workdir), BRIEF_FILE_MAX)
    brief = {"type": "diff", "file": file, "added": added, "removed": removed}
    if secret_file(path):
        # Приёмка 41-3: тело diff файла с секретами в события не пишется — в окне только «+N −M».
        return brief, None
    kept: list[str] = []
    size = 0
    for line in lines:
        # Приёмка 41-3: тело diff НЕ маскируется — маска портила честный код (`token = load()`).
        if size + len(line) + 1 > PATCH_MAX:
            cut = True
            break
        kept.append(line)
        size += len(line) + 1
    detail = fit_detail({"type": "diff", "file": file, "patch": "\n".join(kept), "cut": cut})
    return brief, detail


# Файлы, где секреты лежат как есть: их diff в события сессии не пишется (остаётся строка «+N −M»).
# Только хранилища секретов по имени файла; файлы кода (`tokens.py`, `test_secrets.py`) сюда не попадают.
_SECRET_FILE_RE = re.compile(
    r"(?i)^(?:\.env(?:\..*)?|.*\.env|\.netrc|\.npmrc|\.pypirc|\.pgpass|id_(?:rsa|dsa|ecdsa|ed25519).*"
    r"|.*\.(?:pem|key|p12|pfx|kdbx|keystore|jks|token|session)"
    r"|(?:secrets?|credentials?|tokens?)(?:\.(?:json|ya?ml|toml|ini|txt))?)$"
)


def secret_file(path: str) -> bool:
    return bool(_SECRET_FILE_RE.match(os.path.basename(path or "")))


def _output_details(text: str, *, limit: int, tail: bool) -> tuple[dict | None, dict | None]:
    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    brief = {"type": "output", "lines": lines}
    if not text.strip():
        return brief, None
    # Маскировка — по куску с запасом, а не по всему выводу (он бывает в мегабайты).
    rough = text[-limit * 2 :] if tail else text[: limit * 2]
    masked = mask_secrets(rough)
    piece = masked[-limit:] if tail else masked[:limit]
    cut = len(text) > limit or len(masked) > limit
    detail = fit_detail({"type": "output", "text": piece, "cut": cut, **({"tail": True} if tail else {})})
    if detail is not None:
        detail.pop("tail", None)
    return brief, detail


def tool_result_details(
    name: str | None, tool_input, block: dict, use_result, workdir: str | None
) -> tuple[dict | None, dict | None]:
    """`brief` и `detail` события `tool_done` (таблица 2.2). `name` — инструмент вызова (по `tool_use_id`);
    вызов не найден — подробностей нет (это мог быть `Read`). `use_result` — поле `tool_use_result` строки
    потока (Claude Code: `structuredPatch`, содержимое нового файла; OpenCode — `patch` строкой)."""
    text = _result_text(block.get("content"))
    if block.get("is_error"):
        return _output_details(text, limit=OUTPUT_MAX, tail=False)
    if name is None or name == "Read":
        return None, None
    if name in EDIT_TOOLS:
        return _diff_details(name, tool_input, use_result, workdir)
    if name == "Bash":
        return _output_details(text, limit=BASH_OUTPUT_MAX, tail=True)
    return _output_details(text, limit=OUTPUT_MAX, tail=False)


def _number(value) -> float | None:
    return value if isinstance(value, int | float) and not isinstance(value, bool) else None


def _context_of(usage) -> int | None:
    """Размер контекста одного вызова модели: вход + чтение кэша + запись кэша."""
    if not isinstance(usage, dict):
        return None
    parts = [
        _number(usage.get(k))
        for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    ]
    if all(p is None for p in parts):
        return None
    return int(sum(p or 0 for p in parts))


def _context_limit(model_usage, model: str | None) -> int | None:
    """`modelUsage.<модель>.contextWindow`: модель хода (из `system/init`), иначе единственная в списке."""
    if not isinstance(model_usage, dict) or not model_usage:
        return None
    entry = model_usage.get(model) if model else None
    if not isinstance(entry, dict) and len(model_usage) == 1:
        entry = next(iter(model_usage.values()))
    window = _number(entry.get("contextWindow")) if isinstance(entry, dict) else None
    return int(window) if window is not None and window >= 0 else None


def turn_usage(
    msg: dict, *, prev_total: float | None, delta_usage: dict | None = None, model: str | None = None
) -> tuple[dict | None, dict | None, float | None]:
    """Расход хода по `result` потока (2.2, 2.3). Возвращает (`brief` события `turn_end`, тело `usage` для
    `turn` ядра, накопленная цена процесса после хода).

    Claude Code: `total_cost_usd` — накопительно за процесс, цена хода — разность с предыдущим `result` (у
    первого хода — само значение); контекст — последний вызов модели (`usage.iterations[-1]`, иначе `usage`
    последнего `message_delta`), предел — `modelUsage`. OpenCode (`engine: "opencode"`): `total_cost_usd` —
    уже цена хода, контекст и предел — `context_tokens` / `context_limit` (их кладёт `OcProcess`).
    Нет полей — `brief` без них или `None`, `usage` — `None` (П11). Ф74-5: Codex (`engine: "codex"`) кладёт
    те же поля, что `OcProcess` (у подписки цены нет — `total_cost_usd` не приходит)."""
    oc = msg.get("engine") in ("opencode", "codex")
    usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
    duration = _number(msg.get("duration_ms"))
    steps = _number(msg.get("num_turns"))
    total = _number(msg.get("total_cost_usd"))
    cost = None
    if total is not None and total >= 0:
        if oc:
            cost = float(total)
        else:
            cost = float(total)
            if prev_total is not None and total >= prev_total:
                cost -= prev_total
            prev_total = float(total)
        cost = round(cost, 6)
    if oc:
        names = {key: key for key in ("input", "output", "cache_read", "cache_write")}
    else:
        names = {
            "input": "input_tokens",
            "output": "output_tokens",
            "cache_read": "cache_read_input_tokens",
            "cache_write": "cache_creation_input_tokens",
        }
    tokens = {key: _number(usage.get(src)) for key, src in names.items()}
    tokens = None if all(v is None for v in tokens.values()) else {k: int(v or 0) for k, v in tokens.items()}
    brief = None
    if any(v is not None for v in (duration, steps, cost, tokens)):
        brief = {
            "type": "usage",
            "duration_ms": None if duration is None else int(duration),
            "steps": None if steps is None else int(steps),
            "cost_usd": cost,
            "tokens": tokens,
        }
    if oc:
        context = _number(msg.get("context_tokens"))
        context = int(context) if context is not None and context >= 0 else None
        limit = _number(msg.get("context_limit"))
        limit = int(limit) if limit is not None and limit >= 0 else None
    else:
        iterations = usage.get("iterations")
        last = iterations[-1] if isinstance(iterations, list) and iterations else None
        context = _context_of(last)
        if context is None:
            context = _context_of(delta_usage)
        limit = _context_limit(msg.get("modelUsage"), model)
    payload = None
    if context is not None:
        payload = {"context_tokens": context, "context_limit": limit, "turn_cost_usd": cost}
    return brief, payload, prev_total


# --- процесс ------------------------------------------------------------------------------------------------


def descendants(pid: int) -> list[int]:
    """Все потомки `pid` по `/proc/*/stat` (П12)."""
    children: dict[int, list[int]] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="utf-8", errors="replace") as f:
                stat = f.read()
        except OSError:
            continue
        # comm в скобках может содержать пробелы — поля после последней «)».
        fields = stat.rsplit(")", 1)[-1].split()
        if len(fields) < 2:
            continue
        with contextlib.suppress(ValueError):
            children.setdefault(int(fields[1]), []).append(int(entry))
    result: list[int] = []
    stack = [pid]
    while stack:
        current = stack.pop()
        for child in children.get(current, []):
            if child not in result:
                result.append(child)
                stack.append(child)
    return result


def _signal_all(pids: list[int], sig: int) -> None:
    for p in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(p, sig)


MessageHandler = Callable[[dict], Awaitable[None]]


class Process:
    """Один процесс `claude` задачи. `on_message` зовётся по одной строке stdout за раз (читатель один)."""

    def __init__(self, argv: list[str], *, cwd: str, env: dict, on_message: MessageHandler) -> None:
        self.argv = argv
        self.cwd = cwd
        self.env = env
        self.on_message = on_message
        self.proc: asyncio.subprocess.Process | None = None
        self._write_lock = asyncio.Lock()
        self._reader: asyncio.Task | None = None
        self._stderr: asyncio.Task | None = None
        self.stdin_closed = False
        self.skipped_lines = 0
        self.exited = asyncio.Event()
        self._request_n = 0

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc is not None else None

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv,
            cwd=self.cwd,
            env=self.env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=LINE_LIMIT,
        )
        self._reader = asyncio.create_task(self._read())
        self._stderr = asyncio.create_task(self._drain_stderr())

    async def _read(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        stdout = self.proc.stdout
        while True:
            try:
                line = await stdout.readuntil(b"\n")
            except asyncio.IncompleteReadError as exc:
                line = exc.partial
                if not line:
                    break
            except asyncio.LimitOverrunError as exc:
                # Строка длиннее предела — пропустить её целиком, не падать.
                await stdout.readexactly(exc.consumed)
                self.skipped_lines += 1
                continue
            except ConnectionError, OSError:
                break
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except ValueError:
                continue
            if isinstance(message, dict):
                try:
                    await self.on_message(message)
                except Exception:  # noqa: BLE001 — сбой разбора одной строки не должен ронять читателя
                    _logger.exception("work: строка потока не обработана")
        await self.proc.wait()
        self.exited.set()

    async def _drain_stderr(self) -> None:
        """stderr копит вывод MCP-серверов — читаем, чтобы труба не встала, и не храним (П7, П8)."""
        assert self.proc is not None and self.proc.stderr is not None
        with contextlib.suppress(Exception):
            while await self.proc.stderr.read(65536):
                pass

    async def send(self, message: dict) -> bool:
        """Строка в stdin; `False` — stdin уже закрыт или процесс умер."""
        if self.proc is None or self.stdin_closed or self.proc.stdin is None:
            return False
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            try:
                self.proc.stdin.write(data)
                await self.proc.stdin.drain()
            except ConnectionError, RuntimeError, OSError:
                return False
        return True

    async def control(self, subtype: str, **fields) -> str:
        self._request_n += 1
        request_id = f"hub-{self._request_n}"
        await self.send(
            {"type": "control_request", "request_id": request_id, "request": {"subtype": subtype, **fields}}
        )
        return request_id

    async def respond(self, request_id: str, response: dict) -> bool:
        return await self.send(
            {
                "type": "control_response",
                "response": {"subtype": "success", "request_id": request_id, "response": response},
            }
        )

    async def close_stdin(self) -> None:
        if self.proc is None or self.stdin_closed or self.proc.stdin is None:
            self.stdin_closed = True
            return
        self.stdin_closed = True
        async with self._write_lock:
            with contextlib.suppress(Exception):
                self.proc.stdin.close()
                await self.proc.stdin.wait_closed()

    async def wait(self, limit_s: float | None = None) -> bool:
        """Дождаться выхода (и дочитать stdout). `True` — вышел."""
        try:
            await asyncio.wait_for(self.exited.wait(), limit_s)
        except TimeoutError:
            return False
        return True

    def tree(self) -> list[int]:
        """Процесс и все его потомки сейчас (снимок до закрытия stdin: после выхода `claude` потомки уходят к
        другому родителю и по `/proc` уже не находятся)."""
        if self.proc is None or self.proc.returncode is not None:
            return []
        return [self.proc.pid, *descendants(self.proc.pid)]

    async def kill_tree(self, snapshot: list[int] | None = None, grace_s: float = KILL_GRACE_S) -> None:
        """П12: SIGTERM всему дереву (процесс, его группа, потомки по /proc и снимок `snapshot`), через
        `grace_s` — SIGKILL оставшимся. Docker-контейнеры стенда сюда не входят (их процессы — не потомки)."""
        if self.proc is None:
            return
        pid = self.proc.pid
        pids = list(dict.fromkeys([*(snapshot or []), *self.tree()]))
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGTERM)
        _signal_all(pids, signal.SIGTERM)
        if not await self.wait(grace_s):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGKILL)
            _signal_all([*pids, *self.tree()], signal.SIGKILL)
            await self.wait(5)
        # Остатки: потомки, пережившие SIGTERM (или отцепившиеся до выхода родителя), — SIGKILL.
        _signal_all([p for p in pids if p != pid], signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)

    async def aclose(self) -> None:
        for task in (self._reader, self._stderr):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
