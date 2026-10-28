-- Правка и «удалить у всех» своих сообщений (Ф14-8, docs/architecture.md и поправки
-- П1–П7). Общая схема (main и work): строка живёт в схеме своего сообщения (`db_of(message_id)`), одна на
-- сообщение. Своего AUTOINCREMENT нет (ключ — id сообщения), поэтому в `db._SHARED_TABLES_WITH_OWN_ID` не
-- входит.
--
-- Целевое состояние: `op` — «текст = `text`» (`edit`) или «удалить у всех» (`delete`). Новая операция заменяет
-- прежнюю (`gen += 1`); `leased_gen` — `gen` на момент выдачи (отчёт при сменившемся `gen` снимает только
-- лиз). `state = 'failed'` — итог неудачи виден в хабе (`error_code`/`error_detail`), пока владелец не скроет или
-- не повторит. `pause_until_ms` — пауза после `retry` (FloodWait/бэкофф): новая операция её не обходит.
CREATE TABLE IF NOT EXISTS msg_op (
    message_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    op TEXT NOT NULL CHECK (op IN ('edit', 'delete')),
    text TEXT,
    state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'failed')),
    error_code TEXT,
    error_detail TEXT,
    gen INTEGER NOT NULL DEFAULT 1,
    not_before_ms INTEGER NOT NULL,
    pause_until_ms INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_until_ms INTEGER,
    lease_gen INTEGER NOT NULL DEFAULT 0,
    leased_gen INTEGER,
    created_ms INTEGER NOT NULL,
    updated_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS msg_op_account ON msg_op (account_id, state, not_before_ms);
