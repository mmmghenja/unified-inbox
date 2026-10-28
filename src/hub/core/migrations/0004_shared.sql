-- Метаданные аккаунта у источника (список папок Telegram и т.п.). Раздел A.4 docs/architecture.md.
-- Общая схема (применяется к обоим файлам как есть, см. комментарий в 0001_shared.sql): текст лежит в файле
-- аккаунта, поэтому названия папок/каналов корпоративного аккаунта не попадают в hub.db. Без FK на account —
-- та же причина, что и у остальных общих таблиц (account живёт только в main).
--
-- Версия 0004, не 0002: спека куска 1a называла файл "0002_shared.sql", но версия схемы — `PRAGMA
-- user_version` каждого файла (db.py), и main/work уже реально стоят на версии 3 (0003_main.sql, кусок 5a).
-- Миграция версии <= текущей молча пропускается (`_init_file`) — назови я это 0002, на уже смигрированной
-- боевой БД (main.db на 8787 уже >= 3) она НИКОГДА не применилась бы. Следующая свободная версия — 4.

CREATE TABLE IF NOT EXISTS account_meta (
    account_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    updated_ts INTEGER NOT NULL,
    PRIMARY KEY (account_id, key)
);
