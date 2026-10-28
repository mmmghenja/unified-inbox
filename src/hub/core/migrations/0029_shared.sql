-- Реакции от сессий и реакции в письмах Gmail (Ф15-1, docs/architecture.md и
-- поправки П1, П3). Общая схема (main и work): строки живут в схеме своего сообщения/беседы. Своего
-- AUTOINCREMENT нет ни у одной таблицы, поэтому в `db._SHARED_TABLES_WITH_OWN_ID` не входят.
--
-- `reaction_ack.origin` (П1) — чья строка очереди: клики владельца (`ui`) и операции сессии (`session`) в одной
-- строке не смешиваются. Строки на момент деплоя — `ui`. `token_id` — токен сессии строки `session` (пометки
-- `reaction_by_session` ставятся по `done` с ним).
ALTER TABLE reaction_ack ADD COLUMN origin TEXT NOT NULL DEFAULT 'ui' CHECK (origin IN ('ui', 'session'));
ALTER TABLE reaction_ack ADD COLUMN token_id INTEGER;

-- Реакции, поставленные сессией (решение 5, П3): сессия снимает только их; Telegram — «моя реакция без
-- пометки» считается реакцией владельца. `emoji_key` — без U+FE0F (`reactions._key`). Строка — только по `done`
-- строки сессии с неизменным `gen`; клик владельца по эмодзи пометку снимает. ПДн нет, но строка уходит вместе с
-- сообщением (скрытие у себя, очистка).
CREATE TABLE IF NOT EXISTS reaction_by_session (
    message_id INTEGER NOT NULL,
    emoji_key TEXT NOT NULL,
    token_id INTEGER,
    ts INTEGER NOT NULL,
    PRIMARY KEY (message_id, emoji_key)
);

-- Входящие реакции писем (решение 7, A4): письмо-реакция Gmail / RFC 9078 — не строка ленты, а значок на
-- письме-цели. Ключ — беседа и Message-ID цели (реакция, пришедшая раньше письма-цели, применяется, когда цель
-- придёт); `src_ext_id` — Message-ID самого письма-реакции (повтор -> no-op). `sender_key` — адрес отправителя
-- (ПДн): строки удаляются там же, где скрывается сообщение/беседа.
CREATE TABLE IF NOT EXISTS mail_reaction (
    channel_id INTEGER NOT NULL,
    target_ext_id TEXT NOT NULL,
    src_ext_id TEXT NOT NULL,
    emoji TEXT NOT NULL,
    sender_key TEXT NOT NULL,
    is_me INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    PRIMARY KEY (channel_id, src_ext_id)
);
CREATE INDEX IF NOT EXISTS mail_reaction_target ON mail_reaction (channel_id, target_ext_id);
