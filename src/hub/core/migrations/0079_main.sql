-- Ф56-1а (docs/architecture.md; план фазы — П3): переключатель «канал владельца» у сессии
-- хаба. Ставит только владелец (`PATCH /sessions/{id}`); действует, пока у проекта снят выключатель `human_agent`.
ALTER TABLE hub_session ADD COLUMN via_agent INTEGER NOT NULL DEFAULT 0 CHECK (via_agent IN (0, 1));
