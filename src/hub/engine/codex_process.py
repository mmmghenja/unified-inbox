"""Процесс Codex с поверхностью `hub.work.session.Process` (Ф74-5, docs/architecture.md).

Один `codex app-server` (JSON-RPC по stdio, строка — сообщение) на сессию хаба и одна нить (`thread`) в нём.
Хост написан под `claude -p` stream-json, поэтому выдача в `on_message` — в формате Claude:

- `system/init` в начале каждого хода (`permissionMode: bypassPermissions`, `session_id` — id нити);
- `stream_event` с ключом блока `block` (текст и размышления по мере набора), `assistant` (законченный
  текст элемента `agentMessage` с тем же `block`; `tool_use` в терминах Claude — `codex_tools.stream_item`),
  `user` (`tool_result`; у правки — `tool_use_result {patch, filePath}`);
- `result` в конце хода (`turn/completed`): `engine: "codex"`, `usage` в именах «вход / выход / чтение кэша /
  запись кэша», `context_tokens` / `context_limit` из последнего `thread/tokenUsage/updated` (как у OpenCode —
  `session.turn_usage`), после прерывания — `terminal_reason: "aborted"`;
- сжатие хабом (`compact()`, `thread/compact/start`) — своим ходом: `system/status compacting`,
  `system/compact_boundary` (элемент `contextCompaction`), `result` с `num_turns: 0`, как `/compact` Claude;
- `rate_limit_event` (`engine: "codex"`): исчерпан лимит подписки (`account/rateLimits/updated` с
  `rateLimitReachedType` или ошибка хода `usageLimitExceeded` / `rateLimitExceeded`);
- `command_lifecycle` (`queued` / `started` / `cancelled`) — судьба строк ввода, как у Claude Code: реплика в
  занятую сессию идёт `turn/steer` (`expectedTurnId`) и начата сразу; `steer` не принят (ход кончился,
  сжатие) — строка ждёт и начинает следующий ход вместе с прочими (Ф50-1, Ф55-1);
- `control_request` `can_use_tool` — карточка владельцу: «спросить» гарда (хук `PreToolUse` ждёт решения),
  вопрос модели (`item/tool/requestUserInput` -> `AskUserQuestion`), запрос MCP (`mcpServer/elicitation/
  request`) и одобрения Codex (`item/*/requestApproval` — при `approvalPolicy: never` их быть не должно;
  пришли — карточка, не «да»).

Гард — хук `PreToolUse` (`hub-codex-hook`, флаги запуска `-c hooks.…` и доверие `-c hooks.state=…` по хэшу из
`hooks/list` прохода «узнать»). Хук шлёт вход по абстрактному unix-сокету; собеседник проверяется по
`SO_PEERCRED`: свой uid и потомок процесса app-server. Решает `CodexSpec.decide(req, payload)` (хост: гард и
правила, как у Cursor). Хук Codex при сбое инструмент НЕ блокирует (fail-open), поэтому:
- запуск без принятого (`trusted`, `enabled`) хука `PreToolUse` — отказ (`CodexStartError`);
- элемент `commandExecution` / `fileChange` / `mcpToolCall`, которого хук не разрешил, или ввод с печатным
  текстом в запущенную команду (`write_stdin` хук не проходит, проба 5) — обход гарда: ход прерывается,
  в ленту — ошибка."""

import asyncio
import contextlib
import inspect
import json
import logging
import os
import re
import secrets
import shlex
import signal
import socket
import struct
import time
import types
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from hub.work.session import descendants

from . import codex_tools
from .oc_turn import Allow, Ask, Deny

log = logging.getLogger(__name__)

ENGINE = "codex"
BYPASS = "bypassPermissions"
SOCK_ENV = "HUB_CODEX_SOCK"
PID_ENV = "HUB_CODEX_PID"
WAIT_ENV = "HUB_CODEX_HOOK_WAIT_S"
HOOK_TIMEOUT_S = 86400
HOOK_MARGIN_S = 300  # клиент хука сдаётся раньше таймаута Codex: по таймауту Codex инструмент пропустил бы
ASK_MARGIN_S = 60  # карточка снимается раньше, чем сдастся клиент хука
HOOK_INPUT_MAX = 64 * 1024 * 1024
STREAM_LIMIT = 64 * 1024 * 1024
STDERR_TAIL = 4000
RPC_TIMEOUT_S = 60.0
LEARN_TIMEOUT_S = 60.0
MCP_WAIT_S = 30.0
NO_AUTO_COMPACT = 1_000_000_000  # порог автосжатия Codex выше любого окна: сжимает хаб
PROJECT_DOC_MAX = 1024 * 1024
CANCELLED = "запрос снят: ход прерван"
DENIED_BY_OWNER = "владелец отказал"
PEER_DENIED = "гард хаба: хук не из этой сессии"
HOOK_FAILED = "гард хаба: хук Codex упал — запрет"
ASK_EXPIRED = "гард хаба: карточка не решена за сутки — запрет"
BYPASS_TEXT = "гард хаба: {what} мимо хука — ход прерван"
THREAD_GONE = "разговора сессии у Codex больше нет (нить не найдена) — заведи новую"
QUESTION_TOOL = "AskUserQuestion"
LIMIT_ERRORS = ("usageLimitExceeded", "rateLimitExceeded")
KILL_GRACE_S = 2.0  # команды прерванного хода: SIGTERM, через столько — SIGKILL оставшимся
MODEL_PAGES = 10  # страниц `model/list` не больше (у Codex 0.163.0 — одна)
_UCRED = struct.Struct("3i")

DecideFn = Callable[..., "Allow | Deny | Ask | Awaitable[Allow | Deny | Ask]"]
MessageHandler = Callable[[dict], Awaitable[None]]

# Хэши доверия хуков (`hooks/list` прохода «узнать») по бинарю и строкам команд — на время жизни хоста.
_TRUST_CACHE: dict[tuple, tuple[list[str], int]] = {}
# Модели движка (`model/list`) по бинарю — на время жизни хоста.
_MODELS_CACHE: dict[str, list[dict]] = {}


class CodexStartError(OSError):
    """Процесс Codex не годится для сессии (хук не принят, нить пропала, модель или усилие не те)."""


class RpcError(Exception):
    def __init__(self, method: str, error: dict) -> None:
        self.method = method
        self.error = error if isinstance(error, dict) else {"message": str(error)}
        super().__init__(f"{method}: {self.error.get('message')}")

    @property
    def info(self) -> str:
        data = self.error.get("data")
        info = data.get("codexErrorInfo") if isinstance(data, dict) else None
        return str(info) if info is not None else ""


@dataclass
class HookRequest:
    """Вызов из хука: событие, имя инструмента Codex, его вход, нить, id вызова."""

    event: str
    tool: str
    args: object
    session_id: str
    call_id: str
    child: bool = False


@dataclass
class CodexSpec:
    binary: str
    cwd: str
    env: dict
    model: str | None
    effort: str | None
    system: str | None
    decide: DecideFn
    hook_command: list[str]
    resume: str | None = None
    mcp_servers: dict = field(default_factory=dict)  # `config.mcp_servers` нити
    disable_mcp: list[str] = field(default_factory=list)  # серверы конфигов Codex владельца: выключить флагом
    stop_grace_s: float = 10.0
    mcp_wait_s: float = MCP_WAIT_S
    extra_args: list[str] = field(default_factory=list)


async def _maybe(value):
    return await value if inspect.isawaitable(value) else value


def _is_descendant(pid: int, root: int) -> bool:
    seen = 0
    while pid > 1 and seen < 64:
        if pid == root:
            return True
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
                fields = f.read().rsplit(")", 1)[-1].split()
            pid = int(fields[1])
        except OSError, ValueError, IndexError:
            return False
        seen += 1
    return False


# --- флаги запуска -----------------------------------------------------------------------------------------


def toml_value(value) -> str:
    """Значение для `-c ключ=значение` (TOML): строки — JSON-строкой (те же экранирования), списки и словари
    — встроенными массивами и таблицами."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return json.dumps(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list | tuple):
        return "[" + ",".join(toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(f"{json.dumps(str(k))}={toml_value(v)}" for k, v in value.items()) + "}"
    raise TypeError(type(value).__name__)


def hook_command_line(hook_command: list[str], event: str) -> str:
    """Строка команды хука (её исполняет оболочка): падение интерпретатора (не 0 и не 2) — тоже запрет."""
    head = " ".join(shlex.quote(p) for p in hook_command)
    return f"{head} {event} || {{ echo {shlex.quote(HOOK_FAILED)} >&2; exit 2; }}"


def hook_flags(hook_command: list[str]) -> list[str]:
    """`-c hooks.<Событие>=[…]` на каждое событие хука (источник `sessionFlags`, глобальные файлы
    не пишутся)."""
    flags: list[str] = []
    for event in codex_tools.HOOK_EVENTS:
        handler = {
            "type": "command",
            "command": hook_command_line(hook_command, event),
            "timeout": HOOK_TIMEOUT_S,
        }
        flags += ["-c", f"hooks.{event}=[{{hooks=[{toml_value(handler)}]}}]"]
    return flags


def trust_flag(entries: list[dict]) -> list[str]:
    """`-c hooks.state={"<ключ>"={trusted_hash="<хэш>"}}` для хуков из флагов запуска."""
    parts = [
        f"{json.dumps(e['key'])}={{trusted_hash={json.dumps(e['currentHash'])}}}"
        for e in entries
        if isinstance(e.get("key"), str) and isinstance(e.get("currentHash"), str)
    ]
    return ["-c", "hooks.state={" + ",".join(parts) + "}"] if parts else []


def own_hooks(listing: dict) -> list[dict]:
    """Хуки из флагов запуска в ответе `hooks/list`."""
    found: list[dict] = []
    for entry in (listing or {}).get("data") or []:
        for hook in (entry or {}).get("hooks") or []:
            if isinstance(hook, dict) and hook.get("source") == "sessionFlags":
                found.append(hook)
    return found


_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def mcp_disable_flags(names) -> list[str]:
    """`-c mcp_servers.<имя>.enabled=false` на каждое имя. Ключ `-c` Codex делит по точкам буквально, без
    кавычек TOML (проба 21.10, `.runs/f745b-probe/probe_mcpflag.py`): `mcp_servers."ru-edit"` — другой
    сервер с кавычками в имени, без транспорта, и app-server падает на старте («invalid transport»). Имя не
    из `[A-Za-z0-9_-]` флагом не выразить — пропуск с записью в журнал (вызовы его инструментов всё равно идут
    через хук `PreToolUse`)."""
    flags: list[str] = []
    for name in names:
        name = str(name)
        if not _BARE_KEY_RE.match(name):
            log.warning("Codex: MCP-сервер %r из конфига Codex флагом не выключить — остаётся", name[:80])
            continue
        flags += ["-c", f"mcp_servers.{name}.enabled=false"]
    return flags


# --- JSON-RPC ----------------------------------------------------------------------------------------------


class _Rpc:
    """Клиент app-server: запросы, уведомления, запросы сервера. Строки — по одной на сообщение."""

    def __init__(self, proc: asyncio.subprocess.Process, on_note, on_request) -> None:
        self.proc = proc
        self.on_note = on_note
        self.on_request = on_request
        self.n = 0
        self.waits: dict[int, tuple[str, asyncio.Future]] = {}
        self.closed = asyncio.Event()
        self._reader = asyncio.create_task(self._read())

    def _write(self, message: dict) -> bool:
        stdin = self.proc.stdin
        if stdin is None or stdin.is_closing():
            return False
        try:
            stdin.write(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n")
        except BrokenPipeError, ConnectionResetError, RuntimeError:
            return False
        return True

    async def call(self, method: str, params: dict | None = None, timeout_s: float = RPC_TIMEOUT_S):
        self.n += 1
        number = self.n
        future = asyncio.get_running_loop().create_future()
        self.waits[number] = (method, future)
        if not self._write({"jsonrpc": "2.0", "id": number, "method": method, "params": params or {}}):
            self.waits.pop(number, None)
            raise RpcError(method, {"message": "ввод процесса закрыт"})
        try:
            return await asyncio.wait_for(future, timeout_s)
        finally:
            self.waits.pop(number, None)

    def notify(self, method: str, params: dict | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def answer(self, request_id, result: dict | None = None, error: dict | None = None) -> None:
        message: dict = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            message["error"] = error
        else:
            message["result"] = result or {}
        self._write(message)

    async def _read(self) -> None:
        stdout = self.proc.stdout
        assert stdout is not None
        try:
            while True:
                try:
                    line = await stdout.readline()
                except ValueError, asyncio.LimitOverrunError:
                    log.warning("Codex: строка потока длиннее предела — пропущена")
                    continue
                if not line:
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                try:
                    if "method" in message and "id" in message:
                        await self.on_request(message)
                    elif "id" in message:
                        entry = self.waits.get(message.get("id"))
                        if entry is not None and not entry[1].done():
                            method, future = entry
                            if "error" in message:
                                future.set_exception(RpcError(method, message.get("error") or {}))
                            else:
                                future.set_result(message.get("result"))
                    elif "method" in message:
                        await self.on_note(message)
                except Exception:  # noqa: BLE001 — сбой разбора одного сообщения не роняет читателя
                    log.exception("Codex: сообщение не обработано")
        finally:
            for method, future in list(self.waits.values()):
                if not future.done():
                    future.set_exception(RpcError(method, {"message": "процесс Codex вышел"}))
            self.closed.set()

    async def aclose(self) -> None:
        if not self._reader.done():
            self._reader.cancel()
            with contextlib.suppress(BaseException):
                await self._reader


async def _spawn(binary: str, args: list[str], *, cwd: str, env: dict) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        binary,
        "app-server",
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        start_new_session=True,
        limit=STREAM_LIMIT,
    )


async def _initialize(rpc: _Rpc) -> dict:
    from hub import __version__

    result = await rpc.call(
        "initialize", {"clientInfo": {"name": "hub", "title": "hub", "version": __version__}}, LEARN_TIMEOUT_S
    )
    rpc.notify("initialized")
    return result if isinstance(result, dict) else {}


async def _stop(proc: asyncio.subprocess.Process, grace_s: float = 5.0) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(Exception):
        if proc.stdin is not None:
            proc.stdin.close()
    try:
        await asyncio.wait_for(proc.wait(), grace_s)
        return
    except TimeoutError:
        pass
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), 3)


async def learn_trust(binary: str, flags: list[str], *, cwd: str, env: dict) -> tuple[list[str], int]:
    """Проход «узнать хэш» (проба 21.10, `hookargs.py`): app-server с теми же флагами хуков, `hooks/list` —
    ключи и хэши; итог — флаг доверия и действующий таймаут хука `PreToolUse` (с). Кэш — по бинарю
    и флагам."""
    key = (binary, tuple(flags))
    if key in _TRUST_CACHE:
        return _TRUST_CACHE[key]
    proc = await _spawn(binary, flags, cwd=cwd, env=env)

    async def _drop(_message):
        return None

    async def _refuse(message):
        rpc.answer(message.get("id"), error={"code": -32601, "message": "не поддерживается"})

    rpc = _Rpc(proc, _drop, _refuse)
    try:
        await _initialize(rpc)
        listing = await rpc.call("hooks/list", {"cwds": [cwd]}, LEARN_TIMEOUT_S)
    except (RpcError, TimeoutError) as exc:
        raise CodexStartError(f"Codex: хуки не прочитаны ({exc})") from None
    finally:
        await rpc.aclose()
        await _stop(proc)
    hooks = own_hooks(listing)
    pre = [h for h in hooks if h.get("eventName") == "preToolUse"]
    if not pre:
        raise CodexStartError(
            "гард хаба: Codex не принял хук PreToolUse из флагов запуска — запуск без гарда нельзя"
        )
    timeout = pre[0].get("timeoutSec")
    timeout = int(timeout) if isinstance(timeout, int) and not isinstance(timeout, bool) else 600
    result = (trust_flag(hooks), timeout)
    _TRUST_CACHE[key] = result
    return result


async def list_models(binary: str, *, cwd: str, env: dict) -> list[dict]:
    """Ф74-5б: `model/list` Codex отдельным коротким app-server (без хуков и нити — ходов нет): видимые
    модели, все страницы (`nextCursor`). Сбой — `CodexStartError`."""
    proc = await _spawn(binary, [], cwd=cwd, env=env)

    async def _drop(_message):
        return None

    async def _refuse(message):
        rpc.answer(message.get("id"), error={"code": -32601, "message": "не поддерживается"})

    rpc = _Rpc(proc, _drop, _refuse)
    models: list[dict] = []
    try:
        await _initialize(rpc)
        cursor = None
        for _page in range(MODEL_PAGES):
            params: dict = {"includeHidden": False}
            if cursor:
                params["cursor"] = cursor
            listed = await rpc.call("model/list", params, LEARN_TIMEOUT_S)
            listed = listed if isinstance(listed, dict) else {}
            models += [m for m in listed.get("data") or [] if isinstance(m, dict)]
            cursor = listed.get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                break
    except (RpcError, TimeoutError) as exc:
        raise CodexStartError(f"Codex: модели не прочитаны ({exc})") from None
    finally:
        await rpc.aclose()
        await _stop(proc)
    return models


def ui_models(models: list[dict]) -> list[dict]:
    """`model/list` -> контракт поля `models` профиля в `/status` (`docs/architecture.md`):
    `[{id, efforts, default_effort}]`, скрытые — нет, умолчание движка — первым."""
    out: list[dict] = []
    for m in models:
        if not isinstance(m, dict) or m.get("hidden"):
            continue
        name = m.get("model") or m.get("id")
        if not isinstance(name, str) or not name:
            continue
        efforts = [
            str(e.get("reasoningEffort"))
            for e in m.get("supportedReasoningEfforts") or []
            if isinstance(e, dict) and isinstance(e.get("reasoningEffort"), str)
        ]
        default = m.get("defaultReasoningEffort")
        entry = {
            "id": name,
            "efforts": efforts,
            "default_effort": default if isinstance(default, str) and default in efforts else None,
        }
        if m.get("isDefault"):
            out.insert(0, entry)
        else:
            out.append(entry)
    return out


def check_model(models: list[dict], model: str | None, effort: str | None) -> tuple[str, str | None]:
    """Модель и усилие по `model/list` движка: нет модели — умолчание движка; модели нет в списке или усилия
    нет у модели — `CodexStartError` с понятным текстом (не молчаливая замена)."""
    visible = [m for m in models if isinstance(m, dict) and isinstance(m.get("model") or m.get("id"), str)]
    names = [str(m.get("model") or m.get("id")) for m in visible]
    if model:
        found = next((m for m in visible if model in (m.get("model"), m.get("id"))), None)
        if found is None:
            raise CodexStartError(f"Codex: модели «{model}» нет у движка (есть: {', '.join(names) or '—'})")
    else:
        found = next((m for m in visible if m.get("isDefault")), None) or (visible[0] if visible else None)
        if found is None:
            raise CodexStartError("Codex: движок не назвал ни одной модели")
    name = str(found.get("model") or found.get("id"))
    levels = [
        str(e.get("reasoningEffort"))
        for e in found.get("supportedReasoningEfforts") or []
        if isinstance(e, dict) and e.get("reasoningEffort")
    ]
    if effort:
        if levels and effort not in levels:
            raise CodexStartError(f"Codex: у модели {name} нет усилия «{effort}» (есть: {', '.join(levels)})")
        return name, effort
    default = found.get("defaultReasoningEffort")
    return name, str(default) if isinstance(default, str) and default else None


class CodexProcess:
    """Процесс сессии Codex (см. докстринг модуля)."""

    def __init__(self, spec: CodexSpec, *, on_message: MessageHandler) -> None:
        self.spec = spec
        self.on_message = on_message
        self.session_id: str | None = spec.resume
        self.resumed = bool(spec.resume)
        self.model: str | None = spec.model
        self.effort: str | None = spec.effort
        self.exited = asyncio.Event()
        self._cwd_real = os.path.realpath(spec.cwd)
        self.stdin_closed = False
        self._queue: asyncio.Queue = asyncio.Queue()
        self._dispatcher: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._rpc: _Rpc | None = None
        self._rc: int | None = None
        self._server: asyncio.AbstractServer | None = None
        self.sock_name: str | None = None
        self._stderr = bytearray()
        self._err_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()
        self._inputs: list[tuple[str | None, list[dict]]] = []  # (uuid строки ввода, вход Codex)
        # Строки, поданные `turn/start`, чей `started` ещё не сообщён: сообщается с началом хода — уведомления
        # хода (вплоть до `turn/completed`) могут прийти раньше ответа на сам запрос.
        self._starting: list[tuple[str | None, list[dict]]] = []
        self._sent: set[str] = set()
        self._asks: dict[str, tuple[asyncio.Future, dict]] = {}
        self._ask_reqs: dict[str, HookRequest] = {}  # Ф99-7.1: вызов ждущей карточки — для повторного решения
        self._server_asks: dict[str, tuple[dict, dict]] = {}  # id карточки -> (запрос сервера, нагрузка)
        self._request_n = 0
        self._stopping = False
        self._shut = False
        self._hook_wait_s = float(HOOK_TIMEOUT_S - HOOK_MARGIN_S)
        self._ask_limit_s = self._hook_wait_s - ASK_MARGIN_S
        # MCP (`mcpServer/startupStatus/updated`)
        self._mcp: dict[str, str] = {name: "starting" for name in spec.mcp_servers}
        self._mcp_errors: dict[str, str] = {}
        self._mcp_ready = asyncio.Event()
        self._mcp_since = 0.0
        self._check_mcp()
        # учёт
        self._usage_last: dict | None = None
        self._usage_total: dict | None = None
        self._window: int | None = None
        self._limit_sent: str | None = None
        self._limit_reset: float | None = None  # срок сброса из последнего отправленного отказа (с, эпоха)
        # ход
        self._turn_id: str | None = None
        self._turn_starting = False
        self._turn_kind: str | None = None  # "turn" | "compact"
        self._turn_no = 0
        self._cmd_token = secrets.token_hex(6)  # Ф74-5б: метка команд хода — `<token>.<номер хода>`
        self._turn_t0 = 0.0
        self._turn_total0: dict | None = None
        self._aborted = False
        self._done_turns: set[str] = set()
        self._compacting = False
        self._compact_boundary = False
        self._allowed: set[str] = set()  # id вызовов, разрешённых хуком
        self._patches: dict[str, str] = {}  # id вызова -> текст патча из хука
        self._outputs: dict[str, list[str]] = {}
        self._calls: dict[str, str] = {}  # id элемента -> имя (Claude)
        self._blocks: dict[str, str] = {}  # id элемента текста/размышления -> ключ блока
        self._texts: list[str] = []
        self._denials: list[dict] = []
        self._calls_n = 0
        self._error: str | None = None
        self._error_info: str | None = None
        self._bypass_sent = False
        self._subagents: set[str] = set()

    # --- поверхность `session.Process` ----------------------------------------------------------------------

    @property
    def proc(self):
        if self._proc is not None:
            return self._proc
        return types.SimpleNamespace(returncode=self._rc)

    @property
    def pid(self) -> int | None:
        proc = self._proc
        return proc.pid if proc is not None and proc.returncode is None else None

    @property
    def busy(self) -> bool:
        return self._turn_id is not None or self._turn_starting

    async def start(self) -> None:
        """Сервер хуков, проход доверия хуков, app-server, `initialize`, проверка хука, модель, нить.
        Не вышло — `OSError` (`CodexStartError` — с текстом для владельца)."""
        name = f"hub-codex-{os.getpid()}-{os.urandom(8).hex()}"
        self._server = await asyncio.start_unix_server(self._on_hook, path="\0" + name, limit=HOOK_INPUT_MAX)
        self.sock_name = name
        self._dispatcher = asyncio.create_task(self._dispatch())
        try:
            await self._start_app()
        except BaseException:
            await self._abort_start()
            raise

    async def _abort_start(self) -> None:
        proc = self._proc
        if self._rpc is not None:
            await self._rpc.aclose()
        if proc is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 3)
        if self._server is not None:
            self._server.close()
            self._server = None
        if self._dispatcher is not None:
            self._dispatcher.cancel()
            with contextlib.suppress(BaseException):
                await self._dispatcher

    async def _start_app(self) -> None:
        spec = self.spec
        flags = hook_flags(spec.hook_command)
        learn_env = {k: v for k, v in spec.env.items() if k not in (SOCK_ENV, PID_ENV)}
        trust, hook_timeout = await learn_trust(spec.binary, flags, cwd=spec.cwd, env=learn_env)
        self._hook_wait_s = float(max(hook_timeout - HOOK_MARGIN_S, hook_timeout * 0.9))
        self._ask_limit_s = max(self._hook_wait_s - ASK_MARGIN_S, self._hook_wait_s * 0.9)
        env = {
            **spec.env,
            SOCK_ENV: self.sock_name or "",
            PID_ENV: str(os.getpid()),
            WAIT_ENV: str(int(self._hook_wait_s)),
        }
        args = [*flags, *trust, *mcp_disable_flags(spec.disable_mcp), *spec.extra_args]
        self._proc = await _spawn(spec.binary, args, cwd=spec.cwd, env=env)
        self._err_task = asyncio.create_task(self._drain_err())
        self._rpc = _Rpc(self._proc, self._on_note, self._on_server_request)
        self._spawn(self._watch_rpc())
        try:
            await _initialize(self._rpc)
            listing = await self._rpc.call("hooks/list", {"cwds": [spec.cwd]})
            pre = [h for h in own_hooks(listing) if h.get("eventName") == "preToolUse"]
            if not pre or not all(h.get("enabled") and h.get("trustStatus") == "trusted" for h in pre):
                raise CodexStartError(
                    "гард хаба: Codex не доверяет хуку PreToolUse сессии — запуск без гарда нельзя"
                )
            models = _MODELS_CACHE.get(spec.binary)
            if models is None:
                listed = await self._rpc.call("model/list", {"includeHidden": True})
                models = [m for m in (listed or {}).get("data") or [] if isinstance(m, dict)]
                if models:
                    _MODELS_CACHE[spec.binary] = models
            self.model, self.effort = check_model(models, spec.model, spec.effort)
            config = self._thread_config()
            params = {
                "cwd": spec.cwd,
                "approvalPolicy": "never",
                "approvalsReviewer": "user",
                "sandbox": "danger-full-access",
                "model": self.model,
                "config": config,
            }
            if spec.resume:
                try:
                    await self._rpc.call("thread/read", {"threadId": spec.resume, "includeTurns": False})
                except RpcError as exc:
                    log.warning("Codex: нить %s не прочитана (%s)", spec.resume, exc)
                    raise CodexStartError(THREAD_GONE) from None
                result = await self._rpc.call(
                    "thread/resume", {"threadId": spec.resume, **params, "excludeTurns": True}
                )
            else:
                if spec.system:
                    params["developerInstructions"] = spec.system
                result = await self._rpc.call("thread/start", params)
        except RpcError as exc:
            raise CodexStartError(f"Codex: {exc}") from None
        except TimeoutError:
            raise CodexStartError("Codex: app-server не ответил") from None
        thread = (result or {}).get("thread") if isinstance(result, dict) else None
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str) or not thread_id:
            raise CodexStartError("Codex: нить не создана")
        self.session_id = thread_id
        self._mcp_since = time.monotonic()
        self._check_mcp()

    def _thread_config(self) -> dict:
        """`config` нити: MCP сессии, без встроенных приложений, без автосжатия Codex, подагенты на модели
        сессии,
        `CLAUDE.md` проекта — там, где нет `AGENTS.md` (как его читает Claude Code)."""
        config: dict = {
            # Вопрос с вариантами (`request_user_input`) у Codex 0.163.0 — только в режиме плана; в обычном
            # его включает этот флаг (карточка вопроса, как `AskUserQuestion` у Claude Code).
            "features": {"apps": False, "default_mode_request_user_input": True},
            "model_auto_compact_token_limit": NO_AUTO_COMPACT,
            "project_doc_fallback_filenames": ["CLAUDE.md"],
            "project_doc_max_bytes": PROJECT_DOC_MAX,
            "agents": {"default_subagent_model": self.model},
        }
        if self.effort:
            config["agents"]["default_subagent_reasoning_effort"] = self.effort
        servers = dict(self.spec.mcp_servers)
        # Ф74-8 (стенд, `.runs/f748-stand/probe_codex_thread_mcp.py`): с `mcp_servers` в `config` нити Codex
        # 0.163 флаги `-c mcp_servers.<имя>.enabled=false` app-server теряет — нить поднимала ВСЕ серверы
        # config.toml владельца. Выключение — здесь же, записью `enabled: false` (Codex сливает её с записью
        # config.toml); флаги остаются для запуска без нити.
        for name in self.spec.disable_mcp:
            servers.setdefault(str(name), {"enabled": False})
        if servers:
            config["mcp_servers"] = servers
        return config

    async def send(self, message: dict) -> bool:
        kind = message.get("type")
        if kind == "control_response":
            response = message.get("response") or {}
            if response.get("subtype") == "success":
                return await self.respond(str(response.get("request_id")), response.get("response") or {})
            return await self.respond(
                str(response.get("request_id")),
                {"behavior": "deny", "message": str(response.get("error") or "")},
            )
        if kind != "user" or self.stdin_closed or self._stopping or self.exited.is_set():
            return False
        uid = message.get("uuid")
        if isinstance(uid, str) and uid in self._sent:
            return True
        items = self._input_of((message.get("message") or {}).get("content"))
        if not items:
            return False
        uid = uid if isinstance(uid, str) and uid else None
        if uid is not None:
            self._sent.add(uid)
        self._inputs.append((uid, items))
        self._spawn(self._pump())
        return True

    @staticmethod
    def _input_of(content) -> list[dict]:
        """Содержимое строки `user` -> вход Codex (`UserInput`): текст, картинки (data:-URL)."""
        if isinstance(content, str):
            return [{"type": "text", "text": content}] if content else []
        items: list[dict] = []
        for block in content or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                items.append({"type": "text", "text": block["text"]})
            elif block.get("type") == "image" and isinstance(block.get("url"), str):
                items.append({"type": "image", "url": block["url"]})
            elif block.get("type") == "localImage" and isinstance(block.get("path"), str):
                items.append({"type": "localImage", "path": block["path"]})
        return items

    async def control(self, subtype: str, **fields) -> str:
        self._request_n += 1
        request_id = f"hub-{self._request_n}"
        if subtype == "initialize":
            self._response(request_id, {"current_permission_mode": BYPASS})
        elif subtype == "interrupt":
            if fields.get("cancel_queued"):
                dropped, self._inputs = self._inputs, []
                self._lifecycle(dropped, "cancelled")
            await self._interrupt()
            self._response(request_id, {})
        elif subtype == "mcp_status":
            self._spawn(self._mcp_status(request_id))
        elif subtype == "mcp_reload":
            self._spawn(self._mcp_reload(request_id))
        else:
            self._emit(
                {
                    "type": "control_response",
                    "response": {"subtype": "error", "request_id": request_id, "error": "не поддерживается"},
                }
            )
        return request_id

    def ask_source(self, request_id: str):
        """Ф99-7.1: `(вызов хука, разбор вызова)` ждущей карточки хука; карточки нет или она уже решена —
        `None` (запросы сервера движка повторно не решаются)."""
        entry = self._asks.get(request_id)
        req = self._ask_reqs.get(request_id)
        if entry is None or req is None or self.exited.is_set() or entry[0].done():
            return None
        return req, entry[1]

    async def respond(self, request_id: str, response: dict) -> bool:
        if self.exited.is_set():
            return False
        entry = self._asks.get(request_id)
        if entry is not None:
            future, _payload = entry
            if future.done():
                return False
            if response.get("behavior") == "allow":
                future.set_result(Allow())
            else:
                future.set_result(Deny(str(response.get("message") or DENIED_BY_OWNER)))
            return True
        server = self._server_asks.pop(request_id, None)
        if server is None or self._rpc is None:
            return False
        request, payload = server
        self._rpc.answer(request.get("id"), self._server_answer(request, payload, response))
        return True

    async def close_stdin(self) -> None:
        """Как закрытие stdin у `claude`: идущий ход дорабатывает, затем процесс выходит; простаивает — сразу.
        Реплики, ещё не поданные, не подаются."""
        if self.stdin_closed:
            return
        self.stdin_closed = True
        self._inputs.clear()
        if not self.busy:
            self._spawn(self._shutdown())

    async def wait(self, limit_s: float | None = None) -> bool:
        try:
            await asyncio.wait_for(self.exited.wait(), limit_s)
        except TimeoutError:
            return False
        return True

    def tree(self) -> list[int]:
        pid = self.pid
        return [pid, *descendants(pid)] if pid is not None else []

    async def kill_tree(self, snapshot: list[int] | None = None, grace_s: float = 10.0) -> None:
        self._stopping = True
        self._inputs.clear()
        proc = self._proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), grace_s)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
        for pid in snapshot or []:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        await self.wait(5)

    async def aclose(self) -> None:
        for task in [self._dispatcher, self._err_task, *self._tasks]:
            if task is not None and not task.done() and task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        if self._rpc is not None:
            await self._rpc.aclose()
        if self._server is not None:
            self._server.close()
            self._server = None

    async def compact(self) -> bool:
        """Сжатие хабом (`thread/compact/start`): только между ходами; конец — `result` с `num_turns: 0`."""
        if self.stdin_closed or self._stopping or self.exited.is_set() or self._rpc is None or self.busy:
            return False
        self._compacting = True
        self._compact_boundary = False
        self._turn_starting = True
        try:
            await self._rpc.call("thread/compact/start", {"threadId": self.session_id})
        except (RpcError, TimeoutError) as exc:
            log.warning("Codex: сжатие не принято (%s)", exc)
            self._compacting = False
            self._turn_starting = False
            return False
        return True

    async def inject_developer(self, text: str) -> bool:
        """Поправка к закреплённому системному тексту (Ф48-5) — сообщением роли `developer` в историю нити
        (`thread/inject_items`): `thread/resume` новый `developerInstructions` не применяет, а поправку в
        реплике пользователя модель ставит ниже прежних инструкций разработчика (стенд 21.10: роль не
        сменилась). Не вышло — `False` (хост подаст поправку текстом реплики)."""
        if self._rpc is None or not self.session_id or self.exited.is_set() or self._stopping:
            return False
        item = {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": text}]}
        try:
            await self._rpc.call("thread/inject_items", {"threadId": self.session_id, "items": [item]}, 30)
        except (RpcError, TimeoutError) as exc:
            log.warning("Codex: поправка системного текста не вставлена (%s)", exc)
            return False
        return True

    def compact_abandon(self) -> None:
        """Хост перестал ждать сжатие (срок): поздний исход в поток не идёт."""
        self._compacting = False

    def mcp_summary(self) -> list[tuple[str, str]]:
        return sorted(self._mcp.items())

    async def wait_mcp(self) -> None:
        """Дождаться, пока MCP-серверы нити выйдут из `starting` (не дольше `mcp_wait_s` от начала нити)."""
        await self._wait_mcp()

    async def user_messages(self) -> list[tuple[str | None, str, int | None]] | None:
        """Реплики пользователя нити (`thread/read` с ходами): `(clientId, текст, начало хода, мс)`; не
        прочитано — `None`. Для сверки «реплика уже в разговоре» после перезапуска хоста (П10)."""
        if self._rpc is None or not self.session_id:
            return None
        try:
            result = await self._rpc.call(
                "thread/read", {"threadId": self.session_id, "includeTurns": True}, 30
            )
        except RpcError, TimeoutError:
            return None
        thread = (result or {}).get("thread") if isinstance(result, dict) else None
        out: list[tuple[str | None, str, int | None]] = []
        for turn in (thread or {}).get("turns") or []:
            if not isinstance(turn, dict):
                continue
            started = turn.get("startedAt")
            started_ms = (
                int(started) * 1000 if isinstance(started, int) and not isinstance(started, bool) else None
            )
            for item in turn.get("items") or []:
                if not isinstance(item, dict) or item.get("type") != "userMessage":
                    continue
                text = "".join(
                    str(c.get("text") or "")
                    for c in item.get("content") or []
                    if isinstance(c, dict) and c.get("type") == "text"
                )
                client = item.get("clientId")
                out.append((client if isinstance(client, str) else None, text, started_ms))
        return out

    # --- выдача ---------------------------------------------------------------------------------------------

    def _emit(self, message: dict) -> None:
        self._queue.put_nowait(message)

    def _response(self, request_id: str, body: dict) -> None:
        self._emit(
            {
                "type": "control_response",
                "response": {"subtype": "success", "request_id": request_id, "response": body},
            }
        )

    async def _dispatch(self) -> None:
        while True:
            message = await self._queue.get()
            if message is None:
                break
            try:
                await self.on_message(message)
            except Exception:  # noqa: BLE001 — сбой разбора одного сообщения не роняет читателя
                log.exception("Codex: сообщение не обработано")
        self.exited.set()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _drain_err(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        while chunk := await proc.stderr.read(4096):
            self._stderr.extend(chunk)
            del self._stderr[:-STDERR_TAIL]

    async def _watch_rpc(self) -> None:
        """Процесс app-server вышел (поток кончился): ход — без `result` с ошибкой, затем «процесс вышел»."""
        rpc, proc = self._rpc, self._proc
        if rpc is None or proc is None:
            return
        await rpc.closed.wait()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 5)
        self._rc = proc.returncode
        if self._err_task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._err_task, 2)
        if self._turn_id is not None or self._turn_starting:
            tail = bytes(self._stderr).decode("utf-8", "replace").strip()
            self._finish(
                error=f"Codex: процесс вышел посреди хода (код {self._rc})"
                + (f": {tail[-400:]}" if tail else "")
            )
        await self._shutdown()

    async def _shutdown(self) -> None:
        """«Процесс вышел»: висящие карточки сняты, app-server остановлен, выдача дочитывается, `exited`."""
        if self._shut:
            return
        self._shut = True
        self._stopping = True
        self._cancel_asks()
        proc = self._proc
        if proc is not None:
            await _stop(proc)
            self._rc = proc.returncode
        if self._server is not None:
            self._server.close()
            self._server = None
        if self._rc is None:
            self._rc = 0
        self._queue.put_nowait(None)

    def _lifecycle(self, inputs, state: str) -> None:
        for uid, _items in inputs:
            if uid is not None:
                self._emit({"type": "command_lifecycle", "command_uuid": uid, "state": state})

    # --- ход ------------------------------------------------------------------------------------------------

    async def _pump(self) -> None:
        """Строки ввода -> ход: свободна — `turn/start` всем накопленным; идёт ход — `turn/steer`; `steer` не
        принят — строки ждут конца хода (`queued`) и начинают следующий."""
        async with self._lock:
            while self._inputs and not self._stopping and not self.stdin_closed and self._rpc is not None:
                if self._compacting or self._turn_starting:
                    self._lifecycle(self._inputs, "queued")
                    return
                inputs, self._inputs = self._inputs, []
                items = [item for _uid, part in inputs for item in part]
                if self._turn_id is None:
                    await self._wait_mcp()
                    if self._stopping or self.stdin_closed:
                        return
                    if not await self._start_turn(inputs, items):
                        return
                    continue
                turn = self._turn_id
                steer: dict = {"threadId": self.session_id, "expectedTurnId": turn, "input": items}
                uid = next((u for u, _ in inputs if u), None)
                if uid:
                    steer["clientUserMessageId"] = uid
                try:
                    await self._rpc.call("turn/steer", steer)
                except (RpcError, TimeoutError) as exc:
                    log.info("Codex: реплика посреди хода не принята (%s) — следующим ходом", exc)
                    self._inputs = inputs + self._inputs
                    self._lifecycle(inputs, "queued")
                    if self._turn_id is None and not self._turn_starting:
                        continue  # ход уже кончился — сразу новым
                    return
                self._lifecycle(inputs, "started")

    async def _wait_mcp(self) -> None:
        """До первой подачи — дождаться, пока MCP-серверы нити выйдут из `starting` (не дольше `mcp_wait_s` от
        начала нити); `failed` и прочие исходы подачу не держат."""
        if self._mcp_ready.is_set():
            return
        left = self.spec.mcp_wait_s - (time.monotonic() - self._mcp_since)
        if left > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._mcp_ready.wait(), left)
        if not self._mcp_ready.is_set():
            waiting = sorted(k for k, v in self._mcp.items() if v == "starting")
            log.warning(
                "Codex: MCP не подключились за %g с, подаю как есть: %s",
                self.spec.mcp_wait_s,
                ", ".join(waiting),
            )
            self._mcp_ready.set()

    def _check_mcp(self) -> None:
        if not any(v == "starting" for v in self._mcp.values()):
            self._mcp_ready.set()

    async def _start_turn(self, inputs, items: list[dict]) -> bool:
        assert self._rpc is not None
        self._turn_starting = True
        self._reset_turn()
        params: dict = {"threadId": self.session_id, "input": items, "model": self.model}
        if self.effort:
            params["effort"] = self.effort
        uid = next((u for u, _ in inputs if u), None)
        if uid:
            params["clientUserMessageId"] = uid
        self._starting = list(inputs)
        try:
            result = await self._rpc.call("turn/start", params)
        except (RpcError, TimeoutError) as exc:
            self._turn_starting = False
            log.warning("Codex: ход не начат (%s)", exc)
            self._flush_starting()
            self._emit_init()
            self._error = f"Codex: ход не начат ({exc})"
            self._error_info = exc.info if isinstance(exc, RpcError) else None
            self._finish()
            return False
        turn = (result or {}).get("turn") if isinstance(result, dict) else None
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        self._turn_starting = False
        if isinstance(turn_id, str) and turn_id and turn_id not in self._done_turns:
            self._begin(turn_id, "turn")
        self._flush_starting()
        return True

    def _flush_starting(self) -> None:
        starting, self._starting = self._starting, []
        self._lifecycle(starting, "started")

    def _reset_turn(self) -> None:
        self._turn_t0 = time.monotonic()
        self._turn_total0 = dict(self._usage_total) if self._usage_total else None
        self._aborted = False
        self._calls.clear()
        self._blocks.clear()
        self._texts = []
        self._denials = []
        self._calls_n = 0
        self._error = None
        self._error_info = None
        self._bypass_sent = False

    def _begin(self, turn_id: str, kind: str) -> None:
        if self._turn_id == turn_id:
            return
        self._turn_id = turn_id
        self._turn_kind = kind
        self._turn_no += 1
        if kind == "compact":
            self._emit(
                {"type": "system", "subtype": "status", "status": "compacting", "session_id": self.session_id}
            )
        else:
            self._emit_init()
            self._flush_starting()

    def _emit_init(self) -> None:
        self._emit(
            {
                "type": "system",
                "subtype": "init",
                "session_id": self.session_id,
                "cwd": self.spec.cwd,
                "tools": [],
                "permissionMode": BYPASS,
                "model": self.model,
                "engine": ENGINE,
            }
        )

    async def _interrupt(self) -> None:
        turn = self._turn_id
        if turn is None or self._rpc is None:
            return
        self._aborted = True
        self._cancel_asks()
        try:
            await self._rpc.call("turn/interrupt", {"threadId": self.session_id, "turnId": turn}, 15)
        except (RpcError, TimeoutError) as exc:
            log.warning("Codex: прерывание не принято (%s)", exc)
        self._spawn(self._abort_later(turn))

    async def _abort_later(self, turn: str) -> None:
        """Ход не кончился за `stop_grace_s` после `turn/interrupt` — свой прерванный `result` (поздний
        `turn/completed` этого хода в поток уже не идёт)."""
        await asyncio.sleep(self.spec.stop_grace_s)
        if self._turn_id == turn:
            self._finish(aborted=True)

    def _cmd_mark(self) -> str:
        """Метка команд текущего хода (`CMD_MARK_ENV` в окружении команды, `bound_command`)."""
        return f"{self._cmd_token}.{self._turn_no}"

    def _marked(self, mark: str) -> list[int]:
        """Процессы под процессом Codex с меткой хода `mark` в окружении."""
        pid = self.pid
        if pid is None:
            return []
        needle = f"{codex_tools.CMD_MARK_ENV}={mark}".encode()
        out = []
        for child in descendants(pid):
            try:
                with open(f"/proc/{child}/environ", "rb") as f:
                    env = f.read()
            except OSError:
                continue
            if needle in env.split(b"\0"):
                out.append(child)
        return out

    async def _kill_marked(self, mark: str) -> None:
        """Снять команды прерванного хода: SIGTERM, через `KILL_GRACE_S` — SIGKILL оставшимся."""
        pids = self._marked(mark)
        if not pids:
            return
        log.info("Codex: ход прерван — снимаю его команды (%d процессов)", len(pids))
        for pid in pids:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGTERM)
        await asyncio.sleep(KILL_GRACE_S)
        for pid in self._marked(mark):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)

    def _cancel_asks(self) -> None:
        for request_id, (future, _payload) in list(self._asks.items()):
            if future.done():
                continue
            future.set_result(Deny(CANCELLED))
            self._emit({"type": "control_cancel_request", "request_id": request_id})
        for request_id, (request, _payload) in list(self._server_asks.items()):
            self._server_asks.pop(request_id, None)
            if self._rpc is not None:
                self._rpc.answer(request.get("id"), self._server_answer(request, _payload, None))
            self._emit({"type": "control_cancel_request", "request_id": request_id})

    def _close_blocks(self) -> None:
        for key in list(self._blocks.values()):
            self._stream(key, {"type": "content_block_stop"})
        self._blocks.clear()

    def _finish(
        self, *, aborted: bool | None = None, error: str | None = None, status: str | None = None
    ) -> None:
        """Конец хода: `result` (или конец сжатия), затем следующий ход по строкам, ждущим в очереди."""
        turn, kind = self._turn_id, self._turn_kind
        if turn is not None:
            self._done_turns.add(turn)
        if kind != "compact":
            self._flush_starting()  # конец хода раньше его начала в потоке — строки всё равно начаты
        self._turn_id = None
        self._turn_kind = None
        self._turn_starting = False
        self._cancel_asks()
        self._close_blocks()
        aborted = self._aborted if aborted is None else aborted
        if aborted and kind != "compact":
            # Ф74-5б: Codex кончил ход, а запущенные им команды оставил идти — снять, как Claude Code.
            self._spawn(self._kill_marked(self._cmd_mark()))
        error = error or self._error
        if error and not aborted:
            self._emit({"type": "system", "subtype": "engine_error", "kind": "error", "text": error})
        if self._error_info in LIMIT_ERRORS and self._limit_sent is None:
            # Срок сброса уже назван `account/rateLimits/updated` — ошибка хода его не затирает.
            self._rate_limited(None, reason="лимит подписки Codex")
        is_error = bool(aborted or error or status == "failed")
        if kind == "compact" or (self._compacting and kind is None):
            compacting, self._compacting = self._compacting, False
            if not compacting:
                return  # хост перестал ждать это сжатие
            body = self._result_body(is_error=is_error, aborted=aborted, error=error)
            body["num_turns"] = 0
            self._emit(body)
        else:
            self._emit(self._result_body(is_error=is_error, aborted=aborted, error=error))
        self._aborted = False
        if self.stdin_closed or self._stopping:
            self._spawn(self._shutdown())
        elif self._inputs:
            self._spawn(self._pump())

    def _result_body(self, *, is_error: bool, aborted: bool, error: str | None) -> dict:
        tokens: dict = {}
        last, total, before = self._usage_last or {}, self._usage_total or {}, self._turn_total0 or {}

        def delta(key: str) -> int | None:
            now = total.get(key)
            if not isinstance(now, int) or isinstance(now, bool):
                return None
            prev = before.get(key)
            return now - prev if isinstance(prev, int) and not isinstance(prev, bool) and now >= prev else now

        inp, cached, written, out = (
            delta(k) for k in ("inputTokens", "cachedInputTokens", "cacheWriteInputTokens", "outputTokens")
        )
        if inp is not None:
            tokens["input"] = max(inp - (cached or 0) - (written or 0), 0)
        if out is not None:
            tokens["output"] = out
        if cached is not None:
            tokens["cache_read"] = cached
        if written is not None:
            tokens["cache_write"] = written
        body: dict = {
            "type": "result",
            "subtype": "success"
            if not is_error
            else ("error_during_execution" if aborted else "error_engine"),
            "is_error": is_error,
            "result": self._texts[-1] if self._texts else (error or ""),
            "session_id": self.session_id,
            "num_turns": max(1, self._calls_n) if not is_error or self._calls_n else 0,
            "duration_ms": int((time.monotonic() - self._turn_t0) * 1000),
            "usage": tokens,
            "permission_denials": list(self._denials),
            "terminal_reason": "aborted" if aborted else ("error" if is_error else "completed"),
            "engine": ENGINE,
        }
        context = last.get("inputTokens")
        if isinstance(context, int) and not isinstance(context, bool):
            # Как `_context_of` Claude: вход последнего вызова (у Codex `inputTokens` уже с кэшем).
            body["context_tokens"] = context
        if self._window is not None:
            body["context_limit"] = self._window
        return body

    # --- уведомления ----------------------------------------------------------------------------------------

    async def _on_note(self, message: dict) -> None:
        method = message.get("method")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        thread = params.get("threadId")
        own = thread is None or thread == self.session_id
        if method == "mcpServer/startupStatus/updated":
            name, status = params.get("name"), params.get("status")
            if isinstance(name, str) and isinstance(status, str) and own:
                self._mcp[name] = status
                if status not in ("starting", "ready"):
                    reason = params.get("failureReason") or params.get("error")
                    if reason:
                        self._mcp_errors[name] = str(reason)[:200]
                self._check_mcp()
            return
        if method == "thread/tokenUsage/updated" and own:
            usage = params.get("tokenUsage") if isinstance(params.get("tokenUsage"), dict) else {}
            if isinstance(usage.get("last"), dict):
                self._usage_last = usage["last"]
            if isinstance(usage.get("total"), dict):
                self._usage_total = usage["total"]
            window = usage.get("modelContextWindow")
            if isinstance(window, int) and not isinstance(window, bool) and window > 0:
                self._window = window
            return
        if method == "account/rateLimits/updated":
            limits = params.get("rateLimits") if isinstance(params.get("rateLimits"), dict) else {}
            self._on_limits(limits)
            return
        if method == "turn/started" and own:
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            turn_id = turn.get("id")
            if isinstance(turn_id, str) and turn_id and turn_id not in self._done_turns:
                if self._compacting and self._turn_id is None:
                    self._reset_turn()
                    self._turn_starting = False
                    self._begin(turn_id, "compact")
                elif self._turn_id is None and not self._turn_starting:
                    # Ход, начатый не нами (продолжение после хука `Stop` и т.п.), — тоже ход.
                    self._reset_turn()
                    self._begin(turn_id, "turn")
                elif self._turn_starting and not self._compacting:
                    self._reset_turn() if self._turn_id is None and not self._turn_t0 else None
                    self._turn_starting = False
                    self._begin(turn_id, "turn")
            return
        if method == "turn/completed" and own:
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            turn_id = turn.get("id")
            if not isinstance(turn_id, str) or turn_id in self._done_turns:
                return
            if self._turn_id is not None and turn_id != self._turn_id:
                return
            if self._turn_id is None and not self._turn_starting and not self._compacting:
                return
            status = turn.get("status")
            error = turn.get("error") if isinstance(turn.get("error"), dict) else None
            if error is not None and not self._error:
                self._error = "Codex: " + str(error.get("message") or "ход не удался")
                info = error.get("codexErrorInfo")
                self._error_info = (
                    info
                    if isinstance(info, str)
                    else (next(iter(info), None) if isinstance(info, dict) else None)
                )
            if self._turn_id is None:
                self._turn_id = turn_id
            self._finish(aborted=status == "interrupted" or self._aborted, status=status)
            return
        if method == "error" and own:
            error = params.get("error") if isinstance(params.get("error"), dict) else {}
            if params.get("willRetry"):
                return
            self._error = "Codex: " + str(error.get("message") or "ошибка")
            info = error.get("codexErrorInfo")
            self._error_info = (
                info
                if isinstance(info, str)
                else (next(iter(info), None) if isinstance(info, dict) else None)
            )
            return
        if method == "item/agentMessage/delta":
            self._on_delta(params, "text")
            return
        if method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
            self._on_delta(params, "thinking")
            return
        if method == "item/commandExecution/outputDelta":
            item_id = params.get("itemId")
            if isinstance(item_id, str) and isinstance(params.get("delta"), str):
                self._outputs.setdefault(item_id, []).append(params["delta"])
            return
        if method == "item/commandExecution/terminalInteraction":
            if codex_tools.stdin_bypass(params.get("stdin")):
                await self._bypass("ввод в запущенную команду")
            return
        if method in ("item/started", "item/completed"):
            item = params.get("item") if isinstance(params.get("item"), dict) else {}
            await self._on_item(method == "item/started", item, child=not own)
            return

    def _on_limits(self, limits: dict) -> None:
        reached = limits.get("rateLimitReachedType")
        windows = [w for w in (limits.get("primary"), limits.get("secondary")) if isinstance(w, dict)]
        full = [
            w for w in windows if isinstance(w.get("usedPercent"), int | float) and w["usedPercent"] >= 100
        ]
        if not reached and not full:
            self._limit_sent = None
            self._limit_reset = None
            return
        reset = max(
            (w.get("resetsAt") for w in full if isinstance(w.get("resetsAt"), int | float)), default=None
        )
        if reset is None:
            reset = max(
                (w.get("resetsAt") for w in windows if isinstance(w.get("resetsAt"), int | float)),
                default=None,
            )
        self._rate_limited(reset, reason="лимит подписки Codex")

    def _rate_limited(self, reset, *, reason: str) -> None:
        key = f"{reset}"
        if self._limit_sent == key:
            return
        if reset is None and self._limit_reset is not None and self._limit_reset > time.time():
            # Отказ без срока, пока действует названный срок (живой журнал 26.10: событие с `resetsAt` и
            # следом без него), — не новость: хост укоротил бы паузу до получаса.
            return
        self._limit_sent = key
        self._limit_reset = float(reset) if isinstance(reset, int | float) else None
        # Ф83-1: причина паузы для хоста — лимит подписки (баланса у подписки Codex нет).
        info = {"status": "rejected", "engine": ENGINE, "reason": reason, "kind": "limit"}
        if isinstance(reset, int | float):
            info["resetsAt"] = reset
        self._emit({"type": "rate_limit_event", "rate_limit_info": info})

    def _stream(self, block: str, event: dict) -> None:
        self._emit({"type": "stream_event", "block": block, "session_id": self.session_id, "event": event})

    def _on_delta(self, params: dict, kind: str) -> None:
        item_id, delta = params.get("itemId"), params.get("delta")
        if (
            not isinstance(item_id, str)
            or not isinstance(delta, str)
            or not delta
            or params.get("threadId") not in (None, self.session_id)
        ):
            return
        key = self._blocks.get(item_id)
        if key is None:
            key = f"x{self._turn_no}-{item_id}"
            self._blocks[item_id] = key
            self._stream(key, {"type": "content_block_start", "content_block": {"type": kind}})
        if kind == "text":
            self._stream(key, {"type": "content_block_delta", "delta": {"type": "text_delta", "text": delta}})
        else:
            self._stream(
                key, {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": delta}}
            )

    def _assistant(self, block: dict, *, key: str | None = None, parent: str | None = None) -> None:
        message = {
            "type": "assistant",
            "session_id": self.session_id,
            "parent_tool_use_id": parent,
            "message": {"role": "assistant", "content": [block]},
        }
        if key is not None:
            message["block"] = key
        self._emit(message)

    async def _on_item(self, started: bool, item: dict, *, child: bool) -> None:
        kind, item_id = item.get("type"), item.get("id")
        if not isinstance(item_id, str) or not item_id:
            return
        parent = f"codex-agent-{item_id}" if child else None
        if kind in codex_tools.TOOL_ITEMS and started and item_id not in self._allowed:
            # Хук этот вызов не разрешал (упал, не вызван): Codex пропустил его мимо гарда.
            what = {"commandExecution": "команда", "fileChange": "правка файла", "mcpToolCall": "вызов MCP"}[
                kind
            ]
            await self._bypass(what)
        if kind == "agentMessage":
            if started:
                return
            text = str(item.get("text") or "")
            key = self._blocks.pop(item_id, None)
            if key is not None:
                self._stream(key, {"type": "content_block_stop"})
            if text.strip():
                if not child:
                    self._texts.append(text)
                self._assistant({"type": "text", "text": text}, key=key, parent=parent)
            return
        if kind == "reasoning":
            if not started:
                key = self._blocks.pop(item_id, None)
                if key is not None:
                    self._stream(key, {"type": "content_block_stop"})
            return
        if kind == "contextCompaction":
            if not started and self._turn_kind == "compact":
                self._compact_boundary = True
                pre = (self._usage_last or {}).get("inputTokens")
                meta = {"trigger": "manual"}
                if isinstance(pre, int) and not isinstance(pre, bool):
                    meta["pre_tokens"] = pre
                self._emit(
                    {
                        "type": "system",
                        "subtype": "compact_boundary",
                        "session_id": self.session_id,
                        "compact_metadata": meta,
                    }
                )
            return
        if kind == "collabAgentToolCall":
            self._on_collab(started, item)
        shown = codex_tools.stream_item(item)
        if shown is None:
            return
        name, tool_input = shown
        if started or item_id not in self._calls:
            if item_id in self._calls:
                return
            for key in list(self._blocks.values()):
                self._stream(key, {"type": "content_block_stop"})
            self._blocks.clear()
            self._calls[item_id] = name
            if not child:
                self._calls_n += 1
            self._assistant(
                {"type": "tool_use", "id": item_id, "name": name, "input": tool_input}, parent=parent
            )
            if started:
                return
        text, is_error, use = codex_tools.result_text(
            item, output="".join(self._outputs.pop(item_id, [])), patch=self._patches.pop(item_id, None)
        )
        message = {
            "type": "user",
            "session_id": self.session_id,
            "parent_tool_use_id": parent,
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": item_id, "content": text, "is_error": is_error}
                ],
            },
        }
        if use is not None:
            message["tool_use_result"] = use
        self._emit(message)

    def _on_collab(self, started: bool, item: dict) -> None:
        """Ф48-1: подагенты Codex (`spawnAgent`) — задачи `local_agent` для состояния сессии."""
        tool = item.get("tool")
        receivers = [t for t in item.get("receiverThreadIds") or [] if isinstance(t, str) and t]
        states = item.get("agentsStates") if isinstance(item.get("agentsStates"), dict) else {}
        if tool == "spawnAgent" and not started:
            for thread in receivers:
                if thread in self._subagents:
                    continue
                self._subagents.add(thread)
                self._emit(
                    {
                        "type": "system",
                        "subtype": "task_started",
                        "task_id": thread,
                        "run_id": thread,
                        "task_type": "local_agent",
                        "is_backgrounded": True,
                        "description": str(item.get("prompt") or "")[:200],
                        "subagent_type": str(item.get("model") or ""),
                    }
                )
        for thread, state in states.items():
            if thread not in self._subagents or not isinstance(state, dict):
                continue
            status = state.get("status")
            if status in ("completed", "errored", "shutdown", "interrupted", "notFound"):
                self._subagents.discard(thread)
                self._emit(
                    {
                        "type": "system",
                        "subtype": "task_notification",
                        "task_id": thread,
                        "status": "completed" if status == "completed" else "failed",
                        "summary": str(state.get("message") or "")[:500],
                    }
                )

    async def _bypass(self, what: str) -> None:
        """Обход гарда замечен: ошибка в ленту и прерывание хода (один раз на ход)."""
        if self._bypass_sent:
            return
        self._bypass_sent = True
        text = BYPASS_TEXT.format(what=what)
        log.warning("Codex: %s", text)
        self._emit({"type": "system", "subtype": "engine_error", "kind": "error", "text": text})
        self._spawn(self._interrupt())

    # --- MCP ------------------------------------------------------------------------------------------------

    async def _mcp_status(self, request_id: str) -> None:
        servers = await self._mcp_list()
        self._response(request_id, {"mcpServers": servers})

    async def _mcp_list(self) -> list[dict]:
        """Состояние серверов: `mcpServerStatus/list` (вход и инструменты), поверх — статусы запуска нити."""
        statuses = dict(self._mcp)
        listed: list[dict] = []
        if self._rpc is not None:
            with contextlib.suppress(RpcError, TimeoutError):
                result = await self._rpc.call(
                    "mcpServerStatus/list", {"threadId": self.session_id, "detail": "toolsAndAuthOnly"}, 20
                )
                listed = [s for s in (result or {}).get("data") or [] if isinstance(s, dict)]
        out: list[dict] = []
        seen: set[str] = set()
        off = {str(n) for n in self.spec.disable_mcp} - set(statuses)
        for entry in listed:
            name = str(entry.get("name") or "")
            if not name or name in off:  # Ф74-8: выключенный сервер список Codex тоже называет — не «✓»
                continue
            seen.add(name)
            status = statuses.get(name) or "ready"
            auth = entry.get("authStatus")
            if auth in ("notLoggedIn",):
                status = "needs-auth"
            out.append({"name": name, "status": _mcp_word(status), "error": self._mcp_errors.get(name)})
        for name, status in statuses.items():
            if name not in seen:
                out.append({"name": name, "status": _mcp_word(status), "error": self._mcp_errors.get(name)})
        return sorted(out, key=lambda s: s["name"])

    async def _mcp_reload(self, request_id: str) -> None:
        """«Перечитать MCP» (Ф73-1) без перезапуска процесса: `config/mcpServer/reload`, итог со статусами."""
        ok = False
        if self._rpc is not None:
            try:
                await self._rpc.call("config/mcpServer/reload", {}, 60)
                ok = True
            except (RpcError, TimeoutError) as exc:
                log.warning("Codex: MCP не перечитаны (%s)", exc)
        await asyncio.sleep(0)
        self._response(request_id, {"ok": ok, "mcpServers": await self._mcp_list()})

    # --- запросы сервера ------------------------------------------------------------------------------------

    async def _on_server_request(self, message: dict) -> None:
        method = str(message.get("method") or "")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        assert self._rpc is not None
        if method == "item/tool/requestUserInput":
            questions = [q for q in params.get("questions") or [] if isinstance(q, dict)]
            payload = {
                "tool_name": QUESTION_TOOL,
                "tool_input": {
                    "questions": [
                        {
                            "question": str(q.get("question") or ""),
                            "header": str(q.get("header") or ""),
                            "options": [
                                {
                                    "label": str(o.get("label") or ""),
                                    "description": str(o.get("description") or ""),
                                }
                                for o in q.get("options") or []
                                if isinstance(o, dict)
                            ],
                            "multiSelect": False,
                        }
                        for q in questions
                    ]
                },
                "questions": questions,
            }
            self._server_card(message, payload, str(params.get("itemId") or ""))
            return
        if method in ("item/commandExecution/requestApproval", "execCommandApproval"):
            command = params.get("command")
            command = " ".join(command) if isinstance(command, list) else str(command or "")
            payload = {
                "tool_name": "Bash",
                "tool_input": {"command": command},
                "reason": "Codex просит одобрения команды",
            }
            self._server_card(message, payload, str(params.get("itemId") or ""))
            return
        if method in ("item/fileChange/requestApproval", "applyPatchApproval"):
            payload = {
                "tool_name": "Edit",
                "tool_input": {"file_path": str(params.get("grantRoot") or "")},
                "reason": "Codex просит одобрения правки",
            }
            self._server_card(message, payload, str(params.get("itemId") or ""))
            return
        if method == "item/permissions/requestApproval":
            payload = {
                "tool_name": "Codex: расширение прав",
                "tool_input": {"permissions": params.get("permissions"), "reason": params.get("reason")},
                "reason": "Codex просит больше прав, чем у сессии",
            }
            self._server_card(message, payload, str(params.get("itemId") or ""))
            return
        if method == "mcpServer/elicitation/request":
            server = str(params.get("serverName") or "?")
            payload = {
                "tool_name": f"mcp__{server}__elicitation",
                "tool_input": {
                    "message": str(params.get("message") or ""),
                    "schema": params.get("requestedSchema"),
                    "meta": params.get("_meta"),
                },
                "reason": f"MCP-сервер {server} спрашивает",
            }
            self._server_card(message, payload, "")
            return
        self._rpc.answer(message.get("id"), error={"code": -32601, "message": "не поддерживается хабом"})

    def _server_card(self, request: dict, payload: dict, tool_use_id: str) -> None:
        if self._stopping or self.exited.is_set():
            assert self._rpc is not None
            self._rpc.answer(request.get("id"), self._server_answer(request, payload, None))
            return
        self._request_n += 1
        request_id = f"codex-{self._request_n}"
        self._server_asks[request_id] = (request, payload)
        body = {
            "subtype": "can_use_tool",
            "tool_name": payload["tool_name"],
            "input": payload["tool_input"],
            "tool_use_id": tool_use_id,
        }
        if payload.get("reason"):
            body["decision_reason"] = payload["reason"]
        self._emit({"type": "control_request", "request_id": request_id, "request": body})

    @staticmethod
    def _server_answer(request: dict, payload: dict, response: dict | None) -> dict:
        """Ответ на запрос сервера по решению карточки (`None` — карточка снята: отказ)."""
        method = str(request.get("method") or "")
        allow = isinstance(response, dict) and response.get("behavior") == "allow"
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        if method == "item/tool/requestUserInput":
            answers: dict = {}
            given = ((response or {}).get("updatedInput") or {}).get("answers") if allow else None
            for q in payload.get("questions") or []:
                value = given.get(str(q.get("question") or "")) if isinstance(given, dict) else None
                qid = str(q.get("id") or "")
                if qid:
                    answers[qid] = {"answers": [value] if isinstance(value, str) and value else []}
            return {"answers": answers}
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            return {"decision": "accept" if allow else "decline"}
        if method in ("execCommandApproval", "applyPatchApproval"):
            return {"decision": "approved" if allow else "denied"}
        if method == "item/permissions/requestApproval":
            return {"permissions": params.get("permissions") if allow else {}, "scope": "turn"}
        if method == "mcpServer/elicitation/request":
            return {"action": "accept", "content": {}} if allow else {"action": "decline"}
        return {}

    # --- хуки -----------------------------------------------------------------------------------------------

    def _peer_ok(self, writer) -> bool:
        sock = writer.get_extra_info("socket")
        proc = self._proc
        if sock is None or proc is None or proc.returncode is not None:
            return False
        try:
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size)
        except OSError:
            return False
        pid, uid, _gid = _UCRED.unpack(raw)
        return uid == os.getuid() and _is_descendant(pid, proc.pid)

    async def _on_hook(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        answer = _deny(PEER_DENIED)
        try:
            if self._peer_ok(writer):
                line = await reader.readline()
                message = json.loads(line) if line.endswith(b"\n") else None
                if isinstance(message, dict) and isinstance(message.get("input"), dict):
                    gone = asyncio.ensure_future(reader.read(1))
                    try:
                        answer = await self._decide_hook(
                            str(message.get("event") or ""), message["input"], gone
                        )
                    finally:
                        gone.cancel()
                else:
                    answer = _deny("гард хаба: вход хука не разобран")
        except Exception as exc:  # noqa: BLE001 — сбой разбора: запрет
            log.warning("Codex: хук не обработан (%s)", exc.__class__.__name__)
            answer = _deny("гард хаба: сбой решения — запрет")
        try:
            writer.write(json.dumps(answer, ensure_ascii=False).encode("utf-8") + b"\n")
            await writer.drain()
        except ConnectionError, OSError:
            pass
        finally:
            writer.close()

    async def _decide_hook(self, event: str, data: dict, gone: asyncio.Future) -> dict:
        if event == codex_tools.SUBAGENT:
            return {}
        if event != codex_tools.PRE:
            return _deny(f"гард хаба: незнакомое событие хука {event}")
        if data.get("session_id") not in (None, self.session_id):
            return _deny(PEER_DENIED)
        payload = codex_tools.from_hook(data, cwd=self.spec.cwd)
        call_id = str(data.get("tool_use_id") or "")
        req = HookRequest(
            event=event,
            tool=str(data.get("tool_name") or ""),
            args=data.get("tool_input"),
            session_id=str(data.get("session_id") or self.session_id or ""),
            call_id=call_id,
        )
        if payload.get("skip"):
            verdict: Allow | Deny | Ask = Allow()
        else:
            verdict = await _maybe(self.spec.decide(req, payload))
            if isinstance(verdict, Ask):
                verdict = await self._ask(req, payload, verdict, gone)
        if not isinstance(verdict, Allow):
            reason = verdict.reason if isinstance(verdict, Deny) else "гард хаба: запрет"
            self._denials.append(
                {
                    "tool_name": payload.get("tool_name"),
                    "tool_use_id": call_id,
                    "tool_input": payload.get("tool_input") or {},
                }
            )
            return _deny(reason)
        if call_id:
            self._allowed.add(call_id)
        out: dict = {"hookEventName": "PreToolUse", "permissionDecision": "allow"}
        if req.tool == "Bash" and isinstance((data.get("tool_input") or {}).get("command"), str):
            out["updatedInput"] = {
                "command": codex_tools.bound_command(
                    data["tool_input"]["command"], self._cwd_real, mark=self._cmd_mark()
                )
            }
        elif (
            req.tool == "apply_patch"
            and isinstance((data.get("tool_input") or {}).get("command"), str)
            and call_id
        ):
            self._patches[call_id] = data["tool_input"]["command"]
        return {"hookSpecificOutput": out}

    async def _ask(self, req: HookRequest, payload: dict, ask: Ask, gone: asyncio.Future):
        if self._aborted or self._stopping or not self.busy:
            return Deny(CANCELLED)
        self._request_n += 1
        request_id = f"codex-{self._request_n}"
        tool_input = dict(payload.get("tool_input") or {})
        if payload.get("patch") and isinstance((req.args or {}).get("command"), str):
            tool_input["patch"] = req.args["command"][:20000]
        if payload.get("files") and len(payload["files"]) > 1:
            tool_input["files"] = list(payload["files"])
        request = {
            "subtype": "can_use_tool",
            "tool_name": payload.get("tool_name"),
            "input": tool_input,
            "tool_use_id": req.call_id,
        }
        extra = ask.payload if isinstance(ask.payload, dict) else {}
        if isinstance(extra.get("reason"), str) and extra["reason"]:
            request["decision_reason"] = extra["reason"]
        if isinstance(extra.get("rule"), str) and extra["rule"]:
            request["hub_rule"] = extra["rule"]
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._asks[request_id] = (future, payload)
        self._ask_reqs[request_id] = req
        self._emit({"type": "control_request", "request_id": request_id, "request": request})
        try:
            done, _pending = await asyncio.wait(
                {future, gone}, timeout=self._ask_limit_s, return_when=asyncio.FIRST_COMPLETED
            )
            if not future.done():
                future.set_result(Deny(CANCELLED if gone in done else ASK_EXPIRED))
                self._emit({"type": "control_cancel_request", "request_id": request_id})
            verdict = future.result()
        finally:
            self._asks.pop(request_id, None)
            self._ask_reqs.pop(request_id, None)
        return verdict if isinstance(verdict, Allow | Deny) else Deny(CANCELLED)


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _mcp_word(status: str) -> str:
    """Статус запуска Codex -> слово, как у `mcp_status` Claude Code: `pending` / `connected` / `failed`."""
    return {"starting": "pending", "ready": "connected"}.get(status, status)
