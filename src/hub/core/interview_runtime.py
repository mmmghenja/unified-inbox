"""Помощник на интервью — память ядра (Ф60-1, docs/architecture.md).

Здесь только то, что живёт в процессе ядра и рестартом теряется (это нормально — старт ядра ставит открытое
интервью на паузу, П6):

- таймер режима `live`: после строки `them` ждать `LIVE_QUIET_MS` тишины (новая строка `them` — ждать заново,
  отсчёт от её прихода), между подачами — не меньше `LIVE_GAP_MS`; строки `me` подачу не запускают. Одна
  задача `asyncio` на интервью; ждёт подменяемым `sleep` (тесты — без настоящего ожидания);
- живость службы записи: время последнего `GET /listen/current` (`listener_seen_ms`);
- кадры интерфейсу (`event: interview` потока `GET /sessions/live`, `LiveHub.publish_interview`);
- проход по потолку и тишине (`sweep` — его раз в 30 с зовёт цикл ядра, `runner._interview_loop`).

Запись в базу — `core/interview.py` через `Db.write()`. Текст строк в журнал не пишется — только числа."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from . import interview as interview_mod
from .db import Db
from .hub_sessions import SessionError
from .live import LiveHub

_logger = logging.getLogger("hub.interview")


class InterviewRuntime:
    def __init__(
        self,
        db: Db,
        *,
        clock: Callable[[], float],
        live: LiveHub,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.db = db
        self.clock = clock
        self.live = live
        self._sleep = sleep
        self.listener_seen_ms: int | None = None
        self._pending: dict[int, bool] = {}  # пришла строка `them`, ещё не поданная таймером
        self._last_them: dict[int, int] = {}
        self._last_feed: dict[int, int] = {}
        self._timers: dict[int, asyncio.Task] = {}
        self._closed = False

    def now_ms(self) -> int:
        return int(self.clock() * 1000)

    # --- живость службы ---------------------------------------------------------------------------------

    def listener_alive(self, now_ms: int) -> bool:
        seen = self.listener_seen_ms
        return seen is not None and now_ms - seen <= interview_mod.LISTENER_FRESH_MS

    def seen(self, now_ms: int) -> bool:
        """Служба опросила `listen/current`. Возвращает, «ожила» ли она (была не видна дольше 10 с)."""
        revived = not self.listener_alive(now_ms)
        self.listener_seen_ms = now_ms
        return revived

    def listener_fields(self, now_ms: int) -> dict:
        return {"listener_seen_ms": self.listener_seen_ms, "listener_alive": self.listener_alive(now_ms)}

    # --- кадры ------------------------------------------------------------------------------------------

    def publish_line(self, row: dict, line: dict) -> None:
        self.live.publish_interview(
            {"type": "line", "interview_id": row["id"], "session_id": row["session_id"], "line": line}
        )

    def publish_state(self, interview: dict, now_ms: int) -> None:
        """`interview` — `interview.interview_dict` (с `status`, `warn`, `reason`, `wait_reason`, …)."""
        self.live.publish_interview(
            {
                "type": "state",
                "interview_id": interview["id"],
                "session_id": interview["session_id"],
                "interview": interview,
                **self.listener_fields(now_ms),
            }
        )

    async def publish_state_of(self, interview_id: int) -> None:
        now_ms = self.now_ms()
        with self.db.reader() as conn:
            row = interview_mod._get(conn, interview_id)  # noqa: SLF001
            data = interview_mod.interview_dict(conn, row) if row is not None else None
        if data is not None:
            self.publish_state(data, now_ms)

    # --- таймер `live` ----------------------------------------------------------------------------------

    def due_ms(self, interview_id: int) -> int | None:
        """Когда подавать: `None` — нечего (строки `them` после прошлой подачи не было)."""
        if not self._pending.get(interview_id):
            return None
        due = self._last_them[interview_id] + interview_mod.LIVE_QUIET_MS
        last_feed = self._last_feed.get(interview_id)
        if last_feed is not None:
            due = max(due, last_feed + interview_mod.LIVE_GAP_MS)
        return due

    def on_line(self, row: dict, line: dict, now_ms: int) -> None:
        """Строка записана: кадр интерфейсу; строка `them` в `live` — (пере)запуск таймера подачи."""
        self.publish_line(row, line)
        if line["who"] == "them" and row["mode"] == "live" and row["status"] == "active":
            self._pending[row["id"]] = True
            self._last_them[row["id"]] = now_ms
            self._arm(row["id"])

    def fed(self, interview_id: int, now_ms: int, result: interview_mod.FeedResult) -> None:
        """Подача прошла не таймером («Подскажи»): все строки поданы — ждущий таймер снимается сам."""
        if result.say_ids:
            self._last_feed[interview_id] = now_ms
        if result.lines:
            self._pending[interview_id] = False

    def stop(self, interview_id: int) -> None:
        """Пауза или конец: таймер снят, память интервью забыта (после «Продолжить» неподанное уходит с
        первой новой строкой `them` или по «Подскажи», П6)."""
        task = self._timers.pop(interview_id, None)
        if task is not None and not task.done():
            task.cancel()
        self._pending.pop(interview_id, None)
        self._last_them.pop(interview_id, None)
        self._last_feed.pop(interview_id, None)

    def timer_running(self, interview_id: int) -> bool:
        task = self._timers.get(interview_id)
        return task is not None and not task.done()

    def _arm(self, interview_id: int) -> None:
        if self._closed or self.timer_running(interview_id):
            return  # идущая задача пересчитает срок после своего ожидания
        task = asyncio.get_running_loop().create_task(self._timer(interview_id))
        self._timers[interview_id] = task

    async def _timer(self, interview_id: int) -> None:
        try:
            while not self._closed:
                due = self.due_ms(interview_id)
                if due is None:
                    return
                now_ms = self.now_ms()
                if now_ms < due:
                    await self._sleep((due - now_ms) / 1000)
                    continue
                self._pending[interview_id] = False
                try:
                    result = await self.db.write(
                        lambda conn, now_ms=now_ms: interview_mod.feed_live(conn, interview_id, now_ms=now_ms)
                    )
                except SessionError as exc:
                    _logger.info("интервью #%d: подача по ходу не прошла (%d)", interview_id, exc.status)
                    return
                if result.say_ids:
                    self._last_feed[interview_id] = now_ms
                    _logger.info(
                        "интервью #%d: подано по ходу строк %d, реплик %d",
                        interview_id,
                        result.lines,
                        len(result.say_ids),
                    )
                    await self.publish_state_of(interview_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — сбой подачи не должен ронять ядро; следующая строка запустит снова
            _logger.exception("интервью #%d: таймер подачи упал", interview_id)
        finally:
            if self._timers.get(interview_id) is asyncio.current_task():
                self._timers.pop(interview_id, None)

    # --- само ядро --------------------------------------------------------------------------------------

    async def sweep(self) -> tuple[int, str] | None:
        """П6: потолок 3 часа (конец с итогом) и тишина 20 мин (пауза). Сначала читатель — писать, только
        когда есть что."""
        now_ms = self.now_ms()
        with self.db.reader() as conn:
            if interview_mod.due(interview_mod.current_row(conn), now_ms) is None:
                return None
        done = await self.db.write(lambda conn: interview_mod.sweep(conn, now_ms=now_ms))
        if done is not None:
            interview_id, action = done
            self.stop(interview_id)
            _logger.info(
                "интервью #%d: %s само", interview_id, "кончилось" if action == "end" else "на паузе"
            )
            await self.publish_state_of(interview_id)
        return done

    def close(self) -> None:
        """Остановка ядра: таймеры сняты."""
        self._closed = True
        for task in self._timers.values():
            if not task.done():
                task.cancel()
        self._timers.clear()
