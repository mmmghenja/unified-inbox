-- Ф86-1 (docs/architecture.md.1, C1.2, П12, П16): сводка дня проекта и признак «набрал владелец».
-- `digest` — сводка проекта за календарный день по времени хаба (`day` — `ГГГГ-ММ-ДД`), её пишет внешний клиент
-- (скоуп `digest`, `PUT /digest/{project_id}/{day}`). Оба файла: строка — в файле `agent_runs.schema_for_project`
-- проекта, при записи строка того же `(project_id, day)` из другого файла удаляется (П12); чтение смотрит в оба.
-- Пишет только `core/digest.py`. `upto_ts` — до какого момента учтены источники (мс). Ретенция выключена
--, функция порции — `retention._purge_digest_portion`.
CREATE TABLE IF NOT EXISTS digest (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    text TEXT NOT NULL,
    model TEXT,
    upto_ts INTEGER,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    UNIQUE (project_id, day)
);
-- `held_say.typed` — реплику набрал (или надиктовал) сам владелец в окне сессии хаба (`POST /sessions/{id}/say`
-- от куки) или ответил кукой по карточке канала владельца. Передача дел, интервью, агент владельца — `0`; перенесённая
-- передачей дел реплика сохраняет признак исходной (П3). Старые строки — `0`.
ALTER TABLE held_say ADD COLUMN typed INTEGER NOT NULL DEFAULT 0;
