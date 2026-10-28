"""Общая часть раннеров хоста рабочих сессий (Ф41-2б, 4.1 + П5): процесс движка (`claude -p` stream-json или
`OcProcess`), чтение потока, запросы разрешений и вопросы, доставка решений, решение вызовов OpenCode
(`_oc_decide`), ожидание `initialize`, выход процесса.

Наследник — раннер постоянной сессии хаба (`held.HeldRunner`; раннер задачи по «Го» `host.Runner` убран в
Ф78-1). «Куда писать события и запросы, что отмечать в ядре» — ПРИЁМНИК: методы ниже с `NotImplementedError`.
Базовый класс сам в ядро не ходит и не знает про сессии хаба.

Приёмник раннера (П5):
- `event(kind, summary, *, tool, is_error)` — событие в ленту (буфер хоста);
- `flush()` — отправить накопленные события ЭТОГО раннера (перед запросом: в ленте вызов раньше запроса);
- `post_request(body) -> {"id", "decided"?}` — записать запрос разрешения или вопрос в ядро (Ф66-1, П3:
  `decided: "allow"` — «сразу да», ядро записало разрешение решённым и доставленным);
- `expire(core_id)`, `delivered(core_id)` — запрос снят / решение доставлено процессу;
- `request_notice(entry) -> (заголовок, текст)` — текст уведомления владельцу о карточке;
- `turn_began()` — ход начался (состояние «в работе»); `_on_result(msg)` — ход окончен;
- `on_init(msg)` — `system/init` хода прошёл проверку режима;
- `launch_failed(resume=, why=)` — процесс не запустился;
- `process_exited(process)` — процесс вышел (зовёт `_watch_exit`);
- `fail(why)` — процесс не годится (режим разрешений);
- Ф87-1: `claude_ai_expected()`, `claude_ai_seen()`, `claude_ai_missing()` — коннекторы claude.ai до первой
  подачи (по умолчанию не ожидаются).

Ф41-3 (docs/architecture.md): раннер с `rich = True` (только постоянная сессия)
дополнительно разбирает подробности — `brief`/`detail` событий, `ref` у текста, `stream_event` (текст по мере
набора -> `live()`), расход хода. При `rich = False` строки событий и вызовы приёмника прежние,
`stream_event` пропускается. Нет нужного поля в потоке движка — подробностей нет, ход идёт (П11)."""

import asyncio
import contextlib
import contextvars
import logging
import uuid

from hub.agent.core_api import CoreError, CoreUnauthorized, CoreUnavailable
from hub.engine import oc_config, oc_tools
from hub.engine import registry as engine_registry
from hub.engine.oc_process import OcProcess
from hub.engine.oc_turn import Allow, Ask, Deny
from hub.mcp_server import format as fmt

from . import always as always_mod
from . import files, guard, policy, session

_logger = logging.getLogger("hub.work")

INIT_TIMEOUT_S = 60.0
# Claude Code в `-p` подключает MCP-серверы в фоне и первую реплику не задерживает: ход, начатый через секунду
# после запуска, идёт без инструментов удалённых серверов (12.10.2026: программист Initech после подъёма из сна
# остался без Jira). Проба `.runs/mcp-probe/probe.py` (2.1.295): `mcp_status` отвечает сразу, все серверы
# выходят из `pending` за ~5 с. Поэтому до первой подачи хост ждёт, пока `pending` не останется, но не дольше
# `MCP_WAIT_S`; нет ответа на `mcp_status` (другая версия движка) — не ждёт вовсе.
MCP_WAIT_S = 30.0
MCP_POLL_S = 0.5
MCP_STATUS_TIMEOUT_S = 5.0
# Ф87-1 (docs/architecture.md): коннекторы claude.ai (серверы `claude.ai <Имя>`)
# попадают в `mcp_status` на ~1–2 с позже локальных и все разом, с любым статусом; не прошёл запрос их списка
# к claude.ai — нет ни одного такого имени до конца процесса (22.10.2026: программист Globex без YouTrack и
# GitLab). Если хост их уже видел (метка «ожидаются»), до первой подачи ждать хоть одно такое имя, но не
# дольше `CLAUDE_AI_WAIT_S` от начала ожидания; общий потолок подачи — max(MCP_WAIT_S, CLAUDE_AI_WAIT_S).
CLAUDE_AI_WAIT_S = 20.0
CLAUDE_AI_PREFIX = "claude.ai "
STOP_GRACE_S = 10.0
EVENT_SUMMARY_MAX = 1000
LIVE_TEXT_MAX = 8000  # предел текста блока в `live` (ядро: `live.TEXT_MAX`)
LIVE_KINDS = {"text": "text", "thinking": "thinking", "reasoning": "thinking"}
CALLS_KEEP = 500
# Ф56-1в (2.4; проба 13.10, раздел 9 плана Ф56): таймер движка — вызов этого инструмента виден в `assistant`
# до выполнения; после конца хода движок сам начнёт ход в срок.
WAKEUP_TOOL = "ScheduleWakeup"
INTERRUPTED_TOOL = "инструмент прерван"
BYPASS = "bypassPermissions"
QUESTION_TOOL = "AskUserQuestion"
OC_SUBAGENTS = (
    oc_config.WORK_SUBAGENTS
)  # П5 (Ф16): подагенты — модель роли `explore`, каждый вызов через хост
ENGINE_NAMES = engine_registry.LABELS  # Ф74-1: подписи — из профиля движка
# Ф99-7.1 (П10): повторное решение ждущей карточки (снятая галочка «Автопилота») — без журнала гарда и событий
# ленты: идёт в потоке (`asyncio.to_thread`), а его вердикт — только «закрыть ли карточку».
REDECIDING: contextvars.ContextVar[bool] = contextvars.ContextVar("hub_work_redeciding", default=False)


def redecide_quietly(decide, req, payload):
    """Тело потока повторного решения: флаг ставится в копии контекста этого вызова (`to_thread`)."""
    REDECIDING.set(True)
    return decide(req, payload)


def turn_summary(msg: dict) -> str:
    """Строка события конца хода по `result` потока (общая для задач и постоянных сессий)."""
    denials = msg.get("permission_denials")
    n_denials = len(denials) if isinstance(denials, list) else 0
    summary = (
        f"ход окончен: шагов {msg.get('num_turns')}, {int(msg.get('duration_ms') or 0) / 1000:.0f} с, "
        f"отказов {n_denials}"
    )
    cost = msg.get("total_cost_usd")
    if msg.get("engine") == "opencode" and isinstance(cost, int | float) and not isinstance(cost, bool):
        summary += f", ${cost:.4f}"  # Ф16: деньги провайдера (у Claude — подписка, цену не пишем)
    if msg.get("is_error"):
        summary += f" (ошибка: {fmt.one_line(msg.get('subtype'), 60)})"
    return summary


def denied_text(answer: dict) -> str:
    """Текст отказа: слова владельца, а без них «Владелец отказал». Ф66-1 (C.4): кто решил на самом деле (владелец, его
    агент, «сразу да», `answer.by` прежних записей) сессия не узнаёт — текст всегда от владельца."""
    return answer.get("message") or "владелец отказал"


class BaseRunner:
    """Один процесс движка и его разговор с хостом. Поля состояния заводит `_init_base`."""

    # Ф41-5: снятые выключатели автопилота (только сессия хаба в авто; в ручном режиме — всегда пусто).
    auto_free: frozenset[str] = frozenset()
    # Ф99-7.2 (П11): «Всегда в этой сессии» — ключи `always.MCP`/`always.DIR` (в любом режиме).
    always: frozenset[str] = frozenset()
    # Ф41-3: подробности событий, текст по мере набора и расход — только у постоянной сессии.
    rich = False
    # Ф50-7 (Р5): настоящий путь папки процесса, когда она не папка проекта (сессия хаба с подпапкой; ставит
    # `HeldRunner.launch`), он же `home_dir` гарда. У сессии без подпапки — `None`.
    home_dir: str | None = None

    @property
    def cwd(self) -> str:
        """Папка процесса: подпапка сессии или папка проекта."""
        return self.home_dir or self.workdir

    def _init_base(self, host, *, workdir: str, engine: str) -> None:
        self.host = host
        self.workdir: str = workdir
        self.engine: str = engine
        self._oc_settings: dict = {}
        self.process: session.Process | OcProcess | None = None
        self.busy = False
        self.turn_started: float | None = None
        self.request_wait_ms = 0  # П11: время висящих запросов в текущем ходе
        # request_id CLI -> {"core_id", "tool", "input", "tool_use_id", "kind", "reason", "since"}
        self.pending: dict[str, dict] = {}
        self.by_core: dict[int, str] = {}
        self.control_waiters: dict[str, asyncio.Future] = {}
        self.last_activity = host.monotonic()
        self.reason: str | None = None
        self.snapshot: list[int] = []
        self.dead = False
        # Ф11-6 С2б: ответ `initialize` ждёт отдельная задача; до него процессу ничего не пишется.
        self.initialized = False
        self.first_text: str | None = None
        # Ф41-3 (только `rich`): вызовы инструментов хода, блоки потока и расход процесса.
        self._calls: dict[str, tuple[str, object]] = {}  # tool_use_id -> (имя, вход)
        self._stream_mid: str | None = None  # id сообщения из `message_start`
        self._stream_index: dict[int, str] = {}  # номер блока сообщения -> ключ блока
        self._blocks: dict[str, dict] = {}  # ключ блока -> {"kind", "text", "cut", "sent"}
        self._text_refs: dict[str, list[str]] = {}  # id сообщения -> ключи его текстовых блоков по порядку
        self._delta_usage: dict | None = None  # `usage` последнего `message_delta`
        self._cost_total: float | None = None  # `total_cost_usd` предыдущего `result` этого процесса
        self._model: str | None = None  # модель из `system/init`
        # Ф56-1б (3.2, только `rich`): тексты модели хода после последнего вызова инструмента — последний
        # ответ хода для карточки `turn`. Вызов инструмента и начало хода список очищают; шаги подагентов —
        # мимо.
        self.turn_texts: list[str] = []

    @property
    def alive(self) -> bool:
        return self.process is not None and not self.process.exited.is_set()

    # --- приёмник (П5) --------------------------------------------------------------------------------------

    def event(self, kind: str, summary: str, *, tool: str | None = None, is_error: bool = False) -> None:
        raise NotImplementedError

    async def flush(self) -> None:
        raise NotImplementedError

    async def post_request(self, body: dict) -> dict:
        raise NotImplementedError

    async def expire(self, core_id: int) -> None:
        raise NotImplementedError

    async def delivered(self, core_id: int) -> None:
        raise NotImplementedError

    def request_notice(self, entry: dict) -> tuple[str, str]:
        raise NotImplementedError

    def request_posted(self, entry: dict) -> None:
        """Карточка записана в ядро — уведомить владельца (приёмник может промолчать: день проекта закрыт)."""
        title, text = self.request_notice(entry)
        self.host.notify(title, text, "critical")

    def request_label(self) -> str:
        """Как назвать раннер в журнале хоста (без текстов)."""
        raise NotImplementedError

    async def turn_began(self) -> None:
        raise NotImplementedError

    async def _on_result(self, msg: dict) -> None:
        raise NotImplementedError

    async def on_init(self, msg: dict) -> None:
        raise NotImplementedError

    async def launch_failed(self, *, resume: bool, why: str) -> None:
        raise NotImplementedError

    async def process_exited(self, process) -> None:
        raise NotImplementedError

    async def fail(self, why: str) -> None:
        raise NotImplementedError

    def claude_ai_expected(self) -> bool:
        """Ф87-1 (п. 1): коннекторы claude.ai ожидаются у этого процесса (хост их уже видел)."""
        return False

    def claude_ai_seen(self) -> None:
        """Ф87-1 (п. 1–2): в `mcp_status` появилось имя `claude.ai …` (с любым статусом)."""

    async def claude_ai_missing(self) -> bool:
        """Ф87-1 (п. 3–4): коннекторы ожидались и не появились. `True` — процесс уходит на перезапуск, подачи
        нет; `False` — подавать как есть."""
        return False

    def live(self, block: str, kind: str, text: str, cut: bool, done: bool) -> None:
        """Ф41-3 (3.2): текст идущего блока — весь на этот момент. Зовётся только у `rich`-раннера."""

    def on_wakeup(self, tool_input) -> None:
        """Ф56-1в (2.4): сессия объявила таймер движка (`ScheduleWakeup`). Учитывает постоянная сессия."""

    def tool_interrupted(self) -> bool:
        """Ф41-3 (2.6 п.2): хост послал прерывание, ход ещё не кончился."""
        return False

    # --- OpenCode: решение вызова ---------------------------------------------------------------------------

    def _always_dirs(self) -> frozenset[str]:
        """Ф99-7.2: каталоги «всегда» сессии — гарду вместе со снятыми ключами (`guard.ALWAYS_DIR`)."""
        return frozenset(k for k in self.always if guard.always_dir_entry(k))

    def _always_tool(self, tool: str) -> bool:
        """Ф99-7.2: пишущий MCP-инструмент `tool` у этой сессии — «всегда»."""
        return always_mod.MCP + str(tool) in self.always and not always_mod.hub_tool(tool)

    def _oc_deny(self, tool: str, reason: str) -> Deny:
        if REDECIDING.get():
            return Deny(reason)
        guard._log(str(files.guard_log_path(self.host.data_dir)), tool, "deny", reason)  # noqa: SLF001
        self.event("warn", f"отказ: {reason}", tool=tool)
        return Deny(reason)

    def _oc_ask(self, tool: str, reason: str, rule: str | None = None) -> Allow | Ask:
        """Карточка владельцу. Ф41-5 П3: `rule` — ключ выключателя автопилота; запрос дошёл до хоста, а его ключ
        снят (`auto_free`) — решает хост: разрешить (П3д, у OpenCode правила применяет он сам)."""
        if rule is not None and rule in self.auto_free:
            return Allow()
        if REDECIDING.get():
            return Ask({"reason": reason, "rule": rule} if rule else {"reason": reason})
        guard._log(str(files.guard_log_path(self.host.data_dir)), tool, "ask", reason)  # noqa: SLF001
        return Ask({"reason": reason, "rule": rule} if rule else {"reason": reason})

    def _oc_decide(self, req, payload: dict, *, cwd_unknown: bool = False) -> Allow | Deny | Ask:
        """3.4 для рабочих сессий OpenCode: вызов (в формате хука Claude, `oc_tools.to_claude`) решают те же
        гард `guard.decide` (+ П9, профиль раннера) и правила `policy.decide_tool` (П4), что у Claude; «ask» —
        карточка владельцу, ничего не сказано — `once` (у Claude это `bypassPermissions`). Подагенты (П5) — так же.
        Разрешение на сам `question` проходит (правил на него нет): карточку вопроса делает `question.asked`
        (П6)."""
        name = str(payload.get("tool_name") or req.tool)
        failure = guard.FAILURE  # Ф41-5 П3б: защитные вопросы хоста — свой ключ, не правила проекта
        if payload.get("no_args"):
            return self._oc_ask(name, "гард хаба: аргументы вызова не получены — пусть решит владелец", failure)
        unknown = payload.get("unknown_keys")
        if unknown and req.tool in oc_tools.FILE_TOOLS:
            return self._oc_ask(
                name,
                f"гард хаба: {name} — незнакомые параметры ({', '.join(unknown)}) — пусть решит владелец",
                failure,
            )
        if payload.get("unparsed"):
            return self._oc_ask(name, f"гард хаба: {name} — путь не разобран — пусть решит владелец", failure)
        if req.tool == "task":
            # Ревью Ф16: подагент вне general/explore (агенты проекта `.opencode/agents`) работал бы без
            # запросов разрешений — мимо гарда. OpenCode режет его и сам (права `task` агента, `PERM_WORK`).
            # Ф41-5 П3в: ключ `oc_subagents`; снятый выключатель отказ хоста убирает (сам OpenCode по-прежнему
            # знает только подагентов своего конфига).
            kind = (req.args or {}).get("subagent_type")
            if kind not in OC_SUBAGENTS and "oc_subagents" not in self.auto_free:
                return self._oc_deny(
                    name, f"гард хаба: подагенты рабочей сессии — только {', '.join(OC_SUBAGENTS)}"
                )
        port = self.process.server.port if isinstance(self.process, OcProcess) else None
        asked: tuple[str, str | None] | None = None  # (причина, ключ) первой карточки гарда
        free = (self.auto_free & guard.RULES.keys()) | self._always_dirs()
        for event in oc_tools.guard_events(payload, cwd=self.cwd, session_id=req.session_id):
            try:
                guard.decide(
                    event,
                    workdir=self.workdir,
                    home=str(self.host.home),
                    oc_port=port,
                    free=free,
                    cwd_unknown=cwd_unknown,  # Ф71-1: папку оболочки Cursor хост не знает
                    home_dir=self.home_dir,  # Ф50-7 (П9)
                )
            except guard.Verdict as verdict:
                if verdict.kind == "deny":
                    return self._oc_deny(name, verdict.reason)
                if verdict.key is not None and verdict.key in self.auto_free:
                    continue  # сбой проверки при снятом `guard_failure` — решает хост: дальше
                asked = asked or (verdict.reason, verdict.key)
        tool_input = payload.get("tool_input")
        rule = policy.decide_tool(name, self._oc_settings, tool_input, self.auto_free)
        if rule == "deny":
            return self._oc_deny(name, f"гард хаба: {name} запрещён в рабочей сессии")
        if asked is not None:
            return self._oc_ask(name, asked[0], asked[1])
        if rule == "ask":
            key = policy.rule_key(name, tool_input, self.auto_free)
            if key == policy.MCP_WRITE and self._always_tool(name):
                if not REDECIDING.get():
                    guard._log(  # noqa: SLF001
                        str(files.guard_log_path(self.host.data_dir)), name, "always", "всегда в этой сессии"
                    )
                return Allow()  # Ф99-7.2: «Всегда в этой сессии» — движку разовое «разрешить»
            return self._oc_ask(name, f"гард хаба: {name} — под кнопкой, решает владелец", key)
        return Allow()

    # --- запуск: проверка режима ----------------------------------------------------------------------------

    async def _check_initialize(self) -> None:
        mode = await self._initialize()
        if self.reason is not None:
            return  # пока ждали — «Стоп», закрытие в ядре или остановка хоста
        if mode != BYPASS:
            await self.fail(f"режим разрешений {mode or 'неизвестен'} вместо {BYPASS}")
            return
        if await self._wait_mcp() and await self.claude_ai_missing():
            return  # Ф87-1 (п. 3): процесс перезапускается — первая подача достанется новому
        if self.reason is not None:
            return
        first, self.first_text = self.first_text, None
        if first is not None:
            await self.send_text(first, message_uuid=str(uuid.uuid4()))
        # После первого сообщения: реплики идут следом за ним, не раньше.
        self.initialized = True
        self.host.kick()

    async def _wait_mcp(self) -> bool:
        """До первой подачи — дождаться, пока MCP-серверы процесса выйдут из `pending` (см. `MCP_WAIT_S`).
        Сервер, который так и не подключился (`failed`, `needs-auth`), подачу не держит. Ф87-1 (п. 2):
        коннекторы claude.ai ожидаются, а имени `claude.ai …` ещё нет — ждать его до `CLAUDE_AI_WAIT_S`.
        `True` — ожидались и не появились."""
        if self.engine != "claude" or self.process is None:
            return False
        loop = asyncio.get_running_loop()
        started = loop.time()
        expect = self.claude_ai_expected()
        seen = False
        while self.reason is None and not self.process.exited.is_set():
            status = await self._control_wait("mcp_status", MCP_STATUS_TIMEOUT_S)
            servers = status.get("mcpServers") if status is not None else None
            if not isinstance(servers, list):
                return False
            items = [s for s in servers if isinstance(s, dict)]
            if not seen and any(str(s.get("name") or "").startswith(CLAUDE_AI_PREFIX) for s in items):
                seen = True
                self.claude_ai_seen()
            waiting = [str(s.get("name")) for s in items if s.get("status") == "pending"]
            elapsed = loop.time() - started
            lacking = expect and not seen
            if not (waiting and elapsed < MCP_WAIT_S) and not (lacking and elapsed < CLAUDE_AI_WAIT_S):
                if waiting:
                    _logger.warning(
                        "work: MCP не подключились за %d с, подаю как есть: %s",
                        MCP_WAIT_S,
                        ", ".join(waiting),
                    )
                return lacking
            await asyncio.sleep(MCP_POLL_S)
        return False

    async def _control_wait(self, subtype: str, timeout_s: float) -> dict | None:
        """Управляющий запрос и его ответ; `None` — отказ, нет ответа за `timeout_s` или процесс вышел."""
        assert self.process is not None
        loop = asyncio.get_running_loop()
        request_id = await self.process.control(subtype)
        future: asyncio.Future = loop.create_future()
        self.control_waiters[request_id] = future
        exited = asyncio.ensure_future(self.process.exited.wait())
        try:
            done, _ = await asyncio.wait({future, exited}, timeout=timeout_s, return_when="FIRST_COMPLETED")
        finally:
            exited.cancel()
            self.control_waiters.pop(request_id, None)
        if future not in done:
            return None
        response = future.result()
        return response if isinstance(response, dict) else None

    async def _initialize(self) -> str | None:
        response = await self._control_wait("initialize", INIT_TIMEOUT_S) or {}
        mode = response.get("current_permission_mode")
        return mode if isinstance(mode, str) else None

    async def send_text(
        self,
        text: str,
        *,
        message_uuid: str,
        priority: str | None = None,
        blocks: list[dict] | None = None,
        synthetic: bool = False,
    ) -> bool:
        if not self.alive:
            return False
        ok = await self.process.send(
            session.user_line(
                text,
                message_uuid=message_uuid,
                priority=priority,
                blocks=blocks,
                # Ф41-4: признак «не реплика владельца» понимает только OpenCode.
                synthetic=synthetic and self.engine == "opencode",
            )
        )
        if ok:
            self.last_activity = self.host.monotonic()
        return ok

    # --- поток ----------------------------------------------------------------------------------------------

    async def on_message(self, msg: dict) -> None:
        self.last_activity = self.host.monotonic()
        kind = msg.get("type")
        if kind == "system":
            await self._on_system(msg)
        elif kind == "stream_event":
            if self.rich:
                self._on_stream_event(msg)
        elif kind == "assistant" and self.rich:
            # Ф48-1: шаг подагента (`parent_tool_use_id`) ход сессии не начинает — фоновый подагент работает
            # и после конца хода, а сессия при этом свободна (реплика владельца подаётся сразу).
            if not msg.get("parent_tool_use_id"):
                await self._turn_started()
            self._on_assistant_rich(msg)
        elif kind == "user" and self.rich:
            self._on_user_rich(msg)
        elif kind == "assistant":
            await self._turn_started()
            for block in (msg.get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    name = str(block.get("name") or "?")
                    self.event("tool", session.summarize_tool_use(name, block.get("input")), tool=name)
                elif block.get("type") == "text" and (block.get("text") or "").strip():
                    self.event("text", session.summarize_text(block["text"]))
        elif kind == "user":
            content = (msg.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    summary, is_error = session.summarize_tool_result(block)
                    self.event("tool_done", summary, is_error=is_error)
        elif kind == "result":
            await self._on_result(msg)
        elif kind == "control_request":
            await self._on_control_request(msg)
        elif kind == "control_response":
            response = msg.get("response") or {}
            waiter = self.control_waiters.get(response.get("request_id"))
            if waiter is not None and not waiter.done():
                waiter.set_result(response.get("response") if response.get("subtype") == "success" else None)
        elif kind == "control_cancel_request":
            await self._on_cancel(msg.get("request_id"))
        elif kind == "rate_limit_event":
            self.host.on_rate_limit(msg.get("rate_limit_info"))

    # --- подробности потока (Ф41-3; только `rich`) ----------------------------------------------------------

    def _on_assistant_rich(self, msg: dict) -> None:
        """`assistant` постоянной сессии: у вызова — путь относительно рабочей папки и полный вход по клику;
        у текста — `ref` = ключ блока, который шёл в `live` (окно меняет строку «пишет…» на событие)."""
        message = msg.get("message") if isinstance(msg.get("message"), dict) else {}
        mid = message.get("id")
        explicit = msg.get("block")  # OpenCode: ключ блока назван явно (`partID`)
        own = not msg.get("parent_tool_use_id")  # Ф56-1б: шаг подагента — не ответ сессии
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if own and block.get("type") == "tool_use":
                self.turn_texts = []
            elif own and block.get("type") == "text" and isinstance(block.get("text"), str):
                self.turn_texts.append(block["text"])
            if block.get("type") == "tool_use":
                name = str(block.get("name") or "?")
                tool_input = block.get("input")
                if isinstance(block.get("id"), str):
                    while len(self._calls) >= CALLS_KEEP:
                        self._calls.pop(next(iter(self._calls)))
                    self._calls[block["id"]] = (name, tool_input)
                if own and name == WAKEUP_TOOL:
                    self.on_wakeup(tool_input)  # Ф56-1в (2.4): объявленное пробуждение
                self.event(
                    "tool",
                    session.summarize_tool_use(name, tool_input, self.workdir),
                    tool=name,
                    detail=session.tool_input_detail(name, tool_input),
                )
            elif block.get("type") == "text":
                ref = explicit if isinstance(explicit, str) and explicit else self._pop_text_ref(mid)
                if (block.get("text") or "").strip():
                    self.event("text", session.summarize_text(block["text"]), ref=ref)

    def _pop_text_ref(self, mid) -> str | None:
        """Claude Code: итоговые `assistant` приходят по одному на блок с тем же `message.id`; n-й текстовый
        блок сообщения — n-й текстовый блок, начатый в потоке. Потока не было — `None`."""
        refs = self._text_refs.get(mid) if isinstance(mid, str) else None
        return refs.pop(0) if refs else None

    def _on_user_rich(self, msg: dict) -> None:
        """`user` с `tool_result` постоянной сессии: diff правки, вывод команды (таблица 2.2)."""
        content = (msg.get("message") or {}).get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            call_id = block.get("tool_use_id")
            name, tool_input = None, None
            if isinstance(call_id, str):
                name, tool_input = self._calls.pop(call_id, (None, None))
            summary, is_error = session.summarize_tool_result(block)
            if self.tool_interrupted() and (is_error or self.engine == "opencode"):
                # 2.6 п.2: отказ инструмента после прерывания, посланного хостом, — не английский текст
                # движка и не ошибка, а строка «инструмент прерван». OpenCode прерванную команду отдаёт
                # обычным результатом («User aborted the command» в выводе, живой стенд) — у него так пишется
                # любой результат, пришедший после прерывания и до конца хода.
                self.event("tool_done", INTERRUPTED_TOOL)
                continue
            try:
                brief, detail = session.tool_result_details(
                    name, tool_input, block, msg.get("tool_use_result"), self.workdir
                )
            except Exception:  # noqa: BLE001 — незнакомая форма поля не должна ронять разбор строки (П11)
                brief, detail = None, None
            self.event("tool_done", summary, is_error=is_error, brief=brief, detail=detail)

    def _on_stream_event(self, msg: dict) -> None:
        """Частичные сообщения (`--include-partial-messages`; у OpenCode — тот же формат от `OcProcess`):
        дельты текста складываются в текст блока и уходят в `live`. Блоки инструментов и вызовы подагентов
        (`parent_tool_use_id`) не идут. Ключ блока — `block` строки (OpenCode: `partID`) или
        `<message.id>:<index>`."""
        event = msg.get("event")
        if not isinstance(event, dict) or msg.get("parent_tool_use_id"):
            return
        etype = event.get("type")
        explicit = msg.get("block") if isinstance(msg.get("block"), str) and msg.get("block") else None
        index = event.get("index")
        if etype == "message_start":
            mid = (event.get("message") or {}).get("id") if isinstance(event.get("message"), dict) else None
            self._stream_mid = mid if isinstance(mid, str) and mid else None
            self._stream_index.clear()
        elif etype == "message_delta":
            if isinstance(event.get("usage"), dict):
                self._delta_usage = event["usage"]
        elif etype == "content_block_start":
            key = explicit
            if key is None and self._stream_mid and isinstance(index, int):
                key = f"{self._stream_mid}:{index}"
                self._stream_index[index] = key
            block = event.get("content_block") if isinstance(event.get("content_block"), dict) else {}
            kind = LIVE_KINDS.get(block.get("type"))
            if key is None or kind is None:
                return
            self._blocks[key] = {"kind": kind, "text": "", "cut": False, "sent": False}
            if kind == "text" and explicit is None:
                self._text_refs.setdefault(self._stream_mid, []).append(key)
        elif etype in ("content_block_delta", "content_block_stop"):
            key = explicit or (self._stream_index.get(index) if isinstance(index, int) else None)
            state = self._blocks.get(key) if key is not None else None
            if state is None:
                return
            if etype == "content_block_stop":
                del self._blocks[key]
                if state["sent"]:
                    self.live(key, state["kind"], state["text"], state["cut"], True)
                return
            delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
            piece = delta.get("text") if delta.get("type") == "text_delta" else None
            if delta.get("type") == "thinking_delta":
                piece = delta.get("thinking")
            if not isinstance(piece, str) or not piece:
                return
            text = state["text"] + piece
            if len(text) > LIVE_TEXT_MAX:
                text, state["cut"] = text[-LIVE_TEXT_MAX:], True
            state["text"] = text
            state["sent"] = True
            self.live(key, state["kind"], text, state["cut"], False)

    def turn_usage(self, msg: dict) -> tuple[dict | None, dict | None]:
        """Конец хода `rich`-раннера: (`brief` события `turn_end`, тело `usage` для ядра); состояние потока
        хода сбрасывается. Зовёт приёмник из `_on_result`."""
        try:
            brief, usage, self._cost_total = session.turn_usage(
                msg, prev_total=self._cost_total, delta_usage=self._delta_usage, model=self._model
            )
        except Exception:  # noqa: BLE001 — незнакомая форма `result` не должна ломать конец хода (П11)
            brief, usage = None, None
        self._calls.clear()
        self._blocks.clear()
        self._text_refs.clear()
        self._stream_index.clear()
        self._stream_mid = None
        self._delta_usage = None
        return brief, usage

    async def _on_system(self, msg: dict) -> None:
        if msg.get("subtype") == "engine_error":
            # Ф16 (Р45): ошибка провайдера OpenCode — текстом в ленту.
            self.event("error", fmt.one_line(msg.get("text"), EVENT_SUMMARY_MAX), is_error=True)
            return
        if msg.get("subtype") == "engine_note":
            # Ф74-8: сведение движка без ошибки (Cursor, похоже, сжал разговор сам) — строкой в ленту.
            self.event("status", fmt.one_line(msg.get("text"), EVENT_SUMMARY_MAX))
            return
        if msg.get("subtype") != "init":
            return
        mode = msg.get("permissionMode")
        if mode != BYPASS:
            # Из читателя потока — отдельной задачей (гашение ждёт конца читателя).
            self.host.spawn(self.fail(f"режим разрешений {mode} вместо {BYPASS}"))
            return
        if isinstance(msg.get("model"), str):
            self._model = msg["model"]
        await self._turn_started()
        await self.on_init(msg)

    async def _turn_started(self) -> None:
        """Ход начался (в т.ч. «незаказанный» — фоновая задача разбудила сессию, ФЧ1 п.1)."""
        if self.busy:
            return
        self.busy = True
        self.turn_started = self.host.monotonic()
        self.request_wait_ms = 0
        self.turn_texts = []  # Ф56-1б: последний ответ — только этого хода
        await self.turn_began()

    async def _on_control_request(self, msg: dict, *, rule: str | None = None, reason=None) -> None:
        """Запрос движка -> карточка. Ф41-5 (4.3): `rule` — ключ выключателя автопилота и `reason` — причина
        без метки (их определяет постоянная сессия)."""
        request_id = msg.get("request_id")
        request = msg.get("request") or {}
        if request.get("subtype") != "can_use_tool" or not isinstance(request_id, str):
            # Прочие запросы CLI хост не обслуживает — ответ, чтобы ход не повис.
            if isinstance(request_id, str) and self.process is not None:
                await self.process.send(
                    {
                        "type": "control_response",
                        "response": {
                            "subtype": "error",
                            "request_id": request_id,
                            "error": "не поддерживается хостом",
                        },
                    }
                )
            return
        tool = str(request.get("tool_name") or "?")
        tool_input = request.get("input") if isinstance(request.get("input"), dict) else {}
        reason_parts = [
            str(x) for x in (request.get("decision_reason_type"), request.get("decision_reason")) if x
        ]
        entry = {
            "core_id": None,
            "tool": tool,
            "input": tool_input,
            "tool_use_id": request.get("tool_use_id"),
            "kind": "question" if tool == QUESTION_TOOL else "permission",
            "reason": ": ".join(reason_parts) or request.get("description"),
            "since": self.host.monotonic(),
        }
        if rule is not None:
            entry["rule"] = rule
            parts = [str(x) for x in (request.get("decision_reason_type"), reason) if x]
            entry["reason"] = ": ".join(parts) or request.get("description")
        self.pending[request_id] = entry
        await self.create_request(request_id)

    async def create_request(self, request_id: str) -> None:
        """Запрос разрешения или вопрос -> карточка владельцу (запись в ядре через приёмник)."""
        entry = self.pending.get(request_id)
        # Ревью 9a: «в полёте» — ДО POST; круг хоста (housekeeping) такой запрос не повторяет, иначе владелец
        # получил бы две карточки на один `can_use_tool`. Повтор — только после неудачного POST.
        if entry is None or entry["core_id"] is not None or entry.get("in_flight"):
            return
        entry["in_flight"] = True
        # Ф11-6 С1б: событие `request` ядро пишет само — сначала отправить накопленные события раннера (в том
        # числе `tool` этой команды), иначе в ленте запрос встал бы раньше вызова.
        with contextlib.suppress(CoreUnauthorized):
            await self.flush()
        body = {
            "kind": entry["kind"],
            "tool": entry["tool"],
            "input": entry["input"],
            "reason": entry["reason"],
        }
        if entry.get("rule"):
            body["rule"] = entry["rule"]
        try:
            result = await self.post_request(body)
        except (CoreError, CoreUnavailable) as exc:
            # Ядро не приняло — повтор на следующем круге; сессия ждёт (запрос висит без таймаута, ФЧ1 п.2).
            _logger.warning("work: %s — запрос не записан (%s)", self.request_label(), exc)
            entry["in_flight"] = False
            await self.request_refused(exc)
            return
        entry["in_flight"] = False
        core_id = result["id"]
        decided = result.get("decided") == "allow" and entry["kind"] == "permission"
        if self.pending.get(request_id) is not entry:
            # Пока POST летел, CLI снял запрос или процесс вышел — карточка уже не нужна («сразу да» уже
            # закрыт ядром: его отметку `expired` ядро отвергнет, приёмник это глотает).
            if not decided:
                await self.expire(core_id)
            return
        if decided:
            # Ф66-1 (П3): «сразу да» — ядро записало разрешение решённым и доставленным: ответ движку тот же,
            # что после клика владельца (`deliver_decision`), карточки и уведомления нет.
            self.pending.pop(request_id, None)
            self._request_done(entry)
            response = {
                "behavior": "allow",
                "updatedInput": dict(entry["input"]),
                "toolUseID": entry["tool_use_id"],
            }
            if self.alive:
                await self.process.respond(request_id, response)
            return
        entry["core_id"] = core_id
        self.by_core[core_id] = request_id
        self.request_posted(entry)

    async def request_refused(self, exc: Exception) -> None:
        """Ядро отказало в записи запроса (приёмник может погасить раннер — закрытая сессия)."""

    async def _on_cancel(self, request_id) -> None:
        """П9: CLI снял свой запрос (`priority: now`, `interrupt`) — запрос `expired`."""
        entry = self.pending.pop(request_id, None) if isinstance(request_id, str) else None
        if entry is None:
            return
        self._request_done(entry)
        if entry["core_id"] is not None:
            self.by_core.pop(entry["core_id"], None)
            await self.expire(entry["core_id"])

    def _request_done(self, entry: dict) -> None:
        self.request_wait_ms += int((self.host.monotonic() - entry["since"]) * 1000)

    async def deliver_decision(self, request: dict) -> None:
        """F5: решение владельца -> `control_response`; доставлено — отметка в ядре, процесса нет — `expired`."""
        request_id = self.by_core.pop(request["id"], None)
        entry = self.pending.pop(request_id, None) if request_id else None
        if entry is None:
            return
        self._request_done(entry)
        answer = request.get("answer") or {}
        if request["status"] == "denied":
            response = {
                "behavior": "deny",
                "message": denied_text(answer),
                "toolUseID": entry["tool_use_id"],
            }
        else:
            updated = dict(entry["input"])
            if request["status"] == "answered":
                updated["answers"] = answer.get("answers") or {}
            response = {"behavior": "allow", "updatedInput": updated, "toolUseID": entry["tool_use_id"]}
        ok = self.alive and await self.process.respond(request_id, response)
        if ok:
            await self.delivered(request["id"])
        else:
            await self.expire(request["id"])

    # --- выход процесса -------------------------------------------------------------------------------------

    async def exit_cleanup(self, process) -> None:
        """Процесс вышел: висящие запросы — `expired` (ФЧ2 п.8), ожидания ответов сняты, читатели закрыты."""
        for request_id, entry in list(self.pending.items()):
            self.pending.pop(request_id, None)
            if entry["core_id"] is not None:
                self.by_core.pop(entry["core_id"], None)
                await self.expire(entry["core_id"])
        for waiter in self.control_waiters.values():
            if not waiter.done():
                waiter.set_result(None)
        await process.aclose()

    async def interrupt_and_close(
        self, *, grace_s: float = STOP_GRACE_S, interrupt: bool = True, cancel_queued: bool = True
    ) -> None:
        """Прервать ход (`interrupt`), закрыть ввод, дождаться выхода; не вышел за `grace_s` — погасить дерево
        процессов."""
        if self.process is None:
            return
        self.snapshot = self.process.tree()
        with contextlib.suppress(Exception):
            if not interrupt:
                pass
            elif cancel_queued:
                await self.process.control("interrupt", cancel_queued=True)
            else:
                await self.process.control("interrupt")
        await self.process.close_stdin()
        if not await self.process.wait(grace_s):
            await self.process.kill_tree(self.snapshot)
        else:
            await self.process.kill_tree(self.snapshot, grace_s=1.0)

    async def kill(self) -> None:
        if self.process is not None and not self.process.exited.is_set():
            snapshot = self.process.tree()
            await self.process.close_stdin()
            await self.process.kill_tree(snapshot, grace_s=2.0)

    async def _watch_exit(self) -> None:
        process = self.process
        if process is None:
            return
        await process.exited.wait()
        await self.process_exited(process)
