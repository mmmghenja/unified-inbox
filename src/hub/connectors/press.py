"""Исполнитель нажатий кнопок ботов (Ф44-1c, `docs/architecture.md`).

Отдельная задача коннектора по образцу `connectors/join.Joiner`: `wait_presses` -> `lease_presses(limit=1)` ->
`push_press(item)` коннектора (не дольше 25 с) -> `report_press`. Отличие от прочих очередей — нажатие
НЕ ПОВТОРЯЕТСЯ: всё, что случилось после возможной отправки запроса (таймаут шага, отмена, непонятная ошибка),
— исход `unknown`, а не «попробовать снова». `retry` — только когда запрос заведомо не ушёл (нет связи, пауза
FloodWait): срок годности решения держит ядро.

Темп: не чаще одного нажатия в 2 с на аккаунт (один процесс коннектора — один аккаунт). Журнал — номер
нажатия и исход: без текста кнопки, callback и ответа бота.
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


class PressResult(NamedTuple):
    """Итог нажатия. `done` — с `result` (`message`, `alert`, `url` — ответ бота; `timeout: true` — бот на
    запрос не ответил, это обычный исход); `failed` — с `reason` (`stale_button`, класс ошибки); `unknown` —
    запрос ушёл, исхода нет; `retry` — запрос НЕ уходил: с `retry_after_s` и `reason="flood"` (FloodWait) или
    без паузы (сеть)."""

    outcome: Literal["done", "failed", "unknown", "retry"]
    result: dict | None = None
    reason: str | None = None
    retry_after_s: float | None = None


_logger = logging.getLogger(__name__)

WAIT_PRESSES_S = 25.0
PUSH_TIMEOUT_S = 25.0
PACE_S = 2.0
_BACKOFF_BASE_S = 1.0
_BACKOFF_CAP_S = 30.0
_BLOCKED_SLEEP_S = 60.0
_MAX_RETRY_AFTER_S = 86_400.0
_OUTCOMES = frozenset({"done", "failed", "unknown", "retry"})
_REASON_MAX = 200

PushFn = Callable[[dict], Awaitable[PressResult]]


class Presser:
    """Цикл исполнителя. `push` — `ConnectorBase.push_press`; `mono`/`sleep` — часы коннектора (тесты
    подменяют); `is_blocked()` — вход в источник потерян; `on_auth_required()` — `push` поднял `AuthRequired`
    (отчёта нет: лиз истечёт, ядро закроет строку как `unknown`); `on_unauthorized()` — ядро ответило 401."""

    def __init__(
        self,
        client: CoreClient,
        push: PushFn,
        *,
        mono: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
        is_blocked: Callable[[], bool],
        on_auth_required: Callable[[], Awaitable[None]],
        on_unauthorized: Callable[[], None],
    ) -> None:
        self._client = client
        self._push = push
        self._mono = mono
        self._sleep = sleep
        self._is_blocked = is_blocked
        self._on_auth_required = on_auth_required
        self._on_unauthorized = on_unauthorized
        self._next_at: float | None = None
        self._stopped = False

    async def run(self) -> None:
        backoff = _BACKOFF_BASE_S
        while not self._stopped:
            if self._is_blocked():
                await self._sleep(_BLOCKED_SLEEP_S)
                continue
            try:
                ready = await self._client.wait_presses(WAIT_PRESSES_S)
                if ready:
                    await self.step()
            except asyncio.CancelledError:
                if _own_task_cancelling():
                    raise
                _logger.warning("press: чужая отмена в цикле — повтор")
                continue
            except Exception as exc:  # noqa: BLE001 — цикл не падает
                if _is_unauthorized(exc):
                    self._stopped = True
                    self._on_unauthorized()
                    return
                if not _is_core_unreachable(exc):
                    _logger.warning("press: запрос к ядру не прошёл: %s", type(exc).__name__)
                await self._sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_CAP_S)
                continue
            backoff = _BACKOFF_BASE_S

    async def step(self) -> int:
        """Один шаг: выдержать темп, взять одно нажатие и выполнить. Число обработанных."""
        if self._next_at is not None:
            left = self._next_at - self._mono()
            if left > 0:
                # Темп — ДО выдачи: срок годности решения (60 с) идёт в ядре, а не под лизом.
                await self._sleep(left)
        items = await self._client.lease_presses(limit=1)
        for item in items:
            await self.process(item)
        return len(items)

    async def process(self, item: dict) -> None:
        press_id = item["id"]
        self._next_at = self._mono() + PACE_S
        try:
            result = await asyncio.wait_for(self._push(item), timeout=PUSH_TIMEOUT_S)
        except AuthRequired:
            await self._on_auth_required()
            return
        except TimeoutError:
            # Шаг не уложился: запрос мог уйти — повторять нельзя.
            result = PressResult("unknown", reason="step_timeout")
        except asyncio.CancelledError:
            if _own_task_cancelling():
                raise
            result = PressResult("unknown", reason="cancelled")
        except Exception as exc:  # noqa: BLE001 — сбой одного нажатия не роняет цикл; ушёл ли запрос — неясно
            result = PressResult("unknown", reason=type(exc).__name__)
        if not isinstance(result, PressResult) or result.outcome not in _OUTCOMES:
            result = PressResult("unknown", reason="bad_result")

        body: dict = {"lease_gen": item["lease_gen"], "outcome": result.outcome}
        if result.outcome == "done":
            body["result"] = result.result or {}
        elif result.reason is not None:
            body["reason"] = str(result.reason)[:_REASON_MAX]
        retry_after_s = result.retry_after_s
        if result.outcome == "retry" and retry_after_s is not None:
            valid = (
                isinstance(retry_after_s, int | float) and math.isfinite(retry_after_s) and retry_after_s >= 0
            )
            if valid:
                body["retry_after_ms"] = math.ceil(min(float(retry_after_s), _MAX_RETRY_AFTER_S) * 1000)
        _logger.info("press: id=%s исход %s (%s)", press_id, result.outcome, result.reason)
        try:
            await self._client.report_press(press_id, body)
        except Exception as exc:
            # 404/409 (лиз истёк, строку закрыли) — ядро решило само. 401 — наверх (выход 78). Ядро
            # недоступно — лиз истечёт, строка станет `unknown`: отчёт не повторяется.
            if _is_rejected(exc):
                _logger.warning(
                    "press: ядро отвергло отчёт id=%s (HTTP %s)", press_id, exc.response.status_code
                )
                return
            if _is_core_unreachable(exc):
                _logger.warning("press: отчёт id=%s — ядро недоступно: %s", press_id, type(exc).__name__)
                return
            raise
