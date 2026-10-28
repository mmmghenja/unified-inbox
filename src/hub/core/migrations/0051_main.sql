-- Отправка без подтверждения в выбранные владельцем беседы (Ф37-1, docs/architecture.md и поправка
-- П1). Только главный файл; колонка `outbox.auto_role` — той же версией в `0051_shared.sql` (версия общая на
-- номер: отдельный номер у shared-файла к main уже не применился бы).
--
-- Список «без подтверждения»: пара (беседа, роль подписки). Ведёт только владелец из UI. `channel_id` — id беседы
-- любого файла (main или work), `role_key` — ключ сравнения роли (`subs.role_form`), `role` — как написал владелец.
-- Перенос беседы и архив строку не чистят: при отправке сверяется проект подписки и беседы (A5).
CREATE TABLE IF NOT EXISTS channel_autosend (
    channel_id INTEGER NOT NULL,
    role_key TEXT NOT NULL,
    role TEXT NOT NULL,
    created_ts INTEGER NOT NULL,
    PRIMARY KEY (channel_id, role_key)
);
