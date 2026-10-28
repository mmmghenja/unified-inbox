"""Ф74-7 / 74-3: слияние хуков и MCP хаба в конфиги ВНЕШНИХ окон Codex и Cursor владельца. Зовут установщики
`scripts/codex-hub-install.sh` и `scripts/cursor-hub-install.sh` (их запускает владелец):

    python -m hub.engine.ext_install codex|cursor --home <HOME> --hub-prod <дерево хаба> [--profile all]

- хуки — из шаблона `deploy/<движок>/hooks.json` (`@HOOK@` -> `.venv/bin/hub-ext-hook` дерева хаба); свои
  записи узнаются по команде `hub-ext-hook <движок> `, заменяются целиком, чужие (гард, хуки владельца) — как были;
- MCP: сервер `hub-all` (решение головной 21.10: не отдельный `hub-ext`) — дописывается, только если его нет;
  есть — не трогается вовсе (его ведёт сисадмин);
- меняется файл — рядом копия `<файл>.bak-<ГГГГММДД-ЧЧММСС>`; ничего не изменилось — файл не переписывается.
Секретов модуль не читает: `auth.json` Codex и токены не открываются; из `config.toml` и `mcp.json` читается
только, есть ли в них сервер `hub-all`.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shlex
import sys
import tempfile
import time
import tomllib
from pathlib import Path

ENGINES = ("codex", "cursor")
MCP_NAME = "hub-all"
MCP_TOOL_TIMEOUT_S = 180  # hub_wait держит вызов до 120 с
HOOK_PLACEHOLDER = "@HOOK@"


def hook_marker(engine: str) -> str:
    return f"hub-ext-hook {engine} "


def _ours(command: object, engine: str) -> bool:
    return isinstance(command, str) and hook_marker(engine) in command


def _fill(value, hook: str):
    if isinstance(value, str):
        return value.replace(HOOK_PLACEHOLDER, shlex.quote(hook))
    if isinstance(value, list):
        return [_fill(v, hook) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, hook) for k, v in value.items()}
    return value


def _without_ours(entries: list, engine: str) -> list:
    """Записи события без своих: Codex — группа `{matcher, hooks: [...]}` (свои — внутри неё, пустая
    после чистки группа уходит), Cursor — сама запись с `command`."""
    kept = []
    for entry in entries:
        if not isinstance(entry, dict):
            kept.append(entry)
            continue
        if engine == "cursor":
            if not _ours(entry.get("command"), engine):
                kept.append(entry)
            continue
        inner = entry.get("hooks")
        if not isinstance(inner, list):
            kept.append(entry)
            continue
        rest = [h for h in inner if not (isinstance(h, dict) and _ours(h.get("command"), engine))]
        if len(rest) == len(inner):
            kept.append(entry)
        elif rest:
            kept.append({**entry, "hooks": rest})
    return kept


def merge_hooks(current: dict | None, template: dict, hook: str, engine: str) -> dict:
    """Файл хуков с записями хаба из шаблона. Чужие записи и прочие ключи файла — как были."""
    result = copy.deepcopy(current) if isinstance(current, dict) else {}
    if engine == "cursor":
        result.setdefault("version", template.get("version", 1))
    hooks = result.get("hooks")
    if not isinstance(hooks, dict):
        if hooks is not None:
            raise ValueError("в файле хуков 'hooks' — не объект; правьте вручную")
        hooks = result["hooks"] = {}
    wanted = _fill(template.get("hooks") or {}, hook)
    for event in list(hooks):
        if event not in wanted and isinstance(hooks[event], list):
            hooks[event] = _without_ours(hooks[event], engine)
            if not hooks[event]:
                del hooks[event]
    for event, entries in wanted.items():
        existing = hooks.get(event)
        if existing is not None and not isinstance(existing, list):
            raise ValueError(f"в файле хуков '{event}' — не список; правьте вручную")
        hooks[event] = _without_ours(existing or [], engine) + list(entries)
    return result


def codex_mcp(text: str, hub_bin: str, profile: str) -> tuple[str, str]:
    """`config.toml` с сервером `hub-all` (дописывается в конец, если его нет) и что сделано."""
    data = tomllib.loads(text) if text.strip() else {}
    servers = data.get("mcp_servers") or {}
    if MCP_NAME in servers:
        return text, "есть — не трогаю"
    block = (
        f"[mcp_servers.{MCP_NAME}]\n"
        f"command = {json.dumps(hub_bin)}\n"
        f'args = ["mcp", "--profile", {json.dumps(profile)}]\n'
        f"tool_timeout_sec = {MCP_TOOL_TIMEOUT_S}\n"
    )
    head = text if not text or text.endswith("\n") else text + "\n"
    new = head + ("\n" if head else "") + block
    tomllib.loads(new)
    return new, "добавлен"


def cursor_mcp(current: dict | None, hub_bin: str, profile: str) -> tuple[dict, str]:
    result = copy.deepcopy(current) if isinstance(current, dict) else {}
    servers = result.get("mcpServers")
    if servers is None:
        servers = result["mcpServers"] = {}
    if not isinstance(servers, dict):
        raise ValueError("в mcp.json 'mcpServers' — не объект; правьте вручную")
    if MCP_NAME in servers:
        return result, "есть — не трогаю"
    servers[MCP_NAME] = {"command": hub_bin, "args": ["mcp", "--profile", profile]}
    return result, "добавлен"


def _write(path: Path, text: str, stamp: str) -> str | None:
    """Записать, если содержимое другое: копия прежнего рядом, права прежние (новый файл — 0600)."""
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == text:
        return None
    backup = None
    mode = 0o600
    if old is not None:
        backup = f"{path}.bak-{stamp}"
        Path(backup).write_text(old, encoding="utf-8")
        mode = path.stat().st_mode & 0o777
        os.chmod(backup, mode)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    return backup or "новый файл"


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return None
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: не объект JSON; правьте вручную")
    return data


def install(
    engine: str, *, home: Path, hub_prod: Path, profile: str = "all", stamp: str | None = None
) -> list[str]:
    """Слить хуки и MCP хаба в конфиг движка в `home`. Строки отчёта."""
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    hook = str(hub_prod / ".venv" / "bin" / "hub-ext-hook")
    hub_bin = str(hub_prod / ".venv" / "bin" / "hub")
    template = json.loads((hub_prod / "deploy" / engine / "hooks.json").read_text(encoding="utf-8"))
    report: list[str] = []
    if engine == "codex":
        base = Path(os.environ.get("CODEX_HOME") or home / ".codex")
        config = base / "config.toml"
        text = config.read_text(encoding="utf-8") if config.exists() else ""
        new_text, what = codex_mcp(text, hub_bin, profile)
        done = _write(config, new_text, stamp) if new_text != text else None
        report.append(f"MCP {MCP_NAME} в {config}: {what}" + (f" (копия: {done})" if done else ""))
    else:
        base = home / ".cursor"
        mcp_path = base / "mcp.json"
        current = _load_json(mcp_path)
        new_mcp, what = cursor_mcp(current, hub_bin, profile)
        done = (
            _write(mcp_path, json.dumps(new_mcp, ensure_ascii=False, indent=2) + "\n", stamp)
            if new_mcp != current
            else None
        )
        report.append(f"MCP {MCP_NAME} в {mcp_path}: {what}" + (f" (копия: {done})" if done else ""))
    hooks_path = base / "hooks.json"
    current_hooks = _load_json(hooks_path)
    merged = merge_hooks(current_hooks, template, hook, engine)
    if merged == current_hooks:
        report.append(f"хуки хаба в {hooks_path}: уже на месте")
    else:
        done = _write(hooks_path, json.dumps(merged, ensure_ascii=False, indent=2) + "\n", stamp)
        report.append(f"хуки хаба в {hooks_path}: записаны" + (f" (копия: {done})" if done else ""))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hub.engine.ext_install")
    parser.add_argument("engine", choices=ENGINES)
    parser.add_argument("--home", required=True)
    parser.add_argument("--hub-prod", required=True)
    parser.add_argument("--profile", default="all")
    parser.add_argument("--stamp", default=None, help="метка копий <файл>.bak-<метка> (откат установщика)")
    args = parser.parse_args(argv)
    try:
        lines = install(
            args.engine,
            home=Path(args.home),
            hub_prod=Path(args.hub_prod),
            profile=args.profile,
            stamp=args.stamp,
        )
        for line in lines:
            print("  " + line)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
