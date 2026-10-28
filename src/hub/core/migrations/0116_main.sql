-- Ф94-1 (docs/architecture.md.1): признак «агент сообщает владельцу» у реплики агента в разговоре
-- (`POST /owner_channel/errands/{id}/tell`): бот владельца шлёт такую реплику в Telegram один раз, ответа не ждёт. Логика и
-- проверки — `core/owner_talk.py`. Только главный файл.
ALTER TABLE owner_talk ADD COLUMN tell INTEGER NOT NULL DEFAULT 0 CHECK (tell IN (0, 1));
CREATE INDEX IF NOT EXISTS idx_owner_talk_tell ON owner_talk (ts) WHERE tell = 1;
