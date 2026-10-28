-- Подписка сессии Claude Code на проект (Ф13-1, docs/architecture.md). Только hub.db:
-- work остаётся на версии 21, следующая общая миграция — 0023.
-- Текстов нет: только числа, метки и идентичность сессии. `item_id`/`channel_id` — id из main или work
-- (глобально уникальны), ссылок между файлами нет. Строки не удаляются.
CREATE TABLE IF NOT EXISTS session_sub (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    api_token_id INTEGER NOT NULL, -- токен профиля, которым подписались
    session_pid INTEGER NOT NULL,
    session_proc_start INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    session_cwd TEXT NOT NULL,
    session_name TEXT, -- только если nameSource=='user' (владелец задал /rename), ≤ 64
    created_ts INTEGER NOT NULL,
    last_wait_ts INTEGER,
    last_delivery_ts INTEGER,
    ended_ts INTEGER,
    end_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS session_sub_live_project ON session_sub (project_id) WHERE ended_ts IS NULL;

CREATE TABLE IF NOT EXISTS session_signal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sub_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    project_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    label TEXT NOT NULL CHECK (label IN ('urgent', 'reply')),
    covered_seq INTEGER NOT NULL,
    msg_count INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'delivered', 'skipped', 'ended')),
    reason TEXT,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    delivered_ts INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS session_signal_pending ON session_signal (sub_id, item_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS session_signal_item ON session_signal (item_id, id);
