-- Ф41-3 (docs/architecture.md). Расход постоянной сессии хаба для строки сессии в окне:
-- `{"context_tokens", "context_limit", "cost_usd", "updated_ts"}` — размер контекста последнего хода, предел
-- модели и накопленная цена (сумму присланных хостом цен ходов копит ядро; `null`, пока цены не было). Пишет
-- `POST /work/host/sessions/{id}/turn` (поле `usage`). `NULL` — хост расхода ещё не присылал.
ALTER TABLE hub_session ADD COLUMN usage_json TEXT;
