"""Ф41-4, правки по ревью (б): хост перестал ждать сжатие OpenCode по сроку — поздний отказ `summarize` не
закрывает идущий ход и не мешает следующему сжатию. `opencode` — ПОДДЕЛЬНЫЙ сервер."""

import asyncio

from .oc_helpers import (
    oc,  # noqa: F401 — фикстура `oc`
    started,
)
from .test_held_compact_oc import BIG, MODEL
from .test_held_host import _events, _rec, _say, _session, _turn_done
from .test_held_opencode import _running
from .test_held_window_oc import PROVIDERS


async def test_late_summarize_failure_after_host_timeout_does_not_close_a_turn(env, oc):  # noqa: F811
    turns = ([{"text": "раз"}], [{"sleep": 1.2}, {"text": "два"}])
    oc.scenario(*turns, providers=PROVIDERS, tokens=BIG, compact="late_fail")
    host = await started(env, held_compact_min_growth=0, held_compact_wait_s=0.15)
    sid, _pid, _workdir = await _session(env, engine="opencode", model=MODEL)
    await _running(env, host, oc, sid)
    await env.until(host, lambda: oc.jsonl("summarize.jsonl"), what="summarize вызван")
    runner = host.held.runners[sid]

    async def timed_out():
        return any("не подтверждено" in e["summary"] for e in await _events(env, sid) if e["kind"] == "warn")

    await env.until(host, timed_out, what="срок сжатия вышел")
    assert runner.compacting is None and runner.compact_tail is None
    assert runner.process._compacting is False  # noqa: SLF001 — процесс о сжатии забыл
    # Владелец пишет; пока идёт ход, сервер отвечает на старый `summarize` отказом.
    await _say(env, sid, "ещё")
    await env.until(host, lambda: runner.busy, what="ход идёт")
    ends_before = len([e for e in await _events(env, sid) if e["kind"] == "turn_end"])
    await asyncio.sleep(0.8)
    await env.cycle(host)
    assert runner.busy  # поздний отказ ход не закрыл
    assert len([e for e in await _events(env, sid) if e["kind"] == "turn_end"]) == ends_before
    await _turn_done(env, host, sid)
    events = await _events(env, sid)
    assert [e["summary"] for e in events if e["kind"] == "text"][-1] == "два"
    assert not any("сжатие контекста не удалось" in e["summary"] for e in events)
    assert (await _rec(env, sid))["state"] in ("idle", "running")
