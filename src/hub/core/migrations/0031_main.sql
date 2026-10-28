-- Заявки на выход из групп и каналов Telegram (Ф17-2, docs/architecture.md и поправки П1–П12).
-- Только hub.db: work остаётся на прежней версии. Своего AUTOINCREMENT в `db._SHARED_TABLES_WITH_OWN_ID` не
-- заводится — таблица не общая (как 0030_main); беседу по id ищут в её схеме (`db_of`).
--
-- Статусы: `pending` (просьба сессии, карточка владельцу) -> `approved` (владелец нажал «Выйти»; заявка владельца из шапки
-- беседы — сразу `approved`) -> `left` (`result`: `left` | `not_member`) | `failed` (`error`: причина или
-- `gave_up`). `pending`/`approved` -> `declined` (только пока шаг ни разу не выдавался, `first_leased_ts IS NULL`).
-- `project_id` — проект беседы на момент заявки (`NULL` — «Неразобранное», только у заявок владельца). `channel_id`
-- — беседа хаба; у темы форума — РОДИТЕЛЬ. `chat_kind` — вид беседы хаба (`group`|`channel`); какой вызов
-- Telegram звать, решает коннектор по сущности. Очередь — как `join_request`: `not_before_ms`/`pause_until_ms`
-- (паузы FloodWait аккаунта держит ядро), `attempts`, лиз `lease_until_ms`/`lease_gen`, `leased_gen`;
-- `first_leased_ts` — ПЕРВАЯ выдача шага (граница «Оставить»), `last_leased_ts` — последняя (темп выдач
-- аккаунта). `seq` — глобальный: `/sync` и SSE-кадр `leave`.
-- `archived_ids_json` — id бесед, которые A5 САМ перевёл в архив после выхода (беседа и её темы; уже бывшие в
-- архиве сюда не входят): возврат снимает архив только с них и с тем, появившихся после выхода. `returned_ts` —
-- когда хаб вернул беседу (сообщение после выхода или вступление П2); выход «в силе» — `left` и
-- `returned_ts IS NULL` (поздний архив владельца после возврата — его архив, не выход).
CREATE TABLE IF NOT EXISTS leave_request (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    project_id INTEGER,
    channel_id INTEGER NOT NULL,
    ext_id TEXT NOT NULL,
    chat_kind TEXT NOT NULL CHECK (chat_kind IN ('group', 'channel')),
    title TEXT,
    reason TEXT,
    origin TEXT NOT NULL CHECK (origin IN ('session', 'ui')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'left', 'failed', 'declined')),
    result TEXT CHECK (result IN ('left', 'not_member')),
    error TEXT,
    token_id INTEGER,
    client_name TEXT,
    created_ts INTEGER NOT NULL,
    decided_ts INTEGER,
    done_ts INTEGER,
    archived_ids_json TEXT,
    returned_ts INTEGER,
    updated_ts INTEGER NOT NULL,
    not_before_ms INTEGER NOT NULL DEFAULT 0,
    pause_until_ms INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_until_ms INTEGER,
    lease_gen INTEGER NOT NULL DEFAULT 0,
    leased_gen INTEGER,
    first_leased_ts INTEGER,
    last_leased_ts INTEGER,
    seq INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS leave_request_open ON leave_request (channel_id)
    WHERE status IN ('pending', 'approved');
CREATE INDEX IF NOT EXISTS leave_request_queue ON leave_request (account_id, status, not_before_ms);
CREATE INDEX IF NOT EXISTS leave_request_seq ON leave_request (seq);
CREATE INDEX IF NOT EXISTS leave_request_project ON leave_request (project_id, created_ts);
CREATE INDEX IF NOT EXISTS leave_request_channel ON leave_request (channel_id, done_ts);
CREATE INDEX IF NOT EXISTS leave_request_ext ON leave_request (account_id, ext_id);
