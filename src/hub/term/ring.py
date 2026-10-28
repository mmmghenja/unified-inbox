"""Кольцо байтов вывода терминала со сквозным счётчиком (раздел 2.1). Только в памяти ядра."""


class Ring:
    def __init__(self, size: int) -> None:
        self.size = size
        self._buf = bytearray()
        self.start = 0  # сквозная позиция первого байта в кольце

    @property
    def end(self) -> int:
        return self.start + len(self._buf)

    def append(self, data: bytes) -> None:
        self._buf += data
        extra = len(self._buf) - self.size
        if extra > 0:
            del self._buf[:extra]
            self.start += extra

    def has(self, pos: int) -> bool:
        """Можно ли читать с `pos`: позиция внутри кольца (включая его конец)."""
        return self.start <= pos <= self.end

    def read(self, pos: int, limit: int) -> bytes:
        """Байты с `pos` (не больше `limit`); позиция вне кольца — `ValueError`."""
        if not self.has(pos):
            raise ValueError("позиция вне кольца")
        offset = pos - self.start
        return bytes(self._buf[offset : offset + limit])
