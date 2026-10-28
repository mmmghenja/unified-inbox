-- Ф41-4 (docs/architecture.md, П5). Входящее и жизнь
-- постоянной сессии хаба.
--
-- `want_sub_json` — желаемый фильтр подписки (задаёт владелец в форме; нормализован тем же кодом, что подписка):
-- `{"mode", "msg_filter", "keywords", "exclude", "batch_min", "accounts"}`; `NULL` — подписка не нужна.
-- Подписку по нему оформляет MCP-сервер хаба внутри самой сессии при старте.
-- `wake_requested_ts` — просьба владельца «поднять» тяжёлую и холодную сессию под ждущие сигналы;
-- `compact_requested_ts` — просьба «сжать» (часть 4б). Обе снимает хост (`turn`, `clear`); переход в `asleep`
-- снимает «сжать», начало хода — «поднять».
-- `signal_turn_ts` (П5) — сигналы выданы хосту и ход по ним ещё не кончился; `signal_lost_ts` — такой ход
-- оборван (выключение, хост замолчал, процесс умер): хост подаст сессии постоянную строку «проверь входящее».
ALTER TABLE hub_session ADD COLUMN want_sub_json TEXT;
ALTER TABLE hub_session ADD COLUMN wake_requested_ts INTEGER;
ALTER TABLE hub_session ADD COLUMN compact_requested_ts INTEGER;
ALTER TABLE hub_session ADD COLUMN signal_turn_ts INTEGER;
ALTER TABLE hub_session ADD COLUMN signal_lost_ts INTEGER;
-- П3д: новая причина ожидания `heavy` («контекст большой, кэш остыл» — сигналы ждут «Поднять» или реплики).
-- У столбца своя проверка значений, а SQLite её не правит: столбец пересоздаётся. Значение — сиюминутное
-- (пишет хост, снимается началом хода и стартом хоста), терять в нём нечего.
ALTER TABLE hub_session DROP COLUMN wait_reason;
ALTER TABLE hub_session ADD COLUMN wait_reason TEXT
    CHECK (wait_reason IN ('slot', 'elsewhere', 'paused', 'heavy'));
