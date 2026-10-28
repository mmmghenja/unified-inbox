-- Ф48-4 (docs/architecture.md): сообщения агентов — пачкой. Только hub.db (подписки на
-- проект и `peer_msg` живут в main).
-- `session_sub.agent_batch_min` — свой интервал пачки сообщений агентов у подписки, минуты 0…60 (0 — сразу;
-- NULL — не задан: действует умолчание проекта, ключ `agent_batch_min` политики, у агента вне проектов — 0).
-- `session_sub.agent_batch_ts` — срок пачки, «не раньше»: до этого момента несрочные сообщения агентов
-- подписке не сигналят (NULL или прошедший — сигналят сразу). Абсолютное время — переживает рестарт ядра.
-- `peer_msg.urgent` — пометка срочности `hub_agent_send(urgent=True)`: мимо пачки и забирает накопленное.
ALTER TABLE session_sub ADD COLUMN agent_batch_min INTEGER;
ALTER TABLE session_sub ADD COLUMN agent_batch_ts INTEGER;
ALTER TABLE peer_msg ADD COLUMN urgent INTEGER NOT NULL DEFAULT 0;
