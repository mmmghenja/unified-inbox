-- Ф48-2 (docs/architecture.md): порог сжатия у постоянной сессии хаба.
-- Только hub.db (таблица `hub_session` живёт в main). `compact_tokens` — свой порог авто-сжатия в токенах
-- (50 000…900 000; NULL — умолчание хоста `held_compact_tokens`), `compact_auto` — авто-сжатие включено (0 —
-- только по просьбе «Сжать»), `compact_at` — действующий порог, как его посчитал хост (с правилом «от базы
-- сессии» и потолком по окну модели; NULL — хост ещё не сообщал) — для строки «контекст N из M».
ALTER TABLE hub_session ADD COLUMN compact_tokens INTEGER;
ALTER TABLE hub_session ADD COLUMN compact_auto INTEGER NOT NULL DEFAULT 1 CHECK (compact_auto IN (0, 1));
ALTER TABLE hub_session ADD COLUMN compact_at INTEGER;
-- Раздел B: причина ожидания `compact` — срочная реплика ждёт конца идущего сжатия. У столбца ограничение
-- CHECK — он пересоздаётся (как в 0060): значение живёт минуты, хост поставит причину заново.
ALTER TABLE hub_session DROP COLUMN wait_reason;
ALTER TABLE hub_session ADD COLUMN wait_reason TEXT
    CHECK (wait_reason IN ('slot', 'elsewhere', 'paused', 'heavy', 'compact'));
