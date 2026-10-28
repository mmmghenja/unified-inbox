"""Движок: подсказка языка и загрузка заранее (Ф79-1, docs/architecture.md).
Подделка модели — из test_engine.py."""

# ruff: noqa: F811 — фикстура `_fake_vad_options` импортирована

import io
import json

import pytest

from hub.transcribe import engine as eng

from .test_engine import FakeModel, _audio, _engine, _fake_vad_options  # noqa: F401 — фикстура


class NoDetect(FakeModel):
    def detect_language(self, *args, **kwargs):
        raise AssertionError("язык известен — определять не нужно")


def test_lang_hint_skips_detection():
    model = NoDetect()
    result = eng.recognize(model, _audio(3), lang_hint="en")
    assert result["lang"] == "en" and result["lang_prob"] is None and result["text"] == "привет как дела"
    assert [c["language"] for c in model.calls] == ["en"]


def test_lang_hint_silence_is_silence():
    with pytest.raises(eng.Silence):
        eng.recognize(NoDetect(speech=0.0), _audio(3), lang_hint="ru")


def test_unknown_hint_falls_back_to_detection():
    model = FakeModel(probs=[("en", 0.9), ("ru", 0.05)])
    assert eng.recognize(model, _audio(3), lang_hint="de")["lang"] == "en"


@pytest.mark.parametrize(
    ("line", "want"),
    [
        ('{"wav": "/a.wav", "lang": "ru"}', {"lang": "ru", "preload": False}),
        ('{"wav": "/a.wav", "lang": "de"}', {"lang": None, "preload": False}),
        ('{"preload": true}', {"lang": None, "preload": True}),
        ('{"preload": 1}', {"lang": None, "preload": False}),
        ("кривая строка", {}),
        ("[1]", {}),
    ],
)
def test_parse_options(line, want):
    assert eng.parse_options(line) == want


def test_serve_preload_loads_cuda_once_and_passes_lang(monkeypatch, tmp_path):
    loads = []

    def cuda():
        loads.append(1)
        return FakeModel(probs=[("ru", 0.9)])

    engine = _engine(monkeypatch, tmp_path, {"cuda": cuda, "cpu": FakeModel})
    monkeypatch.setattr(eng, "preload_cuda_libs", lambda: None)
    stdout = io.StringIO()
    stdin = io.StringIO('{"preload": true}\n{"wav": "/1.wav", "lang": "en"}\n{"wav": "/2.wav"}\n')
    assert eng.serve(engine, stdin, stdout) == 0
    preload, hinted, plain = (json.loads(line) for line in stdout.getvalue().splitlines())
    assert preload["code"] == 0 and preload["preload"] is True and preload["device"] == "cuda"
    assert hinted["lang"] == "en" and hinted["lang_prob"] is None
    assert plain["lang"] == "ru" and plain["lang_prob"] == 0.9
    assert loads == [1]  # модель загружена заранее и больше не грузилась


def test_preload_without_model_or_with_cuda_error(monkeypatch, tmp_path):
    assert eng.Engine([str(tmp_path / "нет")], compute_type="int8_float16", device="cuda").preload() == {
        "code": 1,
        "error": "модель не найдена",
    }

    def broken():
        raise RuntimeError("CUDA out of memory")

    engine = _engine(monkeypatch, tmp_path, {"cuda": broken, "cpu": FakeModel})
    out = engine.preload()
    assert out["code"] == 1 and "RuntimeError" in out["error"] and engine.cuda_model is None
