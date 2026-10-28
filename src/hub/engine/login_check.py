"""Двойная проверка входа движка перед переходом на запасной (Ф83-2, план Ф83, 3г П5).

Отказ хода «нет входа» (`failure.classify == "auth"`) у агента владельца и службы фактов — повод перейти на
запасной, только если вход подтверждённо пропал: две ЛОКАЛЬНЫЕ проверки с промежутком `AUTH_GAP_S` (60 с), и
ни одна не сказала «вход есть». Короткий сбой (CLI обновлял токен, гонка записи файла входа) так не уводит
роль на запасной навсегда (обратно переводит только владелец).

Проверки — те же, что у разовых ходов (`hub.engine.oneshot`, `claude_run`, `opencode_run`):
- `claude` — `claude auth status --json` (`claude_run.auth_status`): вход по подписке есть или нет;
- `codex` — `codex login status`, `cursor` — `cursor-agent status` (`oneshot._logged_in`);
- `opencode` — ключи провайдеров локально не проверить (секреты): `opencode_run.auth_status` (есть
  `engine.json`, бинарь отвечает) «нет» — входа нет; «да» — `None` (неизвестно): отказ провайдера
  `ProviderAuthError` — его собственный ответ, повтор через 60 с его не исправит.

Модуль берут служба фактов и начинка агента (`agent-owner/owner_agent/agent_model.py`); его место — рядом с
`hub.engine.failure`, когда кусок ядра будет его переносить. Вывод проверок не журналируется (там почта)."""

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from hub.engine import choice, oneshot
from hub.engine import claude_run as claude
from hub.engine import opencode_run as oc_run

AUTH_GAP_S = 60.0


def _check_sync(engine: str, config_dir: Path | None, binary: str | None) -> bool | None:
    environ = dict(os.environ)
    home = Path.home()
    if engine == "opencode":
        if config_dir is None:
            return None
        ok, _text = oc_run.auth_status(Path(config_dir), environ, home)
        return False if not ok else None
    try:
        path = oneshot._binary(engine, config_dir, home, binary)  # noqa: SLF001 — тот же поиск CLI, что у хода
    except choice.EngineUnavailable:
        return None  # CLI не найден — это не «нет входа»
    env = claude.build_env(environ, path)
    if engine == "claude":
        ok, text = claude.auth_status(path, env)
        if ok:
            return True
        # Ревью Ф83: «нет входа» — только явный ответ CLI; не выполнился, неразборчивый вывод, вход не по
        # подписке — «не проверить», иначе зависший CLI подтверждал бы ложный `auth`.
        return False if text.startswith("claude не залогинен") else None
    argv = [path, "login", "status"] if engine == "codex" else [path, "status"]
    return oneshot._status_logged_in(argv, env)  # noqa: SLF001


async def logged_in(engine: str, config_dir: Path | None, *, binary: str | None = None) -> bool | None:
    """Локальная проверка входа движка: `True` — есть, `False` — нет, `None` — не проверить."""
    return await asyncio.to_thread(_check_sync, engine, config_dir, binary)


async def confirm_auth(
    engine: str,
    config_dir: Path | None,
    *,
    binary: str | None = None,
    check: Callable[[], Awaitable[bool | None]] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    gap_s: float = AUTH_GAP_S,
) -> bool:
    """Вход подтверждённо пропал: две проверки через `gap_s`, ни одна не дала `True`. Первая сказала «вход
    есть» — вторая не делается. Ревью Ф83: у Claude, Codex и Cursor «не проверить» (`None`: CLI завис, вывод
    неразборчив) не подтверждает — нужны два явных «нет»; у OpenCode локальной проверки ключа нет — верим
    отказу хода, если проверка конфига не сказала «есть»."""

    async def probe() -> bool | None:
        if check is not None:
            return await check()
        return await logged_in(engine, config_dir, binary=binary)

    first = await probe()
    if first is True:
        return False
    await sleep(gap_s)
    second = await probe()
    if engine == "opencode":
        return second is not True
    return first is False and second is False
