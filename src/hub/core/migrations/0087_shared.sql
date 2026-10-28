-- Ф57-2 (docs/architecture.md): режима «сводка раз в день» (`digest`) больше нет —
-- его исполняла только убранная дежурная. Ручной режим беседы `digest` -> `info` («к сведению»: сводка
-- означала «не уведомлять»). Оба файла; CHECK столбца не меняется.
UPDATE channel SET attention = 'info' WHERE attention = 'digest';
