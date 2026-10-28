-- Ф76-1 (docs/architecture.md): журнал решений
-- по карточкам — по строке на закрытие карточки, на всех путях (клик владельца, его бот, агент владельца, «сразу да»,
-- снятая галочка «Автопилота») и решения владельца из внешних окон (`owner_ext`, приём сборщика 76-2).
-- Оба файла: строка пишется в файл карточки (как `held_request`: запрос — по `db_of(id)`, черновик — файл
-- строки outbox, прочее — `agent_runs.schema_for_project`). Пишет только `core/decisions.py`.
--
-- `decided_by` — кто решил: `owner_ui` (кука интерфейса), `owner_bot` (бот владельца), `agent_on_word` (агент по
-- слову владельца — реплике разговора или указанию к «Реши»), `owner_ext` (внешнее окно) — учебные строки (П1);
-- `agent` (агент без слова), `auto` («сразу да» таблицы проекта), `autopilot` (снятая галочка), `delegate`
-- (делегат Ф49, только в первой загрузке истории) — видны, не
-- учат. `verdict` — дискретное решение (П3): `yes` / `no` / вариант вопроса; NULL — без счёта, только в
-- «похожие». `fingerprint`, `ask_class` — кодом (`core/decision_print.py`). Тексты — маскированные ядром (П13).
-- `source_key` — против задвоения: `ref` карточки хаба, у внешней истории `ext:<движок>:<разговор>:<номер>`.
-- `basis` (Ф76-3) — основание ответа агента владельца: `rule:…` / `pattern:…` / `similar:…` / `card` / `none` (до 200).
-- `wrong_ts`, `wrong_note` (Ф76-4а) — владелец пометил решение агента «неверно» (заметка маскирована, до 500): такая
-- строка не считается в образцах и не идёт в «похожие».
CREATE TABLE IF NOT EXISTS decision_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    ref TEXT,
    source_key TEXT,
    project_id INTEGER,
    session_id INTEGER,
    role TEXT,
    engine TEXT,
    kind TEXT NOT NULL,
    ask_class TEXT,
    rule TEXT,
    fingerprint TEXT NOT NULL,
    action TEXT NOT NULL,
    verdict TEXT,
    ask_text TEXT,
    answer_text TEXT,
    decided_by TEXT NOT NULL CHECK (
        decided_by IN (
            'owner_ui', 'owner_bot', 'agent_on_word', 'owner_ext', 'agent', 'auto', 'autopilot', 'delegate'
        )
    ),
    word_json TEXT,
    card_ts INTEGER,
    basis TEXT,
    wrong_ts INTEGER,
    wrong_note TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_decision_log_source ON decision_log (source_key)
    WHERE source_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_decision_log_print ON decision_log (project_id, fingerprint, ts);
CREATE INDEX IF NOT EXISTS idx_decision_log_class ON decision_log (project_id, kind, ask_class, ts);
