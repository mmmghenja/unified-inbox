-- Ф83-1 (docs/architecture.md): запасной движок сессий хаба.
-- `hub_session.fallback_json` — свой запасной сессии: NULL — как у проекта; `{"engine": null}` — запасного нет;
-- `{"engine", "model", "effort"}` — свой. `hub_session.on_fallback_json` — пометка «на запасном»
-- `{"main": {"engine", "model", "effort"}, "kind", "reason", "since", "by": "auto"|"owner_channel"}`; NULL — на основном.
-- `project.session_fallback_json` — умолчание проекта для сессий (NULL — нет; форма — как у сессии).
-- Запасной агента владельца и фактов — ключи `main.meta` (`agent_fallback`, `facts_fallback`, `agent_on_fallback`,
-- `facts_on_fallback`), не здесь. Пишет только `core/engine_fallback.py` и передача дел (`core/handoff.py`).
ALTER TABLE hub_session ADD COLUMN fallback_json TEXT;
ALTER TABLE hub_session ADD COLUMN on_fallback_json TEXT;
ALTER TABLE project ADD COLUMN session_fallback_json TEXT;
