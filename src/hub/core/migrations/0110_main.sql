-- Ф86-1 (docs/architecture.md, П1): курсор «слов владельца из соседних сессий» сессии хаба —
-- до какого `held_say.created_ts` соседние реплики уже поданы этой сессии (мс; NULL — ещё ни разу). Двигает
-- только ядро по подтверждению хоста (`POST /work/host/sessions/{id}/feed-extras/ack`), только вперёд.
ALTER TABLE hub_session ADD COLUMN siblings_ts INTEGER;
