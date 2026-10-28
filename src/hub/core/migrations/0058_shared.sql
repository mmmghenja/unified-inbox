-- Ф41-3в (docs/architecture.md). Файлы и картинки, которые владелец прикладывает к реплике
-- постоянной сессии хаба.
--
-- Общая схема (main и work), как у прочих `held_*`: строка лежит в файле сессии (`main.hub_session.store`).
-- id — случайные 128 бит hex (как у `upload`), не счётчик: таблица НЕ входит в
-- `db._SHARED_TABLES_WITH_OWN_ID`, файл строки находится по `store` её сессии. `session_id` — без FOREIGN KEY
-- (запись сессии в main).
--
-- Байты — `<data_dir>/held-files/<sha[:2]>/<sha>`, один файл на содержимое; удаляются, когда на `sha256` не
-- осталось строк с пустым `purged_ts` ни в одном из двух файлов. `say_id` — реплика, к которой файл приложен
-- (пусто — ещё не приложен). `released_ts` — реплика доставлена или снята: от него считаются 24 часа до
-- стирания байтов приложенного файла. Стёртая строка остаётся (`purged_ts`), чтобы лента помнила имя.
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
