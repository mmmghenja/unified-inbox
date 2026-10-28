-- Отправка по графику (Ф12-2, docs/architecture.md–3 и поправка П2). Обе схемы.
--
-- `paced = 1` — черновик «по графику»: подтверждение владельца ставит его не сразу, а в слот окна аккаунта
-- (`core/pace.py`: окно, промежуток, суточный лимит). Все черновики новым адресатам (Ф12-1) и черновики,
-- положенные сессией с `paced=true`. Старые строки — 0 (как было: сразу).
ALTER TABLE outbox ADD COLUMN paced INTEGER NOT NULL DEFAULT 0 CHECK (paced IN (0, 1));

-- Слот графика — отдельно от `not_before_ts`, который двигают и `flood`/`temporary`. По нему считаются
-- промежуток и суточный лимит (вместе с `sent_ts` отправленных); `lease` заново проверяет график при выдаче
-- и при нарушении назначает новый слот сюда и в `not_before_ts` (П2). NULL — строка не по графику или ещё не
-- подтверждена.
ALTER TABLE outbox ADD COLUMN pace_slot_ts INTEGER;

-- Слоты и лимит считаются по строкам графика одного аккаунта — без полного прохода его исходящих.
CREATE INDEX IF NOT EXISTS idx_outbox_paced ON outbox (account_id, status) WHERE paced = 1;
