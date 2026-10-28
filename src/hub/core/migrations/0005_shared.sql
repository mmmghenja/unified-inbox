-- Позиционный маркер прочитанного (находка F8 второго круга критика куска 1b, docs/architecture.md).
-- `last_read_seq = MAX(first_seq)` ломается, когда СТАРОЕ по времени сообщение вставляется ПОЗЖЕ нового
-- (стартовая загрузка + живое, догон после сна, окно перечитывания): новое сообщение получает БОЛЬШИЙ
-- `first_seq`, чем более старое (по времени) прочитанное, и ошибочно считается прочитанным. Позиция
-- (ts, ext_sort, ext_id) последнего прочитанного сообщения не зависит от порядка ВСТАВКИ в БД, только от
-- порядка ПОКАЗА в ленте — маркер двигается по ней (row-value сравнение SQLite), а не по first_seq/seq.
--
-- `last_read_seq` (0001_shared.sql) продолжает писаться как раньше — только ради обратной совместимости
-- `/sync`/UI (клиенты, ожидающие его роста); реальные подсчёты непрочитанного на него больше не опираются.
ALTER TABLE channel ADD COLUMN last_read_ts INTEGER;
ALTER TABLE channel ADD COLUMN last_read_sort INTEGER;
ALTER TABLE channel ADD COLUMN last_read_ext TEXT;

-- Заполнить из существующего last_read_seq: позиция (ts, ext_sort, ext_id) САМОГО ПОЗДНЕГО ПО ПОЗИЦИИ
-- сообщения среди тех, чей first_seq <= last_read_seq (то, что раньше ingest.py/channels.py считали
-- «прочитанным по этой отметке») — лучшее доступное приближение старой seq-семантики новой позиционной.
UPDATE channel
SET last_read_ts = (
        SELECT m.ts FROM message m
        WHERE m.channel_id = channel.id AND m.first_seq <= channel.last_read_seq
        ORDER BY m.ts DESC, m.ext_sort DESC, m.ext_id DESC LIMIT 1
    ),
    last_read_sort = (
        SELECT m.ext_sort FROM message m
        WHERE m.channel_id = channel.id AND m.first_seq <= channel.last_read_seq
        ORDER BY m.ts DESC, m.ext_sort DESC, m.ext_id DESC LIMIT 1
    ),
    last_read_ext = (
        SELECT m.ext_id FROM message m
        WHERE m.channel_id = channel.id AND m.first_seq <= channel.last_read_seq
        ORDER BY m.ts DESC, m.ext_sort DESC, m.ext_id DESC LIMIT 1
    )
WHERE channel.last_read_seq IS NOT NULL;
