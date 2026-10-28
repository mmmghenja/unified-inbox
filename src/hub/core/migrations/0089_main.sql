-- Ф58-1 (docs/architecture.md.1): свободный разговор владельца со своим агентом вне карточек.
-- Только hub.db (рядом с `owner_item` очереди), без чистки — объём мал. Логика и проверки — `core/owner_talk.py`:
-- `text` до 8000 символов, `ref` — карточка очереди (как в `/owner_channel/*`) или NULL, `reply_to` — реплика ленты.
-- `handled_ts` — реплику владельца агент обработал (ответил или отказался), у агентских — NULL; `read_ts` — реплику
-- агента владелец прочёл, у реплик владельца — NULL.
CREATE TABLE IF NOT EXISTS owner_talk (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    author TEXT NOT NULL CHECK (author IN ('owner_channel', 'agent')),
    text TEXT NOT NULL,
    ref TEXT,
    reply_to INTEGER REFERENCES owner_talk (id),
    handled_ts INTEGER,
    read_ts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_owner_talk_open ON owner_talk (author, handled_ts);
