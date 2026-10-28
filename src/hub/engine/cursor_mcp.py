"""Ф71-1: у сессии Cursor — те же MCP, что у Claude Code, и те же общие правила.

- Серверы уровня пользователя Claude Code (`~/.claude.json` → `mcpServers`) уходят в плагин сессии как есть:
  `stdio` (`command`, `args`, `env`) и `http`/`sse` с заголовками (`url`, `headers`). Пропускаются: серверы
  хаба (у сессии свой — с её личностью), `http` без заголовков (вход OAuth — его Cursor делает только для
  серверов из `~/.cursor/mcp.json`: `cursor-agent mcp login <имя>`) и имена, уже заведённые в
  `~/.cursor/mcp.json` (их Cursor поднимает сам).
- Общие правила владельца `~/.claude/CLAUDE.md` Claude Code читает сам, Cursor — нет: хост кладёт их в правило
  сессии первым блоком (`global_rules`).

Ф71-3 (слово владельца, паритет OpenCode): тот же набор серверов уходит и сессии хаба на OpenCode.
`claude_servers` — нейтральный список серверов Claude Code, `claude_user_servers` — его вид для Cursor,
`opencode_servers` — для OpenCode (`type: local` / `type: remote`; серверы без заголовков — OAuth, вход
OpenCode делает сам, URL серверов OAuth Cursor — из `~/.cursor/mcp.json`). `global_rules(engine="opencode")`
— блок правил для системного текста OpenCode: только `~/.claude/CLAUDE.md`, файлы `CLAUDE.md` проекта OpenCode
читает сам (от папки сессии вверх до корня репозитория — проба `.runs/f713-probe/`).

Разнос MCP по проектам — родной механизм Claude Code: `~/.claude.json` →
`projects[<папка>].disabledMcpServers`. Claude Code берёт ключ корня git-репозитория папки процесса
(проба: в подпапке репозитория сервер из списка корня выключен), хаб для Cursor и OpenCode — объединение
списков ключей папки проекта и папки процесса (`claude_disabled`). Серверы OAuth Cursor у Claude Code —
коннекторы claude.ai и плагины: имя сопоставляется с «claude.ai <имя>» и таблицей `CLAUDE_AI_ALIASES`."""

import json
import logging
import os
import re
from pathlib import Path

_logger = logging.getLogger("hub.work")

# Ф71-3: имена серверов `~/.cursor/mcp.json`, у которых в Claude Code другое имя (сверено 19.10 с
# `claudeAiMcpEverConnected` и `disabledMcpServers` в `~/.claude.json`); прочие — «claude.ai <имя>»
# или то же имя.
CLAUDE_AI_ALIASES: dict[str, tuple[str, ...]] = {
    "notion": ("claude.ai Notion - Initech",),
    "atlassian": ("plugin:atlassian:atlassian",),
}

GLOBAL_RULES_MAX = 64_000  # символов; длиннее — обрезается с пометкой


def _load(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        _logger.warning("work: Cursor — не прочитан %s (%s)", path, exc.__class__.__name__)
        return {}
    return data if isinstance(data, dict) else {}


def _servers(data: dict) -> dict:
    servers = data.get("mcpServers")
    return servers if isinstance(servers, dict) else {}


def claude_disabled(home: Path, workdir: str | None = None, cwd: str | None = None) -> set[str]:
    """Имена из `disabledMcpServers` ключей папки проекта и папки процесса в `~/.claude.json`
    (объединение)."""
    projects = _load(Path(home) / ".claude.json").get("projects")
    if not isinstance(projects, dict):
        return set()
    names: set[str] = set()
    for folder in {f for f in (workdir, cwd) if f}:
        entry = projects.get(os.path.realpath(folder))
        listed = entry.get("disabledMcpServers") if isinstance(entry, dict) else None
        if isinstance(listed, list):
            names |= {n for n in listed if isinstance(n, str) and n}
    return names


def oauth_disabled(name: str, disabled: set[str]) -> bool:
    """Сервер OAuth Cursor `name` выключен в Claude Code: в списке его имя, «claude.ai <имя>» или имя из
    `CLAUDE_AI_ALIASES` (без учёта регистра)."""
    if not disabled:
        return False
    wanted = {name, f"claude.ai {name}", *CLAUDE_AI_ALIASES.get(name, ())}
    folded = {n.casefold() for n in disabled}
    return any(w.casefold() in folded for w in wanted)


def _claude_raw(home: Path, workdir: str | None) -> dict:
    """Записи серверов Claude Code: уровня пользователя (`~/.claude.json`), общие проекта
    (`<папка>/.mcp.json`) и локальные проекта (`~/.claude.json` → `projects[<папка>]`); при совпадении имени
    выигрывает более узкий."""
    claude = _load(Path(home) / ".claude.json")
    servers = dict(_servers(claude))
    if workdir:
        servers |= _servers(_load(Path(workdir) / ".mcp.json"))
        local = (claude.get("projects") or {}).get(os.path.realpath(workdir)) or {}
        servers |= _servers(local) if isinstance(local, dict) else {}
    return servers


def claude_servers(home: Path, workdir: str | None = None, cwd: str | None = None) -> dict[str, dict]:
    """Нейтральный список серверов MCP Claude Code (Ф71-3): `{"kind": "stdio", "command", "args", "env"}` или
    `{"kind": "http", "url", "headers"}` (`headers` пустые — сервер со входом OAuth). Серверы хаба (`hub*`) не
    берутся: у сессии свой, с её личностью. Выключенные для папки (`claude_disabled`) и записи без команды или
    адреса пропускаются."""
    disabled = claude_disabled(home, workdir, cwd)
    result: dict[str, dict] = {}
    for name, spec in _claude_raw(home, workdir).items():
        if not isinstance(name, str) or not isinstance(spec, dict) or name.lower().startswith("hub"):
            continue
        if name in disabled:
            continue
        kind = spec.get("type") or ("stdio" if spec.get("command") else None)
        if kind == "stdio" and isinstance(spec.get("command"), str):
            env = spec.get("env")
            result[name] = {
                "kind": "stdio",
                "command": spec["command"],
                "args": [str(a) for a in spec.get("args") or []],
                "env": {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {},
            }
        elif kind in ("http", "sse") and isinstance(spec.get("url"), str):
            headers = spec.get("headers")
            result[name] = {
                "kind": "http",
                "url": spec["url"],
                "headers": {str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {},
            }
    return result


def cursor_oauth_servers(home: Path, workdir: str | None = None, cwd: str | None = None) -> dict[str, str]:
    """Серверы со входом OAuth из `~/.cursor/mcp.json` и `<папка>/.cursor/mcp.json`: `{имя: url}` (у Claude
    Code это коннекторы claude.ai и плагины — их адресов в его файлах нет). Серверы хаба и выключенные в
    Claude Code для папки (`oauth_disabled`) не берутся."""
    servers = dict(_servers(_load(Path(home) / ".cursor" / "mcp.json")))
    if workdir:
        servers |= _servers(_load(Path(workdir) / ".cursor" / "mcp.json"))
    disabled = claude_disabled(home, workdir, cwd)
    return {
        name: spec["url"]
        for name, spec in servers.items()
        if isinstance(name, str)
        and not name.lower().startswith("hub")
        and isinstance(spec, dict)
        and isinstance(spec.get("url"), str)
        and not oauth_disabled(name, disabled)
    }


def claude_user_servers(home: Path, workdir: str | None = None, cwd: str | None = None) -> dict[str, dict]:
    """Серверы MCP Claude Code в виде `mcpServers` Cursor (см. докстринг модуля). Пропускаются `http` без
    заголовков (OAuth) и имена из `~/.cursor/mcp.json` и `<папка>/.cursor/mcp.json` — их Cursor поднимает
    сам."""
    own_names = set(_servers(_load(Path(home) / ".cursor" / "mcp.json")))
    if workdir:
        own_names |= set(_servers(_load(Path(workdir) / ".cursor" / "mcp.json")))
    result: dict[str, dict] = {}
    for name, spec in claude_servers(home, workdir, cwd).items():
        if name in own_names:
            continue
        if spec["kind"] == "stdio":
            entry: dict = {"command": spec["command"], "args": spec["args"]}
            if spec["env"]:
                entry["env"] = spec["env"]
            result[name] = entry
        elif spec["headers"]:  # OAuth: вход — только у серверов `~/.cursor/mcp.json`
            result[name] = {"url": spec["url"], "headers": spec["headers"]}
    return result


def opencode_servers(home: Path, workdir: str | None = None, cwd: str | None = None) -> dict[str, dict]:
    """Ф71-3: те же серверы в формате конфига OpenCode — `{"type": "local", "command": [команда, *аргументы],
    "environment"}` и `{"type": "remote", "url", "headers"}`. Сервер без заголовков — `remote` без них: вход
    OAuth OpenCode делает сам (`opencode mcp auth <имя>`, статус `needs_auth`). Серверы OAuth Cursor
    (`cursor_oauth_servers`) добавляются, если такого имени у Claude Code нет."""
    result: dict[str, dict] = {}
    for name, spec in claude_servers(home, workdir, cwd).items():
        if spec["kind"] == "stdio":
            entry: dict = {"type": "local", "command": [spec["command"], *spec["args"]], "enabled": True}
            if spec["env"]:
                entry["environment"] = spec["env"]
        else:
            entry = {"type": "remote", "url": spec["url"], "enabled": True}
            if spec["headers"]:
                entry["headers"] = spec["headers"]
        result[name] = entry
    for name, url in cursor_oauth_servers(home, workdir, cwd).items():
        result.setdefault(name, {"type": "remote", "url": url, "enabled": True})
    return result


def codex_servers(home: Path, workdir: str | None = None, cwd: str | None = None) -> dict[str, dict]:
    """Ф74-5: те же серверы в формате `mcp_servers` Codex — `{"command", "args", "env"}` и
    `{"url", "http_headers"}`. Сервер без заголовков — с одним адресом: вход OAuth Codex делает сам
    (`codex mcp login <имя>`; вход хранится по имени и адресу — общий с окнами Codex владельца). Серверы OAuth
    Cursor (`cursor_oauth_servers`) добавляются, если такого имени у Claude Code нет — как у OpenCode."""
    result: dict[str, dict] = {}
    for name, spec in claude_servers(home, workdir, cwd).items():
        if spec["kind"] == "stdio":
            entry: dict = {"command": spec["command"], "args": list(spec["args"])}
            if spec["env"]:
                entry["env"] = dict(spec["env"])
        else:
            entry = {"url": spec["url"]}
            if spec["headers"]:
                entry["http_headers"] = dict(spec["headers"])
        result[name] = entry
    for name, url in cursor_oauth_servers(home, workdir, cwd).items():
        result.setdefault(name, {"url": url})
    return result


def codex_config_mcp_names(home: Path, folders) -> set[str]:
    """Ф74-5: имена серверов `[mcp_servers.*]` из конфигов Codex владельца (`~/.codex/config.toml`) и проекта
    (`<папка>/.codex/config.toml`) — сессия хаба их выключает (набор серверов — как у Claude Code). Читаются
    только имена; нечитаемый файл пропускается."""
    import tomllib

    paths = [Path(home) / ".codex" / "config.toml"]
    for folder in folders:
        if folder:
            paths.append(Path(folder) / ".codex" / "config.toml")
    names: set[str] = set()
    for path in paths:
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except OSError, ValueError, UnicodeDecodeError:
            continue
        servers = data.get("mcp_servers")
        if isinstance(servers, dict):
            names |= {str(k) for k in servers}
    return names


_HEAD_COMMON = (
    "Общие правила владельца — те же, что у {others}; это истина для всех моделей. "
    "Где они расходятся с твоими привычками — следуй им. Исключение — подагенты: у каждого движка свои. "
    "Раздел «Модели агентов» и имена агентов Claude Code (implementer, code-reviewer, checker и др.) — "
    "про Claude Code. "
)
_HEAD_ENGINE = {
    "cursor": (
        "Ты работаешь в Cursor: подагенты — встроенные типы Cursor на модели твоей сессии; "
        "параметр model у подагента не задавай (допустим только Grok 4.7: подписка Cursor)."
    ),
    # Слово владельца: сессия хаба на OpenCode — полноценная роль на любой модели; о «резерве» и конкретной
    # модели в шапке ни слова.
    "opencode": (
        "Ты работаешь в OpenCode: подагенты — встроенные помощники OpenCode (general, explore) на моделях, "
        "которые задал хаб; модель подагента не выбирай."
    ),
    # Ф74-5: `CLAUDE.md` проекта Codex читает сам (`project_doc_fallback_filenames`), сюда — только общий.
    "codex": (
        "Ты работаешь в Codex: подагенты (spawn_agent) — на модели твоей сессии; параметр model у подагента "
        "не задавай."
    ),
}
_OTHERS = {
    "cursor": "Claude Code и OpenCode",
    "opencode": "Claude Code и Cursor",
    "codex": "Claude Code, Cursor и OpenCode",
}


def global_rules(home: Path, workdir: str | None, cwd: str | None, engine: str = "cursor") -> str:
    """Блок общих правил для системного текста сессии: `~/.claude/CLAUDE.md`; у Cursor в подпапке ещё и
    `CLAUDE.md` проекта (Cursor читает только файл своей папки). OpenCode файлы `CLAUDE.md` проекта читает сам
    от папки сессии вверх (Ф71-3, проба) — ему только общий файл. Нет файлов — пустая строка."""
    parts = []
    sources = [Path(home) / ".claude" / "CLAUDE.md"]
    if engine == "cursor" and workdir and cwd and os.path.realpath(workdir) != os.path.realpath(cwd):
        sources.append(Path(workdir) / "CLAUDE.md")
    for path in sources:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if len(text) > GLOBAL_RULES_MAX:
            text = text[:GLOBAL_RULES_MAX] + "\n…(обрезано)"
        parts.append(f"===== {path} =====\n{text.strip()}")
    if not parts:
        return ""
    engine = engine if engine in _HEAD_ENGINE else "cursor"
    head = _HEAD_COMMON.format(others=_OTHERS[engine]) + _HEAD_ENGINE[engine]
    return head + "\n\n" + "\n\n".join(parts)


CODEX_DOC_NOTE = (
    "Сами правила Codex подаёт тебе из своего общего AGENTS.md ({path}) — здесь они не повторяются."
)


def codex_global_doc(home: Path, environ) -> Path | None:
    """Общий файл правил, который Codex подаёт сам: `$CODEX_HOME` (иначе `~/.codex`), `AGENTS.override.md`
    раньше `AGENTS.md` (порядок Codex). Нет ни одного — `None`."""
    base = Path(str((environ or {}).get("CODEX_HOME") or "") or Path(home) / ".codex")
    for name in ("AGENTS.override.md", "AGENTS.md"):
        path = base / name
        if path.is_file():
            return path
    return None


def codex_global_rules(home: Path, workdir: str | None, cwd: str | None, environ=None) -> str:
    """Ф74-5б: блок общих правил сессии Codex. Общий `AGENTS.md` Codex (`codex_global_doc`) уже содержит
    текст `~/.claude/CLAUDE.md` (сисадмин кладёт его туда) — текст правил не повторяется, остаётся шапка
    (подагенты, имена агентов Claude Code) со ссылкой на файл; иначе — как `global_rules`."""
    full = global_rules(home, workdir, cwd, engine="codex")
    doc = codex_global_doc(home, environ)
    if not full or doc is None:
        return full
    try:
        claude = (Path(home) / ".claude" / "CLAUDE.md").read_text(encoding="utf-8").strip()
        agents = doc.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return full
    if not claude or claude not in agents:
        return full
    return _head_only("codex", CODEX_DOC_NOTE.format(path=doc))


OPENCODE_DOC_NOTE = (
    "Сами правила OpenCode подаёт тебе из `instructions` своего общего конфига ({path}) — здесь они "
    "не повторяются."
)


def _head_only(engine: str, note: str) -> str:
    return _HEAD_COMMON.format(others=_OTHERS[engine]) + _HEAD_ENGINE[engine] + "\n\n" + note


def opencode_global_rules(home: Path, workdir: str | None, cwd: str | None, environ=None) -> str:
    """Ф74-5б (проба `.runs/f745b-probe/probe_oc.py`): сессия хаба на OpenCode (свои `OPENCODE_CONFIG` и
    `OPENCODE_CONFIG_DIR`) всё равно берёт `instructions` общего конфига владельца (`$XDG_CONFIG_HOME/opencode`,
    иначе `~/.config/opencode`), и свой конфиг хаба их не снимает (`instructions: []` склеивается). Если там
    назван `~/.claude/CLAUDE.md` — текст правил хаб не повторяет (шапка остаётся); иначе — как
    `global_rules`."""
    import fnmatch

    from . import oc_config

    full = global_rules(home, workdir, cwd, engine="opencode")
    if not full:
        return full
    claude = os.path.realpath(Path(home) / ".claude" / "CLAUDE.md")
    xdg = str((environ or {}).get("XDG_CONFIG_HOME") or "") or str(Path(home) / ".config")
    for item in oc_config.config_instructions(Path(xdg) / "opencode"):
        path = str(Path(home) / item[2:]) if item.startswith("~/") else item
        if not os.path.isabs(path):
            continue  # относительные OpenCode ищет от папки сессии — не общий файл
        if os.path.realpath(path) == claude or (
            any(ch in path for ch in "*?[") and fnmatch.fnmatch(claude, path)
        ):
            return _head_only("opencode", OPENCODE_DOC_NOTE.format(path=item))
    return full


def workspace_slug(path: str) -> str:
    """Имя каталога Cursor для папки: `/home/owner/Hooli` -> `home-owner-Hooli` (не буквы и не цифры —
    дефис, подряд — один, по краям — нет; сверено с `~/.cursor/projects` 19.10)."""
    return re.sub(r"[^A-Za-z0-9]+", "-", path).strip("-")


def project_root(path: str) -> str:
    """Корень проекта по правилу Cursor (и Claude Code): корень git-репозитория папки, иначе сама папка."""
    start = os.path.realpath(path)
    current = start
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return start
        current = parent


def sync_disabled(home: Path, workdir: str | None, cwd: str | None) -> list[str]:
    """Ф71-3: серверы OAuth Cursor (`~/.cursor/mcp.json`, `<папка>/.cursor/mcp.json`) выключаются по тем же
    `disabledMcpServers` Claude Code, что у OpenCode (`oauth_disabled`). Механизм Cursor — список
    идентификаторов (у этих серверов — имя) в `~/.cursor/projects/<слаг корня git папки>/mcp-disabled.json`
    (тот же файл пишет `cursor-agent mcp disable`; проба `.runs/f713-probe/`: из подпапки репозитория
    действует список корня). Хаб правит в нём только имена серверов OAuth Cursor: выключенные в Claude Code
    добавляет, прочие убирает; чужие записи остаются. Возвращает выключенные имена."""
    folder = cwd or workdir
    if not folder:
        return []
    names = set(_servers(_load(Path(home) / ".cursor" / "mcp.json")))
    if workdir:
        names |= set(_servers(_load(Path(workdir) / ".cursor" / "mcp.json")))
    names = {n for n in names if isinstance(n, str) and not n.lower().startswith("hub")}
    disabled = claude_disabled(home, workdir, cwd)
    wanted = sorted(n for n in names if oauth_disabled(n, disabled))
    path = Path(home) / ".cursor" / "projects" / workspace_slug(project_root(folder)) / "mcp-disabled.json"
    try:
        with open(path, encoding="utf-8") as f:
            current = json.load(f)
    except FileNotFoundError:
        current = []
    except (OSError, ValueError) as exc:
        _logger.warning("work: Cursor — не прочитан %s (%s)", path, exc.__class__.__name__)
        current = []
    if not isinstance(current, list):
        current = []
    keep = [e for e in current if isinstance(e, str) and e not in names]
    result = keep + wanted
    if result != current and (current or wanted):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    return wanted


def share_auth(home: Path, workspace: str) -> list[str]:
    """Слово владельца (MCP одинаковые везде): вход OAuth `cursor-agent mcp login` Cursor хранит ПО ПАПКЕ —
    `~/.cursor/projects/<слаг>/mcp-auth.json`, а владелец входит один раз. Перед запуском сессии в её папку
    переносится запись сервера (`tokens` + `clientInfo`) из самого свежего файла, где у сервера есть `tokens`,
    если свой файл папки не свежее. Значения не читаются и не пишутся в журнал. Возвращает имена
    перенесённых серверов."""
    projects = Path(home) / ".cursor" / "projects"
    # Ф71-3: Cursor читает вход из папки КОРНЯ git (как `mcp-disabled.json`) — у сессии в подпапке тоже.
    dest = projects / workspace_slug(project_root(workspace)) / "mcp-auth.json"
    best: dict[str, tuple[float, Path, dict]] = {}
    for path in projects.glob("*/mcp-auth.json"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        for name, entry in _load(path).items():
            if (
                isinstance(entry, dict)
                and entry.get("tokens")
                and (name not in best or mtime > best[name][0])
            ):
                best[name] = (mtime, path, entry)
    if not best:
        return []
    own = _load(dest)
    moved = []
    for name, (_mtime, path, entry) in sorted(best.items()):
        if path == dest:
            continue
        own[name] = {k: entry[k] for k in ("tokens", "clientInfo") if k in entry} | {
            k: v for k, v in (own.get(name) or {}).items() if k not in ("tokens", "clientInfo")
        }
        moved.append(name)
    if moved:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(own, f, ensure_ascii=False, indent=2)
        os.replace(tmp, dest)
    return moved
