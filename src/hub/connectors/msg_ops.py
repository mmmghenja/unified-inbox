"""Толкатель правок и «удалить у всех» коннектора (Ф14-8, `docs/architecture.md`) — копия устройства
`connectors/reactions.py` (Ф10-3), своя задача рядом с ним.

`wait_msg_ops` -> `lease_msg_ops(limit=1)` -> `push_msg_op(job)` коннектора (не дольше 60 с; лиз ядра —
120 с, П5) -> `flush_events` (П7: событие с правдой, выпущенное в `push`, уходит в ядро РАНЬШЕ отчёта; не
дольше 10 с) -> `report_msg_op`. Задания — по одному. Что звать у источника, решает коннектор
(`push_msg_op`: Telegram `EditMessage`/`DeleteMessages`, Slack `chat.update`/`chat.delete`); он же после
успеха шлёт событие `edit`/`delete` с правдой источника.

`retry_after_s` от коннектора (FloodWait, `Retry-After`) — пауза ВСЕГО цикла на это время, как у реакций.
Журнал — только `message_id`, op, итог и код/класс ошибки: без текстов и идентификаторов источника.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from typing import Literal, NamedTuple

from .base import (
    AuthRequired,
    CoreClient,
    _is_core_unreachable,
    _is_rejected,
    _is_unauthorized,
    _own_task_cancelling,
)


class MsgOpResult(NamedTuple):
    """Итог `push_msg_op`: `done` — источник выполнил (правду коннектор уже выпустил событием); `retry` —
    повторить позже (`retry_after_s` — пауза источника); `fail` — окончательно, `code` из
    `expired|forbidden|not_found|too_long|other`, `detail` — класс/код ошибки, БЕЗ текста сообщения."""

    outcome: Literal["done", "retry", "fail"]
    retry_after_s: float | None = None
    code: str | None = None
    detail: str | None = None


_logger = logging.getLogger(__name__)

WAIT_MSG_OPS_S = 25.0
PUSH_TIMEOUT_S = 60.0
# П7: сколько ждать, пока событие с правдой уйдёт в ядро, прежде чем отчитаться всё равно.
FLUSH_TIMEOUT_S = 10.0
_BACKOFF_BASE_S = 1.0
_BACKOFF_CAP_S = 30.0
_BLOCKED_SLEEP_S = 60.0
_MAX_RETRY_AFTER_S = 86_400.0  # потолок ядра для `retry_after_ms` — сутки
_OUTCOMES = frozenset({"done", "retry", "fail"})
FAIL_CODES = frozenset({"expired", "forbidden", "not_found", "too_long", "other"})
_MAX_DETAIL_LEN = 200

PushFn = Callable[[dict], Awaitable[MsgOpResult]]
FlushFn = Callable[[float], Awaitable[object]]


class MsgOpWorker:
    """Цикл толкателя. `push` — `ConnectorBase.push_msg_op`; `flush` — `ConnectorBase.flush_events` (П7);
    `mono`/`sleep` — часы коннектора (тесты подменяют); `is_blocked()` — вход в источник потерян
    (`auth_required`) — задания не берутся; `on_auth_required()` — `push` поднял `AuthRequired` (отчёта нет:
    лиз истечёт сам); `on_unauthorized()` — ядро ответило 401 (секрет отозван) — выход 78, как у остальных
    циклов каркаса."""

    def __init__(
        self,
        client: CoreClient,
        push: PushFn,
        *,
        flush: FlushFn,
        mono: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
        is_blocked: Callable[[], bool],
        on_auth_required: Callable[[], Awaitable[None]],
        on_unauthorized: Callable[[], None],
    ) -> None:
        self._client = client
        self._push = push
        self._flush = flush
        self._mono = mono
        self._sleep = sleep
        self._is_blocked = is_blocked
        self._on_auth_required = on_auth_required
        self._on_unauthorized = on_unauthorized
        self._paused_until: float | None = None
        self._stopped = False

    @property
    def paused_until(self) -> float | None:
        return self._paused_until

    async def run(self) -> None:
        backoff = _BACKOFF_BASE_S
        while not self._stopped:
            if self._is_blocked():
                await self._sleep(_BLOCKED_SLEEP_S)
                continue
            if self._paused_until is not None:
                left = self._paused_until - self._mono()
                if left > 0:
                    await self._sleep(left)
                    continue
                self._paused_until = None
            try:
                ready = await self._client.wait_msg_ops(WAIT_MSG_OPS_S)
                if ready:
                    await self.step()
            except asyncio.CancelledError:
                if _own_task_cancelling():
                    raise
                _logger.warning("msg_ops: чужая отмена в цикле — повтор")
                continue
            except Exception as exc:  # noqa: BLE001 — цикл не падает
                if _is_unauthorized(exc):
                    self._stopped = True
                    self._on_unauthorized()
                    return
                if not _is_core_unreachable(exc):
                    _logger.warning("msg_ops: запрос к ядру не прошёл: %s", type(exc).__name__)
                await self._sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_CAP_S)
                continue
            backoff = _BACKOFF_BASE_S

    async def step(self) -> int:
        """Один шаг: взять одно задание и обработать. Число обработанных заданий."""
        items = await self._client.lease_msg_ops(limit=1)
        for item in items:
            await self.process(item)
        return len(items)

    async def _run_push(self, item: dict) -> MsgOpResult | None:
        """`push` с таймаутом; `None` — `AuthRequired` (хук вызван, отчёта не будет)."""
        try:
            result = await asyncio.wait_for(self._push(item), timeout=PUSH_TIMEOUT_S)
        except AuthRequired:
            # Вход в источник потерян: отчёта нет, лиз истечёт сам (как у реакций и отправки).
            await self._on_auth_required()
            return None
        except TimeoutError:
            return MsgOpResult("retry", detail="timeout")
        except asyncio.CancelledError:
            if _own_task_cancelling():
                raise
            return MsgOpResult("retry", detail="cancelled")
        except Exception as exc:  # noqa: BLE001 — сбой одного задания не роняет цикл
            return MsgOpResult("retry", detail=type(exc).__name__)
        if not isinstance(result, tuple) or getattr(result, "outcome", None) not in _OUTCOMES:
            return MsgOpResult("retry", detail="bad_result")
        if result.outcome == "fail" and result.code not in FAIL_CODES:
            return MsgOpResult("fail", code="other", detail=result.detail or "bad_code")
        return result

    async def process(self, item: dict) -> None:
        message_id = item["message_id"]
        lease_gen = item["lease_gen"]
        op = item.get("op")
        result = await self._run_push(item)
        if result is None:
            return

        retry_after_s = result.retry_after_s
        if retry_after_s is not None:
            valid = (
                isinstance(retry_after_s, int | float) and math.isfinite(retry_after_s) and retry_after_s >= 0
            )
            retry_after_s = min(float(retry_after_s), _MAX_RETRY_AFTER_S) if valid else None
        if retry_after_s is not None and result.outcome == "retry":
            # Источник велел ждать — ждёт весь цикл, не только это сообщение (как у реакций).
            self._paused_until = self._mono() + retry_after_s
        else:
            retry_after_s = None

        detail = None if result.detail is None else str(result.detail)[:_MAX_DETAIL_LEN]
        if result.outcome == "done":
            _logger.info("msg_ops: message_id=%s op=%s done", message_id, op)
        else:
            reason = result.code or detail
            _logger.info("msg_ops: message_id=%s op=%s итог %s (%s)", message_id, op, result.outcome, reason)

        # П7: событие с правдой (`edit`/`delete`), выпущенное в `push`, — в ядро раньше отчёта.
        try:
            await self._flush(FLUSH_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 — отчёт важнее: сбой ожидания буфера его не держит
            _logger.warning("msg_ops: ожидание буфера событий не удалось: %s", type(exc).__name__)

        retry_after_ms = math.ceil(retry_after_s * 1000) if retry_after_s is not None else None
        try:
            await self._client.report_msg_op(
                message_id=message_id,
                lease_gen=lease_gen,
                outcome=result.outcome,
                code=result.code if result.outcome == "fail" else None,
                detail=detail,
                retry_after_ms=retry_after_ms,
            )
        except Exception as exc:
            # 404/409 (строку сняли или перехватили) и прочие 4xx — ядро решило само, дальше по очереди.
            # 401 — наверх (выход 78). Ядро недоступно — лиз истечёт, ядро выдаст задание снова.
            if _is_rejected(exc):
                _logger.warning(
                    "msg_ops: ядро отвергло отчёт message_id=%s (HTTP %s)",
                    message_id,
                    exc.response.status_code,
                )
                return
            if _is_core_unreachable(exc):
                _logger.warning(
                    "msg_ops: отчёт message_id=%s — ядро недоступно: %s", message_id, type(exc).__name__
                )
                return
            raise
