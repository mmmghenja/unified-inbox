-- Несколько сессий с подпиской на один проект (Ф26-1, docs/architecture.md). Только главный
-- файл: work остаётся на прежней версии.
--
-- Живая подписка на проект — одна на СЕССИЮ (проект, токен, процесс, разговор), а не на проект, предел живых на
-- проект (5) держит ядро (`subs.MAX_PROJECT_SUBS`). Роль — необязательная метка подписки, задаёт сама сессия:
-- `role` — как задано, `role_key` — нормализованная для сравнения (регистр, ё/е, пробелы), одна живая
-- подписка на роль в проекте.
DROP INDEX IF EXISTS session_sub_live_project;
CREATE UNIQUE INDEX IF NOT EXISTS session_sub_live_session ON session_sub
    (project_id, api_token_id, session_pid, session_proc_start, session_id) WHERE ended_ts IS NULL;
CREATE INDEX IF NOT EXISTS session_sub_live_project_all ON session_sub (project_id) WHERE ended_ts IS NULL;
ALTER TABLE session_sub ADD COLUMN role TEXT;
ALTER TABLE session_sub ADD COLUMN role_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS session_sub_live_role ON session_sub (project_id, role_key)
    WHERE ended_ts IS NULL AND role_key IS NOT NULL;
