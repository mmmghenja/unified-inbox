-- Очередь канала владельца (Ф56-1а, docs/architecture.md; план фазы — П1, А5, П8, П17).
-- Только hub.db: строки — про сессии хаба и проекты, мимо ретенции work.
--
-- Заявочные карточки (разрешения, «нужен человек», черновики, нажатия, вступления, правила, заявки) в очереди
-- считаются при чтении из своих таблиц; здесь — только то, чего в них нет:
-- - ответ ИЗ КАНАЛА на заявочную карточку (источник ответа, П8): `ref` и `kind` карточки, `closed_ts`,
--   `closed_by = 'agent'`, `closed_token_id` — номер записи агента владельца в `api_token`, `payload_json` —
--   `{"action": …}`. Ответ владельца из интерфейса строки не пишет (источник `ui` — умолчание);
-- - поручение агенту (П17): `kind = 'errand'`, `ref = 'errand:<id>'`, `payload_json` — `{"target": <ref
--   карточки>, "action", "label", "text", "progress", "returned"}`; `closed_*` — исполнено, отозвано владельцем
--   (`ui`), возвращено агентом (`agent`) или закрыта сама карточка (`system`);
-- - запас под карточки следующих кусков (`turn`, `stall`, …): строки с `closed_ts IS NULL`, `hold_ts` —
--   «пусть ждёт».
-- `project_id`, `session_id` (`main.hub_session`) — карточки, если известны. `seq` — глобальный счётчик:
-- длинный опрос очереди просыпается по нему.
CREATE TABLE IF NOT EXISTS owner_item (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref TEXT NOT NULL,
    kind TEXT NOT NULL,
    project_id INTEGER,
    session_id INTEGER,
    created_ts INTEGER NOT NULL,
    closed_ts INTEGER,
    closed_by TEXT CHECK (closed_by IN ('ui', 'agent', 'system')),
    closed_token_id INTEGER,
    hold_ts INTEGER,
    payload_json TEXT,
    seq INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS owner_item_ref ON owner_item (ref);
CREATE INDEX IF NOT EXISTS owner_item_closed ON owner_item (closed_ts, seq);
