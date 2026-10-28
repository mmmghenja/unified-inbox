"""Движок расшифровки `src/hub/transcribe/engine.py` (Ф14-6, docs/architecture.md): синтаксис Python 3.12 (интерпретатор
диктовки), тяжёлые пакеты — только внутри функций, чистые
функции, тишина -> «пусто» без сбоя cuda, сбой cuda -> cpu, три сбоя подряд -> выход 3. Модели нет: вместо
faster_whisper — подделка с теми же методами."""

import ast
import io
import json
import sys
import types

import pytest

from hub.transcribe import engine as eng
from hub.transcribe.service import ENGINE_PATH

_STDLIB_TOP = {"argparse", "ctypes", "gc", "json", "os", "sys", "time"}


def test_engine_parses_as_python_312_and_has_no_heavy_top_level_imports() -> None:
    """П12: движок запускается питоном venv диктовки (3.12) — синтаксис 3.14 (`except A, B:`) его уронит;
    numpy и faster_whisper на уровне модуля сломали бы импорт в тестах хаба."""
    src = ENGINE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src, feature_version=(3, 12))
    top = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            top.add((node.module or "").split(".")[0])
    assert top <= _STDLIB_TOP, top - _STDLIB_TOP
    assert "hub" not in top


def test_looks_broken_matches_dictation_rules() -> None:
    assert eng.looks_broken("", 3.0) == "пусто"
    assert eng.looks_broken("   ", 3.0) == "пусто"
    assert eng.looks_broken("привет как дела", 3.0) is None
    # длинная речь, слов мало: 20 с * 0.6 = 12 слов нужно
    assert eng.looks_broken("раз два три", 20.0) == "мало слов: 3 на 20 с речи"
    assert eng.looks_broken(" ".join(["да"] * 30), 10.0) == "зацикливание"
    assert eng.looks_broken(" ".join(f"слово{i}" for i in range(30)), 10.0) is None


def test_pick_lang_takes_bigger_of_ru_en_or_ru_by_default() -> None:
    assert eng.pick_lang([("de", 0.9), ("en", 0.06), ("ru", 0.04)]) == ("en", 0.06)
    assert eng.pick_lang([("ru", 0.7), ("en", 0.2)]) == ("ru", 0.7)
    assert eng.pick_lang([("de", 0.99)]) == ("ru", 0.0)
    assert eng.pick_lang(None) == ("ru", 0.0)
    assert eng.pick_lang([("en",), "мусор", ("ru", "x"), ("en", 0.3)]) == ("en", 0.3)


def test_parse_request() -> None:
    assert eng.parse_request('{"wav": "/tmp/a.wav"}\n') == "/tmp/a.wav"
    assert eng.parse_request(b'{"wav": "/x.wav"}') == "/x.wav"
    for bad in ('{"wav": ""}', "[]", '{"x": 1}', "не json", '{"wav": 5}'):
        with pytest.raises(ValueError):
            eng.parse_request(bad)
    with pytest.raises(ValueError):
        eng.parse_request('{"wav": "' + "a" * (eng.MAX_REQUEST + 1) + '"}')


def test_choose_attempt_takes_second_only_when_better() -> None:
    first = ("раз два", "мало слов: 2 на 20 с речи")
    ok = ("раз два три четыре", None)
    assert eng.choose_attempt(first, ok) is ok
    longer_broken = ("раз два три четыре пять", "мало слов: 5 на 20 с речи")
    assert eng.choose_attempt(first, longer_broken) is longer_broken
    loop = ("да " * 40, "зацикливание")
    assert eng.choose_attempt(first, loop) is first
    shorter = ("раз", "мало слов: 1 на 20 с речи")
    assert eng.choose_attempt(first, shorter) is first


def test_first_model_skips_dirs_without_model_bin(tmp_path) -> None:
    empty = tmp_path / "turbo"
    empty.mkdir()
    medium = tmp_path / "medium"
    medium.mkdir()
    (medium / "model.bin").write_bytes(b"x")
    assert eng.first_model([str(tmp_path / "нет"), str(empty), str(medium)]) == str(medium)
    assert eng.first_model([str(empty)]) is None


# --- распознавание на подделке модели -----------------------------------------------------------------


class _Seg:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeVadOptions:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


@pytest.fixture(autouse=True)
def _fake_vad_options(monkeypatch):
    """Вместо `faster_whisper.vad.VadOptions` (пакета в venv хаба нет) — та же форма вызова."""
    fake = types.ModuleType("faster_whisper.vad")
    fake.VadOptions = FakeVadOptions
    monkeypatch.setitem(sys.modules, "faster_whisper", types.ModuleType("faster_whisper"))
    monkeypatch.setitem(sys.modules, "faster_whisper.vad", fake)


class FakeModel:
    """Как faster_whisper.WhisperModel: `detect_language` и `transcribe`."""

    def __init__(self, *, probs=None, text="привет как дела", silent=False, fail=None, speech=None) -> None:
        self.probs = probs or [("ru", 0.8), ("en", 0.1)]
        self.text = text
        self.silent = silent
        self.fail = fail
        self.speech = speech
        self.calls: list[dict] = []

    def detect_language(self, audio, *, vad_filter, vad_parameters):
        # faster-whisper 1.2.1: словарь здесь не превращается в VadOptions — падало `AttributeError` (стенд)
        assert vad_filter is True and isinstance(vad_parameters, FakeVadOptions)
        assert vad_parameters.kwargs == eng.VAD_PARAMETERS
        if self.fail:
            raise self.fail
        if self.silent:
            raise ValueError("max() arg is an empty sequence")  # как faster-whisper 1.2.1 на тишине (П5)
        return self.probs[0][0], self.probs[0][1], self.probs

    def transcribe(self, audio, **params):
        self.calls.append(params)
        duration = len(audio) / eng.SAMPLE_RATE
        speech = self.speech if self.speech is not None else duration
        info = types.SimpleNamespace(duration=duration, duration_after_vad=speech)
        return iter([_Seg(" " + self.text + " ")]), info


def _audio(seconds: float) -> list[float]:
    return [0.0] * int(seconds * eng.SAMPLE_RATE)


def test_recognize_uses_detected_language_and_vad() -> None:
    model = FakeModel(probs=[("de", 0.5), ("en", 0.3), ("ru", 0.2)], text="hello there friend")
    result = eng.recognize(model, _audio(3))
    assert result["lang"] == "en" and result["lang_prob"] == 0.3
    assert result["problem"] is None and result["text"] == "hello there friend"
    assert len(model.calls) == 1
    call = model.calls[0]
    assert call["language"] == "en" and call["vad_filter"] is True
    assert call["condition_on_previous_text"] is False and "initial_prompt" not in call


def test_recognize_retries_without_vad_when_vad_cut_too_much() -> None:
    model = FakeModel(text=" ".join(f"с{i}" for i in range(20)), speech=1.0)
    result = eng.recognize(model, _audio(20))
    assert [c["vad_filter"] for c in model.calls] == [True, False]
    assert result["problem"] is None  # вторая попытка без VAD — без проблемы, она и взята
    assert result["speech"] == 20.0


def test_recognize_silence_raises_silence() -> None:
    with pytest.raises(eng.Silence):
        eng.recognize(FakeModel(silent=True), _audio(2))


def test_recognize_vad_found_no_speech_is_silence_without_second_pass() -> None:
    """Стенд 28.09: на цифровой тишине язык определяется, VAD оставляет 0 с, а проход без VAD выдумывает
    текст — вторую попытку не делать, «пусто»."""
    model = FakeModel(text="", speech=0.0)
    with pytest.raises(eng.Silence):
        eng.recognize(model, _audio(6))
    assert [c["vad_filter"] for c in model.calls] == [True]


def test_parse_job_and_manual_policy() -> None:
    assert eng.parse_job('{"wav": "/a.wav", "manual": true}') == ("/a.wav", True)
    assert eng.parse_job('{"wav": "/a.wav"}') == ("/a.wav", False)
    assert eng.parse_job('{"wav": "/a.wav", "manual": 1}') == ("/a.wav", False)
    assert eng.retry_without_vad_on_silence(True) is True
    assert eng.retry_without_vad_on_silence(False) is False


def test_recognize_manual_vad_found_no_speech_second_pass_without_vad() -> None:
    """14-6b: ручной повтор при 0 с речи по VAD — проход без VAD всё равно (автоматика — сразу «пусто»)."""
    model = FakeModel(text="тихо сказал", speech=0.0)
    result = eng.recognize(model, _audio(6), manual=True)
    assert [c["vad_filter"] for c in model.calls] == [True, False]
    assert result["text"] == "тихо сказал" and result["problem"] is None and result["speech"] == 6.0


class _SilentVadModel(FakeModel):
    """`detect_language` с VAD падает (речи нет), без VAD — язык по всей записи."""

    def detect_language(self, audio, *, vad_filter, vad_parameters=None):
        if vad_filter:
            raise ValueError("max() arg is an empty sequence")
        return "en", 0.7, [("en", 0.7), ("ru", 0.2)]


def test_recognize_manual_detect_language_without_vad_when_vad_empty() -> None:
    model = _SilentVadModel(text="", speech=0.0)
    with pytest.raises(eng.Silence):
        eng.recognize(model, _audio(3))  # автоматика — «пусто», без прохода
    assert model.calls == []
    result = eng.recognize(model, _audio(3), manual=True)
    assert result["lang"] == "en" and result["problem"] == eng.SILENCE
    assert [c["vad_filter"] for c in model.calls] == [True, False]


def _engine(monkeypatch, tmp_path, models: dict) -> eng.Engine:
    model_dir = tmp_path / "turbo"
    model_dir.mkdir(exist_ok=True)
    (model_dir / "model.bin").write_bytes(b"x")
    monkeypatch.setattr(eng, "load_wav", lambda path: _audio(4))
    engine = eng.Engine([str(model_dir)], compute_type="int8_float16", device="cuda")
    monkeypatch.setattr(engine, "_load", lambda path, device: models[device]())
    return engine


def test_handle_silence_is_not_a_cuda_failure(monkeypatch, tmp_path) -> None:
    """П5: тишина — `code 2`, «пусто», на cuda; счётчик сбоев cuda обнуляется, cpu не зовётся."""
    loaded = []

    def cuda():
        loaded.append("cuda")
        return FakeModel(silent=True)

    def cpu():
        loaded.append("cpu")
        return FakeModel()

    engine = _engine(monkeypatch, tmp_path, {"cuda": cuda, "cpu": cpu})
    engine.cuda_failures = 2
    response = engine.handle("/x.wav")
    assert response["code"] == 2 and response["problem"] == "пусто" and response["text"] == ""
    assert response["device"] == "cuda" and response["lang"] is None
    assert engine.cuda_failures == 0
    assert loaded == ["cuda"]


def test_handle_cuda_error_falls_back_to_cpu(monkeypatch, tmp_path) -> None:
    engine = _engine(
        monkeypatch,
        tmp_path,
        {"cuda": lambda: FakeModel(fail=RuntimeError("CUDA failed")), "cpu": lambda: FakeModel()},
    )
    response = engine.handle("/x.wav")
    assert response["code"] == 0 and response["device"] == "cpu"
    assert response["model"] == "turbo" and "RuntimeError" in response["cuda_error"]
    assert engine.cuda_failures == 1 and engine.cuda_model is None


def test_handle_keeps_cuda_model_between_jobs(monkeypatch, tmp_path) -> None:
    loads = []

    def cuda():
        loads.append(1)
        return FakeModel()

    engine = _engine(monkeypatch, tmp_path, {"cuda": cuda, "cpu": lambda: FakeModel()})
    assert engine.handle("/a.wav")["device"] == "cuda"
    assert engine.handle("/b.wav")["device"] == "cuda"
    assert loads == [1]


def test_serve_answers_each_line_and_exits_3_after_three_cuda_failures(monkeypatch, tmp_path) -> None:
    engine = _engine(
        monkeypatch,
        tmp_path,
        {"cuda": lambda: FakeModel(fail=RuntimeError("CUDA failed")), "cpu": lambda: FakeModel()},
    )
    monkeypatch.setattr(eng, "preload_cuda_libs", lambda: None)
    stdin = io.StringIO(
        '{"wav": "/1.wav"}\nкривая строка\n{"wav": "/2.wav"}\n{"wav": "/3.wav"}\n{"wav": "/4.wav"}\n'
    )
    stdout = io.StringIO()
    assert eng.serve(engine, stdin, stdout) == eng.EXIT_CUDA_BROKEN
    answers = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [a["code"] for a in answers] == [0, 1, 0, 0]  # четвёртое задание не взято: движок вышел
    assert all(a.get("device") == "cpu" for a in answers if a["code"] == 0)


def test_serve_exits_0_on_eof(monkeypatch, tmp_path) -> None:
    engine = _engine(monkeypatch, tmp_path, {"cuda": lambda: FakeModel(), "cpu": lambda: FakeModel()})
    monkeypatch.setattr(eng, "preload_cuda_libs", lambda: None)
    stdout = io.StringIO()
    assert eng.serve(engine, io.StringIO('{"wav": "/1.wav"}\n'), stdout) == 0
    assert json.loads(stdout.getvalue())["code"] == 0


def test_serve_passes_manual_flag(monkeypatch, tmp_path) -> None:
    engine = _engine(
        monkeypatch, tmp_path, {"cuda": lambda: FakeModel(speech=0.0), "cpu": lambda: FakeModel()}
    )
    monkeypatch.setattr(eng, "preload_cuda_libs", lambda: None)
    stdout = io.StringIO()
    eng.serve(engine, io.StringIO('{"wav": "/1.wav"}\n{"wav": "/2.wav", "manual": true}\n'), stdout)
    auto, manual = (json.loads(line) for line in stdout.getvalue().splitlines())
    assert auto["problem"] == eng.SILENCE and auto["text"] == ""
    assert manual["code"] == 0 and manual["text"] == "привет как дела"


def test_handle_without_model_is_error(monkeypatch, tmp_path) -> None:
    engine = eng.Engine([str(tmp_path / "нет")], compute_type="int8_float16", device="cuda")
    assert engine.handle("/x.wav") == {"code": 1, "error": "модель не найдена"}
