-- Ф89-1 (docs/architecture.md.1 и поправки П2, П5): повторяемые напоминания и напоминания
-- без беседы. Только главный файл.
--
-- `every` — период (`day`, `weekdays`, `week`, `<N>h`; NULL — разовое). `at_hm` — 'ЧЧ:ММ' сетки в поясе `tz`
-- (у `day`/`weekdays`/`week`; день недели `week` — `at_wd`). У повторяемого
-- `due_ts` — ВСЕГДА ближайший будущий срок, `fired_ts` — когда сработал текущий. `missed` — сроков подряд без
-- «сделано»; `done_count`, `last_done_ts`, `last_done_role` (роль сессии; NULL — владелец) — учёт «сделано».
-- Беседа «нет» — `channel_id = 0` (столбец NOT NULL, перестройка не нужна; id бесед начинаются с 1).
ALTER TABLE reminder ADD COLUMN every TEXT;
ALTER TABLE reminder ADD COLUMN at_hm TEXT;
ALTER TABLE reminder ADD COLUMN tz TEXT;
ALTER TABLE reminder ADD COLUMN missed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE reminder ADD COLUMN done_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE reminder ADD COLUMN last_done_ts INTEGER;
ALTER TABLE reminder ADD COLUMN last_done_role TEXT;
-- День недели сетки `week` (0 — пн): «Отложить» сдвигает `due_ts`, день регламента остаётся (ревью Ф89-1).
ALTER TABLE reminder ADD COLUMN at_wd INTEGER;
CREATE INDEX IF NOT EXISTS reminder_due_fired ON reminder (due_ts) WHERE status = 'fired' AND every IS NOT NULL;
