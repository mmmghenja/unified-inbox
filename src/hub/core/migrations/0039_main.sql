-- Фильтр подписки по ключевым словам (Ф25-1, docs/architecture.md). Только главный файл:
-- work остаётся на версии 38.
--
-- Ключи подписки «лично мне» — {"keys": [...], "exclude": [...]} (нормализованы ядром, `core/keywords.py`),
-- NULL — без ключей.
ALTER TABLE session_sub ADD COLUMN keywords_json TEXT;
-- Совпавшие ключи сигнала (до 5, объединение по накопленным сообщениям), NULL — сигнал не по ключу.
ALTER TABLE session_chan_signal ADD COLUMN keys_json TEXT;
