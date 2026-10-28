-- Заявки сессий на вступление в группы и каналы Telegram (Ф17-1, docs/architecture.md и поправки
-- П2–П6). Только hub.db: work остаётся на прежней версии. Своего AUTOINCREMENT в `db._SHARED_TABLES_WITH_OWN_ID`
-- не заводится — таблица не общая (как 0025_main), беседу по `ext_id` ищут в схеме аккаунта (`db_of`).
--
-- Статусы: `checking` (ждёт предпросмотра коннектором) -> `pending` (карточка владельцу) | `approved` (владелец нажал или
-- выключатель проекта) | `already` | `failed`, `approved` -> `joined` | `requested` | `already` | `failed`.
-- `pending`/`approved` -> `declined` (только пока шаг «вступить» ни разу не выдавался, П2).
-- `auto_eligible` — заявка живой подписанной сессии этого проекта на момент создания, `auto` — одобрена
-- выключателем. `peek_json` — предпросмотр (название, вид, участники, описание — пишут посторонние: только данные).
-- Очередь — как `read_ack`: `not_before_ms`/`pause_until_ms` (П3: паузы темпа и FloodWait аккаунта держит ядро),
-- `attempts`, лиз `lease_until_ms`/`lease_gen`, `leased_gen`. `peek_leased_ts` — последняя выдача шага
-- «посмотреть» (темп предпросмотров аккаунта), `join_leased_ts` — ПЕРВАЯ выдача шага «вступить» (граница привязки
-- беседы, решение 5, и темп вступлений). `seq` — глобальный (П6): `/sync` и SSE-кадр `join`.
CREATE TABLE IF NOT EXISTS join_request (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    project_id INTEGER NOT NULL,
    target_kind TEXT NOT NULL CHECK (target_kind IN ('username', 'invite')),
    target TEXT NOT NULL,
    reason TEXT,
    status TEXT NOT NULL CHECK (status IN ('checking', 'pending', 'approved', 'joined', 'requested', 'already',
                                           'failed', 'declined')),
    auto INTEGER NOT NULL DEFAULT 0 CHECK (auto IN (0, 1)),
    auto_eligible INTEGER NOT NULL DEFAULT 0 CHECK (auto_eligible IN (0, 1)),
    peek_json TEXT,
    ext_id TEXT,
    title TEXT,
    channel_id INTEGER,
    error TEXT,
    token_id INTEGER,
    client_name TEXT,
    session_json TEXT,
    created_ts INTEGER NOT NULL,
    decided_ts INTEGER,
    done_ts INTEGER,
    updated_ts INTEGER NOT NULL,
    not_before_ms INTEGER NOT NULL DEFAULT 0,
    pause_until_ms INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_until_ms INTEGER,
    lease_gen INTEGER NOT NULL DEFAULT 0,
    leased_gen INTEGER,
    peek_leased_ts INTEGER,
    join_leased_ts INTEGER,
    seq INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS join_request_open ON join_request (account_id, target_kind, target)
    WHERE status IN ('checking', 'pending', 'approved');
CREATE INDEX IF NOT EXISTS join_request_queue ON join_request (account_id, status, not_before_ms);
CREATE INDEX IF NOT EXISTS join_request_seq ON join_request (seq);
CREATE INDEX IF NOT EXISTS join_request_project ON join_request (project_id, created_ts);
CREATE INDEX IF NOT EXISTS join_request_ext ON join_request (account_id, ext_id) WHERE ext_id IS NOT NULL;
