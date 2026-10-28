-- Карточки дежурного агента (ярус 1, разборщик). Спека docs/architecture.md и поправки П2–П5, К3.
-- Общая схема (main и work) — как остальные *_shared.sql: краткое содержание — производное от текстов, живёт
-- рядом с сообщениями своего канала (карточка Globex — в файле work) и подчиняется ретенции work (раздел 7).
-- id в work — от 2^40: `agent_item` входит в `db._SHARED_TABLES_WITH_OWN_ID` (засев идёт по списку, П3), иначе
-- `db_of(id)` увёл бы запрос по карточке work в main. `api_token_id` — без FOREIGN KEY: токены живут в main.

CREATE TABLE IF NOT EXISTS agent_item (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL REFERENCES channel (id),
    label TEXT NOT NULL CHECK (label IN ('urgent', 'reply', 'fyi', 'noise')),
    -- NULL после ретенции (purged = 1)
    summary TEXT,
    action TEXT,
    -- id сообщений хаба, которые покрывает карточка (последние 200 по позиции)
    message_ids_json TEXT NOT NULL,
    -- самое новое по ПОЗИЦИИ (ts, ext_sort, id) среди покрытых (П5)
    last_message_id INTEGER NOT NULL,
    -- максимум `first_seq` среди покрытых сообщений (при слиянии — максимум старого и нового): покрытие по
    -- ПОРЯДКУ ПОСТУПЛЕНИЯ в хаб, а не по времени источника — ночной догон, пришедший после живого сообщения,
    -- не считается покрытым (приёмка 7b, F1)
    covered_seq INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'seen', 'done', 'dismissed')),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    api_token_id INTEGER,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    purged INTEGER NOT NULL DEFAULT 0 CHECK (purged IN (0, 1))
);
-- Одна открытая карточка на беседу (A1).
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_item_open ON agent_item (channel_id) WHERE status = 'new';
CREATE INDEX IF NOT EXISTS idx_agent_item_status ON agent_item (status, updated_ts);
-- Идемпотентность по `covered_seq` (К3 после приёмки, F1) и выдача `?channel=` — карточки одной беседы.
CREATE INDEX IF NOT EXISTS idx_agent_item_channel ON agent_item (channel_id, covered_seq);
