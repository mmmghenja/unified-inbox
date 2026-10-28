"""Конфиг приватного сервера OpenCode (Ф16, 3.3, П5, П8, П9).

Приватный сервер хаба получает этот JSON через `OPENCODE_CONFIG`. Конфиг OpenCode СЛИВАЕТСЯ с глобальным
конфигом владельца и проектным (F2), поэтому всё, что не должно попасть в сессию хаба, выключается здесь явно:
глобальные MCP — по именам из `disable_mcp` (`{"enabled": false}`), автообновление и публикация сессий.

Права агента — параметр (`permission`): у дежурной и оркестратора лишнее режет сам OpenCode
(`"*": "deny"`), остальное — «ask» и решает драйвер хода (`oc_turn`) теми же функциями, что гарды Claude
(3.4). Готовые наборы — `PERM_WAKE`, `PERM_ORCH` (П8) и `PERM_ASK_ALL` (рабочие сессии). Порядок ключей
словаря сохраняется в JSON: общее правило `"*"` идёт первым, частные — после (какое правило побеждает при
совпадении нескольких — проба P6, кусок 16-5).
"""

import json
import re
from collections.abc import Iterable
from pathlib import Path

from hub.agent.files import atomic_write, private_dir

from .choice import role_model

SCHEMA = "https://opencode.ai/config.json"
# Имя MCP-сервера хаба в приватном сервере: инструменты станут `hub_hub_draft` и т. п. (3.3).
HUB_MCP = "hub"
PRIVATE_PLUGIN = Path("deploy") / "opencode" / "hub-private.ts"

PERM_ASK_ALL: dict[str, str] = {"*": "ask"}
# П8 + П5: `task` и `question` у дежурной и оркестратора закрыты общим `"*": "deny"`.
PERM_WAKE: dict[str, str] = {
    "*": "deny",
    "read": "ask",
    "grep": "ask",
    "glob": "ask",
    "list": "ask",
    f"{HUB_MCP}_*": "ask",
}
PERM_ORCH: dict[str, str] = {"*": "deny", f"{HUB_MCP}_*": "ask"}
# 16-6 (ревью Ф16): подагенты рабочей сессии — только `general`/`explore` (П5); агенты проекта
# (`.opencode/agents/*.md`) с правами OpenCode по умолчанию `"*": "allow"` запросов разрешений не дают — их
# `task` режет сам OpenCode, а хост отказывает ещё и в `_oc_decide`.
WORK_SUBAGENTS = ("general", "explore")
PERM_WORK: dict = {"*": "ask", "task": {"*": "deny", **{name: "ask" for name in WORK_SUBAGENTS}}}
# Встроенные агенты OpenCode с правами `"*": "allow"` (основные `build`/`plan`, помощники
# `general`/`explore`): на приватных серверах выключены, если конфиг не задаёт их сам (помощники рабочей
# сессии — `subagents`), — по паролю сервера нельзя создать сессию агентом без запросов разрешений (Ф16).
BUILTIN_OPEN_AGENTS = ("build", "plan", "general", "explore")
DISABLED = {"disable": True}


def hub_mcp(
    *,
    hub: str,
    profile: str,
    url: str | None,
    config_dir: Path,
    data_dir: Path,
    environment: dict[str, str] | None = None,
) -> dict:
    """Запись MCP хаба (`type: local`) — как `wake.claude.mcp_config`, но в формате OpenCode: команда списком,
    каталоги хаба явно (окружение сервера урезано). `url=None` — адрес ядра по умолчанию `hub mcp` (Ф71-3:
    сессия хаба, как у Claude Code и Cursor)."""
    env = {"HUB_CONFIG_DIR": str(config_dir), "HUB_DATA_DIR": str(data_dir)}
    if environment:
        env.update(environment)
    return {
        "type": "local",
        "command": [hub, "mcp", "--profile", profile, *(["--url", url] if url else [])],
        "environment": env,
        "enabled": True,
    }


_CONFIG_FILES = ("config.json", "opencode.json", "opencode.jsonc")


def _strip_jsonc(text: str) -> str:
    """JSONC -> JSON: комментарии `//` и `/* */` вне строк и висячие запятые убираются."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i : j + 1])
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(ch)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def config_instructions(*dirs: Path) -> list[str]:
    """Ф74-5б: записи `instructions` из файлов конфига OpenCode в каталогах `dirs` (как написаны: путь,
    `~/…` или шаблон). Битый или отсутствующий файл — пропуск."""
    out: list[str] = []
    for directory in dirs:
        for fname in _CONFIG_FILES:
            try:
                data = json.loads(_strip_jsonc((Path(directory) / fname).read_text(encoding="utf-8")))
            except OSError, ValueError:
                continue
            items = data.get("instructions") if isinstance(data, dict) else None
            if isinstance(items, list):
                out += [i for i in items if isinstance(i, str) and i and i not in out]
    return out


def config_mcp_names(*dirs: Path) -> list[str]:
    """Ф71-3: имена MCP из файлов конфига OpenCode в каталогах `dirs` (`config.json`, `opencode.json`,
    `opencode.jsonc`; глобальный `~/.config/opencode` и папка проекта). Читаются только имена; битый или
    отсутствующий файл — пропуск."""
    names: set[str] = set()
    for directory in dirs:
        for fname in _CONFIG_FILES:
            try:
                data = json.loads(_strip_jsonc((Path(directory) / fname).read_text(encoding="utf-8")))
            except OSError, ValueError:
                continue
            mcp = data.get("mcp") if isinstance(data, dict) else None
            if isinstance(mcp, dict):
                names |= {n for n in mcp if isinstance(n, str) and n}
    return sorted(names)


def plugin_url(tree: Path) -> str:
    """`file://` плагина `hub-private.ts` из дерева хаба (у прода — `~/projects/hub-prod`, П9)."""
    return (Path(tree).resolve() / PRIVATE_PLUGIN).as_uri()


def agent_entry(
    role: str,
    *,
    permission: dict,
    overrides: dict | None = None,
    description: str | None = None,
    mode: str = "primary",
    steps: int | None = None,
) -> dict:
    """Агент OpenCode роли: модель и `variant` — из `choice.role_model` (с переопределениями владельца), права —
    как переданы. `steps` — аналог `--max-turns` (F21)."""
    provider, model, variant = role_model(role, overrides)
    entry = {
        "description": description or f"hub: {role}",
        "mode": mode,
        "model": f"{provider}/{model}",
        "variant": variant,
        "permission": dict(permission),
    }
    if steps is not None:
        entry["steps"] = int(steps)
    return entry


def build(
    role: str,
    *,
    agent_name: str,
    permission: dict,
    mcp_hub: dict | None = None,
    disable_mcp: Iterable[str] = (),
    plugins: Iterable[str] = (),
    overrides: dict | None = None,
    subagents: dict[str, dict] | None = None,
    extra_agent: dict | None = None,
    description: str | None = None,
    extra_mcp: dict[str, dict] | None = None,
) -> dict:
    """Конфиг приватного сервера роли `role` с одним основным агентом `agent_name`.

    `mcp_hub` — запись `hub_mcp(...)` или `None` (без MCP хаба); `disable_mcp` — имена глобальных MCP владельца
    (из `engine.json`, П8) плюс, если передан, `overrides["disable_mcp"]`; `plugins` — `file://` плагинов
    (`plugin_url` — пароль сервера прячется от bash, П9); `subagents` — переопределения агентов-помощников
    (рабочие сессии: `general`/`explore` с правами «ask», П5); `extra_agent` — дополнительные поля основного
    агента (например, `steps`).

    Ф71-3: `extra_mcp` — серверы сессии сверх MCP хаба (набор Claude Code, `cursor_mcp.opencode_servers`;
    у сессии хаба там же и её MCP хаба под своим именем). Глобальное имя из `disable_mcp`, которое есть в
    `extra_mcp`, не выключается — запись сессии заменяет глобальную; выключатель
    `overrides["disable_mcp"]` действует поверх и `extra_mcp`."""
    extra = {name: dict(entry) for name, entry in (extra_mcp or {}).items() if isinstance(entry, dict)}
    disabled = [name for name in disable_mcp if isinstance(name, str) and name and name not in extra]
    extra_disabled = (overrides or {}).get("disable_mcp")
    if isinstance(extra_disabled, list):
        disabled += [name for name in extra_disabled if isinstance(name, str) and name]
    mcp: dict[str, dict] = dict(extra)
    for name in disabled:
        if name != HUB_MCP or mcp_hub is None:
            mcp[name] = {"enabled": False}
    if mcp_hub is not None:
        mcp[HUB_MCP] = dict(mcp_hub)

    agent = agent_entry(role, permission=permission, overrides=overrides, description=description)
    if extra_agent:
        agent.update(extra_agent)
    own = {agent_name, *(subagents or {})}
    agents: dict[str, dict] = {name: dict(DISABLED) for name in BUILTIN_OPEN_AGENTS if name not in own}
    for name, entry in (subagents or {}).items():
        if name != agent_name:
            agents[name] = dict(entry)
    agents[agent_name] = agent

    data: dict = {
        "$schema": SCHEMA,
        # Оба ключа сверены со схемой `Config` 1.18.31 (`.runs/oc-factcheck/openapi.json`): autoupdate —
        # boolean|"notify", share — "manual"|"auto"|"disabled".
        "autoupdate": False,
        "share": "disabled",
        "mcp": mcp,
        "agent": agents,
    }
    plugin_list = [p for p in plugins if isinstance(p, str) and p]
    if plugin_list:
        data["plugin"] = plugin_list
    return data


def permission_rules(permission: dict) -> list[dict]:
    """Права агента в том виде, в каком их отдаёт `GET /agent` 1.18.31 (правила по порядку; вложенный словарь
    `{"task": {"*": "deny", "general": "ask"}}` — по правилу на шаблон, проба 16-6)."""
    rules = []
    for key, value in permission.items():
        if isinstance(value, dict):
            rules += [{"permission": key, "pattern": p, "action": a} for p, a in value.items()]
        else:
            rules.append({"permission": key, "pattern": "*", "action": value})
    return rules


def _locked(rules) -> bool:
    """Агент заперт: есть правило `"*"`→`deny`, и после последнего такого — только `deny` (и служебные
    `external_directory` самого OpenCode)."""
    if not isinstance(rules, list):
        return False
    last = None
    for i, rule in enumerate(rules):
        if isinstance(rule, dict) and rule.get("permission") == "*" and rule.get("action") == "deny":
            last = i
    if last is None:
        return False
    return all(
        isinstance(r, dict) and (r.get("action") == "deny" or r.get("permission") == "external_directory")
        for r in rules[last + 1 :]
    )


def unlocked_agents(agents: list, config: dict) -> list[str]:
    """Агенты сервера (`GET /agent`), которых нет в нашем конфиге и которые не заперты правами, — агенты
    проекта и владельца (`.opencode/agents/*.md`, `~/.config/opencode/agents`) с правами по умолчанию. Их надо
    выключить (`lock_agents`) и перезапустить сервер (ревью Ф16). Выключенный конфигом агент, которого сервер
    всё ещё показывает, тоже здесь."""
    entries = (config.get("agent") or {}).items()
    ours = {n for n, e in entries if not (isinstance(e, dict) and e.get("disable"))}
    sealed = set(sealed_agents(config))
    return sorted(
        str(a["name"])
        for a in agents
        if isinstance(a, dict)
        and isinstance(a.get("name"), str)
        and (a["name"] not in ours or a["name"] in sealed)
        and not _locked(a.get("permission"))
    )


def sealed_agents(config: dict) -> list[str]:
    """Ревью 41-4 (4): агенты нашего конфига, оставленные включёнными только потому, что заперты целиком
    (права — одни `deny` с `"*"`: встроенный `compaction` постоянной сессии). Запертость обязана
    подтвердиться на сервере: открытый там (другая версия OpenCode, правило пользователя после нашего) —
    среди `unlocked_agents`."""
    names = []
    for name, entry in (config.get("agent") or {}).items():
        permission = entry.get("permission") if isinstance(entry, dict) else None
        if isinstance(entry, dict) and not entry.get("disable") and isinstance(permission, dict):
            rules = permission_rules(permission)
            if rules and all(r.get("action") == "deny" for r in rules) and _locked(rules):
                names.append(str(name))
    return sorted(names)


def lock_agents(config: dict, names: Iterable[str]) -> dict:
    """Копия конфига, где агенты `names` выключены (`disable: true` — агента нет ни в `GET /agent`, ни для
    `task`, ни для `prompt`; проба 16-6)."""
    data = json.loads(json.dumps(config))
    agents = data.setdefault("agent", {})
    for name in names:
        agents[name] = dict(DISABLED)
    return data


def check_agent(agents: list, name: str, permission: dict) -> str | None:
    """П8: права агента на сервере совпадают со сгенерированными. `GET /agent` отдаёт итоговый список правил
    (умолчания OpenCode, глобальный конфиг владельца, затем наши); побеждает последнее совпавшее (проба 16-3),
    поэтому наши правила должны идти подряд, а после них — разве что правила `external_directory` самого
    OpenCode. `None` — всё верно, иначе причина."""
    agent = next((a for a in agents if isinstance(a, dict) and a.get("name") == name), None)
    if agent is None:
        return f"агента {name} нет на сервере OpenCode"
    rules = agent.get("permission")
    if isinstance(rules, dict):  # другая версия OpenCode отдаёт права словарём
        return None if rules == permission else "права агента на сервере не совпали со сгенерированными"
    if not isinstance(rules, list):
        return "сервер OpenCode не показал права агента"
    ours = permission_rules(permission)
    n = len(ours)
    starts = [i for i in range(len(rules) - n + 1) if rules[i : i + n] == ours]
    if not starts:
        return "права агента на сервере не совпали со сгенерированными"
    tail = rules[starts[-1] + n :]
    if any(not isinstance(r, dict) or r.get("permission") != "external_directory" for r in tail):
        return "после прав хаба на сервере OpenCode стоят чужие правила"
    return None


def write(path: Path, data: dict) -> None:
    """Атомарно, 0600, каталог 0700 (как `wake.claude.launch_files`)."""
    path = Path(path)
    private_dir(path.parent)
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=1) + "\n")
