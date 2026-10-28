-- Расшифровка голосовых (Ф14-6, docs/architecture.md). Общая схема
-- (main и work): строка живёт рядом со своей строкой `media` (расшифровка голосового Globex — в файле work).
--
-- Ключ — `media_id` (своего AUTOINCREMENT нет -> `db_of(media_id)` верен без правки
-- `db._SHARED_TABLES_WITH_OWN_ID`). Колонки `message_id` нет (П3): строки `media` перевешиваются на другое
-- сообщение (эхо своей отправки, альбом), сообщение — всегда через `media.message_id`. Строка уходит каскадом
-- вместе со строкой `media` («удалить у себя», замена вложения); замену файла в той же строке (`src_id`
-- сменился) чистит код (`media._sync_existing`, П4).
--
-- `text` — последний удачный текст (при повторе остаётся до нового); `problem` — сомнительный результат
-- («пусто», «мало слов …», «зацикливание»), не ошибка. `queued_ts` — когда строка встала в очередь (порядок
-- лиза и тревога hub-watch), `not_before_ts` — пауза перед повтором после неудачи.

CREATE TABLE IF NOT EXISTS transcript (
    media_id INTEGER PRIMARY KEY REFERENCES media (id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('queued', 'working', 'done', 'failed')),
    manual INTEGER NOT NULL DEFAULT 0,
    text TEXT,
    lang TEXT,
    lang_prob REAL,
    problem TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_gen INTEGER NOT NULL DEFAULT 0,
    lease_expires_ts INTEGER,
    not_before_ts INTEGER,
    error TEXT,
    model TEXT,
    device TEXT,
    elapsed_ms INTEGER,
    created_ts INTEGER NOT NULL,
    queued_ts INTEGER,
    done_ts INTEGER
);
CREATE INDEX IF NOT EXISTS transcript_queue ON transcript (status, manual, queued_ts);

-- Поиск по расшифровке (раздел A): тот же токенайзер и префиксы, что у `message_fts`.
CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(
    text,
    content='transcript',
    content_rowid='media_id',
    tokenize="unicode61 remove_diacritics 2",
    prefix='2 3 4'
);

-- Канон external content (как у `message_fts`): 'delete' — только если старая строка была в индексе.
CREATE TRIGGER IF NOT EXISTS transcript_fts_ai AFTER INSERT ON transcript BEGIN
    INSERT INTO transcript_fts (rowid, text)
    SELECT new.media_id, new.text WHERE new.text IS NOT NULL;
END;

CREATE TRIGGER IF NOT EXISTS transcript_fts_ad AFTER DELETE ON transcript BEGIN
    INSERT INTO transcript_fts (transcript_fts, rowid, text)
    SELECT 'delete', old.media_id, old.text WHERE old.text IS NOT NULL;
END;

CREATE TRIGGER IF NOT EXISTS transcript_fts_au AFTER UPDATE OF text ON transcript BEGIN
    INSERT INTO transcript_fts (transcript_fts, rowid, text)
    SELECT 'delete', old.media_id, old.text WHERE old.text IS NOT NULL;
    INSERT INTO transcript_fts (rowid, text)
    SELECT new.media_id, new.text WHERE new.text IS NOT NULL;
END;
