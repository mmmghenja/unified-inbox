"""Ф41-4, правки по ревью (движок OpenCode): запертый конфигом агент обязан быть заперт и на сервере;
сжатие, которое хост перестал ждать, поздним исходом в поток не идёт. Сервер — ПОДДЕЛЬНЫЙ."""

import asyncio

import pytest

from hub.engine import oc_config
from hub.engine.oc_server import EngineStartError, OcServer

from .test_oc_process import harness  # noqa: F401 — фикстура

ALLOW = {"permission": "*", "pattern": "*", "action": "allow"}
DENY = {"permission": "*", "pattern": "*", "action": "deny"}
SEALED = {"permission": {"*": "deny"}}


def test_sealed_agent_open_on_server_is_unlocked():
    config = {"agent": {"hub-project": {"permission": {"*": "ask"}}, "compaction": dict(SEALED)}}
    assert oc_config.sealed_agents(config) == ["compaction"]
    held_open = [
        {"name": "hub-project", "permission": [ALLOW]},
        {"name": "compaction", "permission": [ALLOW]},
    ]
    assert oc_config.unlocked_agents(held_open, config) == ["compaction"]
    # Правило пользователя после нашего запрета — тоже открыт; заперт — в списке нет.
    late = [{"name": "compaction", "permission": [ALLOW, DENY, {**ALLOW, "permission": "bash"}]}]
    assert oc_config.unlocked_agents(late, config) == ["compaction"]
    assert oc_config.unlocked_agents([{"name": "compaction", "permission": [ALLOW, DENY]}], config) == []
    # Выключенный и агент с запросами разрешений «запертыми конфигом» не считаются.
    other = {"agent": {"a": {"disable": True}, "b": {"permission": {"*": "ask"}}, "c": {"permission": {}}}}
    assert oc_config.sealed_agents(other) == []


async def test_start_locked_refuses_when_sealed_agent_is_open(fake_oc):
    """Сервер показывает запертого конфигом агента открытым — выключать его нельзя (нужен движку): запуск
    отменяется с причиной, сервер погашен, перезапуска нет."""
    fake_oc.scenario(extra_agents={"compaction": {"mode": "primary", "own_permission": {"bash": "allow"}}})
    config = fake_oc.config()
    config["agent"]["compaction"] = dict(SEALED)
    oc_config.write(fake_oc.config_path, config)
    server = OcServer(str(fake_oc.binary), log_file=fake_oc.tmp / "logs" / "l.log")
    fake_oc.servers.append(server)
    with pytest.raises(EngineStartError, match="compaction"):
        await server.start_locked(str(fake_oc.workdir), fake_oc.env(), fake_oc.config_path)
    assert len(fake_oc.jsonl("starts.jsonl")) == 1  # не перезапускался: выключать агента нельзя
    assert "disable" not in fake_oc.json("config.json")["agent"]["compaction"]


async def test_abandoned_compaction_emits_nothing_late(fake_oc, harness):  # noqa: F811
    fake_oc.scenario(turns=[], compact="late_fail")
    h = harness()
    await h.process.start()
    assert await h.process.compact() is True
    assert await h.process.compact() is False  # запрос идёт
    h.process.compact_abandon()  # хост перестал ждать (срок)
    await asyncio.sleep(1.0)  # поздний отказ сервера
    assert h.of("result") == [] and h.of("system", "compact_boundary") == []
    assert await h.process.compact() is True  # следующее сжатие можно просить
