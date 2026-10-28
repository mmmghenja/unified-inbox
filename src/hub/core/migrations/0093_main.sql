-- Ф61-1 (docs/architecture.md и поправки П2, П3): вопрос агента владельцу и ответ из бота.
-- Только основная база. Логика и проверки — `core/owner_talk.py`.
-- `ask` — реплика агента — вопрос владельцу, ждёт ответа; `ask_until_ts` — до какого времени агент ждёт (после —
-- вопрос «просрочен»); `closed_ts`/`closed_outcome` — агент сам закрыл свой вопрос (`answered` — ответ
-- получен, `gave_up` — перестал ждать). `via` — откуда реплика владельца: NULL — интерфейс, `bot` — Telegram;
-- `tg_msg_id` — номер сообщения владельца в личке бота (повтор `bot-reply` с тем же номером второй реплики не даёт).
ALTER TABLE owner_talk ADD COLUMN ask INTEGER NOT NULL DEFAULT 0 CHECK (ask IN (0, 1));
ALTER TABLE owner_talk ADD COLUMN ask_until_ts INTEGER;
ALTER TABLE owner_talk ADD COLUMN closed_ts INTEGER;
ALTER TABLE owner_talk ADD COLUMN closed_outcome TEXT CHECK (closed_outcome IN ('answered', 'gave_up'));
ALTER TABLE owner_talk ADD COLUMN via TEXT CHECK (via IN ('bot'));
ALTER TABLE owner_talk ADD COLUMN tg_msg_id INTEGER;
-- Повтор — номер сообщения Telegram на тот же вопрос (номера начинаются заново в новом чате бота).
CREATE UNIQUE INDEX IF NOT EXISTS idx_owner_talk_bot_tg ON owner_talk (reply_to, tg_msg_id) WHERE via = 'bot';
CREATE INDEX IF NOT EXISTS idx_owner_talk_ask ON owner_talk (ts) WHERE ask = 1;
