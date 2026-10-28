"""Дамп задач asyncio по SIGUSR1 — диагностика залипшего демона без `ptrace` (план, раздел 11.4:
`ptrace_scope=1` не пускает посторонний процесс того же uid, `ptrace_scope=0` отдал бы память хаба с
токенами кому угодно). Спецификация: docs/architecture.md

Обработчик пишет заголовок `hub task dump <name> pid=... tasks=N`, затем по каждой задаче
`asyncio.all_tasks()` — имя, `done/cancelled`, стек (`task.print_stack`), и в конце стеки всех ОС-потоков
(`faulthandler.dump_traceback` — поток-писатель БД не asyncio, его дампит только это). Вывод — в stderr,
journald подхватывает его как обычный лог демона.

Только стеки: НИКАКИХ `repr` локальных переменных, аргументов корутин, текстов сообщений (план, раздел 7 —
логи journald без текстов). Обработчик не должен ронять процесс ни при какой внутренней ошибке — весь код
под `try/except`, ошибка — одной строкой без деталей.

Подключение: `runner._serve_forever` (`name='core'`). Коннекторы фазы 1+ подключают так же (`name=f
'connector:<source>'`) из СВОЕГО цикла запуска — сейчас (кусок 7) такого цикла у `ConnectorBase` ещё нет
(см. отчёт исполнителя куска 7, раздел про `connectors/base.py`), поэтому здесь для них только функция,
вызов — дело фазы 1.
"""

import asyncio
import faulthandler
import os
import signal
import sys
from asyncio import AbstractEventLoop


def install_task_dump(loop: AbstractEventLoop, *, name: str) -> None:
    """Повесить обработчик SIGUSR1 на `loop`. Идемпотентно вызывать не нужно — `add_signal_handler`
    сам заменяет прежний обработчик того же сигнала, если такой уже был (нам это не требуется: один
    процесс — один вызов на цикл)."""

    def _dump() -> None:
        try:
            tasks = list(asyncio.all_tasks(loop))
            print(f"hub task dump {name} pid={os.getpid()} tasks={len(tasks)}", file=sys.stderr)
            for task in tasks:
                print(
                    f"--- task {task.get_name()} done={task.done()} cancelled={task.cancelled()} ---",
                    file=sys.stderr,
                )
                task.print_stack(file=sys.stderr)
            faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        except Exception as exc:  # noqa: BLE001 — обработчик сигнала не должен ронять процесс ни при чём
            print(f"hub task dump {name}: ошибка дампа ({exc.__class__.__name__})", file=sys.stderr)

    loop.add_signal_handler(signal.SIGUSR1, _dump)
