"""Клиент внутреннего сокета и каркас цикла коннектора. Спецификация: docs/architecture.md,
раздел «Клиент коннектора». Настоящие коннекторы (Telegram, Slack, ...) — фазы 1–5, наследуют `ConnectorBase`.
"""

from .base import AuthRequired, ConnectorBase, CoreClient, SendResult

__all__ = ["AuthRequired", "ConnectorBase", "CoreClient", "SendResult"]
