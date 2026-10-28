-- Фильтр подписки на проект по аккаунтам (Ф38-1, docs/architecture.md). Только
-- главный файл: work остаётся на прежней версии.
--
-- `{"only": [..]}` — подписка слышит только беседы этих аккаунтов, `{"except": [..]}` — все, кроме этих
-- (номера `main.account.id`, уникальные, по возрастанию; `core/sub_accounts.py`). NULL — все аккаунты.
ALTER TABLE session_sub ADD COLUMN accounts_json TEXT;
