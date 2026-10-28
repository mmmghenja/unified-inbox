-- Заявки сессий на правило раскладки (Ф38-2, docs/architecture.md).
-- Только главный файл: work остаётся на прежней версии (правила раскладки тоже живут в main).
--
-- Сессия предлагает правило (`kind` + `value_json`), владелец заводит его кнопкой «Завести» или отклоняет:
-- `pending` -> `approved` | `declined`. Само условие при одобрении строится заново из `kind`/`value_json`;
-- `match_key` (П2) — ключ условия БЕЗ `label` (как сравнивает правила `mapping.find_duplicate`): по нему ищутся
-- повторы заявок, переименование папки Telegram ключ не меняет. `who` — линия подписки сессии-просителя,
-- `reason` — её причина (данные). `rule_id` и `moved` — итог одобрения. `seq` — глобальный: `/sync` и кадр SSE
-- `rule_request`.
CREATE TABLE IF NOT EXISTS rule_request (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seq INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    project_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('domain', 'label', 'folder', 'all')),
    value_json TEXT,
    match_key TEXT NOT NULL,
    reason TEXT,
    who TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'declined')),
    token_id INTEGER,
    client_name TEXT,
    created_ts INTEGER NOT NULL,
    decided_ts INTEGER,
    rule_id INTEGER,
    moved INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS rule_request_open ON rule_request (account_id, project_id, match_key)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS rule_request_seq ON rule_request (seq);
CREATE INDEX IF NOT EXISTS rule_request_project ON rule_request (project_id, status);
