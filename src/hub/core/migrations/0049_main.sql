-- Сообщения агентов хаба и диалог владельца с агентом (Ф35-2, docs/architecture.md;
-- поправки П3–П5, П7, Н4, Н6–Н8). Только главный файл: work остаётся на прежней версии (решение 12).
--
-- Отправитель: агент — линия его подписки на момент отправки (`from_sub_id`, `from_project_id`, `from_role`,
-- `from_role_key`, `from_no`), владелец из UI — `from_user = 1` (поля `from_*` пусты). Адресат: линия с ролью в
-- настоящем проекте — `(to_project_id, to_role_key, to_no)`, `to_sub_id IS NULL` (ждёт подписки линии);
-- безрольный и агент вне проектов (проект 0) — ещё и `to_sub_id` (только живому; подписка кончилась — `lost`);
-- Владелец — `to_user = 1` (поля `to_*` пусты; только ответом на его сообщение).
--
-- Статусы: `pending` -> `read`; `pending` -> `lost` (подписка адресата кончилась), `expired` (`expires_ts`
-- прошёл; NULL — не истекает); `blocked` — попытка, остановленная лимитом (никому не доставляется, видна
-- владельцу). Сигнал подписке — сама строка: `signals` — сколько раз выдан (не больше двух, П4), `signaled_ts` и
-- `signal_sub_id` — когда и кому последний раз. `chain_id` — корень цепочки ответов, `hop` — глубина.
-- `notified_ts` — уведомление владельцу на экран (hub-agent). `seq` — глобальный: `/sync` и SSE-кадр `peer_msg`
-- (только UI-сессии).
CREATE TABLE IF NOT EXISTS peer_msg (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_user INTEGER NOT NULL DEFAULT 0 CHECK (from_user IN (0, 1)),
    from_sub_id INTEGER,
    from_project_id INTEGER,
    from_role TEXT,
    from_role_key TEXT,
    from_no INTEGER,
    to_user INTEGER NOT NULL DEFAULT 0 CHECK (to_user IN (0, 1)),
    to_project_id INTEGER,
    to_role TEXT,
    to_role_key TEXT,
    to_no INTEGER,
    to_sub_id INTEGER,
    text TEXT NOT NULL,
    reply_to INTEGER,
    chain_id INTEGER,
    hop INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN ('pending', 'read', 'lost', 'expired', 'blocked')),
    reason TEXT,
    api_token_id INTEGER,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    signal_sub_id INTEGER,
    signaled_ts INTEGER,
    read_sub_id INTEGER,
    read_ts INTEGER,
    closed_ts INTEGER,
    signals INTEGER NOT NULL DEFAULT 0,
    notified_ts INTEGER,
    expires_ts INTEGER,
    seq INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS peer_msg_to_line ON peer_msg (to_project_id, to_role_key, to_no)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS peer_msg_to_sub ON peer_msg (to_sub_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS peer_msg_seq ON peer_msg (seq);
CREATE INDEX IF NOT EXISTS peer_msg_created ON peer_msg (created_ts);
