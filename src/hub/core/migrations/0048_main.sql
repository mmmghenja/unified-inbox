-- Агенты хаба: адрес подписки (Ф35-1, docs/architecture.md, поправки П10, П11). Только
-- главный файл: work остаётся на прежней версии.
--
-- Линия агента — `(project_id, role_key, role_no)`; у безрольных — `(project_id, NULL, role_no)`. Живая линия
-- с ролью — одна (индекс ниже); уникальность номера безрольных держит код (П10: индекса для них нет — откат
-- кода после этой миграции ничего не ломает). `direct` — режим «только адресованное агенту»: в столбец `mode`
-- значение `direct` не пишется никогда (П11, CHECK 0025), наружу режим отдаётся по флагу.
-- `project_id = 0` — агент вне проектов (решение 14), внешнего ключа у столбца нет.
ALTER TABLE session_sub ADD COLUMN role_no INTEGER NOT NULL DEFAULT 1 CHECK (role_no BETWEEN 1 AND 99);
ALTER TABLE session_sub ADD COLUMN direct INTEGER NOT NULL DEFAULT 0 CHECK (direct IN (0, 1));
-- Живым безрольным — номера 1..n по id внутри проекта (до индекса).
UPDATE session_sub SET role_no = (
    SELECT COUNT(*) FROM session_sub o
    WHERE o.project_id = session_sub.project_id AND o.ended_ts IS NULL AND o.role_key IS NULL
        AND o.id <= session_sub.id
) WHERE ended_ts IS NULL AND role_key IS NULL;
DROP INDEX IF EXISTS session_sub_live_role;
CREATE UNIQUE INDEX IF NOT EXISTS session_sub_live_role ON session_sub (project_id, role_key, role_no)
    WHERE ended_ts IS NULL AND role_key IS NOT NULL;
-- Напоминание — линии: номер значим только при роли (П2: безрольные — как раньше).
ALTER TABLE reminder ADD COLUMN role_no INTEGER NOT NULL DEFAULT 1;
