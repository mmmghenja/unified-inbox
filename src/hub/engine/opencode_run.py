"""Разовый запуск OpenCode (вынесено из дежурной `hub-wake`, Ф57-2): окружение, проверка входа, итог хода.

Потребители: служба фактов (`facts/model.py`), хост рабочих сессий (проверка входа). Итог хода — тот же
`claude_run.Outcome`: `ok`, `fail` (`retryable=False` у «модель не найдена»), `pause` (лимит или баланс
провайдера — сейчас + 30 мин; ключ провайдера — тоже пауза, с пометкой `extra["auth"]`).

Ключи провайдеров хаб не читает: OpenCode берёт их из своего `auth.json` сам (нужен `HOME`).
"""

import logging
import os
import re
import subprocess
from pathlib import Path

from hub.core.engine import setup_path
from hub.engine import choice
from hub.engine.oc_turn import TurnResult, classify_error
from hub.work.files import nvm_bin_dirs

from . import claude_run as claude

_logger = logging.getLogger("hub.engine")

ROLE = "work"  # для проверки входа: модель любой живой роли (все на одном провайдере)
LIMIT_PAUSE_S = 1800  # П2: лимит/баланс провайдера — пауза движка 30 мин
VERSION_TIMEOUT_S = 20.0
TESTED_MINOR = ((1, 18), (1, 19))  # Ф99-0: 1.19.3 проверен пробой разрешений 28.10
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
ERROR_TEXT_MAX = 200

def build_env(environ: dict, binary: str, *, overrides: dict, home: Path) -> dict:
    """Белый список `claude.ENV_ALLOW` (+ `LC_*`); в `PATH` — `path_extra` из `engine.json` (каталог бинаря и
    node его установки, П8), node из nvm и каталог самого `opencode`. `HOME` обязателен: ключи провайдеров
    OpenCode читает из своего `auth.json` сам."""
    env = {k: v for k, v in environ.items() if k in claude.ENV_ALLOW or k.startswith("LC_")}
    env.setdefault("HOME", str(home))
    parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    extra = [p for p in overrides.get("path_extra") or [] if isinstance(p, str) and os.path.isabs(p)]
    head = [*extra, os.path.dirname(binary), *nvm_bin_dirs(home)]
    for directory in reversed(head):
        if directory and directory not in parts:
            parts.insert(0, directory)
    env["PATH"] = os.pathsep.join(parts)
    return env


def _version(binary: str, env: dict) -> tuple[str | None, str]:
    """(`1.18.31` или `None`, текст ошибки)."""
    try:
        proc = subprocess.run(  # noqa: S603 — путь из engine.json/which, без shell
            [binary, "--version"],
            env=env,
            capture_output=True,
            text=True,
            timeout=VERSION_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"opencode --version не выполнился ({exc.__class__.__name__})"
    m = _VERSION_RE.search(proc.stdout or "")
    if proc.returncode != 0 or m is None:
        return None, f"opencode --version: неразборчивый ответ (код {proc.returncode})"
    return m.group(0), ""


def auth_status(config_dir: Path, environ: dict, home: Path | None = None) -> tuple[bool, str]:
    """П2: «вход» OpenCode для хаба — `engine.json` есть (П8), бинарь исполняемый, `opencode --version`
    отвечает. Другая версия, чем 1.18, — не отказ, а предупреждение в тексте (раздел 5 спеки). Ключи
    провайдеров не проверяются (секреты): их нехватку покажет первый ход (`classify_error == "auth"`)."""
    home = Path.home() if home is None else home
    if not setup_path(config_dir).is_file():
        return False, "OpenCode: нет engine.json — запустите hub engine setup"
    try:
        overrides = choice.load_overrides(config_dir)
        binary = choice.opencode_path(overrides)
    except choice.EngineUnavailable as exc:
        return False, f"OpenCode: {exc}"
    version, error = _version(binary, build_env(environ, binary, overrides=overrides, home=home))
    if version is None:
        return False, f"OpenCode: {error}"
    provider, model, variant = choice.role_model(ROLE, overrides)
    text = f"opencode {version}, {provider}/{model} ({variant})"
    if tuple(int(x) for x in version.split(".")[:2]) not in TESTED_MINOR:
        text += " — ВНИМАНИЕ: хаб проверен на 1.18.x и 1.19.x"
        _logger.warning("engine: opencode %s — хаб проверен на 1.18.x и 1.19.x", version)
    return True, text


def _error_text(result: TurnResult) -> str:
    err = result.error or {}
    return f"{err.get('name') or 'ошибка'}: {str(err.get('message') or '')[:ERROR_TEXT_MAX]}".rstrip(": ")


def outcome_of(
    result: TurnResult, *, provider: str, model: str, now_s: float, data: dict
) -> claude.Outcome:
    """`TurnResult` -> `claude.Outcome` (контракт 16-3)."""
    if result.ok:
        return claude.Outcome("ok", data=data)
    kind = classify_error(result.error, result.retry_s)
    if kind in ("limit", "retry"):  # Ф83-1: долгий `retry` — пауза, как лимит
        return claude.Outcome(
            "pause",
            f"OpenCode: лимит или баланс провайдера {provider}",
            data=data,
            reset_ts=now_s + LIMIT_PAUSE_S,
            extra={"engine": "opencode"},
        )
    if kind == "auth":
        # П2: ключ провайдера — пауза движка с уведомлением (как проблема входа Claude).
        return claude.Outcome(
            "pause",
            f"OpenCode: нет ключа провайдера {provider} — войдите opencode auth login",
            data=data,
            reset_ts=now_s + LIMIT_PAUSE_S,
            extra={"engine": "opencode", "auth": True},
        )
    if kind == "model":
        # Проба P3: модель провайдера без ключа OpenCode не видит вовсе — «Model not found», а не 401.
        return claude.Outcome(
            "fail",
            f"OpenCode: модель {provider}/{model} не найдена — нет ключа провайдера {provider} "
            "(opencode auth login) или неверная модель в engine.json",
            data=data,
            retryable=False,
        )
    name = (result.error or {}).get("name")
    failure = "timeout" if name == "timeout" else f"OpenCode: {_error_text(result)}"
    return claude.Outcome("fail", failure, data=data)


def turn_stats(messages: list, since_ms: int) -> tuple[int, float | None]:
    """(шагов модели за ход, сумма `cost` их записей) — по `assistant`-записям, созданным с начала хода."""
    n = 0
    cost: float | None = None
    for m in messages:
        info = m.get("info") if isinstance(m, dict) else None
        if not isinstance(info, dict) or info.get("role") != "assistant":
            continue
        created = (info.get("time") or {}).get("created")
        if not isinstance(created, (int, float)) or created < since_ms:
            continue
        n += 1
        if isinstance(info.get("cost"), (int, float)) and not isinstance(info.get("cost"), bool):
            cost = (cost or 0.0) + float(info["cost"])
    return n, cost
