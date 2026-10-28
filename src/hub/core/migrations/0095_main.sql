-- Ф63-1 (docs/architecture.md): правила проекта для входящих — условие -> действия
-- (`wake` — разбудить подписки ролей, `attention` — внимание сообщения). Только основная база. Логика —
-- `core/inbound_rules.py`; столбцы сообщения — `0094_shared.sql`.
-- `match_json` — условие (`accounts`, `from`, `subject`, `text`, `exclude`, `kinds`; пустые ключи не пишутся);
-- `action_json` — действия (`wake`: роли, `attention`: `react`/`info`). Кто завёл: владелец в интерфейсе — оба
-- `created_by_*` NULL; сессия — роль её подписки и номер подписки (`session_sub.id`).
-- `hits`/`last_hit_ts` — счётчик срабатываний (пишется в транзакции ингеста).
CREATE TABLE IF NOT EXISTS inbound_rule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    match_json TEXT NOT NULL,
    action_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_by_role TEXT,
    created_by_session INTEGER,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    hits INTEGER NOT NULL DEFAULT 0,
    last_hit_ts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_inbound_rule_project ON inbound_rule (project_id);
-- П4: сигнал подписке, разбуженной правилом, несёт номера правил (JSON-список, не больше 5) — пометка
-- «(правило #N)» в выдаче ожидания (`hub sub wait`), рядом с совпавшими ключами `keys_json`.
ALTER TABLE session_chan_signal ADD COLUMN rules_json TEXT;
