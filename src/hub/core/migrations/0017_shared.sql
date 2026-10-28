-- Реакции из хаба в источник (Ф10-3, docs/architecture.md и поправка П1). Общая схема
-- (main и work): строка живёт в схеме своего сообщения (`db_of(message_id)`), одна на сообщение. Своего
-- AUTOINCREMENT нет (ключ — id сообщения), поэтому в `db._SHARED_TABLES_WITH_OWN_ID` не входит; скрытое и
-- пропавшее отсеивает выдача.
--
-- `ops_json` — упорядоченный список операций `[[emoji, on], …]`: новая операция по тому же эмодзи заменяет
-- прежнюю и встаёт в конец (П1). `gen` растёт при каждой операции; `leased_gen` — `gen` на момент выдачи
-- (отчёт `done` при сменившемся `gen` снимает только лиз). `pause_until_ms` — пауза после `retry`
-- (FloodWait/бэкофф): новая операция её не обходит.
CREATE TABLE IF NOT EXISTS reaction_ack (
    message_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    ops_json TEXT NOT NULL,
    gen INTEGER NOT NULL,
    not_before_ms INTEGER NOT NULL,
    pause_until_ms INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_until_ms INTEGER,
    lease_gen INTEGER NOT NULL DEFAULT 0,
    leased_gen INTEGER
);
CREATE INDEX IF NOT EXISTS reaction_ack_account ON reaction_ack (account_id, not_before_ms);
