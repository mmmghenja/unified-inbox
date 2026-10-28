-- Ф71-1 (docs/architecture.md): третий движок постоянных сессий хаба — `cursor`
-- (Cursor CLI). Ограничение CHECK столбца `engine` в SQLite не меняется на месте — таблица пересоздаётся
-- по 12 шагам (https://sqlite.org/lang_altertable.html#otheralter): новая таблица с полной текущей схемой,
-- перенос строк с явным списком столбцов (у базы, пришедшей путём «миграция с версии 4», столбцы стоят в
-- другом порядке), счётчик AUTOINCREMENT (`sqlite_sequence`: id закрытых сессий не переиспользуются, П1
-- Ф41-2а), замена таблицы и её индексов. Внешних ключей, триггеров и представлений на `hub_session` нет.
CREATE TABLE hub_session_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    role TEXT,
    role_key TEXT,
    role_no INTEGER,
    engine TEXT NOT NULL CHECK (engine IN ('claude', 'opencode', 'cursor')),
    model TEXT,
    effort TEXT,
    mode TEXT NOT NULL DEFAULT 'manual' CHECK (mode IN ('manual', 'auto')),
    engine_session_id TEXT,
    workdir TEXT NOT NULL,
    title TEXT,
    state TEXT NOT NULL DEFAULT 'asleep' CHECK (state IN ('running', 'idle', 'asleep')),
    proc_pid INTEGER,
    proc_start INTEGER,
    host_seen_ts INTEGER,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    closed_ts INTEGER,
    close_reason TEXT,
    created_by TEXT NOT NULL,
    seq INTEGER NOT NULL DEFAULT 0,
    last_event_id INTEGER,
    interrupt_requested_ts INTEGER,
    sleep_requested_ts INTEGER,
    last_turn_ts INTEGER,
    launch_fails INTEGER NOT NULL DEFAULT 0,
    failure TEXT,
    store TEXT CHECK (store IN ('main', 'work')),
    usage_json TEXT,
    want_sub_json TEXT,
    wake_requested_ts INTEGER,
    compact_requested_ts INTEGER,
    signal_turn_ts INTEGER,
    signal_lost_ts INTEGER,
    autosend_ok INTEGER NOT NULL DEFAULT 1 CHECK (autosend_ok IN (0, 1)),
    launch_key_hash TEXT,
    bg_json TEXT,
    compact_tokens INTEGER,
    compact_auto INTEGER NOT NULL DEFAULT 1 CHECK (compact_auto IN (0, 1)),
    compact_at INTEGER,
    wait_reason TEXT CHECK (wait_reason IN ('slot', 'elsewhere', 'paused', 'heavy', 'compact')),
    role_file TEXT,
    sys_pin TEXT,
    turn_ts INTEGER,
    turn_origin TEXT,
    mid_queued_ts INTEGER,
    subdir TEXT,
    via_agent INTEGER NOT NULL DEFAULT 0 CHECK (via_agent IN (0, 1)),
    turns INTEGER NOT NULL DEFAULT 0
);
INSERT INTO hub_session_new (
    id, project_id, role, role_key, role_no, engine, model, effort, mode, engine_session_id, workdir, title,
    state, proc_pid, proc_start, host_seen_ts, created_ts, updated_ts, closed_ts, close_reason, created_by, seq,
    last_event_id, interrupt_requested_ts, sleep_requested_ts, last_turn_ts, launch_fails, failure, store,
    usage_json, want_sub_json, wake_requested_ts, compact_requested_ts, signal_turn_ts, signal_lost_ts,
    autosend_ok, launch_key_hash, bg_json, compact_tokens, compact_auto, compact_at, wait_reason, role_file,
    sys_pin, turn_ts, turn_origin, mid_queued_ts, subdir, via_agent, turns
)
SELECT
    id, project_id, role, role_key, role_no, engine, model, effort, mode, engine_session_id, workdir, title,
    state, proc_pid, proc_start, host_seen_ts, created_ts, updated_ts, closed_ts, close_reason, created_by, seq,
    last_event_id, interrupt_requested_ts, sleep_requested_ts, last_turn_ts, launch_fails, failure, store,
    usage_json, want_sub_json, wake_requested_ts, compact_requested_ts, signal_turn_ts, signal_lost_ts,
    autosend_ok, launch_key_hash, bg_json, compact_tokens, compact_auto, compact_at, wait_reason, role_file,
    sys_pin, turn_ts, turn_origin, mid_queued_ts, subdir, via_agent, turns
FROM hub_session;
-- Счётчик AUTOINCREMENT: вставка выше завела строку `hub_session_new` по наибольшему перенесённому id, а
-- закрытые и удалённые сессии могли поднять счётчик старой таблицы выше — берётся он.
DELETE FROM sqlite_sequence WHERE name = 'hub_session_new';
INSERT INTO sqlite_sequence (name, seq) SELECT 'hub_session_new', seq FROM sqlite_sequence WHERE name = 'hub_session';
DROP TABLE hub_session;
ALTER TABLE hub_session_new RENAME TO hub_session;
CREATE INDEX hub_session_open ON hub_session (project_id) WHERE closed_ts IS NULL;
CREATE UNIQUE INDEX hub_session_open_role ON hub_session (project_id, role_key, role_no)
    WHERE closed_ts IS NULL AND role_key IS NOT NULL;
CREATE UNIQUE INDEX hub_session_open_engine_sid ON hub_session (engine_session_id)
    WHERE closed_ts IS NULL AND engine_session_id IS NOT NULL;
CREATE INDEX hub_session_seq ON hub_session (seq);
