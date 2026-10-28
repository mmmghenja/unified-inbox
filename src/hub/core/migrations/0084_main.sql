-- Ревью Ф56 (9): сообщения агентов владельцу (`peer_msg.to_user`), ждавшие его ДО появления очереди канала, в
-- очередь не идут — иначе первый деплой выдал бы их карточками `agent_msg` разом. Порог — номер строки на
-- момент миграции: в очередь попадают только сообщения с `id` не меньше записанного (читает
-- `owner_queue.agent_msg_from_id`). В пустой базе порог 1 — все будущие сообщения.
INSERT OR IGNORE INTO meta (key, value)
SELECT 'owner_agent_msg_from_id', CAST(COALESCE(MAX(id), 0) + 1 AS TEXT) FROM peer_msg;
