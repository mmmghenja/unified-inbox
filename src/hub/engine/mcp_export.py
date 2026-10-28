"""Ф74-2 (docs/architecture.md): `hub engine mcp-export` — набор MCP Claude Code в
формате другого движка (Cursor `mcpServers`, OpenCode `mcp`, Codex `[mcp_servers.*]`). Просил сисадмин: чтобы
конфиги внешних окон владельца собирались тем же конвертером, что у сессий хаба (`cursor_mcp.claude_user_servers`,
`opencode_servers`, `codex_servers`), а не вторым.

Секреты: значения `env` и заголовков серверов (ключи, токены) печатаются как есть ТОЛЬКО в файл `--out`
(создаётся с правами 0600, существующий перезаписывается с теми же правами); в stdout — с маской `***`.
Серверы хаба (`hub*`) в выгрузку не входят (у окна свой MCP хаба — `hub-all`)."""

import json
import os
from pathlib import Path

from . import cursor_mcp

FORMATS = ("cursor", "opencode", "codex")
MASK = "***"
# Поля записи сервера, значения которых — секреты (словари «имя -> значение»).
_SECRET_MAPS = ("env", "headers", "environment", "http_headers")


def servers(fmt: str, home: Path, folder: str | None) -> dict[str, dict]:
    """Серверы в формате движка `fmt`; `folder` — папка проекта (её выключенные и `.mcp.json`)."""
    if fmt == "cursor":
        return cursor_mcp.claude_user_servers(home, folder, folder)
    if fmt == "opencode":
        return cursor_mcp.opencode_servers(home, folder, folder)
    if fmt == "codex":
        return cursor_mcp.codex_servers(home, folder, folder)
    raise ValueError(f"format: {', '.join(FORMATS)}")


def masked(entries: dict[str, dict]) -> dict[str, dict]:
    """Копия с маской вместо значений секретных словарей (имена ключей остаются)."""
    result: dict[str, dict] = {}
    for name, entry in entries.items():
        copy = dict(entry)
        for key in _SECRET_MAPS:
            if isinstance(copy.get(key), dict):
                copy[key] = dict.fromkeys(copy[key], MASK)
        result[name] = copy
    return result


def _toml_key(key: str) -> str:
    if key and all(c.isascii() and (c.isalnum() or c in "-_") for c in key):
        return key
    return json.dumps(key, ensure_ascii=False)


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    # Строка TOML «basic» совпадает с JSON-строкой (те же экранирования; `\\uXXXX` допустим).
    return json.dumps(str(value), ensure_ascii=False)


def render(fmt: str, entries: dict[str, dict]) -> str:
    """Текст конфига: JSON `mcpServers` (Cursor) или `mcp` (OpenCode), TOML `[mcp_servers.*]` (Codex)."""
    if fmt == "cursor":
        return json.dumps({"mcpServers": entries}, ensure_ascii=False, indent=2) + "\n"
    if fmt == "opencode":
        return json.dumps({"mcp": entries}, ensure_ascii=False, indent=2) + "\n"
    lines: list[str] = []
    for name, entry in entries.items():
        head = f"mcp_servers.{_toml_key(name)}"
        lines.append(f"[{head}]")
        tables = []
        for key, value in entry.items():
            if isinstance(value, dict):
                tables.append((key, value))
            else:
                lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
        for key, value in tables:
            lines.append("")
            lines.append(f"[{head}.{_toml_key(key)}]")
            lines += [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in value.items()]
        lines.append("")
    return "\n".join(lines)


def write_private(path: Path, text: str) -> None:
    """Файл с правами 0600 (и существующий — права сбрасываются до записи)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


def export(fmt: str, *, home: Path, folder: str | None = None, out: Path | None = None) -> str:
    """Текст для stdout: без `out` — конфиг с маской секретов; с `out` — настоящий конфиг в файл 0600 и строка
    о записанном (имена серверов, без значений)."""
    entries = servers(fmt, home, folder)
    if out is None:
        return render(fmt, masked(entries))
    write_private(out, render(fmt, entries))
    names = ", ".join(entries) or "—"
    return f"записано {out} (0600, формат {fmt}): {len(entries)} серверов — {names}\n"
