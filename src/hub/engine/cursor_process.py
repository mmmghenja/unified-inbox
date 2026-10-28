"""Процесс Cursor с поверхностью `hub.work.session.Process` (Ф71-1, docs/architecture.md).

`cursor-agent -p` живёт ОДИН ход: реплика — в stdin, события — NDJSON в stdout, после хода процесс выходит
(раздел 1 спеки). Хост же написан под долгий процесс `claude -p` stream-json, поэтому `CursorProcess` между
ходами «живой» без ОС-процесса, а каждая реплика запускает новый ход с `--resume <chatId>`:

- `send(user)` — ход: `cursor-agent -p --output-format stream-json --stream-partial-output --model <м>
  --trust --force --approve-mcps --plugin-dir <плагин> --workspace <папка> [--resume <chatId>]`, текст — в
  stdin. Реплики, пришедшие во время хода (хост подаёт их так же, как Claude, — строкой посреди хода),
  входят в идущий ход на ближайшем `preToolUse` ОСНОВНОГО разговора (`additional_context`; у подагента
  свой `conversation_id` — ему не подаются); не вошли до конца хода — уходят следующим ходом ОДНИМ вводом.
  Судьбу строки хост узнаёт, как у Claude, из `command_lifecycle` (`queued` / `started` / `cancelled`);
- выдача в `on_message` — в формате Claude: `system/init` (`permissionMode: bypassPermissions`, П6;
  `session_id`
  — chatId Cursor), `stream_event` с ключом блока `block` (текст и размышления по мере набора), `assistant`
  (законченный текст сегмента с тем же `block`; `tool_use` в терминах Claude — `cursor_tools.stream_call`),
  `user` (`tool_result`, у правки — `tool_use_result {patch, filePath}`), `result` (`usage` в именах Claude,
  `engine: "cursor"`);
- ход кончился без `result` (П5: «Стоп» — rc 130, падение, сеть, пул исчерпан) — свой `result` с `is_error`
  (после «Стопа» — `terminal_reason: "aborted"`), хвост stderr — в `system/engine_error`;
- `control("initialize")` — ответ `bypassPermissions` (П6); `control("interrupt")` — SIGINT группе процесса
  хода, через `stop_grace_s` — SIGKILL;
- Ф74-8: исчерпан лимит подписки Cursor (текст ошибки хода: `usage limit`, `rate limit`, коды
  `*_USAGE_LIMIT`/`*_RATE_LIMIT*`/`USAGE_PRICING_REQUIRED` из сборки 2026.10.19) — `rate_limit_event` с
  `engine: "cursor"` (пауза лимита движка, как у Codex и OpenCode; срок — «in N minutes/hours» из текста,
  иначе минимальный хоста); подагент (`taskToolCall` потока) — задача `local_agent` (Ф48-1) событиями
  `system/task_started`/`task_notification`; разговор, похоже, сжат самим Cursor (подсказка на вызов модели
  упала ниже `SUMMARY_DROP` от наибольшей) — блок правил сессии подаётся снова следующим ходом;
- хуки Cursor (`preToolUse`, `beforeMCPExecution`, `subagentStart`) — тонкий клиент `hub-cursor-hook` шлёт их
  сюда по абстрактному unix-сокету (П1). Собеседник проверяется по `SO_PEERCRED`: свой uid и потомок
  процесса ИДУЩЕГО хода. Решает `CursorSpec.decide(req, payload)` (хост: гард и правила, как у OpenCode):
  `Allow`/`Deny` — сразу, `Ask` — `control_request` `can_use_tool` (карточка владельцу) и ожидание `respond`.
  Ход кончился, «Стоп», хук отвалился — висящий запрос снимается (`control_cancel_request`), хуку — запрет.

`sidecar` — `hub mcp --profile <п>` с личностью сессии на всё время жизни процесса: MCP-сервер Cursor живёт
только ход, а подписка сессии хаба (Ф41-4) оформляется при старте сервера — без него сессию, поднятую ради
подписки, нечему было бы подписать."""

import asyncio
import contextlib
import hashlib
import inspect
import json
import logging
import os
import re
import secrets
import signal
import socket
import struct
import time
import types
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from hub.work import files
from hub.work.session import descendants, proc_start

from . import cursor_tools, failure
from .oc_turn import Allow, Ask, Deny

log = logging.getLogger(__name__)

ENGINE = "cursor"
BYPASS = "bypassPermissions"
SOCK_ENV = "HUB_CURSOR_SOCK"
PID_ENV = "HUB_CURSOR_PID"
HOOK_INPUT_MAX = 64 * 1024 * 1024
STREAM_LIMIT = 64 * 1024 * 1024
STDERR_TAIL = 4000
CANCELLED = "запрос снят: ход прерван"
DENIED_BY_OWNER = "владелец отказал"
PEER_DENIED = "гард хаба: хук не из хода этой сессии"
RULES_NAME = "hub-session.mdc"
HOOK_TIMEOUT_S = 86400
_UCRED = struct.Struct("3i")
LIMIT_REASON = "лимит подписки Cursor"
# Ф74-8: признаки исчерпанного лимита в тексте ошибки хода (хвост stderr, `result`). Коды — из классификатора
# ошибок сборки 2026.10.19 (`FREE_USER_USAGE_LIMIT`, `PRO_USER_RATE_LIMIT_EXCEEDED`, `RATE_LIMITED`,
# `GENERIC_RATE_LIMIT_EXCEEDED`, `USAGE_PRICING_REQUIRED`…); словами — как их пишет Cursor. Живьём лимит не
# получен: настоящий текст неизвестен.
_LIMIT_RE = re.compile(
    r"usage[ _-]?limit|rate[ _-]?limit|usage[ _-]?pricing[ _-]?required|spend(?:ing)?[ _-]?limit"
    r"|hit (?:your|the) (?:\w+ )?limit|out of (?:fast )?(?:requests|usage|credits)"
    r"|quota (?:exceeded|reached)",
    re.IGNORECASE,
)
_LIMIT_IN_RE = re.compile(
    r"\bin (\d{1,4}) ?(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)\b", re.IGNORECASE
)
_LIMIT_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
# Ф74-8: сжатие разговора самим Cursor видно только по расходу: подсказка на вызов модели (вход + кэш,
# делённые на число вызовов модели хода) упала ниже этой доли от наибольшей с последней подачи правил, а
# наибольшая была не меньше `SUMMARY_PEAK_MIN`.
SUMMARY_DROP = 0.4
SUMMARY_PEAK_MIN = 40_000
SUMMARY_NOTE = "Cursor, похоже, сжал разговор сам — правила сессии хаба уйдут заново со следующей репликой"


def limit_hit(text: str | None, now: float | None = None) -> tuple[bool, float | None]:
    """Ф74-8: `(исчерпан ли лимит подписки, срок сброса — секунды эпохи или None)` по тексту ошибки хода."""
    if not isinstance(text, str) or not _LIMIT_RE.search(text):
        return False, None
    m = _LIMIT_IN_RE.search(text)
    if m is None:
        return True, None
    now = time.time() if now is None else now
    return True, now + int(m.group(1)) * _LIMIT_UNITS[m.group(2)[0].lower()]


DecideFn = Callable[..., "Allow | Deny | Ask | Awaitable[Allow | Deny | Ask]"]
MessageHandler = Callable[[dict], Awaitable[None]]


@dataclass
class HookRequest:
    """Вызов, пришедший из хука: событие, имя инструмента Cursor, его вход, разговор, id вызова."""

    event: str
    tool: str
    args: object
    session_id: str
    call_id: str
    child: bool = False


@dataclass
class CursorSpec:
    binary: str
    cwd: str
    env: dict
    plugin_dir: Path
    model: str
    system: str | None
    decide: DecideFn
    resume: str | None = None
    sidecar: list[str] | None = None
    stop_grace_s: float = 10.0
    extra_args: list[str] = field(default_factory=list)


async def _maybe(value):
    return await value if inspect.isawaitable(value) else value


# --- плагин сессии (Р5 с поправками П2, П4, П10) ------------------------------------------------------------


def write_plugin(
    directory: Path,
    *,
    name: str,
    hook_command: list[str],
    mcp_name: str,
    mcp_command: list[str],
    mcp_env: dict,
    extra_servers: dict | None = None,
) -> None:
    """Каталог плагина сессии (0700, файлы 0600): манифест, хуки (все с `failClosed: true` и таймаутом в
    сутки), MCP хаба и `extra_servers`. Личность сессии и ключ запуска — `${env:…}` (П10); ключи серверов
    Claude Code переносятся как есть (файл 0600 в каталоге данных хаба)."""
    import shlex

    directory = Path(directory)
    for sub in (directory, directory / ".cursor-plugin", directory / "hooks", directory / "rules"):
        files.private_dir(sub)
    manifest = {"name": name, "version": "1.0.0", "description": "hub: постоянная сессия хаба"}
    head = " ".join(shlex.quote(p) for p in hook_command)
    hooks = {
        "version": 1,
        "hooks": {
            event: [{"command": f"{head} {event}", "timeout": HOOK_TIMEOUT_S, "failClosed": True}]
            for event in cursor_tools.HOOK_EVENTS
        },
    }
    # Слово владельца: MCP Claude Code (`cursor_mcp.claude_user_servers`) — рядом с сервером хаба.
    servers = {k: v for k, v in (extra_servers or {}).items() if k != mcp_name}
    if mcp_command:
        servers[mcp_name] = {"command": mcp_command[0], "args": mcp_command[1:], "env": mcp_env}
    mcp = {"mcpServers": servers}
    for path, body in (
        (directory / ".cursor-plugin" / "plugin.json", manifest),
        (directory / "hooks" / "hooks.json", hooks),
        (directory / "mcp.json", mcp),
    ):
        files.atomic_write(path, json.dumps(body, ensure_ascii=False, indent=1) + "\n")


MID_HEAD = (
    "Новый ввод пользователя пришёл, пока ты работал (как новая реплика в разговоре). Прочитай его сейчас и "
    "учти в работе, не дожидаясь конца хода:"
)
SYSTEM_MARK = "system-sent.json"
SYSTEM_OPEN = "<hub_session_rules>"
SYSTEM_CLOSE = "</hub_session_rules>"
SYSTEM_HEAD = (
    "Системные правила твоей сессии хаба. Действуют весь разговор; новый такой блок заменяет прежний целиком."
)


def strip_system(text: str) -> str:
    """Реплика без блока `<hub_session_rules>` (сверка «реплика уже в разговоре», П9)."""
    if text.startswith(SYSTEM_OPEN) and (end := text.find(SYSTEM_CLOSE)) >= 0:
        return text[end + len(SYSTEM_CLOSE) :].strip()
    return text


def write_rules(directory: Path, text: str | None) -> None:
    """Системный текст сессии — правило `alwaysApply` плагина; перед каждым ходом (Р4)."""
    body = "---\ndescription: hub — правила сессии хаба\nalwaysApply: true\n---\n" + (text or "") + "\n"
    files.private_dir(Path(directory) / "rules")
    files.atomic_write(Path(directory) / "rules" / RULES_NAME, body)


def _is_descendant(pid: int, root: int) -> bool:
    """`pid` — потомок `root` (по цепочке родителей в `/proc/<pid>/stat`)."""
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


class CursorProcess:
    """Процесс сессии Cursor (см. докстринг модуля)."""

    def __init__(self, spec: CursorSpec, *, on_message: MessageHandler) -> None:
        self.spec = spec
        self.on_message = on_message
        self.session_id: str | None = spec.resume
        self.resumed = bool(spec.resume)
        self.exited = asyncio.Event()
        self.stdin_closed = False
        self._queue: asyncio.Queue = asyncio.Queue()
        self._dispatcher: asyncio.Task | None = None
        self._turn_task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._rc: int | None = None
        self._inputs: list[tuple[str | None, str]] = []  # (uuid строки ввода, текст)
        self._sent: set[str] = set()
        self._server: asyncio.AbstractServer | None = None
        self.sock_name: str | None = None
        self._asks: dict[str, tuple[asyncio.Future, dict]] = {}
        self._ask_reqs: dict[str, HookRequest] = {}  # Ф99-7.1: вызов ждущей карточки — для повторного решения
        self._request_n = 0
        self._aborted = False
        self._system_due: str | None = None
        self._stopping = False
        self._shut = False
        self._sidecar: asyncio.subprocess.Process | None = None
        self._tasks: set[asyncio.Task] = set()
        self._kill_task: asyncio.Task | None = None
        self._interrupted_cmds: list[tuple[int, int | None]] = []  # Ф74-8: команды прерванного хода
        # состояние хода
        self._block: str | None = None
        self._thinking: str | None = None
        self._seg = 0
        self._calls: dict[str, str] = {}  # id вызова -> имя (Claude)
        self._denials: list[dict] = []
        self._model_calls: set[str] = set()
        self._result_seen = False
        self._turn_no = 0
        self._limit_sent = False
        self._ctx_peak = 0  # наибольшая подсказка на вызов модели с последней подачи правил (Ф74-8)
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
        return self._turn_task is not None and not self._turn_task.done()

    async def start(self) -> None:
        """Сервер хуков и `sidecar`; сам ход запускает первая реплика. Не вышло — `OSError`."""
        name = f"hub-cursor-{os.getpid()}-{secrets.token_hex(8)}"
        self._server = await asyncio.start_unix_server(self._on_hook, path="\0" + name, limit=HOOK_INPUT_MAX)
        self.sock_name = name
        self._dispatcher = asyncio.create_task(self._dispatch())
        if self.spec.sidecar:
            try:
                self._sidecar = await asyncio.create_subprocess_exec(
                    *self.spec.sidecar,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    cwd=self.spec.cwd,
                    env=self.spec.env,
                    start_new_session=True,
                )
            except OSError as exc:
                log.warning("Cursor: MCP хаба для подписки не запущен (%s)", exc.__class__.__name__)

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
        content = (message.get("message") or {}).get("content")
        if isinstance(content, str):
            text = content
        else:
            text = "\n\n".join(
                str(b.get("text") or "")
                for b in content or []
                if isinstance(b, dict) and b.get("type") == "text"
            )
        uid = uid if isinstance(uid, str) and uid else None
        if uid is not None:
            self._sent.add(uid)
        self._inputs.append((uid, text))
        if not self.busy:
            self._next_turn()
        else:
            self._lifecycle([(uid, text)], "queued")
        return True

    async def control(self, subtype: str, **fields) -> str:
        self._request_n += 1
        request_id = f"hub-{self._request_n}"
        if subtype == "initialize":
            self._response(request_id, {"current_permission_mode": BYPASS})
        elif subtype == "interrupt":
            if fields.get("cancel_queued"):
                # Как у Claude: не начатые строки ввода выбросить (их подаст очередь хаба по своим правилам).
                dropped, self._inputs = self._inputs, []
                self._lifecycle(dropped, "cancelled")
            await self._interrupt()
            self._response(request_id, {})
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
        entry = self._asks.get(request_id)
        if entry is None or self.exited.is_set():
            return False
        future, _payload = entry
        if future.done():
            return False
        if response.get("behavior") == "allow":
            future.set_result(Allow())
        else:
            future.set_result(Deny(str(response.get("message") or DENIED_BY_OWNER)))
        return True

    async def close_stdin(self) -> None:
        """Как закрытие stdin у `claude`: идущий ход дорабатывает, затем процесс «выходит»; простаивает —
        сразу. Реплики, ещё не поданные в ход, не подаются."""
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
        pids = []
        pid = self.pid
        if pid is not None:
            pids += [pid, *descendants(pid)]
        side = self._sidecar
        if side is not None and side.returncode is None:
            pids += [side.pid, *descendants(side.pid)]
        return pids

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
        if not self.busy:
            await self._shutdown()
        await self.wait(5)

    async def aclose(self) -> None:
        for task in [self._turn_task, self._dispatcher, self._kill_task, *self._tasks]:
            if task is not None and not task.done() and task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        await self._stop_side()
        if self._server is not None:
            self._server.close()
            self._server = None

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
                log.exception("Cursor: сообщение не обработано")
        self.exited.set()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _stop_side(self) -> None:
        side, self._sidecar = self._sidecar, None
        if side is None or side.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(side.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(side.wait(), 3)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(side.pid, signal.SIGKILL)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(side.wait(), 2)

    async def _shutdown(self) -> None:
        """«Процесс вышел»: сервер хуков и `sidecar` — прочь, выдача дочитывается, `exited`."""
        if self._shut:
            return
        self._shut = True
        self._stopping = True
        self._cancel_asks()
        await self._stop_side()
        if self._server is not None:
            self._server.close()
            self._server = None
        if self._rc is None:
            self._rc = 0
        self._queue.put_nowait(None)

    # --- ход ------------------------------------------------------------------------------------------------

    def _next_turn(self) -> None:
        inputs, self._inputs = self._inputs, []
        text = "\n\n".join(t for _uid, t in inputs if t) if len(inputs) > 1 else inputs[0][1]
        self._lifecycle(inputs, "started")
        self._turn_task = asyncio.create_task(self._run_turn(text))

    def _lifecycle(self, inputs: list[tuple[str | None, str]], state: str) -> None:
        """Судьба строк ввода — событием `command_lifecycle`, как у Claude Code (Ф50-1, Ф55-1)."""
        for uid, _text in inputs:
            if uid is not None:
                self._emit({"type": "command_lifecycle", "command_uuid": uid, "state": state})

    def _take_mid(self, event: str, data: dict) -> str | None:
        """Строки ввода, пришедшие посреди хода, — в идущий ход через ответ хука (проба 19.10: текст
        `additional_context` ответа `preToolUse` модель видит сразу). Только основной разговор: у хуков
        подагента свой `conversation_id`."""
        if (
            event != cursor_tools.PRE
            or not self._inputs
            or not self.busy
            or self._aborted
            or self._stopping
            or self.stdin_closed
            or not self.session_id
            or data.get("conversation_id") != self.session_id
        ):
            return None
        inputs, self._inputs = self._inputs, []
        self._lifecycle(inputs, "started")
        body = "\n\n".join(t for _uid, t in inputs if t)
        return f"{MID_HEAD}\n\n{body}"

    def _argv(self) -> list[str]:
        spec = self.spec
        argv = [
            spec.binary,
            "-p",
            "--output-format",
            "stream-json",
            "--stream-partial-output",
            "--model",
            spec.model,
            "--trust",
            "--force",
            "--approve-mcps",
            "--plugin-dir",
            str(spec.plugin_dir),
            "--workspace",
            spec.cwd,
            *spec.extra_args,
        ]
        if self.session_id:
            argv += ["--resume", self.session_id]
        return argv

    def _reset_turn(self) -> None:
        self._block = None
        self._thinking = None
        self._seg = 0
        self._calls.clear()
        self._denials = []
        self._model_calls = set()
        self._result_seen = False
        self._aborted = False
        self._limit_sent = False
        self._subagents = set()

    def _system_mark(self) -> Path:
        return Path(self.spec.plugin_dir) / SYSTEM_MARK

    def _with_system(self, text: str) -> str:
        """Проба 19.10: правило `alwaysApply` из `--plugin-dir` до модели НЕ доходит (правила папки проекта —
        доходят, но они общие на все сессии папки). Системный текст сессии поэтому идёт блоком
        `<hub_session_rules>` перед репликой: в первом ходе разговора и когда текст изменился (режим, роль,
        правила). Отметка «этот текст в этом разговоре уже есть» — файл в плагине сессии (chatId + sha256)."""
        self._system_due = None
        system = (self.spec.system or "").strip()
        if not system:
            return text
        sha = hashlib.sha256(system.encode("utf-8")).hexdigest()
        try:
            mark = json.loads(self._system_mark().read_text(encoding="utf-8"))
        except OSError, ValueError:
            mark = {}
        if self.session_id and isinstance(mark, dict) and mark == {"chat": self.session_id, "sha": sha}:
            return text
        self._system_due = sha
        return f"{SYSTEM_OPEN}\n{SYSTEM_HEAD}\n\n{system}\n{SYSTEM_CLOSE}\n\n{text}"

    def _mark_system_sent(self, sha: str) -> None:
        body = json.dumps({"chat": self.session_id, "sha": sha})
        with contextlib.suppress(OSError):
            files.atomic_write(self._system_mark(), body)

    async def _run_turn(self, text: str) -> None:
        self._turn_no += 1
        self._reset_turn()
        started = time.monotonic()
        stderr_tail = bytearray()
        text = self._with_system(text)
        try:
            write_rules(self.spec.plugin_dir, self.spec.system)
            env = {**self.spec.env, SOCK_ENV: self.sock_name or "", PID_ENV: str(os.getpid())}
            proc = await asyncio.create_subprocess_exec(
                *self._argv(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.spec.cwd,
                env=env,
                start_new_session=True,
                limit=STREAM_LIMIT,
            )
        except OSError as exc:
            log.warning("Cursor: ход не запущен (%s)", exc.__class__.__name__)
            self._finish_without_result(
                started, f"cursor-agent не запустился ({exc.__class__.__name__})", None
            )
            await self._after_turn()
            return
        self._proc = proc

        async def _drain_err() -> None:
            assert proc.stderr is not None
            while chunk := await proc.stderr.read(4096):
                stderr_tail.extend(chunk)
                del stderr_tail[:-STDERR_TAIL]

        err_task = asyncio.create_task(_drain_err())
        try:
            assert proc.stdin is not None
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                proc.stdin.write(text.encode("utf-8"))
                await proc.stdin.drain()
            proc.stdin.close()
            assert proc.stdout is not None
            while True:
                try:
                    line = await proc.stdout.readline()
                except ValueError, asyncio.LimitOverrunError:
                    log.warning("Cursor: строка потока длиннее предела — пропущена")
                    continue
                if not line:
                    break
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    self._on_event(event, started)
            rc = await proc.wait()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(err_task, 2)
        finally:
            if not err_task.done():
                err_task.cancel()
        self._rc = rc
        self._cancel_asks()
        if self._result_seen and self._system_due is not None and self.session_id:
            self._mark_system_sent(self._system_due)
        if not self._result_seen:
            tail = bytes(stderr_tail).decode("utf-8", "replace").strip()
            self._finish_without_result(started, None, rc, tail)
        await self._after_turn()

    async def _after_turn(self) -> None:
        self._reap_interrupted()
        if self._kill_task is not None and not self._kill_task.done():
            self._kill_task.cancel()
        self._kill_task = None
        if self._inputs and not self.stdin_closed and not self._stopping:
            self._next_turn()
            return
        self._turn_task = None
        if self.stdin_closed or self._stopping:
            await self._shutdown()

    def _finish_without_result(self, started: float, why: str | None, rc: int | None, tail: str = "") -> None:
        """П5: `result` не пришёл — свой, с ошибкой; после «Стопа» — прерванный ход."""
        self._close_blocks()
        self._end_subagents(failed=True)
        aborted = self._aborted
        if not aborted:
            self._check_limit(tail or why)
            text = why or f"Cursor: ход оборвался (код {rc})" + (f": {tail[-400:]}" if tail else "")
            log.warning("Cursor: ход без result (код %s)", rc)
            self._emit({"type": "system", "subtype": "engine_error", "kind": "error", "text": text})
        self._emit(
            {
                "type": "result",
                "subtype": "error_during_execution" if aborted else "error_engine",
                "is_error": True,
                "result": tail[-STDERR_TAIL:] if tail else (why or ""),
                "session_id": self.session_id,
                "num_turns": len(self._model_calls),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "permission_denials": list(self._denials),
                "terminal_reason": "aborted" if aborted else "error",
                "engine": ENGINE,
            }
        )

    async def _interrupt(self) -> None:
        proc = self._proc
        if not self.busy or proc is None or proc.returncode is not None:
            return
        self._aborted = True
        self._cancel_asks()
        # Ф74-8 (стенд): команды Shell Cursor стоят в своей группе — SIGINT группе `cursor-agent` их не
        # задевает, и после выхода хода они жили дальше. Снимок потомков (pid + starttime) — до сигнала.
        # Снимаются по концу хода (`_after_turn`).
        self._interrupted_cmds += [(pid, proc_start(pid)) for pid in descendants(proc.pid)]
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGINT)
        if self._kill_task is None or self._kill_task.done():
            self._kill_task = asyncio.create_task(self._kill_later(proc))

    async def _kill_later(self, proc) -> None:
        await asyncio.sleep(self.spec.stop_grace_s)
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)

    def _reap_interrupted(self) -> None:
        """Команды прерванного хода, пережившие его: тот же процесс (pid и starttime совпали) — снять."""
        commands, self._interrupted_cmds = self._interrupted_cmds, []
        for pid, start in commands:
            if start is not None and proc_start(pid) == start:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)

    def _cancel_asks(self) -> None:
        for request_id, (future, _payload) in list(self._asks.items()):
            if future.done():
                continue
            future.set_result(Deny(CANCELLED))
            self._emit({"type": "control_cancel_request", "request_id": request_id})

    # --- события потока -------------------------------------------------------------------------------------

    def _stream(self, block: str, event: dict) -> None:
        self._emit({"type": "stream_event", "block": block, "session_id": self.session_id, "event": event})

    def _close_blocks(self) -> None:
        for attr in ("_thinking", "_block"):
            key = getattr(self, attr)
            if key is not None:
                self._stream(key, {"type": "content_block_stop"})
                setattr(self, attr, None)

    def _assistant(self, block: dict, *, key: str | None = None) -> None:
        message = {
            "type": "assistant",
            "session_id": self.session_id,
            "parent_tool_use_id": None,
            "message": {"role": "assistant", "content": [block]},
        }
        if key is not None:
            message["block"] = key
        self._emit(message)

    def _on_event(self, event: dict, started: float) -> None:
        kind = event.get("type")
        if isinstance(event.get("session_id"), str) and event["session_id"]:
            self.session_id = event["session_id"]
        if kind == "system" and event.get("subtype") == "init":
            self._emit(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": self.session_id,
                    "cwd": self.spec.cwd,
                    "tools": [],
                    "permissionMode": BYPASS,
                    "model": event.get("model") if isinstance(event.get("model"), str) else self.spec.model,
                    "engine": ENGINE,
                }
            )
        elif kind == "thinking":
            if event.get("subtype") == "delta" and isinstance(event.get("text"), str) and event["text"]:
                if self._thinking is None:
                    self._seg += 1
                    self._thinking = f"c{self._turn_no}-t{self._seg}"
                    self._stream(
                        self._thinking, {"type": "content_block_start", "content_block": {"type": "thinking"}}
                    )
                self._stream(
                    self._thinking,
                    {
                        "type": "content_block_delta",
                        "delta": {"type": "thinking_delta", "thinking": event["text"]},
                    },
                )
            elif event.get("subtype") == "completed" and self._thinking is not None:
                self._stream(self._thinking, {"type": "content_block_stop"})
                self._thinking = None
        elif kind == "assistant":
            self._on_assistant(event)
        elif kind == "tool_call":
            self._on_tool(event)
        elif kind == "result":
            self._on_result(event, started)

    def _on_assistant(self, event: dict) -> None:
        content = (event.get("message") or {}).get("content")
        text = "".join(
            str(b.get("text") or "")
            for b in (content if isinstance(content, list) else [])
            if isinstance(b, dict) and b.get("type") == "text"
        )
        if isinstance(event.get("model_call_id"), str):
            self._model_calls.add(event["model_call_id"])
        full = isinstance(event.get("model_call_id"), str) or "timestamp_ms" not in event
        if not full:
            if not text:
                return
            if self._block is None:
                self._seg += 1
                self._block = f"c{self._turn_no}-b{self._seg}"
                self._stream(self._block, {"type": "content_block_start", "content_block": {"type": "text"}})
            self._stream(
                self._block, {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}
            )
            return
        key, self._block = self._block, None
        if key is not None:
            self._stream(key, {"type": "content_block_stop"})
        if text.strip():
            self._assistant({"type": "text", "text": text}, key=key)

    def _on_tool(self, event: dict) -> None:
        call_id = event.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            return
        name, tool_input, result = cursor_tools.stream_call(event.get("tool_call"))
        if event.get("subtype") == "started":
            if call_id in self._calls:
                return
            self._close_blocks()
            self._calls[call_id] = name
            self._assistant({"type": "tool_use", "id": call_id, "name": name, "input": tool_input})
            if name == "Task":
                self._subagent_started(call_id, tool_input)
            return
        if event.get("subtype") != "completed":
            return
        if name == "Task":
            self._subagent_started(call_id, tool_input)  # начало не пришло — задача всё равно была
            self._subagent_done(call_id, result)
        if call_id not in self._calls:
            self._calls[call_id] = name
            self._assistant({"type": "tool_use", "id": call_id, "name": name, "input": tool_input})
        text, is_error, use = cursor_tools.result_text(name, result)
        message = {
            "type": "user",
            "session_id": self.session_id,
            "parent_tool_use_id": None,
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": call_id, "content": text, "is_error": is_error}
                ],
            },
        }
        if use is not None:
            message["tool_use_result"] = use
        self._emit(message)

    def _check_limit(self, text: str | None) -> None:
        """Ф74-8: ошибка хода про лимит подписки — `rate_limit_event` (один на ход)."""
        hit, reset = limit_hit(text)
        if not hit or self._limit_sent:
            return
        self._limit_sent = True
        # Ф83-1: причина паузы для хоста — `failure.classify` (оплата сверх подписки — баланс, иначе лимит).
        cause = "balance" if failure.classify(ENGINE, text) == "balance" else "limit"
        info: dict = {"status": "rejected", "engine": ENGINE, "reason": LIMIT_REASON, "kind": cause}
        if reset is not None:
            info["resetsAt"] = int(reset)
        log.warning("Cursor: %s", LIMIT_REASON)
        self._emit({"type": "rate_limit_event", "rate_limit_info": info})

    def _watch_summary(self, tokens: dict) -> None:
        """Ф74-8: Cursor сжимает разговор сам и не говорит об этом; блок правил сессии лежит в реплике
        разговора и при сжатии может уйти из него. Подсказка на вызов модели упала ниже `SUMMARY_DROP` от
        наибольшей — отметка «правила отданы» снимается: следующий ход подаст их снова."""
        prompt = sum(
            int(tokens.get(k) or 0)
            for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        )
        per_call = prompt // max(1, len(self._model_calls))
        if per_call <= 0:
            return
        if self._ctx_peak >= SUMMARY_PEAK_MIN and per_call < self._ctx_peak * SUMMARY_DROP:
            self._ctx_peak = per_call
            with contextlib.suppress(OSError):
                self._system_mark().unlink()
            # Ф86-1 (C2.1 в): `compacted` — раннер подаст сводку дня первой подачей после сжатия.
            self._emit({"type": "system", "subtype": "engine_note", "text": SUMMARY_NOTE, "compacted": True})
            return
        self._ctx_peak = max(self._ctx_peak, per_call)

    def _on_result(self, event: dict, started: float) -> None:
        self._result_seen = True
        self._close_blocks()
        usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        names = {
            "inputTokens": "input_tokens",
            "outputTokens": "output_tokens",
            "cacheReadTokens": "cache_read_input_tokens",
            "cacheWriteTokens": "cache_creation_input_tokens",
        }
        tokens = {
            dst: usage[src]
            for src, dst in names.items()
            if isinstance(usage.get(src), int) and not isinstance(usage.get(src), bool)
        }
        is_error = bool(event.get("is_error")) or event.get("subtype") != "success"
        self._end_subagents(failed=is_error)
        if is_error:
            self._check_limit(event.get("result") if isinstance(event.get("result"), str) else None)
        elif tokens:
            self._watch_summary(tokens)
        duration = event.get("duration_ms")
        if not isinstance(duration, int) or isinstance(duration, bool):
            duration = int((time.monotonic() - started) * 1000)
        self._emit(
            {
                "type": "result",
                "subtype": "success" if not is_error else str(event.get("subtype") or "error_engine"),
                "is_error": is_error,
                "result": event.get("result") if isinstance(event.get("result"), str) else "",
                "session_id": self.session_id,
                "num_turns": max(1, len(self._model_calls)),
                "duration_ms": duration,
                "usage": tokens,
                "permission_denials": list(self._denials),
                "terminal_reason": "completed" if not is_error else "error",
                "engine": ENGINE,
            }
        )

    # --- подагенты (Ф48-1) ----------------------------------------------------------------------------------

    def _subagent_started(self, call_id: str, tool_input: dict) -> None:
        """Ф74-8: подагент Cursor (`taskToolCall` потока) — задача `local_agent` для состояния сессии (Ф48-1).
        Подагент Cursor живёт внутри хода: конец хода снимает и тех, о конце которых поток не сказал."""
        if call_id in self._subagents:
            return
        self._subagents.add(call_id)
        args = tool_input if isinstance(tool_input, dict) else {}
        kind = args.get("subagentType") or args.get("subagent_type")
        if isinstance(kind, dict):
            kind = next((v for v in kind.values() if isinstance(v, str) and v), None) or next(iter(kind), "")
        self._emit(
            {
                "type": "system",
                "subtype": "task_started",
                "task_id": call_id,
                "run_id": call_id,
                "task_type": "local_agent",
                "is_backgrounded": False,
                "description": str(args.get("description") or "")[:200],
                "subagent_type": str(kind or args.get("model") or ""),
            }
        )

    def _subagent_done(self, call_id: str, result: dict | None) -> None:
        if call_id not in self._subagents:
            return
        self._subagents.discard(call_id)
        ok = result.get("success") if isinstance(result, dict) else None
        summary = ""
        if isinstance(ok, dict):
            summary = str(ok.get("summary") or ok.get("result") or "")
        self._emit(
            {
                "type": "system",
                "subtype": "task_notification",
                "task_id": call_id,
                "status": "completed" if isinstance(ok, dict) else "failed",
                "summary": summary[:500],
            }
        )

    def _end_subagents(self, *, failed: bool) -> None:
        """Ход кончился — подагенты, о конце которых поток не сказал, закончились вместе с ним."""
        for call_id in sorted(self._subagents):
            self._subagent_done(call_id, None if failed else {"success": {}})

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
        answer = {"permission": "deny", "agent_message": PEER_DENIED}
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
                    answer = {"permission": "deny", "agent_message": "гард хаба: вход хука не разобран"}
        except Exception as exc:  # noqa: BLE001 — сбой разбора: запрет
            log.warning("Cursor: хук не обработан (%s)", exc.__class__.__name__)
            answer = {"permission": "deny", "agent_message": "гард хаба: сбой решения — запрет"}
        try:
            writer.write(json.dumps(answer, ensure_ascii=False).encode("utf-8") + b"\n")
            await writer.drain()
        except ConnectionError, OSError:
            pass
        finally:
            writer.close()

    async def _decide_hook(self, event: str, data: dict, gone: asyncio.Future) -> dict:
        answer = await self._verdict(event, data, gone)
        mid = self._take_mid(event, data)
        if mid is not None:
            if answer.get("permission") == "allow":
                answer["additional_context"] = mid
            else:
                reason = answer.get("agent_message") or ""
                answer["user_message"] = answer.get("user_message") or reason  # владельцу — только причина запрета
                answer["agent_message"] = f"{reason}\n\n{mid}".strip()
        return answer

    async def _verdict(self, event: str, data: dict, gone: asyncio.Future) -> dict:
        payload = cursor_tools.from_hook(event, data, cwd=self.spec.cwd)
        if payload.get("skip"):
            return {"permission": "allow"}
        req = HookRequest(
            event=event,
            tool=str(data.get("tool_name") or event),
            args=data.get("tool_input"),
            session_id=str(data.get("conversation_id") or self.session_id or ""),
            call_id=str(data.get("tool_use_id") or ""),
        )
        verdict = await _maybe(self.spec.decide(req, payload))
        if isinstance(verdict, Ask):
            verdict = await self._ask(req, payload, verdict, gone)
        if isinstance(verdict, Allow):
            return {"permission": "allow"}
        reason = verdict.reason if isinstance(verdict, Deny) else "гард хаба: запрет"
        self._denials.append(
            {
                "tool_name": payload.get("tool_name"),
                "tool_use_id": req.call_id,
                "tool_input": payload.get("tool_input") or {},
            }
        )
        return {"permission": "deny", "agent_message": reason}

    async def _ask(self, req: HookRequest, payload: dict, ask: Ask, gone: asyncio.Future):
        if self._aborted or self._stopping or not self.busy:
            return Deny(CANCELLED)
        self._request_n += 1
        request_id = f"cursor-{self._request_n}"
        request = {
            "subtype": "can_use_tool",
            "tool_name": payload.get("tool_name"),
            "input": payload.get("tool_input") or {},
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
            await asyncio.wait({future, gone}, return_when=asyncio.FIRST_COMPLETED)
            if not future.done():
                # Хук отвалился (Cursor снял его или погиб) — карточка больше не нужна.
                future.set_result(Deny(CANCELLED))
                self._emit({"type": "control_cancel_request", "request_id": request_id})
            verdict = future.result()
        finally:
            self._asks.pop(request_id, None)
            self._ask_reqs.pop(request_id, None)
        return verdict if isinstance(verdict, Allow | Deny) else Deny(CANCELLED)
