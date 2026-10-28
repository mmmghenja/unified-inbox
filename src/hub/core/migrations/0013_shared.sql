-- Журнал запусков Claude (Ф8-6, docs/architecture.md и поправка П1). Общая схема (main и work):
-- итог Claude по проекту, где есть беседа work-аккаунта, пересказывает корпоративную переписку — такая строка
-- живёт в work (граница main/work, план 11.4). id в work — от 2^40: `agent_run` входит в
-- `db._SHARED_TABLES_WITH_OWN_ID`. `project_id`, `api_token_id` — без FOREIGN KEY (ссылки через границу файлов
-- проверяет код).
-- П1: `CHECK` с запасом под рабочие сессии Ф9 (`work`; `waiting` — ждёт кнопки владельца, `stopped` — остановлена
-- владельцем) — иначе пересборка таблицы ради `CHECK`; в этом куске их никто не пишет.
CREATE TABLE IF NOT EXISTS agent_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('urgent', 'reply', 'digest', 'work')),
    status TEXT NOT NULL
        CHECK (status IN ('running', 'waiting', 'ok', 'failed', 'paused', 'retry', 'stopped')),
    started_ts INTEGER NOT NULL,
    finished_ts INTEGER,
    card_ids_json TEXT NOT NULL,
    channel_ids_json TEXT NOT NULL,
    draft_ids_json TEXT,
    result TEXT,
    failure TEXT,
    session_id TEXT,
    num_turns INTEGER,
    duration_ms INTEGER,
    api_token_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_agent_run_started ON agent_run (started_ts);
CREATE INDEX IF NOT EXISTS idx_agent_run_project_started ON agent_run (project_id, started_ts);
