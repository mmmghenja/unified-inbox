"""Ф83-1 (П12, раздел 3в плана): единый разбор отказа движка `engine/failure.py::classify`.

Маркеры Claude Code (событие лимита), OpenCode (ошибка хода, долгий retry), Codex и Cursor (текст), итог
разового хода — сведены в один словарь `limit | balance | auth | model | retry | other`; потребители пауз
(`oc_turn.classify_error`) отдают прежние значения."""

from types import SimpleNamespace

import pytest

from hub.engine import failure
from hub.engine.oc_turn import classify_error


@pytest.mark.parametrize(
    ("engine", "source", "kind"),
    [
        # Claude Code: событие лимита хоста.
        ("claude", {"status": "rejected", "resetsAt": 1}, "limit"),
        ("claude", {"status": "allowed_warning", "resetsAt": 1}, "other"),
        ("claude", {"status": "rejected", "rateLimitType": "seven_day_opus", "resetsAt": 1}, "other"),
        # Драйверы кладут свой `kind`.
        ("codex", {"status": "rejected", "kind": "limit", "reason": "x"}, "limit"),
        ("cursor", {"status": "rejected", "kind": "balance", "reason": "x"}, "balance"),
        ("opencode", {"status": "rejected", "reason": "нет ключа провайдера — opencode auth login"}, "auth"),
        ("opencode", {"status": "rejected", "reason": "непонятно"}, "limit"),
        # OpenCode: ошибка хода.
        ("opencode", {"name": "APIError", "message": "Insufficient Balance"}, "balance"),
        ("opencode", {"name": "APIError", "message": "x", "status": 402}, "balance"),
        ("opencode", {"name": "APIError", "message": "Too Many Requests", "status": 429}, "limit"),
        ("opencode", {"name": "ProviderAuthError", "message": "x"}, "auth"),
        ("opencode", {"name": "UnknownError", "message": "Model not found: a/b"}, "model"),
        ("opencode", {"name": "MessageAbortedError", "message": "aborted"}, "other"),
        # Тексты Codex и Cursor.
        ("codex", "You've hit your usage limit. Try again later.", "limit"),
        ("codex", "Please run codex login: not logged in", "auth"),
        ("cursor", "You have exceeded your quota for fast requests", "limit"),
        ("cursor", "usage pricing required", "balance"),
        ("cursor", "Model gpt-x is not supported", "model"),
        ("claude", "", "other"),
        ("claude", None, "other"),
        ("claude", 42, "other"),
    ],
)
def test_classify_table(engine, source, kind):
    assert failure.classify(engine, source) == kind


def test_long_retry_is_retry_not_limit():
    assert failure.classify("opencode", None, retry_s=failure.RETRY_LIMIT_S) == "retry"
    assert failure.classify("opencode", {"name": "UnknownError", "message": "x"}, retry_s=700.0) == "retry"
    assert failure.classify("opencode", {"name": "UnknownError", "message": "x"}, retry_s=599.0) == "other"
    # Потребитель пауз: retry — свой вид, баланс — как лимит (пауза та же), прочее — `fail`.
    assert classify_error(None, 700.0) == "retry"
    assert classify_error({"name": "APIError", "message": "Insufficient Balance"}) == "limit"
    assert classify_error({"name": "UnknownError", "message": "x"}) == "fail"


def test_turn_results():
    def _t(**kw):
        base = {"ok": False, "failure": None, "error": None, "auth": False, "pause": False, "extra": None}
        return SimpleNamespace(**{**base, **kw})

    assert failure.classify("claude", _t(ok=True)) == "other"
    assert failure.classify("claude", _t(extra={"kind": "balance"})) == "balance"
    assert failure.classify("claude", _t(auth=True)) == "auth"
    assert failure.classify("opencode", _t(failure="retry", pause=True)) == "retry"
    assert (
        failure.classify("opencode", _t(failure="limit", pause=True, error="Insufficient Balance"))
        == "balance"
    )
    assert failure.classify("claude", _t(pause=True, error="limit reached")) == "limit"
    assert failure.classify("cursor", _t(error="Model x not found")) == "model"


def test_reason_sets_and_labels():
    assert failure.SESSION_REASONS == {"limit", "balance"}
    assert "retry" not in failure.BACKGROUND_REASONS and "other" not in failure.BACKGROUND_REASONS
    assert {failure.label(k) for k in failure.KINDS} == set(failure.LABELS.values())
    assert failure.label(None) == failure.label("нет такого") == "сбой"
