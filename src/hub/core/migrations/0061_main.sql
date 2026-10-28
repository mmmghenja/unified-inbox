-- Ф41-5 (docs/architecture.md и поправки раздела 11: П1, П13). Авто-режим сессий
-- хаба и заявки агентов.
--
-- `hub_session.autosend_ok` — сессия может отвечать в беседы «без подтверждения» своей роли (список Ф37-1);
-- у сессии, заведённой по заявке агента, — 0, пока владелец не отметил иное.
-- `hub_session.launch_key_hash` (П1) — SHA-256 ключа запуска идущего процесса сессии (сам ключ знает только
-- хост и процесс: `HUB_SESSION_KEY`); пишется привязкой процесса, стирается при сне и закрытии.
-- `agent_request` — заявки агентов владельцу: перенос беседы, расширяющий видимость, и «заведи сессию» (код заявок
-- — шаг 5б; таблица заводится сразу, чтобы не плодить номера миграций).
ALTER TABLE hub_session ADD COLUMN autosend_ok INTEGER NOT NULL DEFAULT 1 CHECK (autosend_ok IN (0, 1));
ALTER TABLE hub_session ADD COLUMN launch_key_hash TEXT;

CREATE TABLE IF NOT EXISTS agent_request (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('move_channel', 'new_session')),
    project_id INTEGER NOT NULL, -- перенос: проект назначения; напарник: проект просителя
    payload_json TEXT NOT NULL,
    match_key TEXT NOT NULL, -- перенос: `channel:<id>`; напарник: `role:<role_key>`
    reason TEXT,
    who TEXT, -- подпись просителя: «роль N» или имя клиента
    token_id INTEGER,
    sub_id INTEGER, -- подписка просителя на момент заявки
    hub_session_id INTEGER, -- NULL — внешняя сессия
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'declined')),
    auto INTEGER NOT NULL DEFAULT 0 CHECK (auto IN (0, 1)), -- прошла без владельца
    note TEXT, -- слова владельца
    result_json TEXT,
    created_ts INTEGER NOT NULL,
    decided_ts INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS agent_request_open ON agent_request (kind, project_id, match_key)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS agent_request_seq ON agent_request (seq);
CREATE INDEX IF NOT EXISTS agent_request_project ON agent_request (project_id, status);
CREATE INDEX IF NOT EXISTS agent_request_token ON agent_request (token_id, created_ts);
