-- Ф41-2а (docs/architecture.md). Сессия хаба — запись ядра, которой принадлежат роль,
-- подписка и ждущее; её процесс может не существовать (сессия спит). Личность такой сессии в подписках —
-- постоянная условная тройка `(-id, 0, "hub:<id>")` (П1): id разговора движка (`engine_session_id`) в неё не
-- входит. `AUTOINCREMENT` обязателен: id закрытой сессии не переиспользуется (иначе новая сессия унаследовала
-- бы тройку чужой завершённой подписки). Таблица и индексы — `IF NOT EXISTS`: имитация «базы версии 4» в
-- тестах их не удаляет.
CREATE TABLE IF NOT EXISTS hub_session (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    role TEXT,
    role_key TEXT,
    role_no INTEGER,
    engine TEXT NOT NULL CHECK (engine IN ('claude', 'opencode')),
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
    created_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS hub_session_open ON hub_session (project_id) WHERE closed_ts IS NULL;
-- Линия роли среди сессий хаба: одна незакрытая на `(проект, роль, номер)`. С живыми подписками внешних
-- сессий линию сводит ядро (`subs._subscribe`, П3) и индекс `session_sub_live_role`.
CREATE UNIQUE INDEX IF NOT EXISTS hub_session_open_role ON hub_session (project_id, role_key, role_no)
    WHERE closed_ts IS NULL AND role_key IS NOT NULL;
-- Разговор движка — у одной незакрытой сессии хаба (`bind`: занятый — 409).
CREATE UNIQUE INDEX IF NOT EXISTS hub_session_open_engine_sid ON hub_session (engine_session_id)
    WHERE closed_ts IS NULL AND engine_session_id IS NOT NULL;
