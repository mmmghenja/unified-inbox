"""Драйвер одного хода OpenCode (Ф16, 3.3, 3.4, П5, П6, П10, F5–F11, F21).

`run_turn` подписывается на поток событий ДО `prompt`, отвечает на разрешения и вопросы своей сессии и её
потомков (подагенты `task`, П5), пересылает события в `on_event` и ждёт конца хода: `session.idle` своей
сессии ИЛИ опрос статуса (страховка от потерянного события) + последняя `assistant`-запись.

Решения — через `decide(request) -> Allow | Deny(reason) | Ask(payload)` (может быть корутиной):
- `Allow` -> `once`; `Deny` -> `reject` с причиной (модель видит её и продолжает ход, F8);
- `Ask` — только там, где есть человек (hub-work): драйвер зовёт `on_ask(request, payload)` и ждёт итога
  (`Allow`/`Deny`); ход стоит, пока ждёт (как карточка у Claude). Нет `on_ask` — отказ.
`always` не отвечается никогда (F20, П10). Вопросы модели (`question.asked`, F21) — `on_question(request) ->
list[list[str]] | None` (метки на каждый вопрос; `None` — отказ); нет `on_question` — отказ (wake/orch, П6).
Запрос чужой сессии (не своя и не потомок) — не наш: без ответа.
"""

import asyncio
import contextlib
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx

from . import failure
from .oc_server import RECONNECTED, OcClient, SessionNotFound

log = logging.getLogger(__name__)

POLL_S = 5.0
RETRY_LIMIT_S = failure.RETRY_LIMIT_S
CONNECT_WAIT_S = 5.0
PARENT_DEPTH = 8
NO_ARGS_REASON = "гард хаба: аргументы вызова не получены — вызов отклонён"
NO_ASK_REASON = "гард хаба: этот вызов требует решения владельца, а спросить некого — отклонено"


@dataclass(frozen=True)
class Allow:
    pass


@dataclass(frozen=True)
class Deny:
    reason: str


@dataclass(frozen=True)
class Ask:
    payload: dict = field(default_factory=dict)


@dataclass
class PermissionRequest:
    """Запрос разрешения OpenCode с аргументами вызова. `tool` — `part.tool` (или имя разрешения, если часть
    не найдена; тогда `args is None`)."""

    id: str
    session_id: str
    permission: str
    tool: str
    args: dict | None
    patterns: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    call_id: str | None = None
    message_id: str | None = None
    child: bool = False


@dataclass
class QuestionRequest:
    id: str
    session_id: str
    questions: list
    child: bool = False


@dataclass
class TurnResult:
    ok: bool
    text: str = ""
    finish: str | None = None
    tokens: dict | None = None
    cost: float | None = None
    error: dict | None = None  # {"name", "message"}
    session_id: str = ""
    retry_s: float | None = None  # сколько подряд длился `status retry` к концу хода


Decision = Allow | Deny | Ask
DecideFn = Callable[[PermissionRequest], "Decision | Awaitable[Decision]"]
AskFn = Callable[[PermissionRequest, dict], "Allow | Deny | Awaitable[Allow | Deny]"]
QuestionFn = Callable[[QuestionRequest], "list[list[str]] | None | Awaitable[list[list[str]] | None]"]
EventFn = Callable[[dict], "None | Awaitable[None]"]


async def _maybe(value):
    return await value if inspect.isawaitable(value) else value


def _error_of(raw) -> dict | None:
    """`{name, message}` из ошибки OpenCode (`{name, data: {message, statusCode?, providerID?}}`)."""
    if not isinstance(raw, dict):
        return None
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    err = {"name": str(raw.get("name") or "UnknownError"), "message": str(data.get("message") or "")}
    if isinstance(data.get("statusCode"), int):
        err["status"] = data["statusCode"]
    if isinstance(data.get("providerID"), str):
        err["provider"] = data["providerID"]
    return err


def classify_error(error: dict | None, status_retry: float | None = None) -> str:
    """`limit | auth | model | retry | fail` (3.3). `status_retry` — сколько секунд подряд сессия была в
    `retry`: дольше 10 минут — `retry` (Ф83-1, П5: провайдер не отвечает — пауза как у лимита, но не повод
    переходить на запасной). Маркеры — общие (`failure.classify`); баланс провайдера здесь — `limit`
    (потребителям хода разница не нужна, нужна — `failure.classify`)."""
    kind = failure.classify("opencode", error, retry_s=status_retry)
    return {"balance": "limit", "other": "fail"}.get(kind, kind)


def _last_assistant(messages: list, since_ms: int) -> dict | None:
    """Последняя `assistant`-запись этого хода (создана не раньше `since_ms`)."""
    best = None
    for m in messages:
        info = m.get("info") if isinstance(m, dict) else None
        if not isinstance(info, dict) or info.get("role") != "assistant":
            continue
        created = (info.get("time") or {}).get("created")
        if isinstance(created, (int, float)) and created < since_ms:
            continue
        best = m
    return best


def _text_of(message: dict | None) -> str:
    if not message:
        return ""
    texts = [
        p.get("text") for p in message.get("parts") or [] if isinstance(p, dict) and p.get("type") == "text"
    ]
    texts = [t for t in texts if isinstance(t, str) and t]
    return texts[-1] if texts else ""


class _Turn:
    def __init__(self, client: OcClient, sid: str, **kw) -> None:
        self.client = client
        self.sid = sid
        self.decide: DecideFn = kw["decide"]
        self.on_ask: AskFn | None = kw.get("on_ask")
        self.on_question: QuestionFn | None = kw.get("on_question")
        self.on_event: EventFn | None = kw.get("on_event")
        self.poll_s: float = kw.get("poll_s", POLL_S)
        self.retry_limit_s: float = kw.get("retry_limit_s", RETRY_LIMIT_S)
        self.mine: set[str] = {sid}
        self.foreign: set[str] = set()
        self.handled: set[str] = set()
        self.tasks: set[asyncio.Task] = set()
        self.connected = asyncio.Event()
        self.idle = asyncio.Event()
        self.check_now = asyncio.Event()
        self.error: dict | None = None
        self.retry_since: float | None = None
        self.last_retry: str | None = None

    # --- чья сессия ----------------------------------------------------------------------------------------

    async def _is_mine(self, session_id) -> bool:
        if not isinstance(session_id, str) or not session_id:
            return False
        if session_id in self.mine:
            return True
        if session_id in self.foreign:
            return False
        chain: list[str] = []
        current = session_id
        for _ in range(PARENT_DEPTH):
            try:
                info = await self.client.get_session(current)
            except httpx.HTTPError, SessionNotFound:
                info = None
            parent = info.get("parentID") if isinstance(info, dict) else None
            chain.append(current)
            if not isinstance(parent, str) or not parent:
                break
            if parent in self.mine:
                self.mine.update(chain)
                return True
            current = parent
        self.foreign.update(chain)
        return False

    # --- события -------------------------------------------------------------------------------------------

    async def read(self) -> None:
        async for event in self.client.events():
            etype = event.get("type")
            props = event.get("properties") if isinstance(event.get("properties"), dict) else {}
            if etype == "server.connected":
                self.connected.set()
                continue
            if etype == RECONNECTED:
                self.connected.set()
                self.check_now.set()
                continue
            if etype == "session.created":
                info = props.get("info") if isinstance(props.get("info"), dict) else {}
                if info.get("parentID") in self.mine and isinstance(info.get("id"), str):
                    self.mine.add(info["id"])
            session_id = props.get("sessionID")
            if session_id is None and isinstance(props.get("info"), dict):
                session_id = props["info"].get("sessionID") or props["info"].get("id")
            if etype == "permission.asked":
                self._spawn(self._permission(props))
                continue
            if etype == "question.asked":
                self._spawn(self._question(props))
                continue
            if session_id not in self.mine:
                continue
            if session_id == self.sid:
                self._own(etype, props)
            if self.on_event is not None:
                try:
                    await _maybe(self.on_event(event))
                except Exception:
                    log.exception("on_event упал на %s", etype)

    def _own(self, etype, props: dict) -> None:
        if etype == "session.idle":
            self.idle.set()
        elif etype == "session.error":
            err = _error_of(props.get("error"))
            if err is not None:
                self.error = err
        elif etype == "session.status":
            status = props.get("status") if isinstance(props.get("status"), dict) else {}
            self._note_status(status)
            if status.get("type") == "idle":
                self.idle.set()

    def _note_status(self, status: dict) -> None:
        if status.get("type") == "retry":
            if self.retry_since is None:
                self.retry_since = time.monotonic()
            self.last_retry = str(status.get("message") or "")
        else:
            self.retry_since = None

    def retry_s(self) -> float | None:
        return None if self.retry_since is None else time.monotonic() - self.retry_since

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    # --- разрешения и вопросы ------------------------------------------------------------------------------

    async def _permission(self, props: dict) -> None:
        rid = props.get("id")
        if not isinstance(rid, str) or rid in self.handled:
            return
        session_id = props.get("sessionID")
        if not await self._is_mine(session_id):
            return  # не наш запрос — не отвечать (П5)
        if rid in self.handled:
            return
        self.handled.add(rid)
        tool_ref = props.get("tool") if isinstance(props.get("tool"), dict) else {}
        req = PermissionRequest(
            id=rid,
            session_id=session_id,
            permission=str(props.get("permission") or ""),
            tool=str(props.get("permission") or ""),
            args=None,
            patterns=list(props.get("patterns") or []),
            metadata=dict(props.get("metadata") or {}),
            call_id=tool_ref.get("callID"),
            message_id=tool_ref.get("messageID"),
            child=session_id != self.sid,
        )
        if req.message_id and req.call_id:
            with contextlib.suppress(httpx.HTTPError, SessionNotFound, ValueError):
                message = await self.client.message(session_id, req.message_id)
                for part in message.get("parts") or []:
                    if isinstance(part, dict) and part.get("callID") == req.call_id:
                        state = part.get("state") if isinstance(part.get("state"), dict) else {}
                        if isinstance(state.get("input"), dict):
                            req.args = state["input"]
                        if isinstance(part.get("tool"), str) and part["tool"]:
                            req.tool = part["tool"]
                        break
        try:
            verdict = await self._verdict(req)
        except Exception as exc:
            log.exception("решение по разрешению %s упало", rid)
            verdict = Deny(f"гард хаба: сбой проверки ({type(exc).__name__}) — вызов отклонён")
        try:
            if isinstance(verdict, Allow):
                await self.client.reply_permission(rid, "once")
            else:
                await self.client.reply_permission(rid, "reject", verdict.reason)
        except httpx.HTTPError, SessionNotFound:
            log.warning("ответ на разрешение %s не доставлен", rid)

    async def _verdict(self, req: PermissionRequest) -> Allow | Deny:
        verdict = await _maybe(self.decide(req))
        if isinstance(verdict, Ask):
            if self.on_ask is None:
                return Deny(NO_ASK_REASON)
            verdict = await _maybe(self.on_ask(req, verdict.payload))
        if isinstance(verdict, Allow | Deny):
            return verdict
        return Deny("гард хаба: непонятное решение — вызов отклонён")

    async def _question(self, props: dict) -> None:
        qid = props.get("id")
        if not isinstance(qid, str) or qid in self.handled:
            return
        session_id = props.get("sessionID")
        if not await self._is_mine(session_id):
            return
        if qid in self.handled:
            return
        self.handled.add(qid)
        req = QuestionRequest(
            id=qid,
            session_id=session_id,
            questions=list(props.get("questions") or []),
            child=session_id != self.sid,
        )
        answers = None
        if self.on_question is not None:
            try:
                answers = await _maybe(self.on_question(req))
            except Exception:
                log.exception("ответ на вопрос %s упал", qid)
                answers = None
        try:
            if answers is None:
                await self.client.reject_question(qid)
            else:
                await self.client.reply_question(qid, answers)
        except httpx.HTTPError:
            log.warning("ответ на вопрос %s не доставлен", qid)

    # --- конец хода ----------------------------------------------------------------------------------------

    async def poll(self) -> bool:
        """Сессия простаивает? (опрос статуса — страховка от потерянного `session.idle`)."""
        try:
            status = await self.client.status_info(self.sid)
        except httpx.HTTPError, SessionNotFound:
            return False
        self._note_status(status)
        return status.get("type") == "idle"

    async def cancel(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        for task in list(self.tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


async def run_turn(
    client: OcClient,
    sid: str,
    *,
    text: str,
    system: str | None = None,
    agent: str | None = None,
    model: tuple[str, str] | None = None,
    variant: str | None = None,
    files: list[dict] | None = None,
    decide: DecideFn,
    on_ask: AskFn | None = None,
    on_question: QuestionFn | None = None,
    on_event: EventFn | None = None,
    timeout_s: float = 600.0,
    poll_s: float = POLL_S,
    retry_limit_s: float = RETRY_LIMIT_S,
) -> TurnResult:
    """Один ход: `prompt` -> ответы на разрешения/вопросы -> конец хода -> `TurnResult` (3.3).

    `SessionNotFound` на `prompt` пробрасывается (вызывающий решает: новая сессия, П14). Таймаут и `retry`
    дольше `retry_limit_s` — `abort` и `ok=False` (`error.name` — `timeout` или `retry`). `files` — части
    `{"type": "file", "mime", "filename", "url"}` после текста (как `OcClient.prompt`)."""
    turn = _Turn(
        client,
        sid,
        decide=decide,
        on_ask=on_ask,
        on_question=on_question,
        on_event=on_event,
        poll_s=poll_s,
        retry_limit_s=retry_limit_s,
    )
    reader = asyncio.create_task(turn.read())
    deadline = time.monotonic() + timeout_s
    try:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(turn.connected.wait(), CONNECT_WAIT_S)
        # Записи этого хода OpenCode создаёт ПОСЛЕ приёма `prompt` (часы те же — машина одна).
        started_ms = int(time.time() * 1000)
        await client.prompt(
            sid, text=text, system=system, agent=agent, model=model, variant=variant, files=files
        )
        sent = time.monotonic()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return await _stop(
                    client, sid, turn, {"name": "timeout", "message": f"ход дольше {timeout_s:g} с"}
                )
            waiter = asyncio.ensure_future(_first(turn.idle, turn.check_now))
            try:
                await asyncio.wait_for(waiter, min(poll_s, remaining))
            except TimeoutError:
                pass
            idle = turn.idle.is_set()
            turn.check_now.clear()
            if not idle and time.monotonic() - sent >= min(poll_s, 1.0):
                idle = await turn.poll()
            retry = turn.retry_s()
            if retry is not None and retry >= retry_limit_s:
                return await _stop(client, sid, turn, {"name": "retry", "message": turn.last_retry or ""})
            if not idle:
                continue
            result = await _finish(client, sid, turn, started_ms)
            if result is not None:
                return result
            turn.idle.clear()
    finally:
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await reader
        await turn.cancel()


async def _first(*events: asyncio.Event) -> None:
    tasks = [asyncio.ensure_future(e.wait()) for e in events]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()


async def _finish(client: OcClient, sid: str, turn: _Turn, started_ms: int) -> TurnResult | None:
    """Сессия простаивает: собрать итог. `None` — ход ещё не дошёл до конца (простой между шагами, нет
    законченной записи и нет ошибки) — ждать дальше."""
    try:
        messages = await client.messages(sid)
    except httpx.HTTPError, SessionNotFound:
        messages = []
    last = _last_assistant(messages, started_ms)
    info = last.get("info") if last else None
    error = turn.error
    if error is None and isinstance(info, dict):
        error = _error_of(info.get("error"))
    if last is None and error is None:
        return None
    if error is None and isinstance(info, dict):
        completed = (info.get("time") or {}).get("completed")
        if not completed or info.get("finish") == "tool-calls":
            return None
    info = info or {}
    return TurnResult(
        ok=error is None,
        text=_text_of(last),
        finish=info.get("finish"),
        tokens=info.get("tokens") if isinstance(info.get("tokens"), dict) else None,
        cost=info.get("cost") if isinstance(info.get("cost"), (int, float)) else None,
        error=error,
        session_id=sid,
        retry_s=turn.retry_s(),
    )


async def _stop(client: OcClient, sid: str, turn: _Turn, error: dict) -> TurnResult:
    with contextlib.suppress(httpx.HTTPError, SessionNotFound):
        await client.abort(sid)
    return TurnResult(ok=False, error=error, session_id=sid, retry_s=turn.retry_s())
