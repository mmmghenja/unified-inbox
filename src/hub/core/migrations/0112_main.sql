-- 23.10.2026. Файлы и картинки,
-- которые владелец прикладывает к реплике разговора «Мой агент» (`main.owner_talk`).
--
-- Устроено как `held_file` (Ф41-3в): id — случайные 128 бит hex; байты — тот же склад
-- `<data_dir>/held-files/<sha[:2]>/<sha>`, один файл на содержимое; удаляются, когда на `sha256` не осталось
-- нестёртых строк ни в `held_file` (main, work), ни здесь. `talk_id` — реплика, к которой файл приложен (пусто —
-- ещё не приложен). Стёртая строка остаётся (`purged_ts`), чтобы лента помнила имя.
CREATE TABLE IF NOT EXISTS talk_file (
    id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL CHECK (size > 0),
    mime TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('image', 'pdf', 'file')),
    name TEXT NOT NULL,
    created_ts INTEGER NOT NULL,
    talk_id INTEGER,
    purged_ts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_talk_file_talk ON talk_file (talk_id);
CREATE INDEX IF NOT EXISTS idx_talk_file_sha ON talk_file (sha256);
