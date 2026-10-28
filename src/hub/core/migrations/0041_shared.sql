-- Разовая починка застрявших тредов Slack (Ф28-1, docs/architecture.md). Общая схема
-- (main и work): треды живут рядом со своей беседой.
--
-- До Ф28-1 снимок Slack «не подписан» (`source_subscribed = 0`, сверка `conversations.replies`) был главнее
-- правила хаба: тег меня или мой ответ ПОСЛЕ снимка тред не подписывали. Теперь такое свидетельство сбрасывает
-- снимок при приёме; здесь — уже застрявшие: мой ответ или прямое (не `@channel`/`@here`) упоминание меня в
-- ответе или в родителе за последние 3 суток. Снимок -> NULL (следующая сверка запишет мнение Slack заново),
-- тред подписан. Сводка треда и seq не пересчитываются: непрочитанное считается от `thread.subscribed` при
-- чтении, вкладки увидят перемену со следующим событием беседы.
--
-- `source_sub_ext` — позиция (ts Slack), на которой снят именно флаг `subscribed` (последний ответ треда, известный
-- снимку `conversations.replies`). С ней `threads.drop_stale_source_sub` отличает свидетельство, о котором снимок уже
-- знал, от нового; `source_latest_reply_ext` для этого не годится — его двигают и снимки без мнения о подписке.
-- У прежних снимков — NULL: свидетельство сбрасывает их без сравнения, следующая сверка запишет позицию.
ALTER TABLE thread ADD COLUMN source_sub_ext TEXT;
UPDATE thread SET source_subscribed = NULL, subscribed = 1
WHERE source_subscribed = 0
  AND EXISTS (
      SELECT 1 FROM message m
      WHERE m.channel_id = thread.channel_id
        AND (
            (m.thread_ext_id = thread.ext_id AND m.ext_id IS NOT NULL AND m.ext_id <> thread.ext_id
             AND (m.is_me = 1 OR (m.mentions_me = 1 AND m.mentions_broadcast = 0)))
            OR (m.ext_id = thread.ext_id AND m.mentions_me = 1 AND m.mentions_broadcast = 0)
        )
        AND m.deleted = 0 AND m.hidden = 0
        AND m.ts >= (CAST(strftime('%s', 'now') AS INTEGER) - 3 * 86400) * 1000
  );
