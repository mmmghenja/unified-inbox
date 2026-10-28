-- Ф70-1 (docs/architecture.md.1): строка outbox, которую владелец отправил или подтвердил
-- сам кукой интерфейса. Оба файла (outbox живёт и в main, и в work). Пока аккаунт спит (день его проекта
-- закрыт, `core/conn_sleep.py`), `outbox.lease` выдаёт только такие строки без графика и без срока
-- (`by_hand = 1 AND paced = 0 AND send_at_ts IS NULL`); остальное ждёт начала дня.
-- Ставит 1: `enqueue(origin='ui')` (ручная отправка) и подтверждение из интерфейса (`approve_draft(source=
-- 'ui')`, подтверждение пачкой). Подтверждение агентом владельца, «сразу да», авто-отправка — 0.
-- Старые строки: ручные отправки (`origin = 'ui'`) — 1; подтверждённые до миграции черновики — 0 (источник
-- подтверждения лежит только в журнале основной базы): при сне такие ждут начала дня.
ALTER TABLE outbox ADD COLUMN by_hand INTEGER NOT NULL DEFAULT 0 CHECK (by_hand IN (0, 1));
UPDATE outbox SET by_hand = 1 WHERE origin = 'ui';
