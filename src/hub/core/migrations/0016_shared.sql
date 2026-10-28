-- Прочитанность из хаба в источник (Ф8-7, docs/architecture.md и поправки П1, П4). Общая схема
-- (main и work): строка живёт в схеме своего канала (`db_of(channel_id)`), одна на канал — новая отметка UI
-- перезаписывает цель. Своего AUTOINCREMENT нет (ключ — id канала), поэтому в `db._SHARED_TABLES_WITH_OWN_ID` не входит.
-- Строки `channel` физически не удаляются, `ON DELETE CASCADE` не нужен: скрытое/пропавшее отсеивает выдача.
--
-- `message_id` — цель UI (может быть своей заготовкой или черновиком: действующую цель подбирает выдача, П1).
-- `gen` растёт при каждой новой цели; `leased_gen` — `gen` на момент выдачи (отчёт `done` при сменившемся `gen`
-- снимает только лиз). `first_ms` — первая постановка после последней выдачи (при выдаче сбрасывается в NULL, П4).
-- `pause_until_ms` — пауза после `retry` (FloodWait/бэкофф): новая отметка UI её не обходит (П4).
CREATE TABLE IF NOT EXISTS read_ack (
    channel_id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    gen INTEGER NOT NULL,
    first_ms INTEGER,
    not_before_ms INTEGER NOT NULL,
    pause_until_ms INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_until_ms INTEGER,
    lease_gen INTEGER NOT NULL DEFAULT 0,
    leased_gen INTEGER
);
CREATE INDEX IF NOT EXISTS read_ack_account ON read_ack (account_id, not_before_ms);
