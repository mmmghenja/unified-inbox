"""Нарезка потока речи на куски по паузам (Ф60-1, раздел 3 и П9) — чистая логика без процессов и звука.

Вход — LPCM 16 кГц Int16LE моно произвольными порциями; выход — куски `Chunk` со временем от начала потока
(мс).
- Энергия окна 30 мс (дБ от полной шкалы) против плавающего порога шума: порог = шум + 12 дБ (шум не ниже
  −70 дБ). Шум — по окнам без речи: вниз сразу, вверх за ~0,6 с; во время речи он ползёт вверх на 1,7 дБ/с,
  чтобы ровный громкий фон (вентилятор, музыка) со временем перестал считаться речью. Минимум по скользящему
  окну не годится: сплошной звук за 2 с сам становится «шумом» и режет длинную фразу.
- Кусок начинается с предзаписи ~200 мс до первого окна речи и кончается после 700 мс тишины.
- П9: весь кусок вместе с предзаписью и хвостом — не длиннее 15 с, поэтому речь режется на 14 с (от начала
  речи в куске); продолжение идёт следующим куском без предзаписи.
- Речь (от первого до последнего окна речи) короче 0,4 с — щелчок, кашель: кусок выбрасывается.

Звук нигде не сохраняется: куски живут в памяти до отправки."""

import math
from array import array
from collections import deque
from dataclasses import dataclass

SAMPLE_RATE = 16_000
BYTES_PER_MS = SAMPLE_RATE * 2 // 1000  # 32: Int16 моно
FRAME_MS = 30
FRAME_BYTES = FRAME_MS * BYTES_PER_MS  # 960
PREROLL_MS = 200
PREROLL_FRAMES = round(PREROLL_MS / FRAME_MS)  # 7 окон = 210 мс
HANG_MS = 700  # конец куска после стольких мс тишины
SPEECH_MAX_MS = 14_000  # П9: речь режется на 14 с
SPEECH_MIN_MS = 400  # короче — выбросить
CHUNK_MAX_MS = 15_000  # предел ядра (480 000 байт); кусок с предзаписью и хвостом в него укладывается
THRESHOLD_DB = 12.0
FLOOR_MIN_DB = -70.0
FLOOR_FOLLOW = 0.05  # доля шага к энергии окна без речи (вверх; вниз — сразу)
FLOOR_RISE_DB = 0.05  # на окно речи: 1,7 дБ/с
SILENCE_DB = -120.0


@dataclass(frozen=True)
class Chunk:
    start_ms: int  # от начала потока
    end_ms: int
    pcm: bytes

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


def frame_db(frame: bytes) -> float:
    """Энергия окна в дБ от полной шкалы (0 — меандр на пределе, тишина — −120)."""
    samples = array("h")
    samples.frombytes(frame)
    if not samples:
        return SILENCE_DB
    power = sum(s * s for s in samples) / len(samples) / (32768.0 * 32768.0)
    if power <= 0:
        return SILENCE_DB
    return max(SILENCE_DB, 10.0 * math.log10(power))


class Segmenter:
    """Один поток (`me` или `them`) — свой экземпляр: порог шума у каждого свой."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._pos_ms = 0  # начало следующего окна от начала потока
        self._noise: float | None = None  # оценка шума, дБ
        self._preroll: deque[bytes] = deque(maxlen=PREROLL_FRAMES)
        self._in_chunk = False
        self._frames: list[bytes] = []
        self._chunk_start = 0
        self._span_start = 0  # от чего считаются 14 с речи
        self._first_voiced: int | None = None
        self._last_voiced_end = 0

    @property
    def in_speech(self) -> bool:
        return self._in_chunk

    def feed(self, pcm: bytes) -> list[Chunk]:
        self._buf += pcm
        out: list[Chunk] = []
        while len(self._buf) >= FRAME_BYTES:
            frame = bytes(self._buf[:FRAME_BYTES])
            del self._buf[:FRAME_BYTES]
            chunk = self._frame(frame)
            if chunk is not None:
                out.append(chunk)
        return out

    def flush(self) -> list[Chunk]:
        """Конец потока (пауза, смена устройства): недоговорённый кусок — как есть, если речи хватает."""
        self._buf.clear()
        if not self._in_chunk:
            return []
        chunk = self._close(self._pos_ms)
        self._in_chunk = False
        self._preroll.clear()
        return [chunk] if chunk is not None else []

    def _is_voiced(self, energy: float) -> bool:
        """Окно речи? И сдвинуть оценку шума (первое окно потока — сразу «шум»)."""
        noise = self._noise
        if noise is None:
            self._noise = energy
            return False
        voiced = energy >= max(FLOOR_MIN_DB, noise) + THRESHOLD_DB
        if energy < noise:
            self._noise = energy
        elif voiced:
            self._noise = noise + FLOOR_RISE_DB
        else:
            self._noise = noise + (energy - noise) * FLOOR_FOLLOW
        return voiced

    def _frame(self, frame: bytes) -> Chunk | None:
        start = self._pos_ms
        end = start + FRAME_MS
        self._pos_ms = end
        voiced = self._is_voiced(frame_db(frame))

        if not self._in_chunk:
            if not voiced:
                self._preroll.append(frame)
                return None
            self._in_chunk = True
            self._frames = [*self._preroll, frame]
            self._chunk_start = start - len(self._preroll) * FRAME_MS
            self._preroll.clear()
            self._span_start = start
            self._first_voiced = start
            self._last_voiced_end = end
            return None

        self._frames.append(frame)
        if voiced:
            if self._first_voiced is None:
                self._first_voiced = start
            self._last_voiced_end = end
        if end - self._last_voiced_end >= HANG_MS:
            chunk = self._close(end)
            self._in_chunk = False
            return chunk
        if end - self._span_start >= SPEECH_MAX_MS:
            chunk = self._close(end)
            # Речь продолжается: следующий кусок сразу, без предзаписи (её звук уже ушёл этим куском).
            self._in_chunk = True
            self._frames = []
            self._chunk_start = end
            self._span_start = end
            self._first_voiced = None
            return chunk
        return None

    def _close(self, end: int) -> Chunk | None:
        frames, first = self._frames, self._first_voiced
        self._frames = []
        self._first_voiced = None
        if first is None or self._last_voiced_end - first < SPEECH_MIN_MS:
            return None
        return Chunk(start_ms=self._chunk_start, end_ms=end, pcm=b"".join(frames))
