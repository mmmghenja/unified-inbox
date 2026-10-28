"""`hub.engine.oc_config`: конфиг приватного сервера OpenCode (Ф16, 3.3, П5, П8, П9)."""

import json
import stat
from pathlib import Path

from hub.engine import oc_config

ROOT = Path(__file__).resolve().parents[2]


def test_build_disables_every_mcp_and_sets_role_model():
    hub = oc_config.hub_mcp(
        hub="/opt/hub",
        profile="wake-3",
        url="http://127.0.0.1:9/api/v1",
        config_dir=Path("/c"),
        data_dir=Path("/d"),
    )
    data = oc_config.build(
        "work",
        agent_name="hub-wake",
        permission=oc_config.PERM_ASK_ALL,
        mcp_hub=hub,
        disable_mcp=["playwright", "globex-gitlab", "hub"],
        overrides={"disable_mcp": ["searxng"]},
        extra_agent={"steps": 12},
    )
    assert data["$schema"] == oc_config.SCHEMA
    assert data["autoupdate"] is False and data["share"] == "disabled"
    mcp = data["mcp"]
    for name in ("playwright", "globex-gitlab", "searxng"):
        assert mcp[name] == {"enabled": False}
    # Имя `hub` в списке выключаемых не глушит собственный MCP хаба.
    assert mcp["hub"] == {
        "type": "local",
        "command": ["/opt/hub", "mcp", "--profile", "wake-3", "--url", "http://127.0.0.1:9/api/v1"],
        "environment": {"HUB_CONFIG_DIR": "/c", "HUB_DATA_DIR": "/d"},
        "enabled": True,
    }
    agent = data["agent"]["hub-wake"]
    assert agent["model"] == "deepseek/deepseek-flash" and agent["variant"] == "max"
    assert agent["mode"] == "primary"
    assert agent["permission"] == {"*": "ask"}
    assert agent["steps"] == 12
    assert "plugin" not in data


def test_build_without_hub_disables_hub_name_too():
    data = oc_config.build("work", agent_name="a", permission=oc_config.PERM_ORCH, disable_mcp=["hub"])
    assert data["mcp"] == {"hub": {"enabled": False}}


def test_build_role_override_permission_and_plugin_and_subagents(tmp_path):
    sub = oc_config.agent_entry("explore", permission=oc_config.PERM_ASK_ALL, mode="subagent")
    data = oc_config.build(
        "work",
        agent_name="hub-work",
        permission=oc_config.PERM_ASK_ALL,
        overrides={"roles": {"work": {"model": "xai/grok-4.7", "variant": "high"}}},
        plugins=[oc_config.plugin_url(ROOT)],
        subagents={"general": sub, "explore": sub},
    )
    assert data["agent"]["hub-work"]["model"] == "xai/grok-4.7"
    assert data["agent"]["hub-work"]["variant"] == "high"
    assert data["agent"]["explore"]["permission"] == {"*": "ask"}
    assert data["agent"]["general"]["mode"] == "subagent"
    [plugin] = data["plugin"]
    assert plugin.startswith("file:///") and plugin.endswith("/deploy/opencode/hub-private.ts")
    source = (ROOT / "deploy" / "opencode" / "hub-private.ts").read_text(encoding="utf-8")
    assert "delete process.env.OPENCODE_SERVER_PASSWORD" in source


def test_wake_and_orch_permissions_deny_rest():
    # П8 + П5: `*` — первым, `task`/`question` у дежурной и оркестратора закрыты общим deny.
    assert list(oc_config.PERM_WAKE)[0] == "*" and oc_config.PERM_WAKE["*"] == "deny"
    assert oc_config.PERM_WAKE["hub_*"] == "ask" and "task" not in oc_config.PERM_WAKE
    assert oc_config.PERM_ORCH == {"*": "deny", "hub_*": "ask"}


def test_write_is_atomic_0600(tmp_path):
    path = tmp_path / "engine" / "wake-1.json"
    data = oc_config.build("work", agent_name="hub-wake", permission=oc_config.PERM_ASK_ALL)
    oc_config.write(path, data)
    assert json.loads(path.read_text(encoding="utf-8")) == data
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    oc_config.write(path, {**data, "share": "manual"})
    assert json.loads(path.read_text(encoding="utf-8"))["share"] == "manual"
    assert [p.name for p in path.parent.iterdir()] == ["wake-1.json"]


def test_build_extra_mcp_replaces_global_and_overrides_win():
    hub = oc_config.hub_mcp(
        hub="/opt/hub", profile="all", url=None, config_dir=Path("/c"), data_dir=Path("/d")
    )
    assert hub["command"] == ["/opt/hub", "mcp", "--profile", "all"]
    extra = {"playwright": {"type": "local", "command": ["/bin/pw"], "enabled": True}, "hub-all": hub}
    data = oc_config.build(
        "work",
        agent_name="a",
        permission=oc_config.PERM_ASK_ALL,
        disable_mcp=["playwright", "hub-all", "globex-gitlab"],
        extra_mcp=extra,
        overrides={"disable_mcp": ["searxng"]},
    )
    assert data["mcp"] == {
        "playwright": {"type": "local", "command": ["/bin/pw"], "enabled": True},
        "hub-all": hub,
        "globex-gitlab": {"enabled": False},
        "searxng": {"enabled": False},
    }
    over = oc_config.build(
        "work",
        agent_name="a",
        permission=oc_config.PERM_ASK_ALL,
        extra_mcp=extra,
        overrides={"disable_mcp": ["playwright"]},
    )
    assert over["mcp"]["playwright"] == {"enabled": False}


def test_config_mcp_names_reads_jsonc(tmp_path):
    (tmp_path / "opencode.jsonc").write_text(
        '{\n // комментарий\n "mcp": {"a": {"url": "https://x//y"}, /* b */ "b": {},},\n}\n'
    )
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "opencode.json").write_text('{"mcp": {"c": {}}}')
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "opencode.json").write_text("{не json")
    assert oc_config.config_mcp_names(tmp_path, tmp_path / "proj", tmp_path / "broken", tmp_path / "нет") == [
        "a",
        "b",
        "c",
    ]
