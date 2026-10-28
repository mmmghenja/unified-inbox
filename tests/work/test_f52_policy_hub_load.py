"""Ф52-1: вход `hub-load` не закрыт выключателем `hub_cli` — на ТЕКУЩИХ списках `policy.py`, у обоих движков.

Claude Code получает списки в `permissions.deny` настроек сессии (`policy.settings_config`), у OpenCode их
применяет хост через `policy.decide_tool`. Шаблоны `Bash(hub *)` и `Bash(*/.venv/bin/hub *)` закрывают
`hub …`; `hub-load …` под них не подпадает ни по имени, ни по полному пути. Обход `hub_cli` через обёртку
закрывает гард (`test_f52_guard_load.py`).
"""

import fnmatch

import pytest

from hub.work import policy

FLAGS = '--what "полный прогон" --cores 4 --mem-gb 6 --minutes 25'
HUB_LOAD = [
    "hub-load",
    "hub-load --json",
    f"hub-load run {FLAGS} -- uv run pytest -q -n 4",
    f"hub-load add --pid 4242 {FLAGS}",
    "hub-load done 3",
    "/home/owner/projects/hub-prod/.venv/bin/hub-load",
    f"/home/owner/projects/hub-prod/.venv/bin/hub-load run {FLAGS} -- npm run build",
    "/home/owner/projects/hub-prod/.venv/bin/hub-load done 3",
]
HUB = ["hub load", f"hub load run {FLAGS} -- ls", "hub status", "/srv/hub-prod/.venv/bin/hub load"]


def _settings(free=()):
    return policy.settings_config(ask=[], guard_command="hub-work-guard", free=free)


def _bash_patterns(rules):
    return [rule[len("Bash(") : -1] for rule in rules if rule.startswith("Bash(") and rule.endswith(")")]


def test_hub_cli_switch_patterns_are_the_expected_ones():
    assert policy.PERMISSION_RULES["hub_cli"][0] == ("Bash(hub *)", "Bash(*/.venv/bin/hub *)")


@pytest.mark.parametrize("command", HUB_LOAD)
def test_hub_load_is_not_caught_by_session_settings(command):
    """Claude Code: ни один шаблон Bash из `permissions.deny`/`ask` настроек сессии команду не ловит."""
    permissions = _settings()["permissions"]
    patterns = _bash_patterns([*permissions["deny"], *permissions["ask"]])
    assert "hub *" in patterns and "*/.venv/bin/hub *" in patterns
    for pattern in patterns:
        assert not fnmatch.fnmatchcase(command, pattern), (pattern, command)


@pytest.mark.parametrize("command", HUB_LOAD)
def test_hub_load_is_not_caught_by_decide_tool(command):
    """OpenCode: хост решает `policy.decide_tool` по тем же настройкам — про `hub-load` ничего не сказано."""
    assert policy.decide_tool("Bash", _settings(), {"command": command}) is None


@pytest.mark.parametrize("command", HUB)
def test_hub_itself_stays_denied(command):
    """Сам `hub …` выключатель закрывает по-прежнему; снят выключатель — открыт."""
    assert policy.decide_tool("Bash", _settings(), {"command": command}) == "deny"
    assert policy.decide_tool("Bash", _settings(free={"hub_cli"}), {"command": command}) is None
