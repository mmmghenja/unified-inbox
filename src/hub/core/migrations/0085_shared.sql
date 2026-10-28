-- Ф57-1 (docs/architecture.md): разборщик без модели. Оба файла.
-- 1) Признак «письмо-рассылка» у сообщения: ставит только коннектор почты (при вставке, как `is_bot`), читает
--    только расчёт внимания (`attention.message_attention`): в почтовой беседе `react` опускается до `info`.
ALTER TABLE message ADD COLUMN bulk INTEGER NOT NULL DEFAULT 0 CHECK (bulk IN (0, 1));
-- 1а) Уже лежащая почта бесед, которые были в «разобрать» (без проекта без ручного режима и с ручным
--    `triage`), помечается рассылкой: иначе после смены режима на «реагирую» все старые непрочитанные
--    письма разом встали бы в «Нужна реакция». Новые письма признак получают от коннектора. Идёт ДО шага 2.
UPDATE message SET bulk = 1 WHERE is_me = 0 AND channel_id IN (SELECT id FROM channel WHERE kind = 'mail_thread' AND (attention = 'triage' OR (attention IS NULL AND project_id IS NULL)));
-- 2) Режима «разобрать» (`triage`) больше нет: ручной режим беседы раскладывается на оставшиеся — лички,
--    групповые лички Slack и почта — «реагирую», остальное — «к сведению». CHECK столбца не меняется.
--    Битый `meta_json` миграцию не роняет (`json_valid`).
UPDATE channel SET attention = CASE WHEN kind IN ('mail_thread', 'dm') OR CASE WHEN json_valid(meta_json) THEN json_extract(meta_json, '$.attrs.slack_type') END = 'mpim' THEN 'react' ELSE 'info' END WHERE attention = 'triage';
