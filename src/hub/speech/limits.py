"""Пределы диктовки в памяти ядра (Ф59-1, раздел 1): 60 кусков в минуту и суточный потолок `daily_chunks`
(сброс в полночь по местному времени). Рестарт ядра счётчики обнуляет — осознанно (как присутствие).

Минутный предел считает каждый кусок, дошедший до этой проверки; суточный — каждый кусок, ушедший в Яндекс
(в том числе неудачный: оплата могла случиться).
"""

import datetime as dt
from collections import deque

PER_MINUTE = 60
_MINUTE_MS = 60_000


class DictateLimits:
    def __init__(self, per_minute: int = PER_MINUTE) -> None:
        self._per_minute = per_minute
        self._recent: deque[int] = deque()
        self._day: dt.date | None = None
        self._day_count = 0

    def take_minute(self, now_ms: int) -> bool:
        """Занять место в минутном окне; `False` — предел исчерпан (место не занято)."""
        while self._recent and self._recent[0] <= now_ms - _MINUTE_MS:
            self._recent.popleft()
        if len(self._recent) >= self._per_minute:
            return False
        self._recent.append(now_ms)
        return True

    def take_day(self, now_ms: int, cap: int) -> bool:
        """Занять кусок суточного потолка; `False` — потолок на сегодня исчерпан (кусок не занят)."""
        today = dt.datetime.fromtimestamp(now_ms / 1000).date()  # местное время
        if today != self._day:
            self._day, self._day_count = today, 0
        if self._day_count >= cap:
            return False
        self._day_count += 1
        return True

    def used_today(self, now_ms: int) -> int:
        today = dt.datetime.fromtimestamp(now_ms / 1000).date()
        return self._day_count if today == self._day else 0
