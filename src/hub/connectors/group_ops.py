"""Исполнитель операций над группами (Ф62-6, `docs/architecture.md` и поправки П1, П9).

Тот же цикл, что у вступления и выхода (`connectors/join.Joiner`: `wait` -> `lease(limit=1)` -> `push` не
дольше 120 с -> `report`), свои вызовы ядра (`/group-op/*`) и своё тело отчёта: `done` — с `result`
(`chat`, `link`, `participants` с кодами по индексам задания); `retry` — с `retry_after_ms` (пауза всему
аккаунту) или `postpone_ms` (отсрочка только этой строки: предел поиска номеров, П9) или без них (бэкофф
ядра); `drop` — с причиной. Журнал — вид, исход и причина: без названий, номеров и ссылок.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Literal, NamedTuple

from .base import AuthRequired, _is_core_unreachable, _is_rejected, _own_task_cancelling
from .join import Joiner

_logger = logging.getLogger(__name__)

# create (сверка П1 + сам запрос + chat_info) и add на 30 человек — дольше шага вступления.
PUSH_TIMEOUT_S = 120.0
_MAX_RETRY_AFTER_S = 86_400.0
_OUTCOMES = frozenset({"done", "retry", "drop"})
_REASON_MAX = 200


class GroupOpResult(NamedTuple):
    """Итог операции. `done` — с `result` (`{chat?, link?, participants?: [{i, code, invite?, jid?}]}`);
    `retry` — с `retry_after_s` (FloodWait/бан — пауза аккаунту), `postpone_s` (только этой строке) или
    без них; `drop` — с `reason` (`not_admin`, `not_member`, …)."""

    outcome: Literal["done", "retry", "drop"]
    retry_after_s: float | None = None
    postpone_s: float | None = None
    reason: str | None = None
    result: dict | None = None


def _seconds(value: object) -> float | None:
    if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
        return None
    return min(float(value), _MAX_RETRY_AFTER_S)


class GroupOpWorker(Joiner):
    """Цикл `Joiner` с вызовами ядра `/group-op/*` и своим `process`."""

    async def process(self, item: dict) -> None:
        op_id = item["id"]
        kind = item.get("kind")
        try:
            result = await asyncio.wait_for(self._push(item), timeout=PUSH_TIMEOUT_S)
        except AuthRequired:
            await self._on_auth_required()  # отчёта нет: лиз истечёт сам
            return
        except TimeoutError:
            result = GroupOpResult("retry", reason="timeout")
        except asyncio.CancelledError:
            if _own_task_cancelling():
                raise
            result = GroupOpResult("retry", reason="cancelled")
        except Exception as exc:  # noqa: BLE001 — сбой одного задания не роняет цикл
            result = GroupOpResult("retry", reason=type(exc).__name__)
        if not isinstance(result, GroupOpResult) or result.outcome not in _OUTCOMES:
            result = GroupOpResult("retry", reason="bad_result")

        retry_after_s = _seconds(result.retry_after_s)
        if retry_after_s is not None:
            self._paused_until = self._mono() + retry_after_s  # ядро ставит паузу всем строкам аккаунта
        postpone_s = _seconds(result.postpone_s)
        _logger.info("%s: вид=%s id=%s итог %s (%s)", self._name, kind, op_id, result.outcome, result.reason)
        body: dict = {"id": op_id, "lease_gen": item["lease_gen"], "outcome": result.outcome}
        if retry_after_s is not None:
            body["retry_after_ms"] = math.ceil(retry_after_s * 1000)
        elif postpone_s is not None and result.outcome == "retry":
            body["postpone_ms"] = max(1, math.ceil(postpone_s * 1000))
        if result.reason is not None and result.outcome != "done":
            body["reason"] = str(result.reason)[:_REASON_MAX]
        if result.outcome == "done":
            body["result"] = result.result or {}
        try:
            await self._report(body)
        except Exception as exc:
            if _is_rejected(exc):
                _logger.warning(
                    "%s: ядро отвергло отчёт id=%s (HTTP %s)", self._name, op_id, exc.response.status_code
                )
                return
            if _is_core_unreachable(exc):
                _logger.warning(
                    "%s: отчёт id=%s — ядро недоступно: %s", self._name, op_id, type(exc).__name__
                )
                return
            raise
