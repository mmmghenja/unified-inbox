-- Ф48-3 (docs/architecture.md): файл роли сессии хаба. Только hub.db (таблица `hub_session`
-- живёт в main). `role_file` — путь к MD-файлу с инструкцией роли ОТНОСИТЕЛЬНО рабочей папки проекта
-- (NULL — не задан); хост читает файл при каждом запуске процесса сессии и кладёт дословно в системный текст.
ALTER TABLE hub_session ADD COLUMN role_file TEXT;
