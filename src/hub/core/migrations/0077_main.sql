-- Заявки «нужен человек» (Ф54-1, docs/architecture.md). Только hub.db
-- (как `button_press`, 0065_main): заявки рабочих проектов тоже лежат здесь, мимо ретенции work — осознанно.
--
-- Сессия с подпиской на проект с ролью зовёт владельца в окно браузера: капча, вход, код, оплата, согласие.
-- Статусы: `pending` -> `done` | `failed` | `later` (решение владельца или ответчика-агента) | `expired` (срок) |
-- `cancelled` (автор снял сам). `site`, `url`, `doing`, `need`, `profile`, `tab` — текст автора (чужой ввод для
-- интерфейса: простым текстом). `shot_*` — скриншот: байты лежат в `<data_dir>/human-files/<sha[:2]>/<sha>`,
-- `shot_purged_ts` — байты стёрты (7 суток после решения, сразу при `cancelled`). `expires_ts` NULL — без срока
-- (снимается через 24 часа). `decided_by` — кто ответил (`ui` — владелец кнопкой, `agent` — токен со скоупом
-- `human`, `bot` — запас под прямой канал Ф54-2); `decided_token_id` — токен ответчика-агента.
-- `author_*` — подписка автора и её линия (проект, роль, номер) на момент создания: адрес сигнала исхода, по
-- линии же считаются пределы и право отмены; `signaled_ts`/`signal_sub_id` — сигнал отдан. `session_json` —
-- тройка сессии и роль словами; `token_id` — токен автора. `seq` — глобальный: `/sync` (раздел
-- `human_requests`, только UI) и кадр SSE `human_request`.
CREATE TABLE IF NOT EXISTS human_request (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL CHECK (project_id > 0),
    kind TEXT NOT NULL CHECK (kind IN ('captcha', 'login', 'code', 'payment', 'consent', 'other')),
    site TEXT,
    url TEXT,
    doing TEXT,
    need TEXT NOT NULL,
    profile TEXT,
    tab TEXT,
    shot_sha256 TEXT,
    shot_size INTEGER,
    shot_mime TEXT,
    shot_purged_ts INTEGER,
    expires_ts INTEGER,
    status TEXT NOT NULL CHECK (status IN ('pending', 'done', 'failed', 'later', 'expired', 'cancelled')),
    decided_ts INTEGER,
    decided_by TEXT CHECK (decided_by IN ('ui', 'agent', 'bot')),
    decided_token_id INTEGER,
    author_sub_id INTEGER,
    author_project_id INTEGER,
    author_role_key TEXT,
    author_role_no INTEGER,
    session_json TEXT,
    token_id INTEGER,
    signaled_ts INTEGER,
    signal_sub_id INTEGER,
    created_ts INTEGER NOT NULL,
    seq INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS human_request_seq ON human_request (seq);
CREATE INDEX IF NOT EXISTS human_request_open ON human_request (project_id, status);
CREATE INDEX IF NOT EXISTS human_request_created ON human_request (project_id, created_ts);
CREATE INDEX IF NOT EXISTS human_request_author ON human_request (author_sub_id) WHERE author_sub_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS human_request_shot ON human_request (shot_sha256) WHERE shot_sha256 IS NOT NULL;
