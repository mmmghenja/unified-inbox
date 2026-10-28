-- Ф41-5 (docs/architecture.md и П8 раздела 11).
--
-- `held_request.rule` — ключ выключателя автопилота (каталог `hub/autopilot.py`), под которым пришёл запрос
-- сессии хаба; NULL — неизвестно (запросы до 41-5).
-- `outbox.auto_session` — id сессии хаба, чьё сообщение ушло без черновика веткой выключателя (`hub_send`,
-- `hub_send_new`; код — шаг 5б); у ветки списка «без подтверждения» (Ф37-1) — NULL.
ALTER TABLE held_request ADD COLUMN rule TEXT;
ALTER TABLE outbox ADD COLUMN auto_session INTEGER;
