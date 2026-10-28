"""Активность владельца в хабе (Ф20-3, `docs/architecture.md`): время последнего «Владелец
работает в окне хаба». Только в ПАМЯТИ процесса ядра (как `presence.py`): рестарт обнуляет. Одно значение
на оба приложения ядра (публичное пишет, внутреннее отдаёт коннектору Slack).
"""


class Activity:
    def __init__(self) -> None:
        self.last_ms: int | None = None

    def touch(self, now_ms: int) -> None:
        if self.last_ms is None or now_ms > self.last_ms:
            self.last_ms = now_ms
