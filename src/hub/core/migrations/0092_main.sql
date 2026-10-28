-- Ф60-1 (docs/architecture.md): помощник на интервью. Только hub.db. Звук не
-- хранится нигде; текст строк — содержимое разговора: в журнал ядра не пишется, в аудит — только числа.
-- Логика и проверки — `core/interview.py`.
-- `interview`: одно `active|paused` на всё ядро (проверка — в транзакции писателя). `fed_line_id` — до какой
-- строки расшифровка подана сессии; `hint_ts` — последняя «Подскажи»; `reason` — почему интервью встало или
-- кончилось само («ядро перезапущено», «тишина 20 мин», «потолок 3 часа», «сессия закрыта»), у действий владельца
-- — NULL; `warn` — строка службы записи для панели (≤200, NULL — нет); `active_ts` — когда интервью последний
-- раз стало `active` (старт, «Продолжить»), `last_line_ts` — время прихода последней строки (оба — для
-- «тишина 20 мин»).
CREATE TABLE IF NOT EXISTS interview (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    project_id INTEGER NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('live', 'button')),
    them_lang TEXT NOT NULL CHECK (them_lang IN ('ru-RU', 'en-US')),
    status TEXT NOT NULL CHECK (status IN ('active', 'paused', 'ended')),
    started_ts INTEGER NOT NULL,
    ended_ts INTEGER,
    fed_line_id INTEGER NOT NULL DEFAULT 0,
    hint_ts INTEGER,
    reason TEXT,
    warn TEXT,
    active_ts INTEGER NOT NULL,
    last_line_ts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_interview_status ON interview (status);
CREATE INDEX IF NOT EXISTS idx_interview_session ON interview (session_id);
-- Строка расшифровки: `who` — `me` (владелец) или `them` (собеседник); `start_ms`/`end_ms` — время стены службы
-- записи, мс от эпохи (П10); `created_ts` — приход в ядро.
CREATE TABLE IF NOT EXISTS interview_line (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id INTEGER NOT NULL REFERENCES interview (id),
    who TEXT NOT NULL CHECK (who IN ('me', 'them')),
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    text TEXT NOT NULL,
    created_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_interview_line ON interview_line (interview_id, id);
