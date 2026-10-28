-- Починка выкладки 08.10.2026. Таблица `held_file` (Ф41-3в) пришла файлом `0058_shared.sql`, а версия 58
-- основной базы к тому времени уже была занята миграцией `0058_main.sql` (Ф41-3а), выложенной отдельным
-- деплоем: версия базы — одно число, и `0058_shared.sql` основная база пропустила. На проде таблица появилась
-- только в рабочей базе — загрузка файла в сессию и ежечасная чистка падали с «no such table: main.held_file».
--
-- Здесь то же определение, что в `0058_shared.sql`, под `IF NOT EXISTS`: где таблица уже есть (рабочая база,
-- свежие базы) — ничего не меняется, где её нет (основная база прода) — создаётся.
--
-- Правило с этого номера (закреплено тестом `tests/core/test_db.py`): один номер — один файл. Нужны правки и
-- main, и shared — это два соседних номера, а не пара `NNNN_main.sql` + `NNNN_shared.sql`.
CREATE TABLE IF NOT EXISTS held_file (
    id TEXT PRIMARY KEY,
    session_id INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL CHECK (size > 0),
    mime TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('image', 'pdf', 'file')),
    name TEXT NOT NULL,
    created_ts INTEGER NOT NULL,
    say_id INTEGER,
    released_ts INTEGER,
    purged_ts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_held_file_session ON held_file (session_id, say_id);
CREATE INDEX IF NOT EXISTS idx_held_file_sha ON held_file (sha256);
CREATE INDEX IF NOT EXISTS idx_held_file_say ON held_file (say_id);
