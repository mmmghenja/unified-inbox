-- Режим подписки на проект «лично мне» и категория сигнала (Ф20-1, docs/architecture.md,
-- C, D и поправки П1, П10). Только главный файл: work остаётся на версии 33.
--
-- П1: «лично мне» — ФИЛЬТР поверх режима `messages`, не новое значение `mode` (все ветки `mode = 'messages'` —
-- выдача, дебаунс, дежурная, протокол — работают для него без правок). `all` — каждое чужое входящее (кроме
-- ботов), `personal` — лички, личные упоминания, ответы в тредах владельца и на его сообщения. При `mode = 'cards'`
-- фильтр всегда `all` (проверяет `subs.subscribe`). Старые подписки — `all`.
ALTER TABLE session_sub ADD COLUMN msg_filter TEXT NOT NULL DEFAULT 'all'
    CHECK (msg_filter IN ('all', 'personal'));
-- D: категория сигнала сообщений — сильнейшая из накопленных (`dm` > `mention` > `thread` > `other`; `NULL` у
-- старых строк — «сообщение») и родитель треда Slack (`ts`, иначе `NULL`) у сообщения, давшего категорию.
ALTER TABLE session_chan_signal ADD COLUMN cat TEXT;
ALTER TABLE session_chan_signal ADD COLUMN thread_ext TEXT;
-- П6: отфильтрованное подпиской на проект (бот, не лично мне) — одна строка `skipped` на (подписка, беседа,
-- причина); её ищет каждое отфильтрованное входящее (`chan_subs._put_filtered`).
CREATE INDEX IF NOT EXISTS session_chan_signal_skipped_project
    ON session_chan_signal (project_sub_id, channel_id) WHERE status = 'skipped' AND project_sub_id IS NOT NULL;
