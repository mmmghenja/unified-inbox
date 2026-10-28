-- Чат оркестратора (Ф9-3, 9c, docs/architecture.md). Общая
-- схема (main и work), но строки пишутся ВСЕГДА в work: разговор пересказывает все проекты, включая
-- корпоративные (граница main/work, план 11.4) — id от 2^40, обе таблицы в `db._SHARED_TABLES_WITH_OWN_ID`.
-- Копия таблиц в main остаётся пустой (так устроены *_shared.sql).
--
-- `seq` — глобальный счётчик: каждая запись в разговор И каждая вставка/смена его сообщения двигают `seq`
-- СТРОКИ РАЗГОВОРА (П3) — `/sync` (UI-сессия), SSE-кадр `orch` и длинное ожидание хоста (`/work/host/wait`)
-- видят изменение; `last_message_id` — UI дочитывает сообщения по `after`. Управление — данными (П3):
-- `stop_requested_ts` — владелец нажал «Стоп», `new_requested_ts` + `closed_ts` — «Новый разговор» (или новый день).
-- `work_say.origin` — кто написал рабочей сессии: владелец из дорожки (`ui`) или оркестратор (`orchestrator`).
CREATE TABLE IF NOT EXISTS orch_conversation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    started_ts INTEGER NOT NULL,
    closed_ts INTEGER,
    state TEXT NOT NULL DEFAULT 'idle' CHECK (state IN ('idle', 'running', 'paused', 'failed')),
    failure TEXT,
    stop_requested_ts INTEGER,
    new_requested_ts INTEGER,
    last_message_id INTEGER,
    updated_ts INTEGER NOT NULL,
    seq INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orch_conversation_seq ON orch_conversation (seq);

-- `me` — реплика владельца (`priority`, `status`), `orch` — текст оркестратора (как пришёл, до 8000), `note` —
-- служебная строка хоста; `task_id` — ссылка на задачу (переход к дорожке). Хост `me` не пишет.
CREATE TABLE IF NOT EXISTS orch_message (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('me', 'orch', 'note')),
    text TEXT NOT NULL,
    priority TEXT CHECK (priority IN ('next', 'now')),
    status TEXT CHECK (status IN ('queued', 'delivered', 'dropped')),
    task_id INTEGER,
    created_ts INTEGER NOT NULL,
    delivered_ts INTEGER,
    seq INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orch_message_conversation ON orch_message (conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_orch_message_status ON orch_message (conversation_id, status);

ALTER TABLE work_say ADD COLUMN origin TEXT NOT NULL DEFAULT 'ui' CHECK (origin IN ('ui', 'orchestrator'));
