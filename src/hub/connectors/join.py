"""Исполнитель заявок на вступление (Ф17-1, `docs/architecture.md`) и
на выход (Ф17-2, `docs/architecture.md`: тот же класс со своими вызовами ядра — `wait`,
`lease`, `report` — и своим именем в журнале; шаг `leave`, `done` — с `result` `left|not_member`).

Отдельная задача коннектора по образцу толкателя прочитанности (`connectors/read_ack.py`): `wait_joins`
-> `lease_joins(limit=1)` -> `push_join(item)` коннектора (не дольше 60 с) -> `report_join`. Шаг `peek` —
посмотреть, что это за цель; шаг `join` — вступить. Темп и паузы держит ядро (П3); `retry_after_s`
(FloodWait) — ещё и пауза всего цикла.

П4: сначала отчёт `done` с `ext_id`, и только потом — `after_join(item, result)` коннектора (стартовая
загрузка беседы — своей задачей, вне 60-секундного шага). Журнал — шаг, исход и класс/причина: без целей и
названий.
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


class JoinResult(NamedTuple):
    """Итог шага. `done` шага `peek` — с `peek` (title, kind, participants, about, request_needed, member,
    forum, ext_id); `done` шага `join` — с `result` (`joined|already|requested`) и, если известны, `ext_id` и
    `title`. `drop` — с `reason` (`not_found`, `not_group`, …). `retry` — с `retry_after_s` (FloodWait) или
    без (сеть)."""

    outcome: Literal["done", "retry", "drop"]
    retry_after_s: float | None = None
    reason: str | None = None
    result: str | None = None
    peek: dict | None = None
    ext_id: str | None = None
    title: str | None = None


_logger = logging.getLogger(__name__)

WAIT_JOINS_S = 25.0
PUSH_TIMEOUT_S = 60.0
_BACKOFF_BASE_S = 1.0
_BACKOFF_CAP_S = 30.0
_BLOCKED_SLEEP_S = 60.0
_MAX_RETRY_AFTER_S = 86_400.0  # потолок ядра для `retry_after_ms` — сутки
_OUTCOMES = frozenset({"done", "retry", "drop"})
_REASON_MAX = 200

PushFn = Callable[[dict], Awaitable[JoinResult]]
AfterFn = Callable[[dict, JoinResult], Awaitable[None]]
WaitFn = Callable[[float], Awaitable[bool]]
LeaseFn = Callable[..., Awaitable[list[dict]]]
ReportFn = Callable[[dict], Awaitable[dict]]


async def _no_after(item: dict, result: JoinResult) -> None:
    del item, result


class Joiner:
    """Цикл исполнителя. `push` — `ConnectorBase.push_join` (`push_leave`); `after` —
    `ConnectorBase.after_join` (после принятого ядром `done`; у выхода — ничего); `mono`/`sleep` — часы
    коннектора (тесты подменяют); `is_blocked()` — вход в источник потерян; `on_auth_required()` — `push`
    поднял `AuthRequired` (отчёта нет: лиз истечёт сам); `on_unauthorized()` — ядро ответило 401 (выход
    78). Ф17-2: `wait`/`lease`/`report` — вызовы ядра (по умолчанию — вступления: `wait_joins`/
    `lease_joins`/`report_join`), `name` — префикс журнала (`join`/`leave`)."""

    def __init__(
        self,
        client: CoreClient,
        push: PushFn,
        *,
        after: AfterFn | None = None,
        mono: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
        is_blocked: Callable[[], bool],
        on_auth_required: Callable[[], Awaitable[None]],
        on_unauthorized: Callable[[], None],
        wait: WaitFn | None = None,
        lease: LeaseFn | None = None,
        report: ReportFn | None = None,
        name: str = "join",
    ) -> None:
        self._client = client
        self._push = push
        self._after = after or _no_after
        # Вызовы ядра — лениво (как было: заглушки ядра в тестах каркаса этих методов не имеют, цикл их
        # ошибку глотает).
        self._wait = wait or (lambda timeout_s: client.wait_joins(timeout_s))
        self._lease = lease or (lambda *, limit: client.lease_joins(limit=limit))
        self._report = report or (lambda body: client.report_join(body))
        self._name = name
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
                ready = await self._wait(WAIT_JOINS_S)
                if ready:
                    await self.step()
            except asyncio.CancelledError:
                if _own_task_cancelling():
                    raise
                _logger.warning("%s: чужая отмена в цикле — повтор", self._name)
                continue
            except Exception as exc:  # noqa: BLE001 — цикл не падает
                if _is_unauthorized(exc):
                    self._stopped = True
                    self._on_unauthorized()
                    return
                if not _is_core_unreachable(exc):
                    _logger.warning("%s: запрос к ядру не прошёл: %s", self._name, type(exc).__name__)
                await self._sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_CAP_S)
                continue
            backoff = _BACKOFF_BASE_S

    async def step(self) -> int:
        """Один шаг: взять одно задание и обработать. Число обработанных заданий."""
        items = await self._lease(limit=1)
        for item in items:
            await self.process(item)
        return len(items)

    async def process(self, item: dict) -> None:
        join_id = item["id"]
        step = item.get("step")
        try:
            result = await asyncio.wait_for(self._push(item), timeout=PUSH_TIMEOUT_S)
        except AuthRequired:
            # Вход в источник потерян: отчёта нет, лиз истечёт сам (как у прочитанности и медиа).
            await self._on_auth_required()
            return
        except TimeoutError:
            result = JoinResult("retry", reason="timeout")
        except asyncio.CancelledError:
            if _own_task_cancelling():
                raise
            result = JoinResult("retry", reason="cancelled")
        except Exception as exc:  # noqa: BLE001 — сбой одного задания не роняет цикл
            result = JoinResult("retry", reason=type(exc).__name__)
        if not isinstance(result, JoinResult) or result.outcome not in _OUTCOMES:
            result = JoinResult("retry", reason="bad_result")

        retry_after_s = result.retry_after_s
        if retry_after_s is not None:
            valid = (
                isinstance(retry_after_s, int | float) and math.isfinite(retry_after_s) and retry_after_s >= 0
            )
            retry_after_s = min(float(retry_after_s), _MAX_RETRY_AFTER_S) if valid else None
        if retry_after_s is not None:
            # Источник велел ждать — ждёт весь цикл (ядро ставит паузу всем строкам аккаунта, П3).
            self._paused_until = self._mono() + retry_after_s

        if result.outcome == "done":
            _logger.info("%s: шаг=%s id=%s исход %s", self._name, step, join_id, result.result or "peek")
        else:
            _logger.info(
                "%s: шаг=%s id=%s итог %s (%s)", self._name, step, join_id, result.outcome, result.reason
            )
        retry_after_ms = math.ceil(retry_after_s * 1000) if retry_after_s is not None else None
        body: dict = {"id": join_id, "lease_gen": item["lease_gen"], "outcome": result.outcome}
        if retry_after_ms is not None:
            body["retry_after_ms"] = retry_after_ms
        if result.reason is not None and result.outcome != "done":
            body["reason"] = str(result.reason)[:_REASON_MAX]
        if result.outcome == "done":
            if step == "peek":
                body["peek"] = result.peek or {}
            else:
                body["result"] = result.result
                if result.ext_id is not None:
                    body["ext_id"] = result.ext_id
                if result.title is not None:
                    body["title"] = result.title
        try:
            await self._report(body)
        except Exception as exc:
            # 404/409 (строку сняли или перехватили) и прочие 4xx — ядро решило само, дальше по очереди.
            # 401 — наверх (выход 78). Ядро недоступно — лиз истечёт, ядро выдаст задание снова.
            if _is_rejected(exc):
                _logger.warning(
                    "%s: ядро отвергло отчёт id=%s (HTTP %s)", self._name, join_id, exc.response.status_code
                )
                return
            if _is_core_unreachable(exc):
                _logger.warning(
                    "%s: отчёт id=%s — ядро недоступно: %s", self._name, join_id, type(exc).__name__
                )
                return
            raise
        if result.outcome != "done":
            return
        # П4: отчёт принят — теперь история беседы (коннектор ставит её своей задачей, шаг не держит).
        try:
            await self._after(item, result)
        except AuthRequired:
            await self._on_auth_required()
        except asyncio.CancelledError:
            if _own_task_cancelling():
                raise
        except Exception as exc:  # noqa: BLE001 — история догонится сверкой
            _logger.warning("%s: после шага id=%s: %s", self._name, join_id, type(exc).__name__)
