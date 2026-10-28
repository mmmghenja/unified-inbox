"""hub-mcp: MCP-сервер хаба для Claude Code и OpenCode (stdio). Спецификация: docs/architecture.md.

Обычный клиент публичного API ядра (bearer-токен профиля), без обращений к БД, внутреннему сокету и файлам
хаба — кроме своего файла токена `<config_dir>/mcp/<профиль>.token`. Имя пакета — `hub.mcp_server`, а не
`hub.mcp`, чтобы не путать с SDK `mcp`.
"""
