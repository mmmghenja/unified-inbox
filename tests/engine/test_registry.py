"""Ф74-1 (docs/architecture.md): профиль движка — значения ровно те, что были в коде до
профиля (таблица снята с прежних констант), и списки потребителей из него совпадают с прежними."""

import pytest

from hub.core import handoff, held
from hub.engine import registry
from hub.mcp_server import server
from hub.work import files, runner_base
from hub.work import held as work_held

# Прежние значения (до Ф74-1), по месту в коде.
OLD_LABELS = {"claude": "Claude Code", "opencode": "OpenCode", "cursor": "Cursor"}
OLD_HELD_ENGINES = ("claude", "opencode", "cursor")  # core/held.py ENGINES
OLD_MID = ("claude", "cursor")  # work/held.py MID_ENGINES
OLD_BINARIES = ("claude", "opencode", "cursor-agent", "agent")  # work/held.py ENGINE_BINARIES
OLD_HOST_DECIDES = {"opencode", "cursor"}  # HeldRunner.host_decides
OLD_COMPACTS_SELF = {"cursor"}  # HeldRunner.compacts_self
OLD_OWN_LIMIT = {"opencode": "held_max_parallel_opencode", "cursor": "held_max_parallel_cursor"}  # _slot
OLD_SWITCH = {"opencode": "held_opencode", "cursor": "held_cursor"}  # _launch
# Интерфейс: ENGINE_SHORT, SESSION_EFFORTS_BY_ENGINE, SESSION_MODEL_HINTS.
OLD_SHORT = {"claude": "Claude", "opencode": "OpenCode", "cursor": "Cursor"}
OLD_EFFORTS = {
    "claude": ("low", "medium", "high", "xhigh", "max"),
    "opencode": ("low", "high", "max"),
    "cursor": ("high", "xhigh"),
}
OLD_HINTS = {
    "claude": ("sonnet", "opus", "haiku"),
    "opencode": ("deepseek/deepseek-flash", "anthropic/claude-sonnet-5-5"),
    "cursor": ("grok-4.7", "claude-sonnet-5-5", "claude-opus-5-5"),
}


@pytest.mark.parametrize("name", OLD_HELD_ENGINES)
def test_profile_matches_old_values(name):
    p = registry.BY_NAME[name]
    assert p.available
    assert p.label == OLD_LABELS[name]
    assert p.short == OLD_SHORT[name]
    assert p.mid_turn == (name in OLD_MID)
    assert p.host_decides == (name in OLD_HOST_DECIDES)
    assert p.compacts_self == (name in OLD_COMPACTS_SELF)
    assert p.context_known == (name != "cursor")  # у Cursor `usage.context_limit` пуст (Ф71-1)
    assert p.efforts == OLD_EFFORTS[name]
    assert p.model_hints == OLD_HINTS[name]
    assert p.max_parallel_key == OLD_OWN_LIMIT.get(name)
    assert p.enabled_key == OLD_SWITCH.get(name)


def test_derived_lists_equal_old():
    # Ф74-5: Codex включён — добавлен в конец списков, прежние значения не тронуты.
    assert registry.HELD_ENGINES == (*OLD_HELD_ENGINES, "codex")
    assert held.ENGINES == (*OLD_HELD_ENGINES, "codex")
    assert held.ENGINES[0] == "claude"  # умолчание заявки на напарника (`held_mod.ENGINES[0]`)
    assert work_held.MID_ENGINES == (*OLD_MID, "codex")
    assert work_held.ENGINE_BINARIES == (*OLD_BINARIES, "codex")
    for labels in (handoff.ENGINE_NAMES, runner_base.ENGINE_NAMES, server.ENGINE_LABEL):
        assert {k: labels[k] for k in OLD_LABELS} == OLD_LABELS
    assert work_held.OPENCODE_OFF == "OpenCode для постоянных сессий пока выключен"
    assert work_held.CURSOR_OFF == "Cursor для постоянных сессий пока выключен"


def test_host_keys_exist_in_default_config():
    for p in registry.PROFILES:
        if not p.available:
            continue
        for key in (p.enabled_key, p.max_parallel_key):
            if key is not None:
                assert key in files.DEFAULT_CONFIG, key


def test_codex_is_held():
    """Ф74-5: сессии хаба на Codex создаются; `codex` — в уборке сирот и в запрете вложенного запуска."""
    codex = registry.BY_NAME["codex"]
    assert codex.label == "Codex"
    assert codex.available and codex.mid_turn and codex.host_decides and codex.context_known
    assert not codex.compacts_self
    assert codex.pins_system and registry.BY_NAME["claude"].pins_system
    assert not registry.BY_NAME["opencode"].pins_system and not registry.BY_NAME["cursor"].pins_system
    assert codex.dead_switches == ("cross_session", "plan_mode")
    assert registry.held("codex") is codex
    assert "codex" in registry.BINARIES
    assert registry.check_held("codex") == "codex"
    with pytest.raises(ValueError, match="claude или opencode или cursor или codex"):
        registry.check_held("nope")


def test_label_fallback():
    assert registry.label("opencode") == "OpenCode"
    assert registry.label("nope") == "nope"
    assert registry.label(None, "Claude Code") == "Claude Code"


def test_status_profiles_shape():
    rows = registry.status_profiles()
    assert [r["name"] for r in rows] == ["claude", "opencode", "cursor", "codex"]
    assert set(rows[0]) == {
        "name",
        "label",
        "short",
        "efforts",
        "model_hints",
        "compacts_self",
        "context_known",
        "mid_turn",
        "available",
        "dead_switches",
        "asks_questions",
        "mid_turn_tool_only",
        "mcp_statuses",
    }
    by = {r["name"]: r for r in rows}
    assert by["cursor"]["efforts"] == ["high", "xhigh"]
    assert by["cursor"]["mid_turn"] is True
    # Ф74-8 (пробы `.runs/f748-probe/`): у Cursor в `-p` нет вопроса с вариантами и режима плана, реплика
    # посреди хода входит только на вызове инструмента, состояний серверов MCP Cursor не даёт.
    assert by["cursor"]["dead_switches"] == ["cross_session", "plan_mode"]
    assert by["cursor"]["asks_questions"] is False and by["cursor"]["mid_turn_tool_only"] is True
    assert by["cursor"]["mcp_statuses"] is False
    assert by["claude"]["asks_questions"] is True and by["claude"]["mid_turn_tool_only"] is False
    assert by["codex"]["available"] is True
    assert by["codex"]["dead_switches"] == ["cross_session", "plan_mode"]
    assert by["claude"]["dead_switches"] == []
