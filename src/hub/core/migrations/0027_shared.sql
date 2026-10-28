-- Разовая уборка карточек перенесённых бесед (Ф11-9 H2, docs/architecture.md). Общая схема (main и
-- work): карточки разборщика живут рядом со своей беседой.
--
-- До Ф11-9 `channel_migrate` архивировал старую беседу (`archived = 1`, `meta.migrated_to`), а её открытые
-- карточки (`new`/`seen`) оставлял висеть. Теперь ингест закрывает их сам; здесь — только уже висящие. Другие
-- архивные беседы (без `migrated_to`) не трогаются.
UPDATE agent_item SET status = 'done'
WHERE status IN ('new', 'seen')
  AND channel_id IN (
      SELECT id FROM channel WHERE archived = 1 AND json_extract(meta_json, '$.migrated_to') IS NOT NULL
  );
