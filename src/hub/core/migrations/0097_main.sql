-- Ф69-1 (docs/architecture.md.2 + П1–П9): стоп первых сообщений новым людям после
-- спам-ограничения Telegram (PeerFlood). Только основная база (таблица `account` живёт в main).
-- `peer_flood_ts` — когда стоп поставлен (мс); NULL — стопа нет. Пока стоит, `outbox.lease` не выдаёт строки
-- бесед «новый человек» (`peer_flood.is_new_peer`), остальная отправка аккаунта идёт. Снимает только владелец:
-- кнопка «Снять стоп» (`POST /api/v1/accounts/{id}/resume`) или `hub account resume N`; сам по времени не снимается.
ALTER TABLE account ADD COLUMN peer_flood_ts INTEGER;
