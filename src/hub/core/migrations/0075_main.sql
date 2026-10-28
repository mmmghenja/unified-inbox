-- Ф52-1 (docs/architecture.md): учёт нагрузки ноутбука — заявки сессий «у меня
-- идёт прогон / сборка». Только hub.db: work остаётся на прежней версии. Хаб по этой таблице ничего не решает
-- и никого не блокирует — только показывает.
-- Запись живёт, пока жив процесс `pid` с тем же временем старта `pid_start` (поле 22 `/proc/<pid>/stat` —
-- против повторного использования pid) и пока не прошёл срок `expires_ts`; чистка — при каждом чтении и
-- каждой новой записи (`core/load.py`). `who` — кто запустил (задаёт вызывающий), `what` — что идёт.
CREATE TABLE IF NOT EXISTS load_claim (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    who TEXT NOT NULL,
    what TEXT NOT NULL,
    cores INTEGER NOT NULL CHECK (cores >= 1),
    mem_mb INTEGER NOT NULL CHECK (mem_mb >= 0),
    pid INTEGER NOT NULL,
    pid_start INTEGER NOT NULL,
    started_ts INTEGER NOT NULL,
    expires_ts INTEGER NOT NULL
);
