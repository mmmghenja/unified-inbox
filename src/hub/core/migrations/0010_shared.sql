-- Внимание: режим беседы (Ф8-3, docs/architecture.md). Общая схема (main и work) — как
-- остальные *_shared.sql. `NULL` = «авто»: режим выводится из политики проекта (`project.agent_policy_json`,
-- ключ `attention`) и встроенных умолчаний — одной функцией ядра (`core/attention.py`, П5).
ALTER TABLE channel ADD COLUMN attention TEXT CHECK (attention IN ('react', 'info', 'digest', 'triage'));

-- Ф8-3 П11: упоминание ТОЛЬКО широковещательное (`@channel`/`@here`/`@everyone` Slack) — `mentions_me = 1` как
-- обычно, но тред им не подписывается (`threads.touch()`). Отдельная колонка нужна потому, что подписка
-- пересчитывается по ВСЕМ ответам треда при каждом `touch()`, а не только по событию, которое его вызвало.
ALTER TABLE message ADD COLUMN mentions_broadcast INTEGER NOT NULL DEFAULT 0 CHECK (mentions_broadcast IN (0, 1));
