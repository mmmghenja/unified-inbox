-- Ф74-2 (docs/architecture.md, план 6.2 «Б2»): движок сессии у подписок на проект и на
-- беседу — `claude`, `opencode`, `codex`, `cursor` (свойства движка — профиль `engine/registry.py`, а не
-- приставка `ses_` в коде ядра). У сессии хаба (условный pid < 0) — движок её записи `hub_session`. Новые строки
-- размечает ядро при вставке; существующие — здесь: `ses_…` — OpenCode, сессия хаба — её запись, прочее —
-- Claude Code (внешних Codex и Cursor до этой версии не было). NULL ядро читает по `session_id`, как раньше.
ALTER TABLE session_sub ADD COLUMN session_engine TEXT;
ALTER TABLE session_chan_sub ADD COLUMN session_engine TEXT;
UPDATE session_sub SET session_engine = CASE
    WHEN session_id LIKE 'ses\_%' ESCAPE '\' THEN 'opencode'
    WHEN session_pid < 0 THEN COALESCE(
        (SELECT h.engine FROM hub_session h WHERE h.id = -session_sub.session_pid), 'claude')
    ELSE 'claude'
END;
UPDATE session_chan_sub SET session_engine = CASE
    WHEN session_id LIKE 'ses\_%' ESCAPE '\' THEN 'opencode'
    WHEN session_pid < 0 THEN COALESCE(
        (SELECT h.engine FROM hub_session h WHERE h.id = -session_chan_sub.session_pid), 'claude')
    ELSE 'claude'
END;
