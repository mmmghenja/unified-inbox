-- Ф41-5б (docs/architecture.md + П6, П7): прежние заявки сессий под выключателями автопилота.
-- `join_request.autopilot_session` — сессия хаба, которой при создании заявки вступление было разрешено
-- выключателем `tg_join` (после предпросмотра заявка принимается сама, если выключатель всё ещё снят);
-- `leave_request.auto`, `rule_request.auto` — заявка принята автопилотом, без владельца (у вступления такой
-- столбец есть с Ф17-1).
ALTER TABLE join_request ADD COLUMN autopilot_session INTEGER;
ALTER TABLE leave_request ADD COLUMN auto INTEGER NOT NULL DEFAULT 0 CHECK (auto IN (0, 1));
ALTER TABLE rule_request ADD COLUMN auto INTEGER NOT NULL DEFAULT 0 CHECK (auto IN (0, 1));
