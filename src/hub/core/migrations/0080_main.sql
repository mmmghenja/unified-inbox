-- Ф56-1а (docs/architecture.md; план фазы — П2, А9): сессия хаба, подавшая заявку на
-- вступление, выход или правило раскладки (по условной тройке запроса; NULL — внешняя сессия или заявка
-- старше этой версии). Из неё строится признак «в канале владельца».
ALTER TABLE join_request ADD COLUMN hub_session_id INTEGER;
ALTER TABLE leave_request ADD COLUMN hub_session_id INTEGER;
ALTER TABLE rule_request ADD COLUMN hub_session_id INTEGER;
