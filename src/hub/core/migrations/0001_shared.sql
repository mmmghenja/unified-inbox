-- Таблицы, одинаковые в hub.db и hub-work.db (свои данные в каждом файле). Раздел 2 плана.
-- Применяется к обоим файлам как есть: FK через границу main/work (account_id, project_id, api_token_id) не
-- ставим — этих таблиц в текущем файле может не быть, проверка кодом (см. спеку). Внутри одного файла FK есть.
-- Для work после этого файла отдельно засевается sqlite_sequence (см. db.py) — id в work глобально >= 2**40.

CREATE TABLE IF NOT EXISTS contact (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    ext_id TEXT NOT NULL,
    name TEXT,
    handle TEXT,
    is_me INTEGER NOT NULL DEFAULT 0 CHECK (is_me IN (0, 1))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_contact_account_ext ON contact (account_id, ext_id);

CREATE TABLE IF NOT EXISTS channel (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    ext_id TEXT NOT NULL,
    parent_channel_id INTEGER REFERENCES channel (id),
    kind TEXT NOT NULL CHECK (kind IN ('dm', 'group', 'channel', 'topic', 'mail_thread', 'issue', 'card', 'task')),
    title TEXT,
    project_id INTEGER,
    tier TEXT,
    muted INTEGER NOT NULL DEFAULT 0 CHECK (muted IN (0, 1)),
    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    map_source TEXT CHECK (map_source IN ('auto', 'manual')),
    map_reason TEXT,
    last_msg_ts INTEGER,
    last_read_seq INTEGER,
    first_seen_ts INTEGER,
    my_msg_count INTEGER NOT NULL DEFAULT 0,
    title_history_json TEXT,
    meta_json TEXT,
    seq INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_channel_account_ext ON channel (account_id, ext_id);
CREATE INDEX IF NOT EXISTS idx_channel_project ON channel (project_id);

-- Миграция группы в супергруппу и т.п.: старый ext_id -> текущий channel_id.
CREATE TABLE IF NOT EXISTS channel_alias (
    account_id INTEGER NOT NULL,
    old_ext_id TEXT NOT NULL,
    channel_id INTEGER NOT NULL REFERENCES channel (id),
    PRIMARY KEY (account_id, old_ext_id)
);

CREATE TABLE IF NOT EXISTS thread (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL REFERENCES channel (id),
    ext_id TEXT NOT NULL,
    last_reply_ts INTEGER,
    subscribed INTEGER NOT NULL DEFAULT 0 CHECK (subscribed IN (0, 1))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_channel_ext ON thread (channel_id, ext_id);

-- Перед message: message.outbox_id на неё ссылается.
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL REFERENCES channel (id),
    account_id INTEGER NOT NULL,
    thread_ext_id TEXT,
    reply_to_ext_id TEXT,
    text TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('ui', 'api')),
    api_token_id INTEGER NOT NULL,
    idempotency_key TEXT,
    send_token TEXT,
    status TEXT NOT NULL DEFAULT 'pending_approval'
        CHECK (status IN ('pending_approval', 'queued', 'sending', 'sent', 'failed', 'unknown', 'cancelled')),
    lease_gen INTEGER NOT NULL DEFAULT 0,
    reconcile INTEGER NOT NULL DEFAULT 0 CHECK (reconcile IN (0, 1)),
    -- NOT NULL: строка с NULL была бы навсегда невидима для lease() — молчаливая потеря.
    not_before_ts INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_expires_ts INTEGER,
    send_started_ts INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    ext_id TEXT,
    error TEXT,
    created_ts INTEGER NOT NULL,
    sent_ts INTEGER,
    UNIQUE (api_token_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_outbox_account_status_not_before ON outbox (account_id, status, not_before_ts);

CREATE TABLE IF NOT EXISTS message (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seq INTEGER NOT NULL UNIQUE,
    first_seq INTEGER NOT NULL,
    -- Числовой ext_id как число: порядок показа (ts, ext_sort, ext_id). Текстом "99" > "100", а у Telegram
    -- ts в секундах и совпадает у соседних сообщений. Нечисловой ext_id -> 0 (Slack ts сортируется текстом верно).
    ext_sort INTEGER NOT NULL DEFAULT 0,
    account_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL REFERENCES channel (id),
    ext_id TEXT,
    thread_ext_id TEXT,
    reply_to_ext_id TEXT,
    grouped_id TEXT,
    sender_contact_id INTEGER REFERENCES contact (id),
    is_me INTEGER NOT NULL DEFAULT 0 CHECK (is_me IN (0, 1)),
    kind TEXT NOT NULL CHECK (kind IN ('text', 'event', 'file')),
    text TEXT,
    entities_json TEXT,
    attachments_json TEXT,
    forward_json TEXT,
    mentions_me INTEGER NOT NULL DEFAULT 0 CHECK (mentions_me IN (0, 1)),
    refs_json TEXT,
    reactions_json TEXT,
    ts INTEGER NOT NULL,
    edited_ts INTEGER,
    deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1)),
    outbox_id INTEGER REFERENCES outbox (id),
    UNIQUE (channel_id, ext_id)
);
CREATE INDEX IF NOT EXISTS idx_message_account_ext ON message (account_id, ext_id);
CREATE INDEX IF NOT EXISTS idx_message_channel_ts ON message (channel_id, ts, ext_sort, ext_id);
CREATE INDEX IF NOT EXISTS idx_message_outbox ON message (outbox_id) WHERE outbox_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_message_channel_first_seq ON message (channel_id, first_seq);

-- Только последние N на аккаунт (чистит код); для work-БД не пишем вовсе (корпоративные тексты).
CREATE TABLE IF NOT EXISTS message_raw (
    message_id INTEGER PRIMARY KEY REFERENCES message (id),
    blob BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS dead_letter (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    payload TEXT NOT NULL,
    error TEXT,
    ts INTEGER NOT NULL
);

-- Курсор коннектора лежит в той же БД, что его сообщения (одна транзакция на файл).
CREATE TABLE IF NOT EXISTS cursor (
    account_id INTEGER PRIMARY KEY,
    value TEXT,
    ts INTEGER,
    updated_ts INTEGER
);

CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
    text,
    content='message',
    content_rowid='id',
    tokenize="unicode61 remove_diacritics 2",
    prefix='2 3 4'
);

-- Канон external content: удалённое из FTS уходит при deleted=1 и не индексируется при вставке с deleted=1.
CREATE TRIGGER IF NOT EXISTS message_fts_ai AFTER INSERT ON message BEGIN
    INSERT INTO message_fts (rowid, text)
    SELECT new.id, new.text WHERE new.deleted = 0;
END;

-- 'delete' шлём только если старая строка реально была в индексе (old.deleted=0) — иначе fts5 считает
-- контент внешней таблицы рассинхронизированным и падает с "database disk image is malformed".
CREATE TRIGGER IF NOT EXISTS message_fts_ad AFTER DELETE ON message BEGIN
    INSERT INTO message_fts (message_fts, rowid, text)
    SELECT 'delete', old.id, old.text WHERE old.deleted = 0;
END;

-- Только text/deleted: правка реакций или seq не должна переиндексировать текст.
CREATE TRIGGER IF NOT EXISTS message_fts_au AFTER UPDATE OF text, deleted ON message BEGIN
    INSERT INTO message_fts (message_fts, rowid, text)
    SELECT 'delete', old.id, old.text WHERE old.deleted = 0;
    INSERT INTO message_fts (rowid, text)
    SELECT new.id, new.text WHERE new.deleted = 0;
END;
