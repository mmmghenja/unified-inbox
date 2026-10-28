-- Доступ с телефона (Ф10-7, docs/architecture.md. Только hub.db.
-- Учёт UI-сессий: когда вошли, когда пользовались в последний раз (продление `POST /auth/renew`), через какой
-- слушатель (`local` — 127.0.0.1:8787, `remote` — туннель с телефона) и с какого устройства (User-Agent,
-- обрезан до 300 символов). Старые строки — NULL («до 25.09»); `via IS NULL` у UI-сессии = локальная.
-- У bearer-токенов все четыре поля NULL.
ALTER TABLE api_token ADD COLUMN created_ts INTEGER;
ALTER TABLE api_token ADD COLUMN last_used_ts INTEGER;
ALTER TABLE api_token ADD COLUMN via TEXT CHECK (via IS NULL OR via IN ('local', 'remote'));
ALTER TABLE api_token ADD COLUMN user_agent TEXT;

-- Неверные пароли входа с телефона за сутки (`GET /remote`, `hub-watch`) считаются по audit
-- (`action='remote_login'`, `result='invalid'`) — без индекса это полный проход append-only таблицы.
CREATE INDEX IF NOT EXISTS audit_action_ts ON audit (action, ts);
