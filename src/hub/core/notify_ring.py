"""Кольцо уведомлений владельца (Ф33-2): память ядра, 500 записей, номер по возрастанию с 1 после каждого запуска.
Пишут службы хаба (`hub watch`, `hub agent`, `hub work`) через внутренний сокет; читает спутник `hub notify`
на ноутбуке (long-poll по публичному API, скоуп `notify`) и показывает через `notify-send`.

`epoch` — случайная метка запуска ядра: клиент, у которого она другая, начинает с начала кольца (номера
после рестарта идут заново, и чужой `after` ничего не значил бы)."""

import asyncio
import contextlib
import secrets
import time
from collections import deque

RING_SIZE = 500
LISTENED_S = 180.0  # «кольцо слушают», если запрос ожидания был за последние 3 минуты или открыт сейчас
URGENCIES = ("critical", "normal", "low")


class NotifyRing:
    def __init__(self, size: int = RING_SIZE, *, clock=time.time) -> None:
        self._items: deque[dict] = deque(maxlen=size)
        self._last = 0
        self._waiters: set[asyncio.Event] = set()
        self._closed = False
        self._clock = clock
        self.epoch = secrets.token_hex(4)
        self._open_waits = 0
        self._last_poll: float | None = None

    @property
    def last(self) -> int:
        return self._last

    def touch(self) -> None:
        """Пришёл запрос `/notify/wait` (спутник жив)."""
        self._last_poll = self._clock()

    def listened(self) -> bool:
        if self._open_waits > 0:
            return True
        return self._last_poll is not None and self._clock() - self._last_poll <= LISTENED_S

    def close(self) -> None:
        self._closed = True
        self._wake()

    def _wake(self) -> None:
        waiters, self._waiters = self._waiters, set()
        for event in waiters:
            event.set()

    def add(self, title: str, text: str, urgency: str) -> dict:
        """Звать в event loop ядра (ручки FastAPI): так ожидающие просыпаются без гонок."""
        self._last += 1
        item = {
            "id": self._last,
            "ts": int(self._clock() * 1000),
            "title": title,
            "text": text,
            "urgency": urgency,
        }
        self._items.append(item)
        self._wake()
        return item

    def after(self, after: int, limit: int = RING_SIZE) -> list[dict]:
        return [item for item in self._items if item["id"] > after][:limit]

    async def wait(self, after: int, timeout_s: float) -> list[dict]:
        found = self.after(after)
        if found or self._closed or timeout_s <= 0:
            return found
        event = asyncio.Event()
        self._waiters.add(event)
        self._open_waits += 1
        try:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(timeout_s):
                    await event.wait()
        finally:
            self._waiters.discard(event)
            self._open_waits -= 1
            self.touch()
        return self.after(after)
