-- Ф66-2 (docs/architecture.md.1): разговор владельца с агентом — по веткам сессий хаба.
-- Только основная база. `owner_talk.session_id` — ветка реплики: id сессии хаба (`hub_session.id`), NULL —
-- «Общее». Пересказ ветки для модели агента — `owner_talk_branch` («Общее» — `session_id = 0`). Логика и
-- проверки — `core/owner_talk.py`. Старые строки получают ветку по `ref` разовым шагом после миграций
-- (`Db._f66_talk_branches`: карточка ещё известна — её сессия хаба, иначе NULL); ключ `meta` ставит этот файл.
ALTER TABLE owner_talk ADD COLUMN session_id INTEGER;
CREATE INDEX IF NOT EXISTS idx_owner_talk_branch ON owner_talk (session_id, id);
CREATE TABLE IF NOT EXISTS owner_talk_branch (
    session_id INTEGER PRIMARY KEY,
    summary TEXT NOT NULL DEFAULT '',
    summary_upto_id INTEGER NOT NULL DEFAULT 0,
    updated_ts INTEGER NOT NULL
);
INSERT OR REPLACE INTO meta (key, value) SELECT 'f66_talk_branches', '1' WHERE EXISTS (SELECT 1 FROM owner_talk);
