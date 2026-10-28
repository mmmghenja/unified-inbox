-- «Разобрано сессией» (Ф37-3, docs/architecture.md.3 и B4). Только главный файл.
--
-- Подписанная сессия проекта с ролью отметила беседу разобранной до сообщения `message_id`: беседа у владельца
-- прочитана до него, а здесь — строка для свёрнутого блока «Разобрано сессией» на главной. `channel_id` и
-- `message_id` — id любого файла (main или work); `project_id` — проект беседы на момент отметки; `role` — роль
-- подписки как записана, `role_key` — её ключ сравнения; `sub_id` — подписка сессии; `note` — пометка сессии
-- (3–200 символов). `dismissed_ts` — когда владелец убрал строку (NULL — показывается). Пока строка не убрана,
-- повторная отметка той же беседы той же ролью обновляет её, а не заводит вторую (частичный уникальный индекс).
CREATE TABLE IF NOT EXISTS channel_handled (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    project_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    role_key TEXT NOT NULL,
    sub_id INTEGER NOT NULL,
    note TEXT NOT NULL,
    created_ts INTEGER NOT NULL,
    dismissed_ts INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS channel_handled_open ON channel_handled (channel_id, role_key)
    WHERE dismissed_ts IS NULL;
CREATE INDEX IF NOT EXISTS channel_handled_shown ON channel_handled (created_ts) WHERE dismissed_ts IS NULL;
