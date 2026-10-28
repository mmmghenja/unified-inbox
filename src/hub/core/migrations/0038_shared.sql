-- Факты о людях (Ф24-1, docs/architecture.md и поправки П1, П2). Общая схема (main и
-- work): факты из переписки в рабочем файле лежат в рабочем файле и уходят вместе с ним (принцип Ф21-1 П5).
-- Связи через границу файлов нет (FK нет): `person_id` указывает на строку `main.person`, как `contact.person_id`.
-- `person_fact` — со своим AUTOINCREMENT: в `db._SHARED_TABLES_WITH_OWN_ID` (id в work — от 2^40).
CREATE TABLE IF NOT EXISTS person_fact (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('auto', 'manual')),
    project_ids_json TEXT NOT NULL DEFAULT '[]', -- П2: проекты бесед-источников (0 — без проекта); ручные — []
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    rejected_ts INTEGER                          -- убран владельцем: не показывается, не считается, модель видит «отклонено»
);
CREATE INDEX IF NOT EXISTS person_fact_person ON person_fact (person_id, rejected_ts, updated_ts);
-- Состояние сбора фактов по (человек, файл): строка — в файле фактов.
CREATE TABLE IF NOT EXISTS person_fact_state (
    person_id INTEGER PRIMARY KEY,
    cursor_ts INTEGER,                           -- обмен до этого ts учтён
    last_try_ts INTEGER,                         -- последняя попытка службы (успех или сбой)
    version INTEGER NOT NULL DEFAULT 0,          -- растёт на каждой перемене фактов (служба или владелец)
    fails INTEGER NOT NULL DEFAULT 0,
    updated_ts INTEGER                           -- последнее применение службой (для «обновлено …»)
);
