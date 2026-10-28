"""`hub-codex-hook <событие>` — хук Codex в сессии хаба (Ф74-5, docs/architecture.md).

Тонкий клиент (`hub.work.hook_client`): вход хука уходит ХОСТУ по абстрактному сокету процесса сессии
(`HUB_CODEX_SOCK`, сверка pid хоста `HUB_CODEX_PID` и своего uid), ответ хоста — уже в формате хука Codex
(`{"hookSpecificOutput": {"hookEventName", "permissionDecision": "allow"|"deny", …}}` для `PreToolUse`,
пустой объект или `hookSpecificOutput` с `additionalContext` для прочих событий) — печатается как есть.

Сбой хука Codex 0.163.0 НЕ блокирует инструмент (справочник хуков: ошибка, таймаут или битый ответ —
«continues the tool call»), блокирует только явный запрет: JSON `permissionDecision: deny` или код выхода 2
с причиной в stderr. Поэтому любой сбой здесь — код 2 и причина в stderr; ожидание хоста — с пределом
`HUB_CODEX_HOOK_WAIT_S` (меньше таймаута хука у Codex: по своему таймауту Codex инструмент пропустил бы), а
падение самого интерпретатора ловит обёртка команды хука (`… || exit 2`, её собирает хост).

Только stdlib: хук зовётся на каждый инструмент."""

import json
import os
import sys

from hub.work import hook_client  # только stdlib: `hub` и `hub.work` при импорте ничего не тянут

EXIT_DENY = 2
SOCK_ENV = "HUB_CODEX_SOCK"
PID_ENV = "HUB_CODEX_PID"
WAIT_ENV = "HUB_CODEX_HOOK_WAIT_S"
DENY_TEXT = "гард хаба: хук не получил решения хоста — запрет"
_DECISIONS = ("allow", "deny")


def _valid(event: str, answer: dict) -> bool:
    if event != "PreToolUse":
        return True
    out = answer.get("hookSpecificOutput")
    return (
        isinstance(out, dict)
        and out.get("hookEventName") == "PreToolUse"
        and out.get("permissionDecision") in _DECISIONS
    )


def run(argv: list[str], stdin, environ) -> tuple[int, str, str]:
    """`(код выхода, stdout, stderr)`."""
    if len(argv) != 1 or not argv[0]:
        return EXIT_DENY, "", DENY_TEXT
    event = argv[0]
    try:
        wait = float(environ.get(WAIT_ENV) or 0) or None
        answer = hook_client.ask_host(
            event,
            stdin.read(hook_client.INPUT_MAX + 1),
            environ,
            sock_env=SOCK_ENV,
            pid_env=PID_ENV,
            timeout_s=wait,
        )
        if not _valid(event, answer):
            raise hook_client.Refused("ответ хоста неразборчив")
    except Exception:  # noqa: BLE001 — любой сбой: запрет
        return EXIT_DENY, "", DENY_TEXT
    return 0, json.dumps(answer, ensure_ascii=False), ""


def main(argv: list[str] | None = None) -> None:
    """Точка входа `hub-codex-hook`."""
    try:
        code, out, err = run(list(sys.argv[1:] if argv is None else argv), sys.stdin.buffer, os.environ)
        if out:
            sys.stdout.write(out + "\n")
            sys.stdout.flush()
        if err:
            sys.stderr.write(err + "\n")
            sys.stderr.flush()
    except BaseException:  # noqa: BLE001 — последний рубеж: не пропустить молча
        code = EXIT_DENY
        try:
            sys.stderr.write(DENY_TEXT + "\n")
            sys.stderr.flush()
        except BaseException:  # noqa: BLE001, S110
            pass
    sys.exit(code)


if __name__ == "__main__":
    main()
