-- Ничего не удалять: удаление у себя в хабе и история правок (Ф8-4, docs/architecture.md,
-- разделы B1, C1 и поправки П2, П12). Общая схема (main и work) — как остальные *_shared.sql.

-- B1: сообщение удалено владельцем в хабе («у себя», не у источника). Строка остаётся (порядок, треды, сверка),
-- содержимое очищено той же функцией, что у ретенции (`purged = 1`); приём из источника флаг не снимает никогда.
ALTER TABLE message ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0 CHECK (hidden IN (0, 1));

-- B1 + П2: беседа удалена владельцем в хабе. Граница — ПОЗИЦИЯ `(ts, ext_sort)` последнего сообщения на момент
-- удаления: входящее с позицией не новее границы ложится сразу скрытым и очищенным. Беседа видна снова, как
-- только в ней есть хоть одно не скрытое сообщение (один построитель — `threads.channel_visible_sql`).
ALTER TABLE channel ADD COLUMN hidden_before_ts INTEGER;
ALTER TABLE channel ADD COLUMN hidden_before_sort INTEGER;

-- C1 + П12: прежние версии правленого сообщения, от старой к новой (порядок — `id`). `id` — обычный
-- `INTEGER PRIMARY KEY` без AUTOINCREMENT и не в `db._SHARED_TABLES_WITH_OWN_ID`: наружу не отдаётся, ищется
-- только по `message_id` в файле сообщения.
CREATE TABLE IF NOT EXISTS message_edit (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES message (id),
    replaced_ts INTEGER NOT NULL,
    text TEXT,
    entities_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_message_edit_message ON message_edit (message_id, id);

-- Ревью Ф8-4: хеш текста исходящего. Скрытая заготовка теряет текст (и `message.text`, и `outbox.text` в
-- конечном статусе), а эхо источника без `send_token` склеивается с заготовкой по тексту
-- (`outbox.match_echo`) — без хеша оно легло бы новой видимой строкой с полным текстом. Считается при
-- `enqueue`/`approve(text)`, для старых строк — при скрытии, до стирания текста.
ALTER TABLE outbox ADD COLUMN text_sha256 TEXT;
