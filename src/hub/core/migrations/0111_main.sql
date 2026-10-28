-- Строка хода агента владельца у реплики владельца в «Мой агент»: `progress` — текст статуса (до 2000), `progress_ts` — когда обновлён (мс). Пишет
-- только агент (`PUT /owner_channel/talk/{id}/progress`), пока реплика не обработана; в историю и пересказы не идёт.
ALTER TABLE owner_talk ADD COLUMN progress TEXT;
ALTER TABLE owner_talk ADD COLUMN progress_ts INTEGER;
