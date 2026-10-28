-- Мелкие хвосты Ф31-1 (docs/architecture.md. Только главный файл: work остаётся на
-- прежней версии.
--
-- Ключи подписки на беседу — {"keys": [...], "exclude": [...]} (нормализованы ядром, `core/keywords.py`, как
-- `session_sub.keywords_json`), NULL — без ключей: сигнал на каждое входящее, как раньше.
ALTER TABLE session_chan_sub ADD COLUMN keywords_json TEXT;

-- «Ключ уже сработал»: подписка (на проект — `project`, на беседу — `channel`) получила сигнал «по ключу»
-- об этом сообщении. Пишется при каждом сигнале `keyword` (приём и перепроверка после расшифровки или
-- правки); перепроверка при наличии строки молчит — правка, где ключ остался, повторно не будит. Только числа.
-- Строки старше 7 дней удаляет ядро при вставке.
CREATE TABLE IF NOT EXISTS sub_keyword_hit (
    sub_kind TEXT NOT NULL CHECK (sub_kind IN ('project', 'channel')),
    sub_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    PRIMARY KEY (sub_kind, sub_id, message_id)
);
CREATE INDEX IF NOT EXISTS sub_keyword_hit_ts ON sub_keyword_hit (ts);

-- Отфильтрованное подпиской на беседу с ключами (`no_keyword`) — одна строка `skipped` на (подписка, беседа,
-- причина), её ищет каждое отфильтрованное входящее (`chan_subs._put_filtered`), как у подписки на проект (0034).
CREATE INDEX IF NOT EXISTS session_chan_signal_skipped_chan
    ON session_chan_signal (chan_sub_id, channel_id) WHERE status = 'skipped' AND chan_sub_id IS NOT NULL;

-- Ревью Ф31-1: сообщения, уже давшие сигнал подписке с ключами ДО этой миграции, — пометкой из строк сигналов
-- (первое и последнее сообщение каждой выданной/ждущей строки за двое суток до последнего сигнала): правка
-- такого сообщения в первые сутки после деплоя не будит повторно. Сообщения из середины склеенной строки
-- не помечаются — принятый остаток риска.
INSERT OR IGNORE INTO sub_keyword_hit (sub_kind, sub_id, message_id, ts)
SELECT 'project', s.project_sub_id, ids.mid, s.updated_ts
FROM session_chan_signal s
JOIN session_sub u ON u.id = s.project_sub_id AND u.keywords_json IS NOT NULL
JOIN (SELECT id AS sid, first_message_id AS mid FROM session_chan_signal
      UNION SELECT id, last_message_id FROM session_chan_signal) ids ON ids.sid = s.id
WHERE s.status IN ('pending', 'delivered')
  AND s.updated_ts > (SELECT COALESCE(MAX(updated_ts), 0) FROM session_chan_signal) - 172800000;
