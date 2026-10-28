-- Рабочие сессии проектов (Ф9-1, 9a, docs/architecture.md,
-- П17, «Мелочи»). Общая схема (main и work): задача по проекту, где есть беседа work-аккаунта, пересказывает
-- корпоративную переписку и код — такая строка живёт в work (граница main/work, план 11.4, как `agent_run`).
-- Файл задачи выбирается при заведении; события, запросы и реплики задачи — в том же файле. id в work — от 2^40:
-- все четыре таблицы входят в `db._SHARED_TABLES_WITH_OWN_ID`. `project_id`, `channel_id`, `agent_run_id` — без
-- FOREIGN KEY (ссылки через границу файлов проверяет код).
--
-- `seq` — глобальный счётчик: каждая запись в `work_task`/`work_request` (и вставка `work_say`, П5) двигает `seq`
-- строки — `/sync`, SSE и длинное ожидание хоста (`/work/host/wait`) видят изменение.
-- `live` — у задачи есть живой процесс `claude` (ставит хост); «Стоп»/«Закрыть» без процесса ядро делает сразу (П5),
-- `max_parallel` считает задачи с живым процессом (П17). `launched_ts` — процесс задачи хоть раз запускался: сбой
-- между `claim` и запуском возвращает задачу в `approved` только без него (П10). `go_version` — версия заголовка и
-- сути, которую показал UI: «Го» с устаревшей версией -> 409 (П13). `active_ms` — сумма длительностей ходов без
-- висящих запросов (П11, ведёт хост). `CHECK` на `origin`/`work_event.kind` — с запасом под 9b/9c и этапы 2–3.
CREATE TABLE IF NOT EXISTS work_task (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    channel_id INTEGER,
    source_message_ids_json TEXT NOT NULL DEFAULT '[]',
    title TEXT NOT NULL,
    brief TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('proposed', 'approved', 'running', 'idle', 'done', 'stopped',
                                           'rejected', 'failed')),
    origin TEXT NOT NULL CHECK (origin IN ('wake', 'ui', 'orchestrator', 'auto')),
    proposed_run_id INTEGER,
    api_token_id INTEGER,
    go_version INTEGER NOT NULL DEFAULT 1,
    created_ts INTEGER NOT NULL,
    approved_ts INTEGER,
    started_ts INTEGER,
    launched_ts INTEGER,
    finished_ts INTEGER,
    stop_requested_ts INTEGER,
    close_requested_ts INTEGER,
    live INTEGER NOT NULL DEFAULT 0 CHECK (live IN (0, 1)),
    active_ms INTEGER NOT NULL DEFAULT 0,
    session_id TEXT,
    result TEXT,
    failure TEXT,
    agent_run_id INTEGER,
    last_event_id INTEGER,
    updated_ts INTEGER NOT NULL,
    seq INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_work_task_seq ON work_task (seq);
CREATE INDEX IF NOT EXISTS idx_work_task_project_status ON work_task (project_id, status);
CREATE INDEX IF NOT EXISTS idx_work_task_channel ON work_task (channel_id, status);

CREATE TABLE IF NOT EXISTS work_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('start', 'tool', 'tool_done', 'text', 'request', 'decision', 'say',
                                       'turn_end', 'status', 'error', 'warn')),
    tool TEXT,
    summary TEXT NOT NULL,
    is_error INTEGER NOT NULL DEFAULT 0 CHECK (is_error IN (0, 1)),
    ref TEXT
);
CREATE INDEX IF NOT EXISTS idx_work_event_task ON work_event (task_id, id);

-- `input_json` — вход инструмента как пришёл в `can_use_tool` (целиком до 256 КБ; длиннее — обрезан с
-- `input_truncated = 1`, П13). `delivered_ts` — решение доставлено процессу (`control_response`); решено, но не
-- доставлено к рестарту хоста -> `expired` (П10).
CREATE TABLE IF NOT EXISTS work_request (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('permission', 'question')),
    tool TEXT,
    input_json TEXT NOT NULL DEFAULT '{}',
    input_truncated INTEGER NOT NULL DEFAULT 0 CHECK (input_truncated IN (0, 1)),
    reason TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'allowed', 'denied', 'answered', 'expired')),
    answer_json TEXT,
    created_ts INTEGER NOT NULL,
    decided_ts INTEGER,
    delivered_ts INTEGER,
    seq INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_work_request_seq ON work_request (seq);
CREATE INDEX IF NOT EXISTS idx_work_request_task ON work_request (task_id, status);

CREATE TABLE IF NOT EXISTS work_say (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    priority TEXT NOT NULL CHECK (priority IN ('next', 'later', 'now')),
    status TEXT NOT NULL CHECK (status IN ('queued', 'delivered', 'dropped')),
    created_ts INTEGER NOT NULL,
    delivered_ts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_work_say_task ON work_say (task_id, status);
