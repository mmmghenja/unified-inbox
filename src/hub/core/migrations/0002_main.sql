-- Первая настоящая миграция версии схемы main (раньше была только 0001). Раздел «Вход UI» docs/architecture.md.
-- Только hub.db: одноразовые коды входа UI. В таблице лежит только sha256(код), не сам код (см. security.py).

CREATE TABLE IF NOT EXISTS login_code (
    hash TEXT PRIMARY KEY,
    expires_ts INTEGER NOT NULL,
    used_ts INTEGER
);
