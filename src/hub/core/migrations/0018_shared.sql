-- Отправка картинок и файлов (Ф10-4, docs/architecture.md и поправки П1, П2, П15).
-- Общая схема (main и work): загрузка привязана к беседе и лежит в файле своего канала (`db_of(channel_id)`),
-- байты — сразу в складе медиа аккаунта канала (`<data_dir>/media/<account_id>/<sha[:2]>/<sha>`). Аккаунта 0
-- нет. `id` — случайные 128 бит (base64url), не AUTOINCREMENT: в `db._SHARED_TABLES_WITH_OWN_ID` не входит,
-- схема строки определяется каналом.
--
-- Строка живёт, пока загрузка не ушла в отправку: при постановке (`outbox.enqueue`) она УДАЛЯЕТСЯ — всё нужное
-- переходит в `outbox.file_json`, ссылкой на файл становится строка `media` заготовки. Неотправленные старше
-- 24 ч удаляет фоновая очистка ядра (`uploads.cleanup`). Строки `upload` — тоже ссылки на файл склада (П2):
-- `media.release_file` их считает.
CREATE TABLE IF NOT EXISTS upload (
    id TEXT PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    mime TEXT NOT NULL,
    detected TEXT NOT NULL,
    name TEXT NOT NULL,
    created_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS upload_account_sha ON upload (account_id, sha256);
CREATE INDEX IF NOT EXISTS upload_created ON upload (created_ts);

-- Строка очереди несёт не больше одного файла: `{sha256, size, mime, name, kind, upload_id}`
-- (`kind` — `photo`|`document`). `upload_id` — только для сравнения при повторе по ключу идемпотентности
-- (П15). «Удалить у себя» стирает всё, кроме `{kind, size}` (П2).
ALTER TABLE outbox ADD COLUMN file_json TEXT;
