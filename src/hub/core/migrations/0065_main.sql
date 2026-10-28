-- Нажатия кнопок под сообщениями Telegram-ботов (Ф44-1c, docs/architecture.md и
-- поправки П2, П3, П8). Только hub.db (как `join_request`, 0030_main): work остаётся на прежней версии; сообщение
-- и беседу по id ищут в схеме их аккаунта (`db_of`).
--
-- Статусы: `pending` (карточка ждёт владельца) -> `approved` -> `leased` -> `done` | `failed` | `unknown`;
-- `pending` -> `declined` | `expired`; `approved`, не выданная коннектору за срок годности, -> `expired`;
-- авто-строка, чьё условие «сразу» к выдаче уже не выполняется, `approved` -> `pending`.
-- `by` — кто нажал (`ui` — владелец кликом, `session` — сессия); `auto` — ушло без карточки (пределы считаются по
-- нему, отдельно от сообщений); `auto_by` — каким из двух исключений: `list` (список «без подтверждения») или
-- `autopilot` (выключатель `hub_press`), по нему же условие перепроверяется при выдаче.
-- `text` и `data_b64` — снимок кнопки на момент нажатия (сырой callback наружу не отдаётся никогда);
-- `result_json` — ответ бота (чужой ввод; только автору и владельцу). `session_json` — тройка сессии, подписка, роль.
-- `author_*` — подписка автора и её линия на момент создания: адрес сигнала исхода (как `draft_outcome`, Ф39);
-- `signaled_ts`/`signal_sub_id` — сигнал отдан. `project_id` — проект беседы на момент нажатия (видимость bearer).
-- `seq` — глобальный: `/sync` (раздел `presses`) и кадр SSE `press`.
CREATE TABLE IF NOT EXISTS button_press (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    project_id INTEGER,
    message_id INTEGER NOT NULL,
    ext_id TEXT NOT NULL,
    row INTEGER NOT NULL,
    col INTEGER NOT NULL,
    text TEXT,
    data_b64 TEXT,
    by TEXT NOT NULL CHECK (by IN ('ui', 'session')),
    token_id INTEGER,
    client_name TEXT,
    session_json TEXT,
    autopilot_session INTEGER,
    auto INTEGER NOT NULL DEFAULT 0 CHECK (auto IN (0, 1)),
    auto_by TEXT CHECK (auto_by IN ('list', 'autopilot')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'leased', 'done', 'failed', 'unknown',
                                           'declined', 'expired')),
    reason TEXT,
    result_json TEXT,
    created_ts INTEGER NOT NULL,
    decided_ts INTEGER,
    done_ts INTEGER,
    updated_ts INTEGER NOT NULL,
    not_before INTEGER NOT NULL DEFAULT 0,
    lease_gen INTEGER NOT NULL DEFAULT 0,
    lease_until INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    author_sub_id INTEGER,
    author_project_id INTEGER,
    author_role_key TEXT,
    author_role_no INTEGER,
    signaled_ts INTEGER,
    signal_sub_id INTEGER,
    seq INTEGER NOT NULL
);
-- П2: одна живая строка на кнопку.
CREATE UNIQUE INDEX IF NOT EXISTS button_press_live ON button_press (message_id, row, col)
    WHERE status IN ('pending', 'approved', 'leased');
CREATE INDEX IF NOT EXISTS button_press_queue ON button_press (account_id, status, not_before);
CREATE INDEX IF NOT EXISTS button_press_seq ON button_press (seq);
CREATE INDEX IF NOT EXISTS button_press_created ON button_press (created_ts);
CREATE INDEX IF NOT EXISTS button_press_author ON button_press (author_sub_id) WHERE author_sub_id IS NOT NULL;
