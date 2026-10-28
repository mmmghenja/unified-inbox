-- Признак бота у сообщения (Ф20-1, docs/architecture.md и поправка П8). Общая
-- схема (main и work). Пишется только при вставке сообщения (`ingest._insert_message`); правка и сверка его не
-- меняют. Старые сообщения — `0`, без пересчёта (они прочитаны; П12).
ALTER TABLE message ADD COLUMN is_bot INTEGER NOT NULL DEFAULT 0;
