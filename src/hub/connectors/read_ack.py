"""Толкатель прочитанности коннектора (Ф8-7, `docs/architecture.md`).

Отдельная задача коннектора, по образцу загрузчика медиа (`connectors/media.py`): `wait_read_ack` ->
`lease_read_ack(limit=1)` -> `push_read(item)` коннектора (не дольше 60 с) -> `report_read_ack`. Задания — по
одному; своя задача — сверку и отправку не держит. Что именно звать у источника — решает коннектор
(`push_read`: Telegram `readHistory`/`ReadDiscussionRequest`, Slack `conversations.mark`, почта `\\Seen`).

`retry_after_s` от коннектора (FloodWait, `Retry-After`) — пауза ВСЕГО цикла на это время (П4), как у медиа.
Журнал — только `channel_id`, итог и класс ошибки/`reason`: без текстов и идентификаторов источника.
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


class ReadAckResult(NamedTuple):
    outcome: Literal["done", "retry", "drop"]
    retry_after_s: float | None = None
    reason: str | None = None


_logger = logging.getLogger(__name__)

WAIT_READ_ACK_S = 25.0
PUSH_TIMEOUT_S = 60.0
_BACKOFF_BASE_S = 1.0
_BACKOFF_CAP_S = 30.0
_BLOCKED_SLEEP_S = 60.0
_MAX_RETRY_AFTER_S = 86_400.0  # потолок ядра для `retry_after_ms` — сутки
_OUTCOMES = frozenset({"done", "retry", "drop"})

PushFn = Callable[[dict], Awaitable[ReadAckResult]]


class ReadAcker:
    """Цикл толкателя. `push` — `ConnectorBase.push_read`; `mono`/`sleep` — часы коннектора (тесты
    подменяют); `is_blocked()` — вход в источник потерян (`auth_required`) — задания не берутся;
    `on_auth_required()` — `push` поднял `AuthRequired` (отчёта нет: лиз истечёт сам); `on_unauthorized()` —
    ядро ответило 401 (секрет отозван) — выход 78, как у остальных циклов каркаса."""

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
                ready = await self._client.wait_read_ack(WAIT_READ_ACK_S)
                if ready:
                    await self.step()
            except asyncio.CancelledError:
                if _own_task_cancelling():
                    raise
                _logger.warning("read_ack: чужая отмена в цикле — повтор")
                continue
            except Exception as exc:  # noqa: BLE001 — цикл не падает
                if _is_unauthorized(exc):
                    self._stopped = True
                    self._on_unauthorized()
                    return
                if not _is_core_unreachable(exc):
                    _logger.warning("read_ack: запрос к ядру не прошёл: %s", type(exc).__name__)
                await self._sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_CAP_S)
                continue
            backoff = _BACKOFF_BASE_S

    async def step(self) -> int:
        """Один шаг: взять одно задание и обработать. Число обработанных заданий."""
        items = await self._client.lease_read_ack(limit=1)
        for item in items:
            await self.process(item)
        return len(items)

    async def process(self, item: dict) -> None:
        channel_id = item["channel_id"]
        lease_gen = item["lease_gen"]
        try:
            result = await asyncio.wait_for(self._push(item), timeout=PUSH_TIMEOUT_S)
        except AuthRequired:
            # Вход в источник потерян: отчёта нет, лиз истечёт сам (как у медиа и отправки).
            await self._on_auth_required()
            return
        except TimeoutError:
            result = ReadAckResult("retry", reason="timeout")
        except asyncio.CancelledError:
            if _own_task_cancelling():
                raise
            result = ReadAckResult("retry", reason="cancelled")
        except Exception as exc:  # noqa: BLE001 — сбой одного задания не роняет цикл
            result = ReadAckResult("retry", reason=type(exc).__name__)
        if not isinstance(result, tuple) or getattr(result, "outcome", None) not in _OUTCOMES:
            result = ReadAckResult("retry", reason="bad_result")

        retry_after_s = result.retry_after_s
        if retry_after_s is not None:
            valid = (
                isinstance(retry_after_s, int | float) and math.isfinite(retry_after_s) and retry_after_s >= 0
            )
            retry_after_s = min(float(retry_after_s), _MAX_RETRY_AFTER_S) if valid else None
        if retry_after_s is not None:
            # П4: источник велел ждать — ждёт весь цикл, не только эта беседа.
            self._paused_until = self._mono() + retry_after_s

        if result.outcome == "done":
            _logger.debug("read_ack: channel_id=%s done", channel_id)
        else:
            _logger.info("read_ack: channel_id=%s итог %s (%s)", channel_id, result.outcome, result.reason)
        retry_after_ms = math.ceil(retry_after_s * 1000) if retry_after_s is not None else None
        try:
            await self._client.report_read_ack(
                channel_id=channel_id,
                lease_gen=lease_gen,
                outcome=result.outcome,
                retry_after_ms=retry_after_ms,
            )
        except Exception as exc:
            # 404/409 (строку сняли или перехватили) и прочие 4xx — ядро решило само, дальше по очереди.
            # 401 — наверх (выход 78). Ядро недоступно — лиз истечёт, ядро выдаст задание снова.
            if _is_rejected(exc):
                _logger.warning(
                    "read_ack: ядро отвергло отчёт channel_id=%s (HTTP %s)",
                    channel_id,
                    exc.response.status_code,
                )
                return
            if _is_core_unreachable(exc):
                _logger.warning(
                    "read_ack: отчёт channel_id=%s — ядро недоступно: %s", channel_id, type(exc).__name__
                )
                return
            raise
