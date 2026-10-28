"""Ф60-1, часть B: нарезка по паузам на синтетическом сигнале (раздел 3, П9) — без звука и процессов."""

import math
import random
from array import array

from hub.listen import segment
from hub.listen.segment import BYTES_PER_MS, Segmenter

RATE = segment.SAMPLE_RATE


def _noise(ms: int, amp: int, rng: random.Random) -> list[int]:
    return [rng.randint(-amp, amp) for _ in range(ms * RATE // 1000)]


def _tone(ms: int, amp: int = 8000, freq: float = 310.0) -> list[int]:
    return [int(amp * math.sin(2 * math.pi * freq * i / RATE)) for i in range(ms * RATE // 1000)]


def _speech(ms: int, amp: int = 8000) -> list[int]:
    """Тон с огибающей слогов 4 Гц (провалы до 10 % — как у живой речи, без пауз между словами)."""
    out = []
    for i, v in enumerate(_tone(ms, amp)):
        env = 0.55 + 0.45 * math.cos(2 * math.pi * 4 * i / RATE)
        out.append(int(v * env))
    return out


def _pcm(*parts: list[int]) -> bytes:
    samples = array("h")
    for part in parts:
        samples.extend(part)
    return samples.tobytes()


def _feed_all(pcm: bytes, piece: int = 3200) -> list[segment.Chunk]:
    seg = Segmenter()
    out = []
    for i in range(0, len(pcm), piece):
        out += seg.feed(pcm[i : i + piece])
    return out


def test_silence_speech_silence_gives_one_chunk_with_preroll_and_tail():
    rng = random.Random(1)
    pcm = _pcm(_noise(1000, 30, rng), _tone(2000), _noise(1500, 30, rng))
    chunks = _feed_all(pcm)
    assert len(chunks) == 1
    chunk = chunks[0]
    # Речь с 1000 мс; окно 990–1020 уже с речью -> начало куска на предзапись (7 окон = 210 мс) раньше.
    assert 990 - segment.PREROLL_FRAMES * segment.FRAME_MS <= chunk.start_ms <= 1000 - segment.PREROLL_MS
    # Конец — после 700 мс тишины за последним окном речи (речь до 3000 мс).
    assert (
        3000 + segment.HANG_MS - segment.FRAME_MS <= chunk.end_ms <= 3000 + segment.HANG_MS + segment.FRAME_MS
    )
    assert len(chunk.pcm) == chunk.duration_ms * BYTES_PER_MS
    # Тело в пределах ядра: 0,3–15 с.
    assert 9_600 <= len(chunk.pcm) <= 480_000


def test_long_speech_is_cut_so_each_chunk_with_preroll_and_tail_fits_15s():
    rng = random.Random(2)
    pcm = _pcm(_noise(1000, 30, rng), _speech(31_000), _noise(1500, 30, rng))
    chunks = _feed_all(pcm, piece=1234)  # порции не кратны окну
    assert len(chunks) == 3
    for chunk in chunks:
        assert chunk.duration_ms <= segment.CHUNK_MAX_MS
        assert len(chunk.pcm) <= 480_000
        assert len(chunk.pcm) == chunk.duration_ms * BYTES_PER_MS
    # П9: первая часть — предзапись + 14 с речи (с точностью до окна), продолжение — без предзаписи, встык.
    assert chunks[0].duration_ms <= segment.PREROLL_FRAMES * segment.FRAME_MS + segment.SPEECH_MAX_MS + 30
    assert chunks[1].start_ms == chunks[0].end_ms
    assert chunks[2].start_ms == chunks[1].end_ms
    assert chunks[2].end_ms >= 32_000 + segment.HANG_MS - segment.FRAME_MS


def test_short_click_is_dropped():
    rng = random.Random(3)
    pcm = _pcm(_noise(1000, 30, rng), _tone(60, amp=20000), _noise(2000, 30, rng))
    assert _feed_all(pcm) == []
    # Речь 0,3 с (меньше 0,4) — тоже мимо, 0,5 с — кусок.
    assert _feed_all(_pcm(_noise(1000, 30, rng), _tone(300), _noise(1500, 30, rng))) == []
    assert len(_feed_all(_pcm(_noise(1000, 30, rng), _tone(500), _noise(1500, 30, rng)))) == 1


def test_steady_loud_noise_is_floor_not_speech():
    """Плавающий порог: ровный громкий шум с самого начала — «шум», речь над ним на 12+ дБ — кусок."""
    rng = random.Random(4)
    pcm = _pcm(_noise(5000, 2000, rng), _tone(2000, amp=12000), _noise(3000, 2000, rng))
    chunks = _feed_all(pcm)
    assert len(chunks) == 1
    assert 4700 <= chunks[0].start_ms <= 5000
    assert chunks[0].end_ms <= 7000 + segment.HANG_MS + segment.FRAME_MS


def test_two_phrases_two_chunks_and_flush_returns_unfinished():
    rng = random.Random(5)
    seg = Segmenter()
    chunks = seg.feed(_pcm(_noise(500, 30, rng), _tone(1000), _noise(1000, 30, rng), _tone(800)))
    assert len(chunks) == 1  # вторая фраза ещё не кончилась
    assert seg.in_speech
    rest = seg.flush()
    assert len(rest) == 1 and rest[0].start_ms > chunks[0].end_ms
    assert not seg.in_speech and seg.flush() == []


def test_digital_silence_never_triggers():
    assert _feed_all(bytes(10 * RATE * 2)) == []


def test_noise_jump_stops_counting_as_speech():
    """Тихо, потом ровный громкий фон на 40 с: сначала он — «речь», но порог догоняет его за ~15 с."""
    rng = random.Random(6)
    chunks = _feed_all(_pcm(_noise(1000, 30, rng), _noise(40_000, 2000, rng)))
    assert chunks, "скачок фона сначала похож на речь"
    assert max(c.end_ms for c in chunks) < 1000 + 25_000
