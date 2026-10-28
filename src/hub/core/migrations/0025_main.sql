-- Подписка сессии Claude Code на беседу (Ф14-5, docs/architecture.md + П2, П20). Только
-- hub.db: work остаётся на версии 24. Подписки на проект (0022) не трогаются, кроме столбца `mode` (П20).
-- Текстов нет: только числа и идентичность сессии. `channel_id`/`*_message_id` — id из main или work
-- (глобально уникальны), ссылок между файлами нет. Строки не удаляются.
CREATE TABLE IF NOT EXISTS session_chan_sub (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,        -- id беседы (main или work)
    project_id INTEGER,                 -- проект беседы при подписке; NULL — «Неразобранное»
    api_token_id INTEGER NOT NULL,
    session_pid INTEGER NOT NULL,
    session_proc_start INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    session_cwd TEXT NOT NULL,
    session_name TEXT,
    created_ts INTEGER NOT NULL,
    last_wait_ts INTEGER,
    last_delivery_ts INTEGER,
    ended_ts INTEGER,
    end_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS session_chan_sub_live ON session_chan_sub (channel_id) WHERE ended_ts IS NULL;
CREATE INDEX IF NOT EXISTS session_chan_sub_session ON session_chan_sub (session_pid, session_proc_start)
    WHERE ended_ts IS NULL;

-- Сигнал о новых входящих беседы: подписке на беседу (`chan_sub_id`) или подписке на проект в режиме
-- «каждое сообщение» (`project_sub_id`, П20) — ровно одно из двух. Один `pending` копит число сообщений и
-- id последнего (П2: не `seq` — он растёт при правке, реакции, медиа).
CREATE TABLE IF NOT EXISTS session_chan_signal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chan_sub_id INTEGER,
    project_sub_id INTEGER,
    channel_id INTEGER NOT NULL,
    first_message_id INTEGER NOT NULL,
    last_message_id INTEGER NOT NULL,
    msg_count INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'delivered', 'skipped', 'ended')),
    reason TEXT,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    delivered_ts INTEGER,
    CHECK ((chan_sub_id IS NULL) <> (project_sub_id IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS session_chan_signal_pending_chan ON session_chan_signal (chan_sub_id)
    WHERE status = 'pending' AND chan_sub_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS session_chan_signal_pending_project
    ON session_chan_signal (project_sub_id, channel_id) WHERE status = 'pending' AND project_sub_id IS NOT NULL;

-- П20: режим подписки на проект — `cards` (карточки дежурного, как 13-1) или `messages` (каждое чужое
-- входящее беседы проекта). Старые подписки — `cards`.
ALTER TABLE session_sub ADD COLUMN mode TEXT NOT NULL DEFAULT 'cards' CHECK (mode IN ('cards', 'messages'));
