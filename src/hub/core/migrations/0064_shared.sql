-- Ф44-1a (docs/architecture.md): кнопки под сообщениями Telegram-ботов.
-- Общая схема (main и work).
-- `buttons_json` — инлайн-клавиатура сообщения: `{"kind": "inline", "rows": [[{"t", "k", "d"?, "pw"?, "u"?,
-- "c"?}]], "cut"?: true}` (форма события коннектора, с сырым callback `d` — наружу он не отдаётся); NULL —
-- кнопок нет. `buttons_ts` — отметка времени снимка кнопок у источника, мс (сырой `edit_date`, иначе `date`);
-- NULL — источник кнопок не присылал. Старые строки не пересчитываются: кнопки запишет ближайшая сверка окна.
ALTER TABLE message ADD COLUMN buttons_json TEXT;
ALTER TABLE message ADD COLUMN buttons_ts INTEGER;
