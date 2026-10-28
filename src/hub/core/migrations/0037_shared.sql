-- Просмотры постов каналов Telegram (Ф23-3, docs/architecture.md).
-- Общая схема (main и work). Только сообщения бесед `kind='channel'` (лента-канал, broadcast):
-- `{"views": int, "forwards": int|null, "ts": мс снятия}`. NULL — не пост канала или ещё не снимали; старые
-- строки не пересчитываются (заполнит следующий приём или часовой проход коннектора).
ALTER TABLE message ADD COLUMN views_json TEXT;
