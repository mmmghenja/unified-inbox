"""Ф41-4б для постоянных сессий на OpenCode (docs/architecture.md): сжатие —
`POST /session/{id}/summarize` моделью сессии, конец — событие `session.compacted`. `opencode` — ПОДДЕЛЬНЫЙ
сервер `tests/engine/fake_opencode.py`."""

import json

from hub.engine import oc_config
from hub.work import files

from .oc_helpers import (
    oc,  # noqa: F401 — фикстура `oc`
    started,
)
from .test_held_host import _events, _rec, _session, _state, _turn_done
from .test_held_opencode import _running
from .test_held_window_oc import PROVIDERS

MODEL = "deepseek/deepseek-flash"
BIG = {"total": 760_000, "input": 759_000, "output": 45, "reasoning": 0, "cache": {"read": 0, "write": 0}}


async def test_oc_compaction_by_size_uses_summarize(env, oc):  # noqa: F811
    # Предел модели — 1 млн: порог — 75 % предела и не выше настройки (200 тыс.).
    oc.scenario([{"text": "раз"}], providers=PROVIDERS, tokens=BIG)
    host = await started(env, held_compact_min_growth=0)
    sid, _pid, _workdir = await _session(env, engine="opencode", model=MODEL)
    await _running(env, host, oc, sid)
    await env.until(host, lambda: oc.jsonl("summarize.jsonl"), what="summarize вызван")
    runner = host.held.runners[sid]
    await env.until(host, lambda: runner.compacting is None, what="сжатие закончено")
    await env.until(host, _state(env, sid, "idle"), what="idle после сжатия")
    [call] = oc.jsonl("summarize.jsonl")
    assert call["body"] == {"providerID": "deepseek", "modelID": "deepseek-flash"}
    assert len(oc.prompts()) == 1  # сжатие — не реплика
    # Встроенный агент сжатия оставлен на сервере запертым: все инструменты запрещены.
    config = json.loads((files.held_dir(env.data_dir, sid) / "opencode-held.json").read_text())
    assert config["agent"]["compaction"] == {"permission": {"*": "deny"}}
    assert (
        oc_config.unlocked_agents(
            [
                {
                    "name": "compaction",
                    "permission": oc_config.permission_rules({"*": "allow"})
                    + oc_config.permission_rules({"*": "deny"}),
                }
            ],
            {"agent": {}},
        )
        == []
    )
    events = await _events(env, sid)
    kinds = [e["kind"] for e in events]
    assert kinds.count("turn_end") == 1 and kinds.count("compact") == 1
    [note] = [e for e in events if e["kind"] == "compact"]
    assert note["brief"] == {"type": "compact", "before": 760_000}
    rec = await _rec(env, sid)
    assert rec["usage"]["context_tokens"] is None and rec["usage"]["context_limit"] == 1_000_000
    for _ in range(4):
        await env.cycle(host)
    assert len(oc.jsonl("summarize.jsonl")) == 1  # второе сжатие сразу не запускается


async def test_oc_compaction_refused_by_server_is_warn(env, oc):  # noqa: F811
    oc.scenario([{"text": "раз"}], providers=PROVIDERS, tokens=BIG, compact="fail")
    host = await started(env, held_compact_min_growth=0)
    sid, _pid, _workdir = await _session(env, engine="opencode", model=MODEL)
    await _running(env, host, oc, sid)
    await _turn_done(env, host, sid)
    await env.until(host, lambda: oc.jsonl("summarize.jsonl"), what="summarize вызван")
    for _ in range(4):
        await env.cycle(host)
    rec = await _rec(env, sid)
    # Сервер отказал: сжатия нет, сессия свободна; повтор — каждым кругом не идёт.
    assert rec["state"] == "idle" and rec["usage"]["context_tokens"] == 760_000
    events = await _events(env, sid)
    assert "compact" not in [e["kind"] for e in events]
    assert [e["summary"] for e in events if e["kind"] == "warn" and "сжатие" in e["summary"]] == [
        "сжатие контекста не удалось"
    ]
    assert len(oc.jsonl("summarize.jsonl")) == 1
