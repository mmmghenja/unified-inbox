-- Операции над группами WhatsApp (Ф62-6, docs/architecture.md и поправки П1–П16). Только
-- hub.db (как `leave_request`, 0031_main): таблица не общая.
--
-- `kind`: `create` (новая группа: `title`, участники), `add` / `remove` (участники группы `channel_id`),
-- `link` (текущая ссылка-приглашение — чтение, без карточки, П5), `link_reset` (сбросить ссылку).
-- `members` (состав группы — чтение `group_info`, без карточки, только интерфейс; итог `{members}`).
-- Статусы: `pending` (просьба сессии, карточка владельцу) -> `approved` (владелец или выключатель автопилота
-- `wa_group`, тогда `auto = 1`; заявка владельца из интерфейса и `link` — сразу `approved`) -> `done` | `failed`
-- (`error`: причина или `gave_up`). `pending`/`approved` -> `declined`, пока шаг ни разу не выдавался.
-- `project_id` — проект заявки (у `create` — куда положить новую беседу; `NULL` — «Неразобранное», только у
-- заявок владельца). `channel_id` — беседа группы (у `create` — после появления беседы приёмом), `ext_id` — jid
-- группы (у `create` — из отчёта).
-- `participants_json` — список `{kind, mask, jid, channel_id, name, input}`: `input` (сырой номер) стирается
-- при закрытии заявки (П9), остаются маска и jid. `result_json` — итог: у `create` — `{chat, participants}`,
-- у `add`/`remove` — `{participants}` (код каждого), у `link`/`link_reset` — `{link}`.
-- `session_json` — личность сессии (аудит), `hub_session_id` — сессия хаба (канал владельца, П6); `author_*`,
-- `signaled_ts`, `signal_sub_id` — исход сигналом автору (как у нажатий кнопок, Ф44-1c).
-- Очередь — как `leave_request`: `not_before_ms`/`pause_until_ms`, `attempts`, лиз `lease_until_ms`/
-- `lease_gen`, `first_leased_ts` (граница «Отказать» и сверки create, П1), `last_leased_ts` (темп аккаунта).
-- `seq` — глобальный: `/sync` (`group_ops`) и SSE-кадр `group_op`.
CREATE TABLE IF NOT EXISTS group_op (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    project_id INTEGER,
    channel_id INTEGER,
    ext_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('create', 'add', 'remove', 'link', 'link_reset', 'members')),
    title TEXT,
    participants_json TEXT,
    reason TEXT,
    origin TEXT NOT NULL CHECK (origin IN ('session', 'ui')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'done', 'failed', 'declined')),
    auto INTEGER NOT NULL DEFAULT 0,
    result_json TEXT,
    error TEXT,
    token_id INTEGER,
    client_name TEXT,
    session_json TEXT,
    hub_session_id INTEGER,
    author_sub_id INTEGER,
    author_project_id INTEGER,
    author_role_key TEXT,
    author_role_no INTEGER,
    signaled_ts INTEGER,
    signal_sub_id INTEGER,
    created_ts INTEGER NOT NULL,
    decided_ts INTEGER,
    done_ts INTEGER,
    updated_ts INTEGER NOT NULL,
    not_before_ms INTEGER NOT NULL DEFAULT 0,
    pause_until_ms INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_until_ms INTEGER,
    lease_gen INTEGER NOT NULL DEFAULT 0,
    first_leased_ts INTEGER,
    last_leased_ts INTEGER,
    seq INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS group_op_queue ON group_op (account_id, status, not_before_ms);
CREATE INDEX IF NOT EXISTS group_op_seq ON group_op (seq);
CREATE INDEX IF NOT EXISTS group_op_project ON group_op (project_id, created_ts);
CREATE INDEX IF NOT EXISTS group_op_channel ON group_op (channel_id, created_ts);
CREATE INDEX IF NOT EXISTS group_op_ext ON group_op (account_id, ext_id);
CREATE INDEX IF NOT EXISTS group_op_author ON group_op (author_sub_id, signaled_ts);
