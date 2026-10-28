-- Ф41-2б (docs/architecture.md). Лента, запросы и очередь реплик постоянной сессии
-- хаба. Таблицы задач по «Го» (`work_*`) не трогаются: у постоянной сессии свои (план 6а П12).
--
-- Общая схема (main и work): сессия проекта с беседой рабочего аккаунта пересказывает корпоративную переписку
-- и код — её строки живут в work. Файл — `main.hub_session.store`, фиксируется при создании сессии (П2); все
-- три таблицы сессии — только в нём. id в work — от 2^40 (таблицы входят в `db._SHARED_TABLES_WITH_OWN_ID`):
-- `db_of(id)` запроса или реплики обязан совпасть с `store` сессии. `session_id` — без FOREIGN KEY (запись
-- сессии лежит в main, ссылку через границу файлов проверяет код).
--
-- `held_event.detail_json` (до 64 КиБ) — задел под 41-3 (diff, расход хода); в 41-2б не заполняется.
-- `held_request` — как `work_request`: `seq` двигается каждой записью (`/sync`, SSE, ожидание хоста);
-- `delivered_ts` — решение доставлено процессу; решено, но не доставлено к рестарту хоста -> `expired`.
-- `held_say` — реплики владельца из интерфейса (источник один, столбца `origin` нет); `now` — «прервать и
-- написать» (прерывает сам хост, П3), `later` у постоянной сессии нет.
CREATE TABLE IF NOT EXISTS held_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('start', 'tool', 'tool_done', 'text', 'request', 'decision', 'say',
                                       'turn_end', 'status', 'error', 'warn', 'interrupt', 'compact')),
    tool TEXT,
    summary TEXT NOT NULL,
    is_error INTEGER NOT NULL DEFAULT 0 CHECK (is_error IN (0, 1)),
    ref TEXT,
    detail_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_held_event_session ON held_event (session_id, id);

CREATE TABLE IF NOT EXISTS held_request (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
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
CREATE INDEX IF NOT EXISTS idx_held_request_seq ON held_request (seq);
CREATE INDEX IF NOT EXISTS idx_held_request_session ON held_request (session_id, status);

CREATE TABLE IF NOT EXISTS held_say (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    priority TEXT NOT NULL CHECK (priority IN ('next', 'now')),
    status TEXT NOT NULL CHECK (status IN ('queued', 'delivered', 'dropped')),
    created_ts INTEGER NOT NULL,
    delivered_ts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_held_say_session ON held_say (session_id, status);
