-- Ф33-6 (docs/architecture.md): учёт нагрузки по машинам. `host` — метка машины клиента
-- (`hostid.host_id()`); пустая = машина ядра (старые записи). У записи чужой машины pid ядро по `/proc` не
-- проверяет: она живёт до `expires_ts` или явного `done`.
ALTER TABLE load_claim ADD COLUMN host TEXT NOT NULL DEFAULT '';
