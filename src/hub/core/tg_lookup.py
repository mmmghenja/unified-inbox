"""Поиск каналов и групп Telegram и чтение публичных без вступления — посредник в памяти ядра (Ф29-1,
docs/architecture.md).

Ядро в Telegram не ходит: заявку сессии (`search`/`peek`) кладёт в очередь аккаунта в памяти, коннектор берёт
её long-poll'ом (`next`), идёт в Telegram и отвечает (`result`). Ничего не хранится: ни заявки, ни ответы не
пишутся в БД (кроме строки аудита в ручке). Рестарт ядра или коннектора теряет заявки в полёте — сессия
получает 504/503 и повторяет.

Темп держит ядро (решение 5): за скользящие сутки на аккаунт — `DAILY` (поиск 60, чтение 100); между
выдачами аккаунта — не меньше `GAP_MS`; в очереди аккаунта — не больше `QUEUE_MAX`; FloodWait Telegram — пауза
аккаунта, до её конца новые заявки сразу 429, а ждущие в очереди отбиваются тем же 429. Рестарт ядра всё это
обнуляет — допустимо.

Один объект на процесс ядра (`runner` отдаёт его обоим приложениям, как `Activity`); все методы — в одном
event loop, без потоков.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import math
import sqlite3
from collections import deque
from collections.abc import Callable
from datetime import datetime
from typing import Any

from . import join as join_mod
from .security import check_attach

KINDS = ("search", "peek", "resolve")
DAY_MS = 24 * 60 * 60 * 1000
# поиск: 60 — слово владельца (было 30). 24.10: `resolve` — кто это по @имени (`hub_draft_new`): свой счёт.
DAILY = {"search": 60, "peek": 100, "resolve": 100}
# 24.10: проверка адресата `hub_draft_new` ждёт коннектор недолго — не дождались, заготовка создаётся как
# раньше (проверка при отправке остаётся); ответ помнится час, повтор того же @имени в Telegram не ходит.
RESOLVE_TIMEOUT_S = 8.0
RESOLVE_CACHE_MS = 60 * 60 * 1000
RESOLVE_CACHE_MAX = 500
# Виды чата, которым первое сообщение не пишут (коннектор: `_chat_kind`).
NOT_PERSON_KINDS = {"broadcast": "канал", "megagroup": "группа", "group": "группа"}
GAP_MS = 3_000
QUEUE_MAX = 5
ANSWER_TIMEOUT_S = 40.0
NEXT_MAX_WAIT_S = 25.0
# Пульс коннектора (`account.last_ok_ts`, раз в 60 с) молчит дольше — коннектор не запущен; тот же порог,
# что у `hub watch` (`watch.SILENT_CONNECTOR_MS`, `account:<id>:silent`).
CONNECTOR_SILENT_MS = 5 * 60 * 1000
# Выданная коннектору заявка, ответ на которую так и не пришёл (коннектор умер), забывается через это время.
_INFLIGHT_TTL_MS = 120_000
_MIN_WAIT_S = 0.05

SEARCH_QUERY_MIN = 3
SEARCH_QUERY_MAX = 64
SEARCH_LIMIT_MAX = 20
SEARCH_LIMIT_DEFAULT = 10
PEEK_LIMIT_MAX = 50
PEEK_LIMIT_DEFAULT = 20
ABOUT_MAX = 300
TEXT_MAX = 2000

_WHAT = {"search": "поисков", "peek": "чтений", "resolve": "проверок адресата"}

# C4a: причины коннектора -> (HTTP-код, текст для сессии). Прочие причины — 502 «Telegram отказал: …».
_REASONS: dict[str, tuple[int, str]] = {
    "not_found": (404, "такого канала или группы нет"),
    "not_group": (422, "это человек или бот"),
    "private": (422, "закрытый — читать можно только после вступления (hub_join)"),
    "temporary": (503, "Telegram не отвечает, повторите позже"),
    "query_too_short": (422, "короткий запрос"),
    "auth_required": (503, "Telegram потребовал войти заново — вход делает владелец (hub auth tg)"),
    "unsupported": (422, "этот аккаунт не умеет искать и читать Telegram"),
}


class LookupFailed(Exception):
    """Отказ для сессии: `status` — HTTP-код, `text` — текст ответа, `reason` — короткий код для аудита."""

    def __init__(self, status: int, text: str, *, reason: str) -> None:
        super().__init__(text)
        self.status = status
        self.text = text
        self.reason = reason


class NotFound(Exception):
    """`result` на неизвестную, чужую или уже снятую (таймаут) заявку — 404."""


def _hhmm(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000).strftime("%H:%M")


def failure(reason: str, retry_after_s: float | None = None) -> LookupFailed:
    """C4a: причина коннектора -> отказ с текстом для сессии."""
    if reason == "flood":
        wait = max(1, math.ceil(retry_after_s or 0))
        return LookupFailed(429, f"Telegram просит подождать {wait} с", reason="flood")
    if reason in _REASONS:
        status, text = _REASONS[reason]
        return LookupFailed(status, text, reason=reason)
    return LookupFailed(502, f"Telegram отказал: {reason}"[:300], reason="error")


def _shutdown() -> LookupFailed:
    return LookupFailed(503, "ядро останавливается, повторите позже", reason="shutdown")


class _Ticket:
    __slots__ = ("account_id", "future", "id", "issued_ms", "kind", "params", "stamp")

    def __init__(self, ticket_id: int, account_id: int, kind: str, params: dict, future, stamp: list) -> None:
        self.id = ticket_id
        self.account_id = account_id
        self.kind = kind
        self.params = params
        self.future = future
        self.stamp = stamp  # [время приёма] в окне суточного лимита — убирается, если заявка не выдавалась
        self.issued_ms: int | None = None


class TgLookup:
    """A1: очередь заявок «сессия -> коннектор Telegram» в памяти ядра. `answer_timeout_s` — сколько ручка
    ждёт ответа (тесты укорачивают)."""

    def __init__(self, *, answer_timeout_s: float = ANSWER_TIMEOUT_S) -> None:
        self.answer_timeout_s = answer_timeout_s
        self._ids = itertools.count(1)
        self._queues: dict[int, deque[_Ticket]] = {}
        self._inflight: dict[int, _Ticket] = {}
        self._stamps: dict[tuple[int, str], deque[list]] = {}
        self._paused_until: dict[int, int] = {}
        self._last_issue: dict[int, int] = {}
        self._events: dict[int, asyncio.Event] = {}
        self._resolved: dict[tuple[int, str], tuple[int, str]] = {}
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Ф32-2 п.4 + П3: остановка ядра (зовёт `Notifier.close()` в том же event loop). Ждущие сессии —
        и в очереди, и с заявкой, уже выданной коннектору, — получают 503; выданные забываются (поздний
        `result` коннектора — 404, как после таймаута); висящие `next` просыпаются и отвечают пусто."""
        self._closed = True
        for queue in self._queues.values():
            while queue:
                ticket = queue.popleft()
                if not ticket.future.done():
                    ticket.future.set_exception(_shutdown())
                self._refund(ticket)
        for ticket in self._inflight.values():
            if not ticket.future.done():
                ticket.future.set_exception(_shutdown())
        self._inflight.clear()
        for event in self._events.values():
            event.set()

    # --- сессия ---------------------------------------------------------------------------------------------

    def submit(self, account_id: int, kind: str, params: dict, *, now_ms: int) -> _Ticket:
        """Заявка в очередь аккаунта. Отказы (`LookupFailed` 429): пауза FloodWait, суточный лимит вида,
        очередь полна."""
        if kind not in KINDS:
            raise ValueError(kind)
        if self._closed:
            raise _shutdown()
        self._purge(now_ms)
        paused = self._paused_until.get(account_id, 0)
        if paused > now_ms:
            raise failure("flood", (paused - now_ms) / 1000)
        stamps = self._stamps.setdefault((account_id, kind), deque())
        while stamps and stamps[0][0] <= now_ms - DAY_MS:
            stamps.popleft()
        limit = DAILY[kind]
        if len(stamps) >= limit:
            nxt = stamps[0][0] + DAY_MS
            raise LookupFailed(
                429,
                f"лимит: {limit} {_WHAT[kind]} в сутки на аккаунт Telegram, следующий после {_hhmm(nxt)}",
                reason="limit",
            )
        queue = self._queues.setdefault(account_id, deque())
        if len(queue) >= QUEUE_MAX:
            raise LookupFailed(
                429, "очередь запросов к Telegram занята — повторите через минуту", reason="busy"
            )
        stamp = [now_ms]
        stamps.append(stamp)
        ticket = _Ticket(
            next(self._ids), account_id, kind, params, asyncio.get_running_loop().create_future(), stamp
        )
        queue.append(ticket)
        self._wake(account_id)
        return ticket

    async def wait(self, ticket: _Ticket, *, timeout_s: float | None = None) -> dict:
        """Ответ коннектора (`dict`) или `LookupFailed`. Не дождались `answer_timeout_s` (или `timeout_s`) —
        504, заявка снимается (не выдана) или её поздний ответ игнорируется (404 коннектору)."""
        timeout = self.answer_timeout_s if timeout_s is None else min(timeout_s, self.answer_timeout_s)
        try:
            return await asyncio.wait_for(asyncio.shield(ticket.future), timeout)
        except TimeoutError:
            self._abandon(ticket)
            raise LookupFailed(
                504,
                f"Telegram не ответил за {math.ceil(timeout)} с, повторите позже",
                reason="timeout",
            ) from None
        except asyncio.CancelledError:
            # Сессия ушла (разрыв соединения) — заявка больше никому не нужна.
            self._abandon(ticket)
            raise

    def _abandon(self, ticket: _Ticket) -> None:
        if not ticket.future.done():
            ticket.future.cancel()
        if ticket.issued_ms is None:
            queue = self._queues.get(ticket.account_id)
            if queue is not None:
                with contextlib.suppress(ValueError):
                    queue.remove(ticket)
            self._refund(ticket)

    def _refund(self, ticket: _Ticket) -> None:
        """Заявка не дошла до Telegram — в суточный лимит не считается."""
        stamps = self._stamps.get((ticket.account_id, ticket.kind))
        if stamps is not None:
            with contextlib.suppress(ValueError):
                stamps.remove(ticket.stamp)

    def _purge(self, now_ms: int) -> None:
        stale = [
            t.id
            for t in self._inflight.values()
            if t.issued_ms is not None and now_ms - t.issued_ms > _INFLIGHT_TTL_MS and t.future.done()
        ]
        for ticket_id in stale:
            del self._inflight[ticket_id]

    # --- коннектор ------------------------------------------------------------------------------------------

    def _event(self, account_id: int) -> asyncio.Event:
        event = self._events.get(account_id)
        if event is None:
            event = self._events[account_id] = asyncio.Event()
        return event

    def _wake(self, account_id: int) -> None:
        event = self._events.get(account_id)
        if event is not None:
            event.set()

    def _try_issue(self, account_id: int, now_ms: int) -> tuple[dict | None, int | None]:
        """`(заявка, None)` — выдана; `(None, мс до срока)` — есть, но темп/пауза; `(None, None)` — пусто."""
        queue = self._queues.get(account_id)
        while queue and queue[0].future.done():
            queue.popleft()
        if not queue:
            return None, None
        paused = self._paused_until.get(account_id, 0)
        if paused > now_ms:
            return None, paused - now_ms
        last = self._last_issue.get(account_id)
        if last is not None and now_ms < last + GAP_MS:
            return None, last + GAP_MS - now_ms
        ticket = queue.popleft()
        ticket.issued_ms = now_ms
        self._last_issue[account_id] = now_ms
        self._inflight[ticket.id] = ticket
        return {"id": ticket.id, "kind": ticket.kind, "params": ticket.params}, None

    async def next(self, account_id: int, timeout_s: float, clock_ms: Callable[[], int]) -> dict | None:
        """Long-poll коннектора: одна заявка `{id, kind, params}` или `None` за `timeout_s` (≤ 25 с). Интервал
        `GAP_MS` между выдачами аккаунта и пауза FloodWait соблюдаются здесь."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, min(float(timeout_s), NEXT_MAX_WAIT_S))
        event = self._event(account_id)
        while True:
            if self._closed:
                # Ф32-2 П3: до `_try_issue` — заявка не уходит в «выдана» (ответа коннектор уже не донесёт).
                return None
            event.clear()
            item, due_ms = self._try_issue(account_id, clock_ms())
            if item is not None:
                return item
            left = deadline - loop.time()
            if left <= 0:
                return None
            if due_ms is not None:
                left = min(left, max(due_ms / 1000, _MIN_WAIT_S))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(event.wait(), left)

    def result(
        self,
        account_id: int,
        lookup_id: int,
        *,
        outcome: str,
        payload: dict | None,
        reason: str | None,
        retry_after_s: float | None,
        now_ms: int,
    ) -> None:
        """Ответ коннектора. `ok` — ответ сессии; `error` — отказ с причиной (C4a); `flood` ставит паузу
        аккаунта (и отбивает ждущие в очереди). Неизвестная/чужая/снятая заявка — `NotFound`."""
        ticket = self._inflight.get(lookup_id)
        if ticket is None or ticket.account_id != account_id:
            raise NotFound(lookup_id)
        del self._inflight[lookup_id]
        if outcome == "error" and reason == "flood" and retry_after_s is not None and retry_after_s > 0:
            self._pause(account_id, now_ms + math.ceil(retry_after_s * 1000), now_ms)
        if ticket.future.done():
            raise NotFound(lookup_id)
        if outcome == "ok":
            ticket.future.set_result(payload if isinstance(payload, dict) else {})
        else:
            ticket.future.set_exception(failure(reason or "error", retry_after_s))

    def _pause(self, account_id: int, until_ms: int, now_ms: int) -> None:
        self._paused_until[account_id] = max(self._paused_until.get(account_id, 0), until_ms)
        queue = self._queues.get(account_id)
        while queue:
            ticket = queue.popleft()
            if not ticket.future.done():
                ticket.future.set_exception(failure("flood", (until_ms - now_ms) / 1000))
            self._refund(ticket)

    def paused_until(self, account_id: int) -> int:
        return self._paused_until.get(account_id, 0)

    # --- 24.10: кто это по @имени (проверка адресата `hub_draft_new`) ------------------------------------

    async def resolve_kind(self, account_id: int, username: str, *, now_ms: int) -> str | None:
        """Вид адресата по @имени: `user`, `bot`, `broadcast`, `megagroup`, `group` — или `None`, если узнать
        не вышло (лимит, пауза FloodWait, очередь занята, коннектор молчит или старый, имени нет). Не
        бросает: проверка — довесок к созданию заготовки, решает отправка. Ответ помнится
        `RESOLVE_CACHE_MS`."""
        key = (account_id, username.casefold())
        cached = self._resolved.get(key)
        if cached is not None and now_ms - cached[0] < RESOLVE_CACHE_MS:
            return cached[1]
        try:
            payload = await self.wait(
                self.submit(account_id, "resolve", {"username": username}, now_ms=now_ms),
                timeout_s=RESOLVE_TIMEOUT_S,
            )
        except LookupFailed:
            return None
        kind = payload.get("kind")
        if kind not in ("user", "bot", *NOT_PERSON_KINDS):
            return None
        if len(self._resolved) >= RESOLVE_CACHE_MAX:
            self._resolved.pop(next(iter(self._resolved)))
        self._resolved[key] = (now_ms, kind)
        return kind


# --- проверки ручек (A2) ----------------------------------------------------------------------------------


def resolve_ready(conn: sqlite3.Connection, account_id: int, *, now_ms: int) -> bool:
    """24.10: спрашивать ли коннектор, кто это по @имени: аккаунт Telegram, вход не потерян, коннектор жив
    (пульс свежее `CONNECTOR_SILENT_MS`). Иначе проверку не делаем — заготовка как раньше."""
    row = conn.execute(
        "SELECT source, status, last_ok_ts FROM main.account WHERE id = ?", (account_id,)
    ).fetchone()
    if row is None or row[0] != "telegram" or row[1] in ("auth_required", "limited"):
        return False
    return row[2] is not None and now_ms - int(row[2]) <= CONNECTOR_SILENT_MS


def not_person_text(username: str, kind: str) -> str:
    """Отказ `hub_draft_new` по @имени канала или группы."""
    what = NOT_PERSON_KINDS.get(kind, "группа")
    return (
        f"@{username} — {what}, а не человек: первое сообщение — только человеку или боту. Вступить — "
        "hub_join, читать без вступления — hub_tg_peek"
    )


def prepare(conn: sqlite3.Connection, principal, account_id: int | None, *, now_ms: int) -> int:
    """Запреты `check_attach` (403), выбор аккаунта как у `hub_join` (422), коннектор запущен (503).
    Возвращает аккаунт."""
    row = conn.execute("SELECT name FROM main.api_token WHERE id = ?", (principal.token_id,)).fetchone()
    name = row[0] if row and isinstance(row[0], str) else None
    try:
        check_attach(name=name, kind=principal.kind, scopes=principal.scopes)
    except ValueError as exc:
        raise LookupFailed(
            403,
            "поиск и чтение Telegram — только из интерактивной сессии (не дежурная и не оркестратор)",
            reason="denied",
        ) from exc
    if account_id is not None:
        # Ф62-6 П5: вступление теперь и WhatsApp — поиск и чтение остаются только Telegram.
        source = conn.execute("SELECT source FROM main.account WHERE id = ?", (account_id,)).fetchone()
        if source is not None and source[0] not in ("telegram", "fake"):
            raise LookupFailed(
                422, "account_id: поиск и чтение — только аккаунтом Telegram", reason="account"
            )
    try:
        picked = join_mod._pick_account(conn, account_id)  # noqa: SLF001 — тот же выбор, что у hub_join
    except join_mod.JoinError as exc:
        raise LookupFailed(422, str(exc), reason="account") from exc
    status, last_ok_ts = conn.execute(
        "SELECT status, last_ok_ts FROM main.account WHERE id = ?", (picked,)
    ).fetchone()
    if status == "auth_required":
        raise LookupFailed(
            503, f"коннектор Telegram #{picked}: нужен вход (hub auth tg)", reason="no_connector"
        )
    if last_ok_ts is None or now_ms - int(last_ok_ts) > CONNECTOR_SILENT_MS:
        raise LookupFailed(503, f"коннектор Telegram #{picked} не запущен", reason="no_connector")
    return picked


def channel_ids(conn: sqlite3.Connection, account_id: int, ext_ids: list[str]) -> dict[str, int]:
    """Беседы хаба этого аккаунта по ext_id (`chat.channel_id`, «уже в хабе #id»)."""
    row = conn.execute("SELECT db FROM main.account WHERE id = ?", (account_id,)).fetchone()
    schema = row[0] if row is not None else None
    if schema not in ("main", "work") or not ext_ids:
        return {}
    out: dict[str, int] = {}
    for ext_id in dict.fromkeys(ext_ids):
        found = conn.execute(
            f"SELECT id FROM {schema}.channel WHERE account_id = ? AND ext_id = ?",  # noqa: S608 — схема из списка
            (account_id, ext_id),
        ).fetchone()
        if found is not None:
            out[ext_id] = found[0]
    return out


# --- ответ сессии: только известные поля (C2/C3) -------------------------------------------------------

_SEARCH_FIELDS = (
    "ext_id",
    "username",
    "title",
    "kind",
    "participants",
    "member",
    "verified",
    "scam",
    "fake",
    "join_request",
)
_CHAT_FIELDS = ("ext_id", "username", "title", "kind", "participants", "about", "member")
_MESSAGE_FIELDS = (
    "id",
    "ts",
    "author",
    "fwd",
    "text",
    "media",
    "views",
    "forwards",
    "replies",
    "reactions",
    "album",
)


def _pick(item: Any, fields: tuple[str, ...]) -> dict | None:
    if not isinstance(item, dict):
        return None
    return {k: item.get(k) for k in fields}


def search_response(conn: sqlite3.Connection, account_id: int, payload: dict) -> dict:
    results = [r for r in (_pick(x, _SEARCH_FIELDS) for x in payload.get("results") or []) if r is not None]
    known = channel_ids(conn, account_id, [r["ext_id"] for r in results if isinstance(r.get("ext_id"), str)])
    for r in results:
        r["channel_id"] = known.get(r["ext_id"]) if isinstance(r.get("ext_id"), str) else None
    return {"account_id": account_id, "results": results}


def peek_response(conn: sqlite3.Connection, account_id: int, payload: dict) -> dict:
    chat = _pick(payload.get("chat"), _CHAT_FIELDS) or {k: None for k in _CHAT_FIELDS}
    if isinstance(chat.get("about"), str):
        chat["about"] = chat["about"][:ABOUT_MAX]
    ext_id = chat.get("ext_id")
    chat["channel_id"] = (
        channel_ids(conn, account_id, [ext_id]).get(ext_id) if isinstance(ext_id, str) else None
    )
    messages = [
        m for m in (_pick(x, _MESSAGE_FIELDS) for x in payload.get("messages") or []) if m is not None
    ]
    next_before = payload.get("next_before")
    if not isinstance(next_before, int) or isinstance(next_before, bool):
        next_before = None
    return {"account_id": account_id, "chat": chat, "messages": messages, "next_before": next_before}
