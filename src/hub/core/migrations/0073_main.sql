-- Ф50-4 (docs/architecture.md): состояние роли для
-- оркестратора. Только основная база (таблица `hub_session` живёт в main).
-- `turn_ts` — начало идущего хода (мс часов ядра): ставится, когда сессия становится `running`, снимается
-- любым уходом из `running`. `turn_origin` — источник идущего хода: `say` (реплика владельца), `signal` (сигнал
-- хаба), `self` (движок продолжил сам), `compact` (сжатие); NULL — хода нет или хост источник не назвал.
-- `mid_queued_ts` — сигнал подан сессии посреди хода, но строка ещё стоит в очереди движка (ждёт конца
-- инструмента); ставит и снимает хост, сон сессии снимает сам.
ALTER TABLE hub_session ADD COLUMN turn_ts INTEGER;
ALTER TABLE hub_session ADD COLUMN turn_origin TEXT;
ALTER TABLE hub_session ADD COLUMN mid_queued_ts INTEGER;
