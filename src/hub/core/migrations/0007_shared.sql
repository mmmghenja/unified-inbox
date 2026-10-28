-- Треды Slack: сводка, физическая ретенция. Раздел C docs/architecture.md.
-- Общая схема (main и work) — как остальные *_shared.sql (комментарий в 0001_shared.sql).

ALTER TABLE message ADD COLUMN thread_broadcast INTEGER NOT NULL DEFAULT 0 CHECK (thread_broadcast IN (0, 1));
-- Ретенция (план 7, 11.7, 11.8): строка, чьё содержимое уже прошло функцию очистки — text/entities/forward
-- обнулены, attachments/refs урезаны. `purged=1` не снимается никогда (раздел F).
ALTER TABLE message ADD COLUMN purged INTEGER NOT NULL DEFAULT 0 CHECK (purged IN (0, 1));

-- Частичный индекс: лента треда (`channel_id, thread_ext_id` -> порядок показа) — тот же курсор, что у ленты
-- канала, но по ответам конкретного треда.
CREATE INDEX IF NOT EXISTS idx_message_thread
    ON message (channel_id, thread_ext_id, ts, ext_sort, ext_id)
    WHERE thread_ext_id IS NOT NULL;

-- Сводка треда со стороны источника (Slack ThreadInfo) + собственный маркер прочтения треда (позиция, как у
-- канала — раздел 11.5/этот файл). `last_reply_ts`/`subscribed` уже были (0001_shared.sql).
ALTER TABLE thread ADD COLUMN source_reply_count INTEGER;
ALTER TABLE thread ADD COLUMN source_latest_reply_ext TEXT;
ALTER TABLE thread ADD COLUMN source_subscribed INTEGER;
ALTER TABLE thread ADD COLUMN last_read_ts INTEGER;
ALTER TABLE thread ADD COLUMN last_read_sort INTEGER;
ALTER TABLE thread ADD COLUMN last_read_ext TEXT;
ALTER TABLE thread ADD COLUMN first_seen_ts INTEGER;
