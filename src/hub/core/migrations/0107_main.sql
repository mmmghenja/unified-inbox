-- Ф77-1 (docs/architecture.md): реестр машин, с которых сессии ходят к ядру. Только
-- основная база (подписки живут в main). `host` — 16 hex из `hub.hostid` (то же, что `session_host` подписок,
-- не сам `machine-id`). Пишет только `core/machines.py`: клиент сообщает `hostname`, путь до ядра и версию хаба
-- в `POST /subs`, `POST /chan-subs`, `POST /subs/beat` (поле `machine`); своя машина — при старте ядра.
-- `name` — имя от владельца (интерфейс, `hub machine name`), NULL — показывать `hostname`. `route` — `core` (машина
-- ядра) | `lan` | `s1` | `s1-socks` (маршрут `hub-link`) | NULL (неизвестно). `version` — короткий коммит хаба
-- на машине. `last_ts` — последний контакт (пишется не чаще раза в 60 с). Строки не удаляются: старая машина —
-- «давно не на связи».
CREATE TABLE IF NOT EXISTS machine (
  host     TEXT PRIMARY KEY,
  hostname TEXT,
  name     TEXT,
  route    TEXT,
  version  TEXT,
  first_ts INTEGER NOT NULL,
  last_ts  INTEGER NOT NULL
);
