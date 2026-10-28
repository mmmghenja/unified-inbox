-- Ф56-2 (docs/architecture.md; план фазы — П13): делегат передал карточку владельцу.
-- Оба файла (таблица `held_request` живёт в файле строк сессии). Все пять — NULL, пока карточку не передали;
-- передача снимает признак делегата (`delegate_*` -> NULL) и пишет сюда, кто передал и что написал.
-- `passed_sub_id` — подписка делегата; `passed_role` / `passed_role_no` — его линия на момент передачи;
-- `passed_ts` — когда; `passed_text` — текст делегата владельцу как есть (показ — через маскировку).
ALTER TABLE held_request ADD COLUMN passed_sub_id INTEGER;
ALTER TABLE held_request ADD COLUMN passed_role TEXT;
ALTER TABLE held_request ADD COLUMN passed_role_no INTEGER;
ALTER TABLE held_request ADD COLUMN passed_ts INTEGER;
ALTER TABLE held_request ADD COLUMN passed_text TEXT;
