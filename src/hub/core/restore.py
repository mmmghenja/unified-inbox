"""Ф33-3 (К12): удержание очереди отправки в восстановленной копии — до первого старта ядра.

Что удерживается (прежняя машина могла выполнить это уже после снимка):
- `outbox` в `queued`/`sending` -> «могло уйти» (`unknown`), решает владелец (`outbox.hold_after_restore`);
- нажатия кнопок ботов в `approved`/`leased` -> `unknown` (`presses.hold_after_restore`);
- одобренные операции над группами WhatsApp -> снова «ждёт решения владельца» (`group_ops.hold_after_restore`).
Вступления и выходы (`join_request`, `leave_request`) не трогаются: повтор безвреден (уже участник / уже
вышел).

Зовёт `hub backup --hold-outbox <каталог данных>` из `scripts/hub-restore.sh` на собранном, ещё не
поставленном на место каталоге: в каталог данных хаба копия попадает уже с удержанием, и ядро, даже запущенное
сразу, не выдаст коннектору ни одной строки, бывшей в пути на прежней машине. Ядро в этом каталоге работать не
должно — берётся та же блокировка `core.lock`, что у `hub core` (`runner._acquire_lock`).
"""

import fcntl
import os
from pathlib import Path

from . import group_ops, outbox, presses
from .db import Db

# То же имя, что `runner.LOCK_NAME` (runner сюда не импортируется: он тянет за собой веб-сервер); равенство
# держит тест.
LOCK_NAME = "core.lock"


class RestoreError(Exception):
    """Каталог не годится для удержания: нет баз или в нём работает ядро."""


def _hold_all(conn, *, now_ms: int) -> dict[str, int]:
    return {
        "held": outbox.hold_after_restore(conn, now_ms=now_ms),
        "presses": presses.hold_after_restore(conn, now_ms=now_ms),
        "group_ops": group_ops.hold_after_restore(conn, now_ms=now_ms),
    }


def hold_outbox(data_dir: Path, *, now_ms: int) -> dict[str, int]:
    """Удержание в одной транзакции; ответ — сколько строк удержано: `held` (очередь отправки), `presses`,
    `group_ops`."""
    db = Db(data_dir)
    if not db.main_path.is_file() or not db.work_path.is_file():
        raise RestoreError(f"в {data_dir} нет файлов БД — это не каталог данных хаба")
    fd = os.open(data_dir / LOCK_NAME, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RestoreError(f"в {data_dir} работает hub core — сначала остановить") from exc
        db.start()
        try:
            return db.write_sync(lambda conn: _hold_all(conn, now_ms=now_ms))
        finally:
            db.close()
    finally:
        os.close(fd)
