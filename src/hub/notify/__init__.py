"""Спутник уведомлений `hub-notify` (Ф33-2, docs/architecture.md): живёт на ноутбуке,
long-poll'ом берёт уведомления владельца из кольца ядра (где бы ядро ни работало) и показывает их `notify-send`.
`files.py` — токен; `commands.py` — `hub notify setup|status`; `service.py` — `hub notify run`."""
