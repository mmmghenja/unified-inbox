-- Упоминание группы Slack (Ф28-2, docs/architecture.md). Общая схема (main и work).
--
-- `mentions_group = 1` — меня упомянули ТОЛЬКО через мою группу пользователей Slack (`<!subteam^…>`, rich text
-- `usergroup`), без личного тега. `mentions_me` при этом 1: сам тег — «упоминание для меня» (категория
-- `mention`, непрочитано, сигнал «лично мне»), но тред он НЕ подписывает (`threads.thread_sub_mention_sql`).
-- Старые строки группы не различают (0): треды, уже подписанные тегом группы, остаются подписанными.
ALTER TABLE message ADD COLUMN mentions_group INTEGER NOT NULL DEFAULT 0 CHECK (mentions_group IN (0, 1));
