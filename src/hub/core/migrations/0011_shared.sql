-- Медиа: картинки и файлы Telegram и Slack (Ф8-5, docs/architecture.md и поправки П1, П10).
-- Общая схема (main и work): строка живёт рядом со своим сообщением (медиа Globex — в файле work). id в work —
-- от 2^40: `media` входит в `db._SHARED_TABLES_WITH_OWN_ID` (П1), иначе `db_of(id)` увёл бы запрос по медиа work
-- в main. Сами байты — не в БД: `<data_dir>/media/<account_id>/<sha[:2]>/<sha>` (см. core/media.py).
-- `ref_json` — служебная ссылка коннектора (Slack: file_id, url_private…), наружу не отдаётся никогда.
-- `src_id` — идентичность файла у источника (Telegram photo.id/document.id, Slack file_id), П10.
-- `lease_expires_ts` не NULL у `pending`/`failed` — пометка «лиз истёк, но его никто не перехватил»: такой
-- `PUT` с тем же `lease_gen` ещё принимается (П4); любой переход мимо лиза её снимает.

CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES message (id),
    account_id INTEGER NOT NULL,
    idx INTEGER NOT NULL,
    kind TEXT NOT NULL,
    src_id TEXT,
    mime TEXT,
    size INTEGER,
    name TEXT,
    width INTEGER,
    height INTEGER,
    duration_s INTEGER,
    ref_json TEXT,
    status TEXT NOT NULL
        CHECK (status IN ('deferred', 'pending', 'fetching', 'ready', 'failed', 'gone', 'too_big')),
    priority INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_gen INTEGER NOT NULL DEFAULT 0,
    lease_expires_ts INTEGER,
    not_before_ts INTEGER,
    error TEXT,
    sha256 TEXT,
    stored_size INTEGER,
    detected TEXT,
    created_ts INTEGER NOT NULL,
    fetched_ts INTEGER,
    UNIQUE (message_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_media_status ON media (status, priority, created_ts);
CREATE INDEX IF NOT EXISTS idx_media_account_status ON media (account_id, status, not_before_ts);
-- Правило ссылок (П5): файл удаляется, только когда на его sha в пределах аккаунта не осталось строк.
CREATE INDEX IF NOT EXISTS idx_media_account_sha ON media (account_id, sha256) WHERE sha256 IS NOT NULL;
