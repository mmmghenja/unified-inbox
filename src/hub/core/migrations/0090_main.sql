-- Ф56-5 (docs/architecture.md): журнал запусков в терминале хаба — только `run` (кнопка
-- «▶ в терминал» и агент владельца по ключу `term`). Свободный ввод (`input`) сюда НЕ пишется — в нём пароли.
-- `who` — `ui` или `token:<id>`; `text` до 16 000 символов; срок хранения 30 дней (ежечасная чистка ядра,
-- `term/journal.py`). Номер терминала растёт (`main.meta`, ключ `term_seq`) и не переиспользуется.
CREATE TABLE IF NOT EXISTS term_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    term_id INTEGER NOT NULL,
    who TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('ui_block', 'agent')),
    source_ref TEXT,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_term_run_ts ON term_run (ts);
