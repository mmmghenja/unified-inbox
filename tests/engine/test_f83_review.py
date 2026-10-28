"""Ревью Ф83: воспроизведения находок (вне git)."""

import asyncio

from hub.engine import failure, login_check, oneshot


def _turn(error: str, failure_name: str = "error", engine: str = "claude") -> oneshot.Turn:
    return oneshot.Turn(ok=False, error=error, failure=failure_name, engine=engine)


def test_digits_402_in_error_text_is_not_balance():
    # Ошибка контекста Claude с числом токенов: «402» внутри «214020» — не баланс.
    turn = _turn("claude: error — API Error: 400 prompt is too long: 214020 tokens > 200000 maximum")
    kind = failure.classify("claude", turn)
    assert kind not in failure.BACKGROUND_REASONS, kind


def test_digits_429_in_error_text_is_not_limit():
    turn = _turn("claude: error — API Error: 400 prompt is too long: 214290 tokens > 200000 maximum")
    kind = failure.classify("claude", turn)
    assert kind not in failure.BACKGROUND_REASONS, kind


def test_opencode_transient_model_unavailable_is_not_model():
    # 503 провайдера «модель временно недоступна» — сбой провайдера, не «модели нет».
    err = {"name": "APIError", "message": "The model is temporarily unavailable, please retry", "status": 503}
    kind = failure.classify("opencode", err)
    assert kind not in failure.BACKGROUND_REASONS, kind


def test_login_check_claude_status_not_run_is_not_logged_out(monkeypatch, tmp_path):
    # `claude auth status` не выполнился (таймаут/неразборчивый вывод) — «не проверить», а не «входа нет».
    monkeypatch.setattr(login_check.oneshot, "_binary", lambda *a, **k: "/bin/true")
    monkeypatch.setattr(
        login_check.claude,
        "auth_status",
        lambda path, env: (False, "claude auth status не выполнился (TimeoutExpired)"),
    )

    async def no_sleep(_s):
        return None

    confirmed = asyncio.run(login_check.confirm_auth("claude", tmp_path, sleep=no_sleep))
    assert confirmed is False, (
        "две несостоявшиеся проверки подтвердили «нет входа» -> агент уйдёт на запасной навсегда"
    )


def test_offer_sees_codex_from_engine_json(monkeypatch, tmp_path):
    # Карточка паузы (`engine_fallback.offer`) зовёт `available(engine, None)`: у codex/cursor engine.json
    # из окружения не читается -> кнопки «Перейти на Codex» нет, хотя ответ `fallback` переход сделал бы.
    import json
    import os
    import stat

    from hub.core import engine as engine_mod

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    binary = tmp_path / "bin" / "codex"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\n")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    (cfg / "engine.json").write_text(json.dumps({"codex": {"path": str(binary)}}))
    monkeypatch.setenv("HUB_CONFIG_DIR", str(cfg))
    monkeypatch.setattr(engine_mod, "_home", lambda: tmp_path / "nohome")
    assert engine_mod.available("codex", cfg)[0] is True
    assert engine_mod.available("codex", None)[0] is True, "без config_dir codex из engine.json не виден"
    assert os.environ["HUB_CONFIG_DIR"] == str(cfg)
