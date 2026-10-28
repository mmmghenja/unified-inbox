-- Стадии бесед (Ф34-1, docs/architecture.md, поправка П3). Обе схемы.
--
-- Стадия — свободная короткая метка беседы, одна на беседу; ставит владелец в UI или сессия (скоуп `organize`).
-- Словаря стадий нет: список стадий проекта — разные `stage_key` его живых бесед. `stage` — как записано,
-- `stage_key` — ключ сравнения (`casefold` обрезанной строки, считает Python при записи: `lower()` SQLite
-- кириллицу не трогает), `stage_note` — заметка к стадии, `stage_ts` — когда стадия поставлена или сменена.
-- Все четыре NULL — стадии нет (все старые беседы).
ALTER TABLE channel ADD COLUMN stage TEXT;
ALTER TABLE channel ADD COLUMN stage_key TEXT;
ALTER TABLE channel ADD COLUMN stage_note TEXT;
ALTER TABLE channel ADD COLUMN stage_ts INTEGER;
CREATE INDEX IF NOT EXISTS channel_stage ON channel (project_id, stage_key) WHERE stage_key IS NOT NULL;
