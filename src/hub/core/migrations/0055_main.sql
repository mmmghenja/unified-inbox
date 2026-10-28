-- Ф39 (docs/architecture.md). Главный файл: подписки сессий и исходы черновиков живут в
-- main; столбцы автора у `outbox` — той же версией в `0055_shared.sql` (оба файла).
--
-- Часть A (A1): подписка на беседу — своя у каждой сессии. Было: одна живая подписка на беседу на весь хаб
-- (`session_chan_sub_live` из 0025). Стало: одна живая на пару (беседа, сессия); ключ сессии — токен, pid,
-- starttime процесса, `session_id` (`chan_subs.rec_key`). Сессий у беседы — сколько угодно; второй индекс —
-- для выборки «все живые подписки беседы». П9: старый код на этой схеме не падает.
DROP INDEX IF EXISTS session_chan_sub_live;
CREATE UNIQUE INDEX IF NOT EXISTS session_chan_sub_live_session ON session_chan_sub
    (channel_id, api_token_id, session_pid, session_proc_start, session_id) WHERE ended_ts IS NULL;
CREATE INDEX IF NOT EXISTS session_chan_sub_live_channel ON session_chan_sub (channel_id) WHERE ended_ts IS NULL;

-- Часть B (B1 + П5): исход черновика — сигнал его автору. Строку пишет ручка владельца (подтвердил, поправил,
-- отклонил, снял после подтверждения) в той же транзакции; сигнал — сама строка (`signaled_ts IS NULL`), как у
-- напоминаний. Линия автора (`project_id`, `role_key`, `role_no`) — из его подписки `author_sub_id` на момент
-- исхода: проект 0 — агент вне проектов, `role_key` NULL — автор без роли (сигнал только самой подписке).
-- Текстов в таблице нет, строки не удаляются. Беседы в строке нет (П5): она читается из строки outbox при
-- выдаче — перенос беседы таблицу не знает.
CREATE TABLE IF NOT EXISTS draft_outcome (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    outbox_id INTEGER NOT NULL,
    author_sub_id INTEGER NOT NULL,
    project_id INTEGER NOT NULL,
    role_key TEXT,
    role_no INTEGER NOT NULL DEFAULT 1,
    outcome TEXT NOT NULL CHECK (outcome IN ('approved', 'edited', 'declined', 'withdrawn')),
    created_ts INTEGER NOT NULL,
    signaled_ts INTEGER,
    signal_sub_id INTEGER
);
CREATE INDEX IF NOT EXISTS draft_outcome_wait ON draft_outcome (project_id, role_key, role_no)
    WHERE signaled_ts IS NULL;
