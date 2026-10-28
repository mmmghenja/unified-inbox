-- Ф95-1 (docs/architecture.md.1): правило раскладки почты, заведённое заявкой
-- «нужен человек» по домену регистрации. NULL — обычное правило; число — id заявки (`main.human_request`), по
-- итогу которой правило снимается (`failed`, `expired`, `cancelled`) или остаётся (`done`, `later`). Логика —
-- `core/reg_mail_rule.py`. Только главный файл.
ALTER TABLE map_rule ADD COLUMN origin_human_id INTEGER;
