"""Толкатель реакций коннектора (Ф10-3, `docs/architecture.md`) — копия
устройства `connectors/read_ack.py` (Ф8-7), своя задача рядом с ним.

`wait_reactions` -> `lease_reactions(limit=1)` -> `push_reaction(job)` коннектора (не дольше 60 с) ->
`report_reaction`. Задания — по одному. Что именно звать у источника — решает коннектор (`push_reaction`:
Telegram `SendReactionRequest`, Slack `reactions.add`/`reactions.remove`); он же после успеха шлёт событие
`reaction` с правдой источника.

`retry_after_s` от коннектора (FloodWait, `Retry-After`) — пауза ВСЕГО цикла на это время, как у
прочитанности.
Журнал — только `message_id`, итог и класс ошибки/`reason`: без текстов, эмодзи и идентификаторов источника.
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


class ReactionResult(NamedTuple):
    outcome: Literal["done", "retry", "drop"]
    retry_after_s: float | None = None
    reason: str | None = None


_logger = logging.getLogger(__name__)

WAIT_REACTIONS_S = 25.0
PUSH_TIMEOUT_S = 60.0
_BACKOFF_BASE_S = 1.0
_BACKOFF_CAP_S = 30.0
_BLOCKED_SLEEP_S = 60.0
_MAX_RETRY_AFTER_S = 86_400.0  # потолок ядра для `retry_after_ms` — сутки
_OUTCOMES = frozenset({"done", "retry", "drop"})

PushFn = Callable[[dict], Awaitable[ReactionResult]]


class Reactor:
    """Цикл толкателя. `push` — `ConnectorBase.push_reaction`; `mono`/`sleep` — часы коннектора (тесты
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
                ready = await self._client.wait_reactions(WAIT_REACTIONS_S)
                if ready:
                    await self.step()
            except asyncio.CancelledError:
                if _own_task_cancelling():
                    raise
                _logger.warning("reactions: чужая отмена в цикле — повтор")
                continue
            except Exception as exc:  # noqa: BLE001 — цикл не падает
                if _is_unauthorized(exc):
                    self._stopped = True
                    self._on_unauthorized()
                    return
                if not _is_core_unreachable(exc):
                    _logger.warning("reactions: запрос к ядру не прошёл: %s", type(exc).__name__)
                await self._sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_CAP_S)
                continue
            backoff = _BACKOFF_BASE_S

    async def step(self) -> int:
        """Один шаг: взять одно задание и обработать. Число обработанных заданий."""
        items = await self._client.lease_reactions(limit=1)
        for item in items:
            await self.process(item)
        return len(items)

    async def process(self, item: dict) -> None:
        message_id = item["message_id"]
        lease_gen = item["lease_gen"]
        try:
            result = await asyncio.wait_for(self._push(item), timeout=PUSH_TIMEOUT_S)
        except AuthRequired:
            # Вход в источник потерян: отчёта нет, лиз истечёт сам (как у медиа и отправки).
            await self._on_auth_required()
            return
        except TimeoutError:
            result = ReactionResult("retry", reason="timeout")
        except asyncio.CancelledError:
            if _own_task_cancelling():
                raise
            result = ReactionResult("retry", reason="cancelled")
        except Exception as exc:  # noqa: BLE001 — сбой одного задания не роняет цикл
            result = ReactionResult("retry", reason=type(exc).__name__)
        if not isinstance(result, tuple) or getattr(result, "outcome", None) not in _OUTCOMES:
            result = ReactionResult("retry", reason="bad_result")

        retry_after_s = result.retry_after_s
        if retry_after_s is not None:
            valid = (
                isinstance(retry_after_s, int | float) and math.isfinite(retry_after_s) and retry_after_s >= 0
            )
            retry_after_s = min(float(retry_after_s), _MAX_RETRY_AFTER_S) if valid else None
        if retry_after_s is not None:
            # Источник велел ждать — ждёт весь цикл, не только это сообщение (как у прочитанности).
            self._paused_until = self._mono() + retry_after_s

        if result.outcome == "done":
            _logger.debug("reactions: message_id=%s done", message_id)
        else:
            _logger.info("reactions: message_id=%s итог %s (%s)", message_id, result.outcome, result.reason)
        retry_after_ms = math.ceil(retry_after_s * 1000) if retry_after_s is not None else None
        extra: dict = {}
        if result.outcome == "done" and result.reason == "owner_reacted":
            # Ф15-1 П1: операции сессии не применены (у сообщения реакция владельца) — ядро не ставит пометок.
            extra["reason"] = result.reason
        try:
            await self._client.report_reaction(
                message_id=message_id,
                lease_gen=lease_gen,
                outcome=result.outcome,
                retry_after_ms=retry_after_ms,
                **extra,
            )
        except Exception as exc:
            # 404/409 (строку сняли или перехватили) и прочие 4xx — ядро решило само, дальше по очереди.
            # 401 — наверх (выход 78). Ядро недоступно — лиз истечёт, ядро выдаст задание снова.
            if _is_rejected(exc):
                _logger.warning(
                    "reactions: ядро отвергло отчёт message_id=%s (HTTP %s)",
                    message_id,
                    exc.response.status_code,
                )
                return
            if _is_core_unreachable(exc):
                _logger.warning(
                    "reactions: отчёт message_id=%s — ядро недоступно: %s", message_id, type(exc).__name__
                )
                return
            raise
