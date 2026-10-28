-- Ф33-1 (docs/architecture.md и 12а К2, К9): живость сессии на другой машине. Только
-- основная база (подписки живут в main). `session_host` — машина сессии из тела `session` (первые 16 hex
-- sha256 `/etc/machine-id`, `hub.hostid`); NULL — клиент поля не прислал (старый клиент) — сессия своя, живость
-- по `/proc` ядра, как раньше. По этому столбцу ядро после рестарта засевает реестр удалённых сессий в памяти
-- (`core/session_host.install`). Номер 0099 (`0099_shared.sql`) — за параллельной работой, уже в master: на прод
-- она должна попасть РАНЬШЕ этой или вместе (версия базы — одно число, файл с меньшим номером после 0100 не
-- применится).
-- `session_boot` — загрузка той машины (`/proc/sys/kernel/random/boot_id`): первый контакт машины с новой
-- загрузкой снимает подписки прежних загрузок (`session_host.Registry.touch`); NULL — клиент не прислал, такие
-- строки по загрузке не снимаются.
ALTER TABLE session_sub ADD COLUMN session_host TEXT;
ALTER TABLE session_chan_sub ADD COLUMN session_host TEXT;
ALTER TABLE session_sub ADD COLUMN session_boot TEXT;
ALTER TABLE session_chan_sub ADD COLUMN session_boot TEXT;
-- `session_seen_ts` — последний контакт сессии другой машины (пишется не чаще раза в 10 мин): после рестарта
-- ядра окно 12 ч без связи считается от него (нет — от `created_ts`), рестарты мёртвую подписку не продлевают.
ALTER TABLE session_sub ADD COLUMN session_seen_ts INTEGER;
ALTER TABLE session_chan_sub ADD COLUMN session_seen_ts INTEGER;
