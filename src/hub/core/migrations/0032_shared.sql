-- Правки черновиков владельца (Ф19-1, docs/architecture.md и поправка П7). Общая схема (main и
-- work): колонки строки `outbox` в файле своей беседы. Только новые колонки и индекс, без пересчётов.
--
-- `draft_text` — текст черновика ДО правки владельца при подтверждении (`outbox.approve(text=…)`); `NULL` — не
-- правил (или не черновик). Строки до миграции — `NULL`: прошлых правок не восстановить. ПДн того же класса,
-- что `outbox.text`: стирается там же («удалить у себя», эхо после скрытия, ретенция). `draft_ts` — когда
-- правил (время подтверждения); ПДн нет, при стирании остаётся.
ALTER TABLE outbox ADD COLUMN draft_text TEXT;
ALTER TABLE outbox ADD COLUMN draft_ts INTEGER;
-- `GET /outbox/feedback`: правленые — по времени правки, новые сверху.
CREATE INDEX IF NOT EXISTS outbox_draft_edited ON outbox (draft_ts) WHERE draft_text IS NOT NULL;
