"""`hub-cursor-hook <событие>` — хук Cursor в сессии хаба (Ф71-1, docs/architecture.md).

Тонкий клиент: сам ничего не решает. Вход хука (JSON из stdin) уходит ХОСТУ хаба одной строкой
`{"event", "input"}` по абстрактному unix-сокету хода (`HUB_CURSOR_SOCK`), ответ хоста — одна строка
`{"permission": "allow"|"deny", "agent_message"?, "user_message"?, "additional_context"?}` — печатается
как есть (Ф71-1: `additional_context` — реплика владельца или сигнал, вошедшие в идущий ход). Хост может держать
ответ сколько угодно (карточка владельцу); хук ждёт.

Подлинность сервера: `SO_PEERCRED` собеседника — pid хоста (`HUB_CURSOR_PID`) и свой uid (общий клиент
`hub.work.hook_client`, Ф74-5; у Codex свой формат ответа — `hub.work.codex_hook`). Любой сбой (нет
переменных, нет соединения, не тот собеседник, хост закрыл соединение без ответа, неразборчивый вход или
ответ, исключение) — печатается запрет и код выхода 2: в `hooks.json` у хука `failClosed: true`, и Cursor
сбой хука тоже считает запретом (вторая проба 19.10).

Только stdlib: хук зовётся на каждый инструмент."""

import json
import os
import sys

from hub.work import hook_client  # только stdlib: `hub` и `hub.work` при импорте ничего не тянут

EXIT_DENY = 2
SOCK_ENV = "HUB_CURSOR_SOCK"
PID_ENV = "HUB_CURSOR_PID"
INPUT_MAX = hook_client.INPUT_MAX
DENY_TEXT = "гард хаба: хук не получил решения хоста — запрет"


class _Refused(Exception):
    pass


def _deny(text: str = DENY_TEXT) -> str:
    return json.dumps({"permission": "deny", "agent_message": text, "user_message": text}, ensure_ascii=False)


def ask_host(event: str, raw: bytes, environ) -> dict:
    """Решение хоста по входу хука. Не вышло — исключение (вызывающий печатает запрет)."""
    answer = hook_client.ask_host(event, raw, environ, sock_env=SOCK_ENV, pid_env=PID_ENV)
    if answer.get("permission") not in ("allow", "deny"):
        raise _Refused("ответ хоста неразборчив")
    return answer


def run(argv: list[str], stdin, environ) -> tuple[int, str]:
    if len(argv) != 1 or not argv[0]:
        return EXIT_DENY, _deny()
    try:
        answer = ask_host(argv[0], stdin.read(INPUT_MAX + 1), environ)
    except Exception:  # noqa: BLE001 — любой сбой: запрет
        return EXIT_DENY, _deny()
    shown = {"permission": answer["permission"]}
    for key in ("agent_message", "user_message", "additional_context"):
        if isinstance(answer.get(key), str) and answer[key]:
            shown[key] = answer[key]
    if shown["permission"] == "deny" and "agent_message" in shown and "user_message" not in shown:
        shown["user_message"] = shown["agent_message"]
    return 0, json.dumps(shown, ensure_ascii=False)


def main(argv: list[str] | None = None) -> None:
    """Точка входа `hub-cursor-hook`."""
    try:
        code, out = run(list(sys.argv[1:] if argv is None else argv), sys.stdin.buffer, os.environ)
        sys.stdout.write(out + "\n")
        sys.stdout.flush()
    except BaseException:  # noqa: BLE001 — последний рубеж: не пропустить молча
        code = EXIT_DENY
        try:
            sys.stdout.write(_deny() + "\n")
            sys.stdout.flush()
        except BaseException:  # noqa: BLE001, S110
            pass
    sys.exit(code)


if __name__ == "__main__":
    main()
