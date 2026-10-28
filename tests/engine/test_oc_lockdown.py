"""Ревью Ф16 (Р70+): агенты приватного сервера OpenCode без запросов разрешений выключаются.

`oc_config`: встроенные `build`/`plan`/`general`/`explore` выключены, если конфиг их не задаёт; права `task`
рабочей сессии — вложенным словарём (по правилу на шаблон, проба 16-6); `OcServer.start_locked` выключает
агентов проекта перезапуском сервера. Сервер — поддельный (`fake_opencode.py`)."""

import os

import pytest

from hub.engine import oc_config
from hub.engine.oc_server import EngineStartError, OcServer

ALLOW = {"permission": "*", "pattern": "*", "action": "allow"}
EXT = {"permission": "external_directory", "pattern": "*", "action": "ask"}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
        return f.read().rsplit(")", 1)[1].split()[0] != "Z"


def test_build_disables_open_builtins_but_keeps_own():
    data = oc_config.build("work", agent_name="hub-orch", permission=oc_config.PERM_ORCH)
    for name in oc_config.BUILTIN_OPEN_AGENTS:
        assert data["agent"][name] == {"disable": True}, name
    sub = {"permission": {"*": "ask"}}
    data = oc_config.build(
        "work",
        agent_name="hub-work",
        permission=oc_config.PERM_WORK,
        subagents={"general": sub, "explore": sub},
    )
    assert data["agent"]["general"] == data["agent"]["explore"] == sub
    assert data["agent"]["build"] == data["agent"]["plan"] == {"disable": True}
    assert data["agent"]["hub-work"]["permission"] == oc_config.PERM_WORK


def test_permission_rules_nested_and_check_agent():
    rules = oc_config.permission_rules(oc_config.PERM_WORK)
    assert rules == [
        {"permission": "*", "pattern": "*", "action": "ask"},
        {"permission": "task", "pattern": "*", "action": "deny"},
        {"permission": "task", "pattern": "general", "action": "ask"},
        {"permission": "task", "pattern": "explore", "action": "ask"},
    ]
    agents = [{"name": "hub-work", "permission": [ALLOW, *rules, EXT]}]
    assert oc_config.check_agent(agents, "hub-work", oc_config.PERM_WORK) is None
    # сервер не применил запрет `task` — расхождение видно
    flat = [{"name": "hub-work", "permission": [ALLOW, rules[0], EXT]}]
    assert oc_config.check_agent(flat, "hub-work", oc_config.PERM_WORK) is not None
    # правило агента `.md` после наших (так 1.18.31 ставит frontmatter) — чужие правила
    late = [{"name": "hub-work", "permission": [ALLOW, *rules, {**ALLOW, "permission": "bash"}]}]
    assert oc_config.check_agent(late, "hub-work", oc_config.PERM_WORK) is not None


def test_unlocked_agents_and_lock():
    deny = {"permission": "*", "pattern": "*", "action": "deny"}
    agents = [
        {"name": "hub-work", "permission": [ALLOW]},  # наш — не трогаем
        {"name": "code-explorer", "permission": [ALLOW, EXT]},
        {"name": "compaction", "permission": [ALLOW, deny, EXT]},  # служебный OpenCode — заперт
        {"name": "md-deny-then-allow", "permission": [ALLOW, deny, {**ALLOW, "permission": "bash"}]},
        {"name": "asker", "permission": [ALLOW, {**deny, "action": "ask"}]},
    ]
    config = {"agent": {"hub-work": {"permission": {"*": "ask"}}}}
    names = oc_config.unlocked_agents(agents, config)
    assert names == ["asker", "code-explorer", "md-deny-then-allow"]
    locked = oc_config.lock_agents(config, names)
    assert all(locked["agent"][n] == {"disable": True} for n in names)
    assert "code-explorer" not in config["agent"]  # исходный конфиг не тронут


async def test_start_locked_without_foreign_agents_starts_once(fake_oc):
    oc_config.write(fake_oc.config_path, fake_oc.config())
    server = OcServer(str(fake_oc.binary), log_file=fake_oc.tmp / "logs" / "l.log")
    fake_oc.servers.append(server)
    agents = await server.start_locked(str(fake_oc.workdir), fake_oc.env(), fake_oc.config_path)
    assert [a["name"] for a in agents] == ["hub-test"]
    assert len(fake_oc.jsonl("starts.jsonl")) == 1


async def test_start_locked_disables_project_agents_with_restart(fake_oc):
    fake_oc.scenario(
        extra_agents={
            "code-explorer": {"mode": "all"},
            "locked-md": {"mode": "subagent", "own_permission": {"*": "deny"}},
        }
    )
    oc_config.write(fake_oc.config_path, fake_oc.config())
    server = OcServer(str(fake_oc.binary), log_file=fake_oc.tmp / "logs" / "l.log")
    fake_oc.servers.append(server)
    agents = await server.start_locked(str(fake_oc.workdir), fake_oc.env(), fake_oc.config_path)
    names = {a["name"] for a in agents}
    assert "code-explorer" not in names and "locked-md" in names
    starts = fake_oc.jsonl("starts.jsonl")
    assert len(starts) == 2 and not _alive(starts[0]["pid"]) and _alive(starts[1]["pid"])
    config = fake_oc.json("config.json")
    assert config["agent"]["code-explorer"] == {"disable": True}
    assert "locked-md" not in config["agent"]
    assert server.proc is not None and server.proc.pid == starts[1]["pid"]


async def test_start_locked_fails_when_agent_stays_open(fake_oc):
    fake_oc.scenario(extra_agents={"stubborn": {"mode": "all", "sticky": True}})
    oc_config.write(fake_oc.config_path, fake_oc.config())
    server = OcServer(str(fake_oc.binary), log_file=fake_oc.tmp / "logs" / "l.log")
    fake_oc.servers.append(server)
    with pytest.raises(EngineStartError, match="stubborn"):
        await server.start_locked(str(fake_oc.workdir), fake_oc.env(), fake_oc.config_path)
    starts = fake_oc.jsonl("starts.jsonl")
    assert len(starts) == 2 and not any(_alive(s["pid"]) for s in starts)
