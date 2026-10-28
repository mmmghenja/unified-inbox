-- Напоминание по беседе в срок (Ф34-2, docs/architecture.md и поправки П1, П2, П9).
-- Только главный файл: work остаётся на прежней версии. Беседа может лежать в work — ссылка по `channel_id`
-- без FK (как у заявок 0030/0031), беседу ищут в схеме по `db_of`.
--
-- Статусы: `pending` -> `fired` -> `done`; из `pending`: `cancelled` (сняли, беседа в архиве), `answered`
-- (собеседник ответил); `fired` -> `answered` — ответ принят с опозданием (П6); перенос возвращает `fired` в
-- `pending`. `project_id` и роль (`role` — как задала сессия, `role_key` — ключ сравнения) — из подписки
-- поставившей сессии (П2); у напоминаний владельца из UI `api_token_id IS NULL`, `project_id` — проект беседы
-- (`NULL` — «Неразобранное»). `base_message_ts` — последнее сообщение беседы на момент постановки (ответ —
-- чужое сообщение позже него). Сигнал сессии — сама строка (П1): `signal_sub_id`/`signaled_ts` — какой
-- подписке и когда отдан `hub sub wait`; `delivered_ts` — заметку прочли (`hub_reminders`); `notified_ts` —
-- уведомление владельцу на экран (hub-agent). `seq` — глобальный (П9): `/sync` и SSE-кадр `reminder`.
CREATE TABLE IF NOT EXISTS reminder (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    project_id INTEGER,
    due_ts INTEGER NOT NULL,
    note TEXT,
    cancel_if_reply INTEGER NOT NULL DEFAULT 1 CHECK (cancel_if_reply IN (0, 1)),
    base_message_ts INTEGER NOT NULL,
    role TEXT,
    role_key TEXT,
    api_token_id INTEGER,
    status TEXT NOT NULL CHECK (status IN ('pending', 'fired', 'done', 'cancelled', 'answered')),
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    fired_ts INTEGER,
    signal_sub_id INTEGER,
    signaled_ts INTEGER,
    delivered_ts INTEGER,
    notified_ts INTEGER,
    closed_ts INTEGER,
    end_reason TEXT,
    seq INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS reminder_due ON reminder (due_ts) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS reminder_fired ON reminder (project_id) WHERE status = 'fired';
CREATE INDEX IF NOT EXISTS reminder_channel ON reminder (channel_id);
CREATE INDEX IF NOT EXISTS reminder_seq ON reminder (seq);
