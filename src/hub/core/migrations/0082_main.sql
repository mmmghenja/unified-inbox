-- Ф56-1б (docs/architecture.md; план фазы — А4, А6).
-- `peer_msg.ask` — вопрос владельцу (`hub_ask_owner`): сессия с ролью пишет ему первой, мимо правила суток;
-- `ask_about` — о какой роли вопрос (как её назвала сессия, до 128 символов; `NULL` — о себе).
ALTER TABLE peer_msg ADD COLUMN ask INTEGER NOT NULL DEFAULT 0 CHECK (ask IN (0, 1));
ALTER TABLE peer_msg ADD COLUMN ask_about TEXT;
-- `hub_session.turns` — счётчик законченных ходов сессии хаба (ключ карточки `turn:<сессия>:<turns>`). Ведёт
-- ядро, а не хост: счётчик хоста после его рестарта начался бы заново и повторил бы ключ.
ALTER TABLE hub_session ADD COLUMN turns INTEGER NOT NULL DEFAULT 0;
