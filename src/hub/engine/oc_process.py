"""Процесс OpenCode с поверхностью `hub.work.session.Process` (Ф16, кусок 16-5; П12 с уточнениями круга 2).

Хост hub-work и оркестратор написаны под `claude -p` stream-json: один процесс, строки в stdin, строки из
stdout. `OcProcess` даёт ТУ ЖЕ поверхность (`start/send/control/respond/close_stdin/wait/tree/kill_tree/
aclose`, `exited`, `proc.returncode`, `pid`, `stdin_closed`) и ТУ ЖЕ форму сообщений в `on_message`, но
внутри — приватный `opencode serve` (`OcServer`) с одной сессией `ses_…` и HTTP API вместо труб:

- выдаёт (по одному читателю, в порядке событий):
  - `control_response` на `initialize` (`current_permission_mode` — `OcSpec.permission_mode`) — отдельной
    задачей ПОСЛЕ возврата `control()`, иначе хост не успел бы зарегистрировать ожидание;
  - `system/init` в начале КАЖДОГО хода до первого `assistant` (`tools` — `OcSpec.tools`, `session_id` —
    `ses_…`);
  - `assistant` (блок `text` — законченная часть текста; блок `tool_use` — вызов инструмента, имя и вход в
    формате Claude через `oc_tools.to_claude`) и `user` (`tool_result`);
  - `system/engine_error` (ошибка провайдера текстом) и `rate_limit_event` (лимит/баланс или ключ
    провайдера: `status: rejected`, `resetsAt` — сейчас + 30 мин, `engine: opencode`);
  - `result` (конец хода: `result` — текст последнего ответа, `is_error`, `subtype`, `num_turns`,
    `duration_ms`, `total_cost_usd`, `permission_denials`, после `abort` — `terminal_reason: "aborted"`);
  - `control_request` `can_use_tool` — «ask» решения (`OcSpec.ask`) и вопросы модели (`AskUserQuestion`),
    `request_id` — id запроса OpenCode; `control_cancel_request` — запрос снят «Стопом»;
- принимает: `send(user_line)` -> `prompt_async` (повтор `uuid` второй раз не шлётся; в занятую сессию —
  в очередь хода, F10); `send(control_response)` и `respond(request_id, response)` -> `once`/`reject` с
  текстом или ответ на вопрос; `control("interrupt")` -> `abort`; `close_stdin()` -> остановить сервер
  после хода (сразу, если простаивает).

Разрешения решает сам процесс: `OcSpec.decide(request, payload) -> Allow | Deny | Ask` (`payload` — вызов в
формате хука Claude, `oc_tools.to_claude`); `Ask` при `OcSpec.ask` -> `control_request`, иначе отказ. Потомки
сессии (подагенты `task`, П5) и чужие сессии различает `oc_turn._Turn` (он же — постоянный читатель потока).
`always` не отвечается никогда (F20). Неудачный `prompt_async` = процесс негоден: сервер гасится, как умерший
`claude` (хост видит выход).

Ф41-3 (`OcSpec.rich`, только постоянные сессии хаба; у прочих запусков выдача прежняя):
- текст и размышления по мере набора — `stream_event` в формате частичных сообщений Claude
  (`content_block_start` / `content_block_delta` с `text_delta` или `thinking_delta` / `content_block_stop`),
  ключ блока — поле `block` строки (`partID`); тип части — из `message.part.updated`, дельты — из
  `message.part.delta`. Тот же `block` — у итогового `assistant` с текстом;
- у `user` с `tool_result` правки — `tool_use_result` `{"filePath", "patch", "additions", "deletions"}` из
  `state.metadata.filediff` / `metadata.diff`;
- в `result` — `context_tokens` (`tokens.total` последнего завершённого сообщения ассистента),
  `context_limit` (`limit.context` модели, спрашивается раз на запуск сервера) и `usage.cache_read` /
  `usage.cache_write`.
Нет этих событий или полей (другая версия OpenCode) — выдача без них, ход не ломается.
"""

import asyncio
import contextlib
import inspect
import logging
import os
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from hub.work.session import descendants

from . import failure, oc_config, oc_tools
from .oc_server import OcClient, OcServer, SessionNotFound
from .oc_turn import (
    Allow,
    Ask,
    Deny,
    PermissionRequest,
    QuestionRequest,
    _error_of,
    _Turn,
    classify_error,
)

log = logging.getLogger(__name__)

LIMIT_PAUSE_S = 1800  # П2: лимит/баланс или ключ провайдера — пауза движка 30 мин
STALL_S = 120.0  # сессия простаивает, а ответа на реплику нет так долго — ход окончен с ошибкой
CONNECT_WAIT_S = 5.0
WATCH_S = 0.1
ERROR_TEXT_MAX = 200
QUESTION_TOOL = "AskUserQuestion"
ENGINE = "opencode"
DENIED_BY_OWNER = "владелец отказал"
PARTS_KEEP = 2000  # Ф41-3: частей текста одного хода в словаре типов (предохранитель)
CANCELLED = "запрос снят: ход прерван"


class OcStartError(Exception):
    """Сервер поднялся, но не годится (права агента, лишний MCP, сбой API) — текст для заметки/журнала."""


DecideFn = Callable[[PermissionRequest, dict], "Allow | Deny | Ask | Awaitable[Allow | Deny | Ask]"]
MessageHandler = Callable[[dict], Awaitable[None]]


@dataclass
class OcSpec:
    """Всё, что нужно процессу: сервер (бинарь, каталог, окружение, конфиг, журнал), агент и его права (для
    проверки `GET /agent`, П8), модель, системный текст, как себя назвать в `system/init` и как решать."""

    binary: str
    cwd: str
    env: dict
    config_path: Path
    log_file: Path
    agent: str
    permission: dict
    model: tuple[str, str]
    variant: str | None
    system: str | None
    title: str
    permission_mode: str
    tools: tuple[str, ...]
    decide: DecideFn
    mcp_servers: tuple[str, ...] = (oc_config.HUB_MCP,)
    # Набор подключённых MCP ровно такой (оркестратор); `None` — не проверять (рабочие сессии).
    strict_mcp: frozenset[str] | None = None
    # 16-6: имена MCP-серверов для перевода `<сервер>_<инструмент>` дополнить списком `GET /mcp` сервера
    # (рабочие сессии: MCP владельца и проекта не выключаются, их имена заранее известны не все).
    discover_mcp: bool = False
    ask: bool = False
    poll_s: float = 5.0
    turn_timeout_s: float | None = None
    retry_limit_s: float = 600.0
    ready_timeout_s: float = 20.0
    stop_grace_s: float = 5.0
    # Ф41-3: подробности потока (дельты текста, diff правок, размер контекста) — только постоянным сессиям.
    rich: bool = False
    # Ф71-3: `OPENCODE_CONFIG_DIR` сервера (сессия хаба: свой глобальный `AGENTS.md`), `None` — не задавать.
    config_dir: Path | None = None


@dataclass
class _TurnState:
    started_ms: int
    started_mono: float
    prompted: asyncio.Event = field(default_factory=asyncio.Event)
    denials: list = field(default_factory=list)
    aborted: bool = False
    forced: dict | None = None  # {"name", "message"}: таймаут хода или долгий `retry` (-> `limit`)


async def _maybe(value):
    return await value if inspect.isawaitable(value) else value


def _created(message: dict) -> int:
    info = message.get("info") if isinstance(message, dict) else None
    created = ((info or {}).get("time") or {}).get("created")
    return int(created) if isinstance(created, (int, float)) and not isinstance(created, bool) else 0


def question_input(questions: list) -> dict:
    """`QuestionInfo` OpenCode (F21) -> вход `AskUserQuestion` Claude (карточка вопроса хоста)."""
    out = []
    for q in questions if isinstance(questions, list) else []:
        if not isinstance(q, dict):
            continue
        out.append(
            {
                "question": str(q.get("question") or ""),
                "header": str(q.get("header") or ""),
                "options": [
                    {"label": str(o.get("label") or ""), "description": str(o.get("description") or "")}
                    for o in q.get("options") or []
                    if isinstance(o, dict)
                ],
                "multiSelect": bool(q.get("multiple")),
            }
        )
    return {"questions": out}


def question_answers(questions: list, answers: dict) -> list[list[str]]:
    """Ответ карточки (`{текст вопроса: ответ}`, как у Claude) -> метки на каждый вопрос OpenCode. Несколько
    меток у вопроса с `multiple` — через «, » (так их склеивает карточка)."""
    result: list[list[str]] = []
    for q in questions if isinstance(questions, list) else []:
        q = q if isinstance(q, dict) else {}
        value = answers.get(str(q.get("question") or "")) if isinstance(answers, dict) else None
        if not isinstance(value, str) or not value:
            result.append([])
            continue
        labels = {str(o.get("label")) for o in q.get("options") or [] if isinstance(o, dict)}
        pieces = [p.strip() for p in value.split(", ")]
        if q.get("multiple") and len(pieces) > 1 and all(p in labels for p in pieces):
            result.append(pieces)
        else:
            result.append([value])
    return result


def _use_result(metadata) -> dict | None:
    """Ф41-3 (2.2): diff правки из `state.metadata` части `tool` — `filediff {file, patch, additions,
    deletions}`, иначе `diff` строкой. Нет ни того, ни другого — `None`."""
    if not isinstance(metadata, dict):
        return None
    filediff = metadata.get("filediff") if isinstance(metadata.get("filediff"), dict) else {}
    patch = filediff.get("patch") if isinstance(filediff.get("patch"), str) else metadata.get("diff")
    if not isinstance(patch, str) or not patch:
        return None
    result: dict = {"patch": patch}
    if isinstance(filediff.get("file"), str):
        result["filePath"] = filediff["file"]
    for key in ("additions", "deletions"):
        if isinstance(filediff.get(key), int) and not isinstance(filediff.get(key), bool):
            result[key] = filediff[key]
    return result


def error_text(kind: str, error: dict | None, provider: str, model: str) -> str:
    """Текст ошибки хода для заметки (как у дежурной 16-3)."""
    err = error or {}
    if kind in ("limit", "retry"):
        return f"OpenCode: лимит или баланс провайдера {provider} — пауза 30 мин"
    if kind == "auth":
        return f"OpenCode: нет ключа провайдера {provider} — войдите opencode auth login"
    if kind == "model":
        return (
            f"OpenCode: модель {provider}/{model} не найдена — нет ключа провайдера {provider} "
            "(opencode auth login) или неверная модель в engine.json"
        )
    message = str(err.get("message") or "")[:ERROR_TEXT_MAX]
    return f"OpenCode: {err.get('name') or 'ошибка'}: {message}".rstrip(": ")


class OcProcess:
    """Один приватный `opencode serve` с одной сессией — вместо процесса `claude` (см. докстринг модуля)."""

    def __init__(self, spec: OcSpec, *, on_message: MessageHandler) -> None:
        self.spec = spec
        self.on_message = on_message
        self.server = OcServer(spec.binary, log_file=spec.log_file, config_dir=spec.config_dir)
        self.client: OcClient | None = None
        self.session_id: str | None = None
        self.resumed = False
        self.exited = asyncio.Event()
        self.stdin_closed = False
        self._queue: asyncio.Queue = asyncio.Queue()
        self._tasks: set[asyncio.Task] = set()
        self._reader: asyncio.Task | None = None
        self._watcher: asyncio.Task | None = None
        self._dispatcher: asyncio.Task | None = None
        self._turn: _Turn | None = None
        self._t: _TurnState | None = None
        self._turn_task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._sent: set[str] = set()
        self._compacting = False  # Ф41-4б: хост попросил сжатие, ждём `session.compacted`
        self._request_n = 0
        self._prompt_seq = 0
        self._last_prompt_ms = 0
        self._roles: dict[str, str] = {}
        self._texts: set[str] = set()
        self._tool_used: set[str] = set()
        self._tool_done: set[str] = set()
        self._payloads: dict[str, dict] = {}
        self._asks: dict[str, tuple[asyncio.Future, str, object]] = {}
        self._stopping = False
        # Ф41-3 (`rich`): части ответа, идущие по мере набора, и сведения о модели.
        self._part_kinds: dict[str, str] = {}  # partID -> "text" | "thinking"
        self._part_open: set[str] = set()
        # Ф48-1 (раздел D, `rich`): дочерние сессии (подагенты `task`), о которых сказано приёмнику, и те из
        # них, что уже закончили.
        self._kids: set[str] = set()
        self._kids_done: set[str] = set()
        self.model_info: dict | None = None  # {"context", "image"} — `OcClient.model_info`

    # --- поверхность `session.Process` ---------------------------------------------------------------------

    @property
    def proc(self):
        return self.server.proc

    @property
    def pid(self) -> int | None:
        return self.server.pid

    @property
    def busy(self) -> bool:
        return self._t is not None

    async def start(self, resume: str | None = None) -> str:
        """Поднять сервер, проверить права агента и набор MCP (П8), продолжить сессию `resume` (если она есть
        у OpenCode, F16) или создать новую. Возвращает `ses_…`. Не вышло — `EngineStartError` (сервер не
        поднялся) или `OcStartError` (не годится); сервер в обоих случаях погашен."""
        spec = self.spec
        # Ревью Ф16: агенты проекта/владельца без запросов разрешений выключаются (перезапуск сервера один раз).
        await self.server.start_locked(
            spec.cwd, spec.env, spec.config_path, ready_timeout_s=spec.ready_timeout_s
        )
        try:
            self.client = self.server.client(spec.cwd)
            problem = oc_config.check_agent(await self.client.agents(), spec.agent, spec.permission)
            if problem is not None:
                raise OcStartError(problem)
            await self._check_mcp()
            if spec.discover_mcp:
                await self._discover_mcp()
            sid = None
            if resume:
                info = await self.client.get_session(resume)
                if isinstance(info, dict) and info.get("id") == resume:
                    sid = resume
            if sid is None:
                sid = str((await self.client.create_session(spec.title, spec.agent))["id"])
        except Exception as exc:
            await self.server.stop(grace_s=0.5)
            if self.client is not None:
                await self.client.aclose()
                self.client = None
            if isinstance(exc, OcStartError):
                raise
            if isinstance(exc, (httpx.HTTPError, SessionNotFound, KeyError, TypeError, ValueError)):
                raise OcStartError(f"сбой API OpenCode ({exc.__class__.__name__})") from None
            raise
        self.session_id = sid
        self.resumed = sid == resume
        self._turn = _Turn(
            self.client,
            sid,
            decide=self._decide,
            on_ask=self._on_ask if spec.ask else None,
            on_question=self._on_question if spec.ask else None,
            on_event=self._on_event,
            poll_s=spec.poll_s,
            retry_limit_s=spec.retry_limit_s,
        )
        self._dispatcher = asyncio.create_task(self._dispatch())
        self._reader = asyncio.create_task(self._turn.read())
        self._watcher = asyncio.create_task(self._watch())
        if spec.rich:
            self._spawn(self._load_model_info())
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._turn.connected.wait(), CONNECT_WAIT_S)
        return sid

    async def _check_mcp(self) -> None:
        if self.spec.strict_mcp is None:
            return
        assert self.client is not None
        status = await self.client.mcp_status()
        connected = sorted(
            n for n, i in status.items() if isinstance(i, dict) and i.get("status") == "connected"
        )
        extra = [n for n in connected if n not in self.spec.strict_mcp]
        if extra:
            raise OcStartError(
                f"в OpenCode подключён лишний MCP {', '.join(extra)} — добавьте его в disable_mcp: "
                "hub engine setup"
            )
        missing = sorted(n for n in self.spec.strict_mcp if n not in connected)
        if missing:
            states = ", ".join(f"{n}: {(status.get(n) or {}).get('status') or 'нет'}" for n in missing)
            raise OcStartError(f"MCP хаба в OpenCode не подключился ({states})")

    async def image_input(self) -> bool:
        """Ф41-3в: принимает ли модель сессии картинки (`capabilities.input.image`). Сведения ещё не пришли —
        запросить сейчас; не вышло — «нет» (картинка ляжет файлом в папку проекта)."""
        if self.model_info is None:
            await self._load_model_info()
        return (self.model_info or {}).get("image") is True

    async def _load_model_info(self) -> None:
        """Ф41-3 (2.3): предел контекста модели — раз на запуск сервера; не вышло — `None`. Ответ сервера с
        ключом провайдера остаётся внутри `OcClient.model_info`; в журнал — только вид сбоя."""
        if self.client is None:
            return
        try:
            self.model_info = await self.client.model_info(*self.spec.model)
        except Exception as exc:  # noqa: BLE001 — сведения необязательные, запуск они не держат
            log.info("OpenCode: предел контекста модели не получен (%s)", exc.__class__.__name__)

    async def _discover_mcp(self) -> None:
        assert self.client is not None
        try:
            status = await self.client.mcp_status()
        except httpx.HTTPError, ValueError:
            return
        names = {n for n in status if isinstance(n, str) and n}
        self.spec.mcp_servers = tuple(sorted(set(self.spec.mcp_servers) | names))

    async def send(self, message: dict) -> bool:
        """Строка «stdin»: `user` -> реплика в сессию; `control_response` -> ответ на свой запрос. `False` —
        процесс закрыт или негоден (как закрытый stdin у `claude`)."""
        kind = message.get("type")
        if kind == "control_response":
            response = message.get("response") or {}
            if response.get("subtype") == "success":
                return await self.respond(str(response.get("request_id")), response.get("response") or {})
            return await self.respond(
                str(response.get("request_id")),
                {"behavior": "deny", "message": str(response.get("error") or "")},
            )
        if kind != "user":
            return False
        if self.stdin_closed or self._stopping or self.exited.is_set() or self.client is None:
            return False
        uid = message.get("uuid")
        if isinstance(uid, str) and uid in self._sent:
            return True
        content = (message.get("message") or {}).get("content")
        # Ф41-3в: части `file` (картинки реплики, data:-URL) идут в OpenCode как есть, после текста.
        parts = [
            {k: b[k] for k in ("type", "mime", "filename", "url") if k in b}
            for b in (content if isinstance(content, list) else [])
            if isinstance(b, dict) and b.get("type") == "file" and isinstance(b.get("url"), str)
        ]
        texts: list[tuple[str, bool]] | None = None
        if isinstance(content, str):
            text = content
        else:
            pieces = [b for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
            text = "\n".join(str(b.get("text") or "") for b in pieces)
            if any("synthetic" in b for b in pieces):
                # Ф86-1 (П2): справка хаба (сводка, соседи) — синтетическими частями, реплика — обычной.
                texts = [(str(b.get("text") or ""), b.get("synthetic") is True) for b in pieces]
        async with self._send_lock:
            if self.stdin_closed or self._stopping or self.exited.is_set():
                return False
            self._prompt_seq += 1
            started = int(time.time() * 1000)
            self._last_prompt_ms = started
            new = self._t is None
            if new:
                self._begin_turn(started)
            state = self._t
            try:
                await self.client.prompt(
                    self.session_id or "",
                    text=text,
                    system=self.spec.system,
                    agent=self.spec.agent,
                    model=self.spec.model,
                    variant=self.spec.variant,
                    files=parts or None,
                    synthetic=message.get("synthetic") is True,  # Ф41-4: сигнал хаба, не реплика владельца
                    **({"texts": texts} if texts is not None else {}),
                )
            except (httpx.HTTPError, SessionNotFound) as exc:
                log.warning("OpenCode: реплика не принята (%s) — сервер гасится", exc.__class__.__name__)
                self._stopping = True
                self._spawn(self.server.stop(grace_s=1.0))
                return False
            if new and state is not None:
                state.prompted.set()
            if isinstance(uid, str):
                self._sent.add(uid)
        return True

    async def compact(self) -> bool:
        """Ф41-4б: сжать разговор (`summarize` моделью сессии). Только между ходами; конец — событие
        `session.compacted`: в поток идут `system/compact_boundary` и `result` с `num_turns: 0` (как после
        `/compact` у Claude Code). Запрос уходит отдельной задачей (сервер может держать ответ до конца
        сжатия); его отказ — `result` с `is_error` без границы. `False` — ход идёт или процесс закрыт."""
        if self.stdin_closed or self._stopping or self.exited.is_set() or self.client is None:
            return False
        if self._t is not None or not self.spec.model or self._compacting:
            return False
        self._compacting = True
        self._spawn(self._summarize())
        return True

    def compact_abandon(self) -> None:
        """Ревью 41-4 (б): хост перестал ждать сжатие (срок) — поздний исход запроса `summarize` в поток не
        идёт (его `result` закрыл бы чужой ход), следующее сжатие можно просить заново."""
        self._compacting = False

    async def _summarize(self) -> None:
        assert self.client is not None
        try:
            await self.client.summarize(self.session_id or "", self.spec.model)
        except (httpx.HTTPError, SessionNotFound) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            log.warning("OpenCode: сжатие не принято (%s, HTTP %s)", exc.__class__.__name__, status)
            if self._compacting:
                self._compacting = False
                self._emit(
                    {
                        "type": "result",
                        "subtype": "error_engine",
                        "is_error": True,
                        "num_turns": 0,
                        "session_id": self.session_id,
                        "engine": ENGINE,
                    }
                )

    async def control(self, subtype: str, **fields) -> str:
        """`initialize` — ответ отдельной задачей (после возврата, П12); `interrupt` — `abort` хода."""
        self._request_n += 1
        request_id = f"hub-{self._request_n}"
        if subtype == "initialize":
            self._response(request_id, {"current_permission_mode": self.spec.permission_mode})
        elif subtype == "interrupt":
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

    async def respond(self, request_id: str, response: dict) -> bool:
        """Решение по `control_request` `can_use_tool`: `{"behavior": "allow", "updatedInput": …}` или
        `{"behavior": "deny", "message": …}`. `False` — запроса уже нет."""
        entry = self._asks.get(request_id)
        if entry is None or self.exited.is_set():
            return False
        future, kind, extra = entry
        if future.done():
            return False
        allow = response.get("behavior") == "allow"
        if kind == "question":
            updated = response.get("updatedInput") if isinstance(response.get("updatedInput"), dict) else {}
            if allow:
                future.set_result(question_answers(extra, updated.get("answers") or {}))
            else:
                # Ф41-5: после `/question/{id}/reject` настоящий сервер ход не продолжает и не заканчивает
                # (стенд 5а/5б) — отказ уходит свободным ответом с тем же текстом на каждый вопрос.
                text = str(response.get("message") or DENIED_BY_OWNER)
                count = len(extra) if isinstance(extra, list) and extra else 1
                future.set_result([[text] for _ in range(count)])
        else:
            future.set_result(Allow() if allow else Deny(str(response.get("message") or DENIED_BY_OWNER)))
        return True

    async def close_stdin(self) -> None:
        """Как закрытие stdin у `claude`: идущий ход дорабатывает, затем сервер гасится; простаивает —
        сразу."""
        if self.stdin_closed:
            return
        self.stdin_closed = True
        if self._t is None:
            self._shutdown()

    async def wait(self, limit_s: float | None = None) -> bool:
        try:
            await asyncio.wait_for(self.exited.wait(), limit_s)
        except TimeoutError:
            return False
        return True

    def tree(self) -> list[int]:
        pid = self.server.pid
        if pid is None or not self.server.alive():
            return []
        return [pid, *descendants(pid)]

    async def kill_tree(self, snapshot: list[int] | None = None, grace_s: float = 10.0) -> None:
        pid = self.server.pid
        self._stopping = True
        await self.server.stop(grace_s=grace_s)
        for p in snapshot or []:
            if p != pid:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(p, signal.SIGKILL)
        await self.wait(5)

    async def aclose(self) -> None:
        for task in [self._reader, self._watcher, self._turn_task, self._dispatcher, *self._tasks]:
            if task is not None and not task.done() and task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        if self._turn is not None:
            await self._turn.cancel()
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    # --- выдача сообщений ----------------------------------------------------------------------------------

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
        """Один «читатель stdout»: `on_message` по одному сообщению за раз, в порядке выдачи."""
        while True:
            message = await self._queue.get()
            if message is None:
                break
            try:
                await self.on_message(message)
            except Exception:  # noqa: BLE001 — сбой разбора одного сообщения не роняет читателя
                log.exception("OpenCode: сообщение не обработано")
        self.exited.set()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _shutdown(self) -> None:
        if not self._stopping:
            self._stopping = True
            self._spawn(self.server.stop(grace_s=self.spec.stop_grace_s))

    async def _watch(self) -> None:
        """Сервер вышел (погашен или упал): снять ход и ожидания, дочитать выдачу, `exited`."""
        while self.server.alive():  # noqa: ASYNC110 — выход чужого процесса (Popen), события нет
            await asyncio.sleep(WATCH_S)
        for task in (self._reader, self._turn_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        for future, kind, _extra in list(self._asks.values()):
            if not future.done():
                future.set_result(None if kind == "question" else Deny(CANCELLED))
        # Группа сервера (MCP-дети) — добить, если сервер упал сам (F18: killpg их гасит).
        with contextlib.suppress(Exception):
            await self.server.stop(grace_s=0.2)
        self._queue.put_nowait(None)

    # --- ход ----------------------------------------------------------------------------------------------

    def _begin_turn(self, started_ms: int) -> None:
        """Начало хода: `system/init` до первого `assistant` (П12) и драйвер конца хода."""
        state = _TurnState(started_ms=started_ms, started_mono=time.monotonic())
        self._t = state
        if self._turn is not None:
            self._turn.error = None
            self._turn.idle.clear()
            self._turn.retry_since = None
        self._emit(
            {
                "type": "system",
                "subtype": "init",
                "session_id": self.session_id,
                "cwd": self.spec.cwd,
                "tools": list(self.spec.tools),
                "permissionMode": self.spec.permission_mode,
                "model": f"{self.spec.model[0]}/{self.spec.model[1]}",
                "engine": ENGINE,
            }
        )
        self._turn_task = asyncio.create_task(self._run_turn(state))

    async def _interrupt(self) -> None:
        state = self._t
        if state is None or self.client is None:
            return
        state.aborted = True
        self._cancel_asks()
        with contextlib.suppress(httpx.HTTPError, SessionNotFound):
            await self.client.abort(self.session_id or "")

    def _cancel_asks(self) -> None:
        for request_id, (future, kind, _extra) in list(self._asks.items()):
            if future.done():
                continue
            future.set_result(None if kind == "question" else Deny(CANCELLED))
            self._emit({"type": "control_cancel_request", "request_id": request_id})

    async def _run_turn(self, state: _TurnState) -> None:
        turn = self._turn
        assert turn is not None and self.client is not None
        await state.prompted.wait()
        spec = self.spec
        deadline = None if spec.turn_timeout_s is None else time.monotonic() + spec.turn_timeout_s
        while True:
            waiter = asyncio.ensure_future(self._first(turn.idle, turn.check_now))
            try:
                await asyncio.wait_for(waiter, spec.poll_s)
            except TimeoutError:
                pass
            turn.idle.clear()
            turn.check_now.clear()
            if state.forced is None:
                retry = turn.retry_s()
                if retry is not None and retry >= spec.retry_limit_s:
                    state.forced = {"name": "retry", "message": turn.last_retry or "", "limit": True}
                elif deadline is not None and time.monotonic() >= deadline:
                    state.forced = {"name": "timeout", "message": f"ход дольше {spec.turn_timeout_s:g} с"}
                if state.forced is not None:
                    with contextlib.suppress(httpx.HTTPError, SessionNotFound):
                        await self.client.abort(self.session_id or "")
            seq = self._prompt_seq
            finished = await self._check_finish(state)
            if finished is None:
                continue
            messages, error = finished
            self._end_turn(state, messages, error, seq)
            return

    @staticmethod
    async def _first(*events: asyncio.Event) -> None:
        tasks = [asyncio.ensure_future(e.wait()) for e in events]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()

    async def _check_finish(self, state: _TurnState) -> tuple[list, dict | None] | None:
        """Ход окончен? Сессия простаивает И последняя запись — законченный ответ после реплики (или ошибка).
        Последняя запись — реплика (сессия ещё не взялась за неё, F10) — ждать."""
        assert self.client is not None and self._turn is not None
        try:
            status = await self.client.status_info(self.session_id or "")
        except httpx.HTTPError, SessionNotFound:
            return None
        self._turn._note_status(status)  # noqa: SLF001 — тот же учёт `retry`, что у драйвера хода
        if status.get("type") != "idle":
            return None
        try:
            messages = await self.client.messages(self.session_id or "")
        except httpx.HTTPError, SessionNotFound:
            return None
        turn_messages = [m for m in messages if _created(m) >= state.started_ms]
        error = self._turn.error
        last = messages[-1] if messages else None
        info = (last or {}).get("info") if isinstance(last, dict) else None
        info = info if isinstance(info, dict) else {}
        stalled = time.monotonic() - state.started_mono >= STALL_S
        if info.get("role") != "assistant" or _created(last) < state.started_ms:
            if error is not None:
                return turn_messages, error
            if state.forced is not None:
                return turn_messages, None
            if stalled:
                return turn_messages, {"name": "stalled", "message": "сессия простаивает, ответа нет"}
            return None
        error = error or _error_of(info.get("error"))
        if error is None and state.forced is None:
            completed = (info.get("time") or {}).get("completed")
            if not completed or info.get("finish") == "tool-calls":
                return None
        return turn_messages, error

    def _end_turn(self, state: _TurnState, messages: list, error: dict | None, seq: int) -> None:
        """Сверка выданного с записями хода, ошибка, `result`; реплика, пришедшая после сверки, — новый
        ход."""
        assistants = [m for m in messages if ((m.get("info") or {}).get("role")) == "assistant"]
        for message in assistants:
            for part in message.get("parts") or []:
                if isinstance(part, dict):
                    self._emit_part(part)
        aborted = state.aborted
        if state.forced is not None:
            error = {k: v for k, v in state.forced.items() if k in ("name", "message")}
            aborted = False
        elif error is not None and error.get("name") == "MessageAbortedError":
            aborted = True
        if aborted:
            error = None
        provider, model = self.spec.model
        if error is not None:
            limit = bool((state.forced or {}).get("limit"))
            retry_s = self._turn.retry_s() if self._turn else None
            kind = "limit" if limit else classify_error(error, retry_s)
            text = error_text(kind, error, provider, model)
            log.warning("OpenCode: ход с ошибкой (%s, %s)", kind, error.get("name"))
            self._emit({"type": "system", "subtype": "engine_error", "kind": kind, "text": text})
            if kind in ("limit", "auth", "retry"):
                # Ф83-1: `kind` — причина паузы для хоста (`failure.classify`: лимит, баланс, вход, долгий
                # retry); пауза и её текст — как были.
                cause = "limit" if limit else failure.classify(ENGINE, error, retry_s=retry_s)
                self._emit(
                    {
                        "type": "rate_limit_event",
                        "rate_limit_info": {
                            "status": "rejected",
                            "resetsAt": int(time.time()) + LIMIT_PAUSE_S,
                            "engine": ENGINE,
                            # Пауза — на провайдера модели (ключ `opencode:<провайдер>`): лимит или ключ
                            # одного провайдера не держит сессии на другом.
                            "provider": provider,
                            "reason": text,
                            "kind": cause,
                        },
                    }
                )
        last = assistants[-1] if assistants else None
        texts = [
            p.get("text")
            for p in (last or {}).get("parts") or []
            if isinstance(p, dict)
            and p.get("type") == "text"
            and isinstance(p.get("text"), str)
            and p["text"]
        ]
        cost = None
        tokens: dict = {}
        for message in assistants:
            info = message.get("info") or {}
            if isinstance(info.get("cost"), (int, float)) and not isinstance(info.get("cost"), bool):
                cost = (cost or 0.0) + float(info["cost"])
            for key, value in (info.get("tokens") or {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    tokens[key] = tokens.get(key, 0) + value
        ok = error is None and not aborted
        result = {
            "type": "result",
            "subtype": "success" if ok else ("error_during_execution" if aborted else "error_engine"),
            "is_error": not ok,
            "result": texts[-1] if texts else "",
            "session_id": self.session_id,
            "num_turns": len(assistants),
            "duration_ms": int((time.monotonic() - state.started_mono) * 1000),
            "total_cost_usd": cost,
            "usage": tokens,
            "permission_denials": list(state.denials),
            "terminal_reason": "aborted" if aborted else ("completed" if ok else "error"),
            "engine": ENGINE,
        }
        if self.spec.rich:
            self._rich_result(result, assistants)
        self._emit(result)
        self._t = None
        self._turn_task = None
        if self._prompt_seq != seq and not self._stopping:
            # Реплика ушла, пока сверялись записи: её ход ещё впереди — новый ход с той же отметки времени.
            self._begin_turn(self._last_prompt_ms)
            if self._t is not None:
                self._t.prompted.set()
        elif self.stdin_closed:
            self._shutdown()

    def _rich_result(self, result: dict, assistants: list) -> None:
        """Ф41-3 (2.3): размер контекста — `tokens.total` последнего завершённого сообщения ассистента в
        ходе; предел — из сведений о модели; чтение и запись кэша — в `usage`."""
        context = None
        cache_read = cache_write = None
        for message in assistants:
            info = message.get("info") or {}
            tokens = info.get("tokens") if isinstance(info.get("tokens"), dict) else {}
            total = tokens.get("total")
            completed = (info.get("time") or {}).get("completed")
            if completed and isinstance(total, int | float) and not isinstance(total, bool) and total > 0:
                context = int(total)
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            for key in ("read", "write"):
                value = cache.get(key)
                if isinstance(value, int | float) and not isinstance(value, bool):
                    if key == "read":
                        cache_read = (cache_read or 0) + value
                    else:
                        cache_write = (cache_write or 0) + value
        usage = dict(result.get("usage") or {})
        if cache_read is not None:
            usage["cache_read"] = cache_read
        if cache_write is not None:
            usage["cache_write"] = cache_write
        result["usage"] = usage
        result["context_tokens"] = context
        result["context_limit"] = (self.model_info or {}).get("context")
        self._part_kinds.clear()
        self._part_open.clear()

    # --- события потока ------------------------------------------------------------------------------------

    def _stream(self, block: str, event: dict) -> None:
        self._emit({"type": "stream_event", "block": block, "session_id": self.session_id, "event": event})

    def _on_delta(self, props: dict) -> None:
        """Ф41-3 (3.2): `message.part.delta {partID, field: "text", delta}` — добавка к тексту части, тип
        которой известен из предшествующего `message.part.updated`."""
        pid = props.get("partID")
        delta = props.get("delta")
        if props.get("sessionID") != self.session_id or props.get("field") != "text":
            return
        if not isinstance(pid, str) or pid not in self._part_open or not isinstance(delta, str) or not delta:
            return
        kind = self._part_kinds[pid]
        body = {"type": "thinking_delta", "thinking": delta} if kind == "thinking" else {"type": "text_delta"}
        if kind != "thinking":
            body["text"] = delta
        self._stream(pid, {"type": "content_block_delta", "delta": body})

    def _on_stream_part(self, part: dict) -> None:
        """Ф41-3 (3.2): часть `text` / `reasoning` ответа — начало блока при первом появлении, конец — когда
        у части есть `time.end`."""
        pid = part.get("id")
        kind = {"text": "text", "reasoning": "thinking"}.get(part.get("type"))
        if kind is None or not isinstance(pid, str) or part.get("synthetic"):
            return
        timing = part.get("time") if isinstance(part.get("time"), dict) else {}
        if pid not in self._part_kinds:
            if len(self._part_kinds) >= PARTS_KEEP:
                return
            self._part_kinds[pid] = kind
            if timing.get("end") or not timing:
                return  # часть пришла сразу законченной — набирать нечего
            self._part_open.add(pid)
            self._stream(pid, {"type": "content_block_start", "content_block": {"type": kind}})
        if timing.get("end") and pid in self._part_open:
            self._part_open.discard(pid)
            self._stream(pid, {"type": "content_block_stop"})

    async def _on_event(self, event: dict) -> None:
        etype = event.get("type")
        props = event.get("properties") if isinstance(event.get("properties"), dict) else {}
        if self.spec.rich and etype in ("session.created", "session.idle", "session.status"):
            self._on_kid(etype, props)
        if etype == "session.compacted":
            if self._compacting and props.get("sessionID") == self.session_id:
                self._compacting = False
                base = {"session_id": self.session_id, "engine": ENGINE}
                self._emit({"type": "system", "subtype": "compact_boundary", **base})
                done = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 0}
                self._emit({**done, **base})
            elif props.get("sessionID") == self.session_id:
                # Ф86-1 (П7): OpenCode сжал разговор сам (без просьбы хоста) — граница раннеру (сводка дня
                # пойдёт первой подачей); `result` не выдаётся: ход, если идёт, продолжается.
                base = {"session_id": self.session_id, "engine": ENGINE, "self_compacted": True}
                self._emit({"type": "system", "subtype": "compact_boundary", **base})
            return
        if etype == "message.part.delta":
            if self.spec.rich and self._t is not None:
                self._on_delta(props)
            return
        if etype == "message.updated":
            info = props.get("info") if isinstance(props.get("info"), dict) else {}
            if isinstance(info.get("id"), str) and isinstance(info.get("role"), str):
                self._roles[info["id"]] = info["role"]
            return
        if etype != "message.part.updated" or self._t is None:
            return
        part = props.get("part") if isinstance(props.get("part"), dict) else None
        if part is None or part.get("sessionID") != self.session_id:
            return  # подагенты (потомки) в поток хода не выдаются — их разрешения решает `_Turn`
        mid = part.get("messageID")
        if not isinstance(mid, str):
            return
        role = self._roles.get(mid)
        if role is None and self.client is not None:
            with contextlib.suppress(httpx.HTTPError, SessionNotFound, ValueError):
                info = (await self.client.message(self.session_id or "", mid)).get("info") or {}
                if isinstance(info.get("role"), str):
                    role = self._roles[mid] = info["role"]
        if role == "assistant":
            if self.spec.rich:
                self._on_stream_part(part)
            self._emit_part(part, final=False)

    def _on_kid(self, etype: str, props: dict) -> None:
        """Ф48-1 (раздел D): дочерняя сессия (подагент `task`) — приёмнику теми же событиями, что фоновые
        задачи Claude Code: `task_started` при создании, `task_updated` — когда она простаивает. Сюда
        доходят только события своей сессии и её потомков (`_Turn.mine`)."""
        base = {"type": "system", "session_id": self.session_id, "engine": ENGINE}
        if etype == "session.created":
            info = props.get("info") if isinstance(props.get("info"), dict) else {}
            kid, parent = info.get("id"), info.get("parentID")
            if not isinstance(kid, str) or not kid or kid == self.session_id or kid in self._kids:
                return
            if not isinstance(parent, str) or not parent:
                return
            self._kids.add(kid)
            self._emit(
                {
                    **base,
                    "subtype": "task_started",
                    "task_id": kid,
                    "run_id": kid,
                    "task_type": "local_agent",
                    "is_backgrounded": False,
                    "description": info.get("title") if isinstance(info.get("title"), str) else "",
                    "parent_task_id": parent if parent != self.session_id else None,
                }
            )
            return
        kid = props.get("sessionID")
        if kid not in self._kids or kid in self._kids_done:
            return
        status = props.get("status") if isinstance(props.get("status"), dict) else {}
        if etype == "session.idle" or status.get("type") == "idle":
            self._kids_done.add(kid)
            patch = {"status": "completed"}
            self._emit({**base, "subtype": "task_updated", "task_id": kid, "run_id": kid, "patch": patch})

    def _emit_part(self, part: dict, *, final: bool = True) -> None:
        ptype = part.get("type")
        if ptype == "text":
            pid = part.get("id")
            text = part.get("text")
            timing = part.get("time") if isinstance(part.get("time"), dict) else None
            done = final or timing is None or bool(timing.get("end"))
            if not isinstance(pid, str) or pid in self._texts or not done or part.get("synthetic"):
                return
            if not isinstance(text, str) or not text.strip():
                return
            self._texts.add(pid)
            self._assistant({"type": "text", "text": text}, part_id=pid)
        elif ptype == "tool":
            call_id = part.get("callID")
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            status = state.get("status")
            if not isinstance(call_id, str) or status not in ("running", "completed", "error"):
                return
            if call_id not in self._tool_used:
                self._tool_used.add(call_id)
                payload = oc_tools.to_claude(
                    str(part.get("tool") or "?"), state.get("input"), mcp_servers=self.spec.mcp_servers
                )
                self._assistant(
                    {
                        "type": "tool_use",
                        "id": call_id,
                        "name": payload["tool_name"],
                        "input": payload["tool_input"],
                    }
                )
            if status in ("completed", "error") and call_id not in self._tool_done:
                self._tool_done.add(call_id)
                is_error = status == "error"
                content = state.get("error") if is_error else state.get("output")
                message = {
                    "type": "user",
                    "session_id": self.session_id,
                    "parent_tool_use_id": None,
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": call_id,
                                "content": str(content or ""),
                                "is_error": is_error,
                            }
                        ],
                    },
                }
                if self.spec.rich and not is_error:
                    use_result = _use_result(state.get("metadata"))
                    if use_result is not None:
                        message["tool_use_result"] = use_result
                self._emit(message)

    def _assistant(self, block: dict, *, part_id: str | None = None) -> None:
        message = {
            "type": "assistant",
            "session_id": self.session_id,
            "parent_tool_use_id": None,
            "message": {"role": "assistant", "content": [block]},
        }
        if self.spec.rich and part_id is not None:
            message["block"] = part_id  # Ф41-3: тот же ключ, что шёл в `stream_event` этой части
        self._emit(message)

    # --- решения -------------------------------------------------------------------------------------------

    async def _decide(self, req: PermissionRequest) -> Allow | Deny | Ask:
        if req.args is None:
            payload = {"tool_name": req.tool, "tool_input": {}, "unparsed": True, "no_args": True}
        else:
            payload = oc_tools.to_claude(req.tool, req.args, mcp_servers=self.spec.mcp_servers)
        self._payloads[req.id] = payload
        verdict = await _maybe(self.spec.decide(req, payload))
        if isinstance(verdict, Deny):
            self._denied(req, payload)
        return verdict

    def _denied(self, req: PermissionRequest, payload: dict) -> None:
        if self._t is not None:
            self._t.denials.append(
                {
                    "tool_name": payload.get("tool_name"),
                    "tool_use_id": req.call_id,
                    "tool_input": payload.get("tool_input") or {},
                }
            )

    async def _ask_card(self, request_id: str, kind: str, extra, request: dict):
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._asks[request_id] = (future, kind, extra)
        self._emit({"type": "control_request", "request_id": request_id, "request": request})
        try:
            return await future
        finally:
            self._asks.pop(request_id, None)

    async def _on_ask(self, req: PermissionRequest, extra: dict) -> Allow | Deny:
        """«ask» решения -> карточка хоста (`can_use_tool`); ответ владельца — через `respond`."""
        payload = self._payloads.pop(req.id, None) or {"tool_name": req.tool, "tool_input": req.args or {}}
        request = {
            "subtype": "can_use_tool",
            "tool_name": payload.get("tool_name"),
            "input": payload.get("tool_input") or {},
            "tool_use_id": req.call_id,
        }
        reason = (extra or {}).get("reason") if isinstance(extra, dict) else None
        if isinstance(reason, str) and reason:
            request["decision_reason"] = reason
        rule = (extra or {}).get("rule") if isinstance(extra, dict) else None
        if isinstance(rule, str) and rule:
            request["hub_rule"] = rule  # Ф41-5 П3а: ключ выключателя автопилота — прямо, без меток
        if req.child:
            request["agent_id"] = req.session_id
        # Ф99-7.1 (П10): вызов и его разбор живут при карточке, пока она ждёт: снятая галочка «Автопилота»
        # прогоняет решение хоста заново (`ask_source`).
        verdict = await self._ask_card(req.id, "permission", (req, payload), request)
        if not isinstance(verdict, Allow | Deny):
            verdict = Deny(CANCELLED)
        if isinstance(verdict, Deny):
            self._denied(req, payload)
        return verdict

    def ask_source(self, request_id: str):
        """Ф99-7.1: `(запрос разрешения, разбор вызова)` ждущей карточки разрешения; карточки нет, она уже
        решена или это вопрос — `None`."""
        entry = self._asks.get(request_id)
        if entry is None or self.exited.is_set():
            return None
        future, kind, extra = entry
        if future.done() or kind != "permission" or not isinstance(extra, tuple):
            return None
        return extra

    async def _on_question(self, req: QuestionRequest) -> list[list[str]] | None:
        """Вопрос модели (F21) -> карточка `AskUserQuestion`; ответ владельца -> метки, отказ -> `None`."""
        request = {
            "subtype": "can_use_tool",
            "tool_name": QUESTION_TOOL,
            "input": question_input(req.questions),
            "tool_use_id": req.id,
        }
        return await self._ask_card(req.id, "question", req.questions, request)
