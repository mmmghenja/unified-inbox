-- Таблицы только в main (hub.db): проекты, аккаунты, правила раскладки, токены, аудит, служебные счётчики.
-- Раздел 2 плана + решения архитектора в docs/architecture.md.

CREATE TABLE IF NOT EXISTS project (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    color TEXT,
    sort INTEGER NOT NULL DEFAULT 0,
    muted INTEGER NOT NULL DEFAULT 0 CHECK (muted IN (0, 1)),
    workdir TEXT,
    agent_policy_json TEXT
);

CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    title TEXT,
    config_json TEXT,
    db TEXT NOT NULL CHECK (db IN ('main', 'work')),
    status TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'stale', 'auth_required', 'limited', 'no_network')),
    last_ok_ts INTEGER,
    last_error TEXT,
    flood_until INTEGER
);

CREATE TABLE IF NOT EXISTS map_rule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES account (id),
    match_json TEXT NOT NULL,
    project_id INTEGER REFERENCES project (id),
    priority INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS api_token (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('ui', 'interactive', 'background')),
    hash TEXT NOT NULL,
    scopes TEXT,
    project_scope_json TEXT,
    expires_ts INTEGER,
    revoked INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1))
);

-- Ссылки на channel_id/outbox_id — без FOREIGN KEY: они живут в main ИЛИ work, audit только в main (см. спеку).
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    actor TEXT,
    api_token_id INTEGER REFERENCES api_token (id),
    client_name TEXT,
    request_id TEXT,
    action TEXT NOT NULL,
    result TEXT,
    channel_id INTEGER,
    outbox_id INTEGER,
    detail_json TEXT
);

-- append-only: правки и удаления запрещены на уровне БД, не только кодом.
CREATE TRIGGER IF NOT EXISTS audit_no_update
BEFORE UPDATE ON audit
BEGIN
    SELECT RAISE(ABORT, 'audit is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_no_delete
BEFORE DELETE ON audit
BEGIN
    SELECT RAISE(ABORT, 'audit is append-only');
END;

-- Один глобальный seq на весь процесс (main и work вместе); next_seq делает UPDATE ... RETURNING в потоке-писателе.
CREATE TABLE IF NOT EXISTS seq_counter (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    value INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO seq_counter (id, value) VALUES (1, 0);

-- Версия схемы здесь НЕ хранится: она в PRAGMA user_version каждого файла (см. db.py).
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
