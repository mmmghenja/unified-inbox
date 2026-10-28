"""Делегат карточек Ф49-1 убран (Ф66-1, docs/architecture.md): кому идут
карточки проекта, решает таблица маршрута (`card_route`). Столбцы делегата в таблицах и ключ `card_delegate` в
`project.agent_policy_json` остаются мёртвыми (П8). Здесь — только переходник `card_view` (его импортирует
`agent-owner/tests/test_prompt.py`); сама функция — в очереди."""

from .owner_queue import card_view

__all__ = ["card_view"]
