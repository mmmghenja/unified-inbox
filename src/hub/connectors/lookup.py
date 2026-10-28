"""Исполнитель поиска и чтения Telegram без вступления (Ф29-1, `docs/architecture.md`).

Отдельная задача коннектора рядом с толкателем прочитанности (`connectors/read_ack.py`): `lookup_next`
(long-poll ядра) -> `run_lookup(item)` коннектора (не дольше 30 с) -> `lookup_result`. Темп, лимиты и паузы
FloodWait держит ядро (`core/tg_lookup.py`), здесь — только исполнение. Ничего не хранится.

Журнал — вид, исход, класс причины и время: без строки поиска, имён и текстов.
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


class LookupResult(NamedTuple):
    """Итог заявки: `ok` — с `payload` (ответ сессии); `error` — с `reason` (`not_found`, `not_group`,
    `private`, `flood` + `retry_after_s`, `temporary`, `query_too_short`, `auth_required`, `unsupported` или
    код ошибки Telegram)."""

    outcome: Literal["ok", "error"]
    payload: dict | None = None
    reason: str | None = None
    retry_after_s: float | None = None


_logger = logging.getLogger(__name__)

WAIT_LOOKUP_S = 25.0
RUN_TIMEOUT_S = 30.0
_BACKOFF_BASE_S = 1.0
_BACKOFF_CAP_S = 30.0
_BLOCKED_SLEEP_S = 60.0
_MAX_RETRY_AFTER_S = 86_400.0
_REASON_MAX = 200

RunFn = Callable[[dict], Awaitable[LookupResult]]


class Lookuper:
    """Цикл исполнителя. `run` — `ConnectorBase.run_lookup`; `mono`/`sleep` — часы коннектора (тесты
    подменяют); `is_blocked()` — вход в источник потерян (заявки не берутся); `on_auth_required()` — `run`
    поднял `AuthRequired` (после отчёта `auth_required`); `on_unauthorized()` — ядро ответило 401 (выход
    78)."""

    def __init__(
        self,
        client: CoreClient,
        run: RunFn,
        *,
        mono: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
        is_blocked: Callable[[], bool],
        on_auth_required: Callable[[], Awaitable[None]],
        on_unauthorized: Callable[[], None],
    ) -> None:
        self._client = client
        self._run = run
        self._mono = mono
        self._sleep = sleep
        self._is_blocked = is_blocked
        self._on_auth_required = on_auth_required
        self._on_unauthorized = on_unauthorized
        self._stopped = False

    async def run(self) -> None:
        backoff = _BACKOFF_BASE_S
        while not self._stopped:
            if self._is_blocked():
                await self._sleep(_BLOCKED_SLEEP_S)
                continue
            try:
                await self.step()
            except asyncio.CancelledError:
                if _own_task_cancelling():
                    raise
                _logger.warning("lookup: чужая отмена в цикле — повтор")
                continue
            except Exception as exc:  # noqa: BLE001 — цикл не падает
                if _is_unauthorized(exc):
                    self._stopped = True
                    self._on_unauthorized()
                    return
                if not _is_core_unreachable(exc):
                    _logger.warning("lookup: запрос к ядру не прошёл: %s", type(exc).__name__)
                await self._sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_CAP_S)
                continue
            backoff = _BACKOFF_BASE_S

    async def step(self) -> bool:
        """Один шаг: дождаться заявки и исполнить. `True` — заявка была."""
        item = await self._client.lookup_next(WAIT_LOOKUP_S)
        if item is None:
            return False
        await self.process(item)
        return True

    async def process(self, item: dict) -> None:
        lookup_id = item.get("id")
        kind = item.get("kind")
        started = self._mono()
        auth_lost = False
        try:
            result = await asyncio.wait_for(self._run(item), timeout=RUN_TIMEOUT_S)
        except AuthRequired:
            auth_lost = True
            result = LookupResult("error", reason="auth_required")
        except TimeoutError:
            result = LookupResult("error", reason="temporary")
        except asyncio.CancelledError:
            if _own_task_cancelling():
                raise
            result = LookupResult("error", reason="temporary")
        except Exception as exc:  # noqa: BLE001 — сбой одной заявки не роняет цикл
            result = LookupResult("error", reason=type(exc).__name__)
        if not isinstance(result, LookupResult) or result.outcome not in ("ok", "error"):
            result = LookupResult("error", reason="bad_result")

        retry_after_s = result.retry_after_s
        if retry_after_s is not None:
            valid = (
                isinstance(retry_after_s, int | float) and math.isfinite(retry_after_s) and retry_after_s >= 0
            )
            retry_after_s = min(float(retry_after_s), _MAX_RETRY_AFTER_S) if valid else None
        elapsed_ms = int(max(0.0, self._mono() - started) * 1000)
        _logger.info(
            "lookup: вид=%s, исход=%s, класс=%s, %d мс",
            kind,
            result.outcome,
            result.reason if result.outcome == "error" else "-",
            elapsed_ms,
        )
        body: dict = {"id": lookup_id, "outcome": result.outcome}
        if result.outcome == "ok":
            body["payload"] = result.payload if isinstance(result.payload, dict) else {}
        else:
            body["reason"] = str(result.reason or "error")[:_REASON_MAX]
            if retry_after_s is not None:
                body["retry_after_s"] = retry_after_s
        try:
            await self._client.lookup_result(body)
        except Exception as exc:
            # 404 — ядро уже не ждёт (таймаут сессии, рестарт ядра); прочие 4xx — ядро решило само. 401 —
            # наверх (выход 78). Ядро недоступно — сессия получит 504 и повторит.
            if _is_rejected(exc):
                _logger.info("lookup: ядро не приняло ответ (HTTP %s)", exc.response.status_code)
            elif _is_core_unreachable(exc):
                _logger.warning("lookup: ответ не доставлен — ядро недоступно: %s", type(exc).__name__)
            else:
                raise
        if auth_lost:
            await self._on_auth_required()
