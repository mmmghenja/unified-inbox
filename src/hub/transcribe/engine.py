#!/usr/bin/env python3
"""Движок расшифровки голосовых хаба (Ф14-6, docs/architecture.md).

САМОСТОЯТЕЛЬНЫЙ скрипт: без импортов хаба. Его запускает служба `hub transcribe run` интерпретатором venv
диктовки (`engine_python`, Python 3.12 с faster-whisper и cuBLAS) — в venv хаба (Python 3.14) этих пакетов нет
и не будет (решение 2 спеки). Поэтому numpy и faster_whisper импортируются ТОЛЬКО внутри функций (П12):
чистые функции (`pick_lang`, `looks_broken`, `parse_request`, `choose_attempt`) тесты хаба импортируют без
этих пакетов.

Протокол — строки JSON по stdin/stdout, по одному заданию: запрос `{"wav": "<путь к wav 16 кГц моно>",
"manual": false, "lang": "ru"}` (`manual` — ручной повтор по кнопке, 14-6b; нет — `false`; `lang` — подсказка
языка, Ф79-1: `ru`/`en` — язык не определяется по записи, прочее или нет — как раньше) или
`{"preload": true}` (Ф79-1: загрузить модель заранее, ответ `{"code": 0, "preload": true, …}`) ->
ответ `{"code": 0|2|1, "text", "lang", "lang_prob", "duration", "speech", "problem", "device", "model",
"elapsed", "error"?}`. Код 0 — ок, 2 — сомнительный результат (`problem`: «пусто», «мало слов …»,
«зацикливание», «VAD оставил …»), 1 — ошибка. stdin закрыт — выход 0. В stdout — только ответы; журнал — в
stderr, без текста расшифровки.

Логика распознавания — копия из `~/.local/share/dictation_tool/dictation_core.py` (сверено 28.09.2026):
подгрузка cuBLAS из `nvidia.cublas`, `looks_broken`, попытка с VAD -> при подозрении без VAD, параметры VAD,
выбор попытки. Модули диктовки НЕ импортируются — правки диктовки не ломают хаб и наоборот. Отличия от
диктовки: язык не жёстко `ru`, а выбирается по записи (`ru`/`en`, решение 4); подсказок (hotwords) нет;
модель на cuda держится, пока служба не закроет stdin (выгрузка по простою — решение 3).

Сбой на cuda — это задание считается на cpu (`int8`, 8 потоков); три сбоя cuda подряд — после ответа движок
выходит с кодом 3 (служба поднимет новый процесс со свежим CUDA-контекстом). Тишина (`detect_language` с VAD
на записи без речи падает `ValueError` из `max({})`, П5) — не сбой: ответ `code 2`, `problem: "пусто"`.
"""

import argparse
import ctypes
import gc
import json
import os
import sys
import time

SAMPLE_RATE = 16000
# Из dictation_core (сверено 28.09): русская речь ~2 слова/с; ниже порога на длинной записи — развалилось.
MIN_WORDS_PER_SEC = 0.6
LONG_AUDIO_SEC = 12
VAD_KEEP_MIN = 0.15  # «VAD оставил < 15 %» длинной записи — повод перепроверить без VAD
# Из dictation_core: микрофон тихий — порог ниже стандартного, паузы режем мягко.
VAD_PARAMETERS = {"threshold": 0.35, "min_silence_duration_ms": 700, "speech_pad_ms": 300}
LANGS = ("ru", "en")
DEFAULT_LANG = "ru"
MAX_CUDA_FAILURES = 3
EXIT_CUDA_BROKEN = 3
MAX_REQUEST = 64 * 1024
CPU_THREADS = 8
SILENCE = "пусто"


def log(message: str) -> None:
    sys.stderr.write(f"engine: {message}\n")
    sys.stderr.flush()


# --- чистые функции (импортируются тестами хаба) ----------------------------------------------------------


def looks_broken(text: str, speech_sec: float) -> str | None:
    """Из dictation_core, без изменений: пусто / мало слов на длинной речи / зацикливание."""
    words = text.split()
    if not words:
        return SILENCE
    if speech_sec >= LONG_AUDIO_SEC and len(words) < speech_sec * MIN_WORDS_PER_SEC:
        return f"мало слов: {len(words)} на {speech_sec:.0f} с речи"
    if len(words) > 15:
        uniq = {w.lower().strip(".,!?:;«»\"") for w in words}
        if len(uniq) < len(words) / 5:
            return "зацикливание"
    return None


def pick_lang(all_language_probs) -> tuple[str, float]:
    """Решение 4: из вероятностей всех языков — больший из `ru`/`en`; ни того, ни другого — `ru` (0.0)."""
    best: tuple[str, float] | None = None
    for entry in all_language_probs or ():
        try:
            lang, prob = entry[0], float(entry[1])
        except (TypeError, ValueError, IndexError):
            continue
        if lang in LANGS and (best is None or prob > best[1]):
            best = (lang, prob)
    return best if best is not None else (DEFAULT_LANG, 0.0)


def parse_job(line: str | bytes) -> tuple[str, bool]:
    """Строка запроса -> `(путь к wav, ручной повтор)`. Кривой запрос — `ValueError` (ответ `code 1`, движок
    живёт дальше). `manual` не `true` — автоматика."""
    if isinstance(line, bytes):
        line = line.decode("utf-8")
    if len(line) > MAX_REQUEST:
        raise ValueError("запрос слишком длинный")
    data = json.loads(line)
    wav = data.get("wav") if isinstance(data, dict) else None
    if not isinstance(wav, str) or not wav:
        raise ValueError("нет пути к wav")
    return wav, data.get("manual") is True


def parse_options(line: str | bytes) -> dict:
    """Ф79-1: необязательные поля запроса — `lang` (подсказка, только `ru`/`en`, иначе `None`) и `preload`.
    Кривой запрос — пустой словарь (`parse_job` уже ответит ошибкой)."""
    if isinstance(line, bytes):
        line = line.decode("utf-8", "replace")
    if len(line) > MAX_REQUEST:
        return {}
    try:
        data = json.loads(line)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    lang = data.get("lang")
    return {"lang": lang if lang in LANGS else None, "preload": data.get("preload") is True}


def parse_request(line: str | bytes) -> str:
    """Строка запроса -> путь к wav (см. `parse_job`)."""
    return parse_job(line)[0]


def retry_without_vad_on_silence(manual: bool) -> bool:
    """14-6b (приёмка 14-6a, п.2): VAD не нашёл речи (0 с). Автоматика — сразу «пусто»: проход без VAD на
    цифровой тишине выдумывает «Thank you.» (стенд 28.09). Ручной повтор по кнопке — владелец сам просит ещё раз
    (VAD мог срезать тихую речь) — проход без VAD всё равно."""
    return bool(manual)


def choose_attempt(first: tuple[str, str | None], second: tuple[str, str | None]) -> tuple[str, str | None]:
    """Из dictation_core: вторая попытка (без VAD) берётся, если в ней нет проблемы или она не зацикливание и
    текста в ней больше. Попытка — `(text, problem)`."""
    text, _problem = first
    text2, problem2 = second
    if problem2 is None or (problem2 != "зацикливание" and len(text2) > len(text)):
        return second
    return first


def first_model(model_dirs) -> str | None:
    """Первый каталог со `model.bin`."""
    for path in model_dirs or ():
        path = os.path.expanduser(str(path))
        if os.path.exists(os.path.join(path, "model.bin")):
            return path
    return None


# --- распознавание (faster_whisper, numpy — только здесь) -----------------------------------------------


def preload_cuda_libs() -> str | None:
    """Из dictation_core: cuBLAS из pip-пакета nvidia-cublas-cu12 внутри venv — ctranslate2 открывает
    libcublas.so.12 по имени лениво; уже загруженная с тем же soname подхватывается без LD_LIBRARY_PATH."""
    try:
        import nvidia.cublas  # type: ignore[import-not-found]

        libdir = os.path.join(list(nvidia.cublas.__path__)[0], "lib")
        for name in ("libcublasLt.so.12", "libcublas.so.12"):
            ctypes.CDLL(os.path.join(libdir, name), mode=ctypes.RTLD_GLOBAL)
        return libdir
    except (ImportError, OSError, IndexError):
        return None


def load_wav(path: str):
    """wav 16 кГц моно s16 (его пишет ffmpeg службы) -> float32 [-1, 1]."""
    import wave

    import numpy as np

    with wave.open(path, "rb") as f:
        if f.getframerate() != SAMPLE_RATE or f.getnchannels() != 1 or f.getsampwidth() != 2:
            raise ValueError("ожидается wav 16 кГц моно 16 бит")
        frames = f.readframes(f.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


class Silence(Exception):
    """Речи нет: `detect_language` с VAD падает `ValueError` на пустом наборе кусков (П5)."""


def _run(model, audio, *, vad: bool, lang: str):
    """Из dictation_core.run: один проход распознавания."""
    params = {
        "language": lang,
        "beam_size": 5,
        # Без подсказки и без опоры на предыдущий текст: они давали «технические слова» и схлопывание.
        "condition_on_previous_text": False,
        "vad_filter": vad,
    }
    if vad:
        params["vad_parameters"] = dict(VAD_PARAMETERS)
    segments, info = model.transcribe(audio, **params)
    text = " ".join(s.text.strip() for s in segments).strip()
    speech = info.duration_after_vad if vad else info.duration
    return text, info.duration, speech


def vad_options():
    """`detect_language` в faster-whisper 1.2.1 передаёт `vad_parameters` в `get_speech_timestamps` как есть
    (словарь не превращает в `VadOptions`, в отличие от `transcribe`) — падает `AttributeError`
    (стенд 28.09)."""
    from faster_whisper.vad import VadOptions

    return VadOptions(**VAD_PARAMETERS)


def recognize(model, audio, *, manual: bool = False, lang_hint: str | None = None) -> dict:
    """Язык по записи (решение 4), затем dictation_core.recognize: VAD -> при подозрении без VAD. `manual` —
    ручной повтор: при 0 с речи по VAD — проход без VAD (`retry_without_vad_on_silence`). `lang_hint`
    (Ф79-1, диктовка и интервью: язык известен заранее) — язык не определяется, `lang_prob` — `None`."""
    if lang_hint in LANGS:
        lang, lang_prob = lang_hint, None
    else:
        try:
            _lang, _prob, all_probs = model.detect_language(
                audio, vad_filter=True, vad_parameters=vad_options()
            )
        except ValueError as exc:
            if not retry_without_vad_on_silence(manual):
                raise Silence from exc
            try:
                _lang, _prob, all_probs = model.detect_language(audio, vad_filter=False)
            except ValueError as exc2:
                raise Silence from exc2
        lang, lang_prob = pick_lang(all_probs)
    text, total, speech = _run(model, audio, vad=True, lang=lang)
    if speech <= 0:
        # Стенд 28.09: на цифровой тишине faster-whisper 1.2.1 `detect_language` НЕ падает (язык по
        # дополненной нулями записи), VAD не оставляет ничего, а проход без VAD выдумывает «Thank you.».
        # Речи нет — «пусто» (П5), без второй попытки. Ручной повтор — проход без VAD всё равно.
        if not retry_without_vad_on_silence(manual):
            raise Silence
        text, total, speech = _run(model, audio, vad=False, lang=lang)
        return {
            "text": text,
            "lang": lang,
            "lang_prob": None if lang_prob is None else round(float(lang_prob), 4),
            "duration": round(float(total), 1),
            "speech": round(float(speech), 1),
            "problem": looks_broken(text, speech),
        }
    problem = looks_broken(text, speech)
    if not problem and total >= LONG_AUDIO_SEC and speech < total * VAD_KEEP_MIN:
        problem = f"VAD оставил {speech:.0f} с из {total:.0f} с"
    if problem:
        text2, _total2, speech2 = _run(model, audio, vad=False, lang=lang)
        second = (text2, looks_broken(text2, speech2))
        chosen = choose_attempt((text, problem), second)
        if chosen is second:
            speech = speech2
        text, problem = chosen
    return {
        "text": text,
        "lang": lang,
        "lang_prob": None if lang_prob is None else round(float(lang_prob), 4),
        "duration": round(float(total), 1),
        "speech": round(float(speech), 1),
        "problem": problem,
    }


class Engine:
    def __init__(self, model_dirs, *, compute_type: str, device: str) -> None:
        self.model_dirs = model_dirs
        self.compute_type = compute_type
        self.device = device
        self.cuda_model = None
        self.cuda_failures = 0

    def _load(self, path: str, device: str):
        from faster_whisper import WhisperModel

        if device == "cuda":
            return WhisperModel(path, device="cuda", compute_type=self.compute_type, local_files_only=True)
        return WhisperModel(
            path, device="cpu", compute_type="int8", cpu_threads=CPU_THREADS, local_files_only=True
        )

    def _cuda(self, path: str):
        if self.cuda_model is None:
            started = time.time()
            self.cuda_model = self._load(path, "cuda")
            name = os.path.basename(path)
            log(f"модель {name} на cuda ({self.compute_type}) за {time.time() - started:.1f} с")
        return self.cuda_model

    def preload(self) -> dict:
        """Ф79-1: модель на видеокарту заранее (движок диктовки — до первого куска). На cpu модель грузится на
        каждое задание — здесь ничего не делается."""
        started = time.time()
        model_path = first_model(self.model_dirs)
        if model_path is None:
            return {"code": 1, "error": "модель не найдена"}
        if self.device == "cuda":
            try:
                self._cuda(model_path)
            except Exception as exc:  # noqa: BLE001 — сбой загрузки: задания посчитаются на cpu
                self.cuda_model = None
                gc.collect()
                return {"code": 1, "preload": True, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        return {
            "code": 0,
            "preload": True,
            "device": self.device,
            "model": os.path.basename(model_path),
            "elapsed": round(time.time() - started, 2),
        }

    def handle(self, wav: str, manual: bool = False, lang: str | None = None) -> dict:
        started = time.time()
        model_path = first_model(self.model_dirs)
        if model_path is None:
            return {"code": 1, "error": "модель не найдена"}
        audio = load_wav(wav)
        result = None
        device = None
        cuda_error = None
        if self.device == "cuda":
            try:
                result = recognize(self._cuda(model_path), audio, manual=manual, lang_hint=lang)
                device = "cuda"
                self.cuda_failures = 0
            except Silence:
                device = "cuda"
                self.cuda_failures = 0
                result = self._silence(audio)
            except Exception as exc:  # noqa: BLE001 — любая ошибка cuda: запись считается на cpu
                cuda_error = f"{type(exc).__name__}: {str(exc)[:200]}"
                self.cuda_failures += 1
                self.cuda_model = None
                gc.collect()
        if result is None:
            device = "cpu"
            try:
                result = recognize(self._load(model_path, "cpu"), audio, manual=manual, lang_hint=lang)
            except Silence:
                result = self._silence(audio)
        response = {
            "code": 2 if result["problem"] else 0,
            **result,
            "device": device,
            "model": os.path.basename(model_path),
            "elapsed": round(time.time() - started, 2),
        }
        if cuda_error:
            response["cuda_error"] = cuda_error
        return response

    @staticmethod
    def _silence(audio) -> dict:
        duration = round(len(audio) / SAMPLE_RATE, 1)
        return {
            "text": "",
            "lang": None,
            "lang_prob": None,
            "duration": duration,
            "speech": 0.0,
            "problem": SILENCE,
        }


def serve(engine: Engine, stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    log(f"старт; cuBLAS: {preload_cuda_libs() or 'не найден'}")
    for line in stdin:
        if not line.strip():
            continue
        options = parse_options(line)
        try:
            if options.get("preload"):
                response = engine.preload()
            else:
                wav, manual = parse_job(line)
                lang = options.get("lang")
                response = engine.handle(wav, manual, lang) if lang else engine.handle(wav, manual)
        except Exception as exc:  # noqa: BLE001 — ошибка задания не роняет движок
            response = {"code": 1, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        stdout.flush()
        cuda_note = f" cuda_error={response['cuda_error']}" if "cuda_error" in response else ""
        log(
            f"задание: code={response.get('code')} device={response.get('device')} "
            f"elapsed={response.get('elapsed')}{cuda_note}"
        )
        if engine.cuda_failures >= MAX_CUDA_FAILURES:
            log(f"cuda сбоит {engine.cuda_failures} раз подряд — выхожу")
            return EXIT_CUDA_BROKEN
    return 0


def check(model_dirs) -> tuple[bool, dict]:
    """`--check`: faster_whisper импортируется, модель на месте — без загрузки на видеокарту."""
    info: dict = {"python": sys.version.split()[0]}
    try:
        import faster_whisper

        info["faster_whisper"] = getattr(faster_whisper, "__version__", "?")
    except ImportError as exc:
        info["error"] = f"faster_whisper не импортируется: {exc}"
        return False, info
    info["cublas"] = preload_cuda_libs()
    model = first_model(model_dirs)
    if model is None:
        info["error"] = "нет модели (model.bin) ни в одном из каталогов"
        return False, info
    info["model"] = model
    return True, info


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="движок расшифровки хаба (строки JSON по stdin/stdout)")
    parser.add_argument(
        "--model-dir", action="append", default=[], help="каталог модели (первый с model.bin)"
    )
    parser.add_argument("--compute-type", default="int8_float16")
    parser.add_argument("--device", default="cuda", choices=("cuda", "cpu"))
    parser.add_argument("--check", action="store_true", help="проверить пакеты и модель, не загружая её")
    args = parser.parse_args(argv)
    if args.check:
        ok, info = check(args.model_dir)
        print(json.dumps({"ok": ok, **info}, ensure_ascii=False))
        return 0 if ok else 1
    return serve(Engine(args.model_dir, compute_type=args.compute_type, device=args.device))


if __name__ == "__main__":
    sys.exit(main())
