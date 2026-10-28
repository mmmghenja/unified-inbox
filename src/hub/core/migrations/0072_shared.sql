-- Ф49-1 (docs/architecture.md): признак «карточка под делегатом» на
-- карточке сессии хаба. Оба файла (таблица `held_request` живёт в файле строк сессии).
-- `delegate_sub_id` — подписка роли-делегата, которой карточка отдана на решение (NULL — решает владелец);
-- `delegate_until_ts` — до какого времени ждём делегата (потом признак снимает минутный цикл ядра);
-- `delegate_signal_ts` — когда подписке делегата выдан сигнал `card` (сигнал выдаётся один раз).
-- Признак снимается целиком (все три столбца -> NULL): подписка делегата кончилась, владелец сменил или снял
-- делегата, сессия стала ручной, срок вышел, карточка решена.
ALTER TABLE held_request ADD COLUMN delegate_sub_id INTEGER;
ALTER TABLE held_request ADD COLUMN delegate_until_ts INTEGER;
ALTER TABLE held_request ADD COLUMN delegate_signal_ts INTEGER;
