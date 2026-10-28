-- Люди (Ф21-1, docs/architecture.md и поправки П5, П10). Общая схема (main и work): связь
-- контакта с человеком, почта контакта, имя человека, заданное владельцем, и статистика переписки — в файле
-- контакта (корпоративное — в рабочем файле и уходит вместе с ним). Связи через границу файлов нет (FK нет):
-- `person_id` в рабочем файле указывает на строку `main.person`.
ALTER TABLE contact ADD COLUMN person_id INTEGER;            -- main.person.id; NULL — не человек (B1) или ещё не связан
ALTER TABLE contact ADD COLUMN person_manual INTEGER NOT NULL DEFAULT 0 CHECK (person_manual IN (0, 1));
ALTER TABLE contact ADD COLUMN email TEXT;                   -- нижний регистр; почта — адрес, Slack — профиль
ALTER TABLE contact ADD COLUMN person_name TEXT;             -- П5: имя человека, заданное владельцем
CREATE INDEX IF NOT EXISTS contact_person ON contact (person_id) WHERE person_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS contact_email ON contact (email) WHERE email IS NOT NULL;
-- B1 (бот — есть сообщение `is_bot`), П10 (виден — есть не скрытое сообщение), участники беседы.
CREATE INDEX IF NOT EXISTS message_sender_ts ON message (sender_contact_id, ts) WHERE sender_contact_id IS NOT NULL;
-- B1/П3: «есть сообщение бота» у каждого контакта на каждом проходе связывания — по маленькому частичному индексу,
-- а не перебором всех сообщений контакта (проход держит писатель).
CREATE INDEX IF NOT EXISTS message_bot_sender ON message (sender_contact_id) WHERE is_bot = 1;
-- D: статистика переписки по контакту-человеку, пересчёт раз в час целиком (DELETE + вставка).
CREATE TABLE IF NOT EXISTS contact_stats (
    contact_id INTEGER PRIMARY KEY,
    known INTEGER NOT NULL DEFAULT 0 CHECK (known IN (0, 1)),   -- D7
    last_ts INTEGER,           -- последнее взаимодействие с владельцем (входящее «Владельцу» или исходящее ему)
    seen_ts INTEGER,           -- последнее его сообщение где угодно
    in_total INTEGER NOT NULL DEFAULT 0,
    out_total INTEGER NOT NULL DEFAULT 0,
    in_30d INTEGER NOT NULL DEFAULT 0,
    out_30d INTEGER NOT NULL DEFAULT 0,
    stats_json TEXT NOT NULL,  -- D8
    computed_ts INTEGER NOT NULL
);
