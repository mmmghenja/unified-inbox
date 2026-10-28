-- Ф63-1 (docs/architecture.md): правила проекта для входящих.
-- Оба файла. Правила оцениваются ОДИН раз — при вставке нового входящего в ингесте (`core/inbound_rules.py`);
-- правка сообщения их не пересчитывает, правило, заведённое позже, старые сообщения не трогает.
-- `rule_hits` — JSON-список номеров совпавших правил (`main.inbound_rule.id`, не больше 5, по возрастанию);
-- `rule_attention` — внимание, назначенное правилом (`react` поднимает рассылку до карточки, `info` гасит);
-- читает только расчёт внимания (`attention.message_attention`): ручной режим беседы и заглушка сильнее.
ALTER TABLE message ADD COLUMN rule_hits TEXT;
ALTER TABLE message ADD COLUMN rule_attention TEXT CHECK (rule_attention IN ('react', 'info'));
