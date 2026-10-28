-- Ф48-5 (docs/architecture.md): снимок закреплённого системного текста сессии хаба.
-- Только hub.db (таблица `hub_session` живёт в main). Claude Code закрепляет системный текст при создании
-- разговора; `sys_pin` — JSON `{mode, role, role_no, role_file, file_sha}` составляющих на тот момент (NULL —
-- снимка нет: сессия создана до этой версии или движок не Claude Code). Пишет только хост вместе с привязкой
-- разговора движка.
ALTER TABLE hub_session ADD COLUMN sys_pin TEXT;
