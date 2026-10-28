-- Ф41-2б (docs/architecture.md 4а: П1, П2, П13). Дополнение записи сессии хаба
-- под хост постоянных сессий и окно в интерфейсе.
--
-- `seq` — общий счётчик изменений (`next_seq`): любое изменение записи, которое должен увидеть интерфейс или
-- хост, двигает его (`/sync`, SSE, длинное ожидание хоста). Подтверждение состояния без изменений
-- (`host_seen_ts`) `seq` не двигает. `last_event_id` — последнее событие ленты сессии (`held_event`).
-- `interrupt_requested_ts` / `sleep_requested_ts` — просьбы владельца «прервать ход» и «усыпить»; переход в `asleep`
-- обе снимает, уход из `running` снимает первую. `last_turn_ts` — конец последнего хода. `launch_fails` и
-- `failure` — неудачные запуски подряд и причина последней (показывается в окне).
-- `store` (П2) — файл базы событий, запросов и реплик сессии (`main` | `work`); фиксируется при создании и не
-- меняется. `NULL` — только у записей, созданных до этой миграции (41-2а): значение ставится при первой записи.
-- `wait_reason` (П13) — почему реплика ждёт: `slot` — нет места, `elsewhere` — сессия открыта в другом окне,
-- `paused` — пауза лимита; пишет хост.
ALTER TABLE hub_session ADD COLUMN seq INTEGER NOT NULL DEFAULT 0;
ALTER TABLE hub_session ADD COLUMN last_event_id INTEGER;
ALTER TABLE hub_session ADD COLUMN interrupt_requested_ts INTEGER;
ALTER TABLE hub_session ADD COLUMN sleep_requested_ts INTEGER;
ALTER TABLE hub_session ADD COLUMN last_turn_ts INTEGER;
ALTER TABLE hub_session ADD COLUMN launch_fails INTEGER NOT NULL DEFAULT 0;
ALTER TABLE hub_session ADD COLUMN failure TEXT;
ALTER TABLE hub_session ADD COLUMN store TEXT CHECK (store IN ('main', 'work'));
ALTER TABLE hub_session ADD COLUMN wait_reason TEXT CHECK (wait_reason IN ('slot', 'elsewhere', 'paused'));
CREATE INDEX IF NOT EXISTS hub_session_seq ON hub_session (seq);
