"""Ф48-1 (docs/architecture.md–D): фоновые задачи постоянной сессии хаба —
учёт в хосте, «задачи держат сессию» (простой, место, смена модели, сжатие), снимок `bg` ядру.

Ядро — в процессе, `claude` — подставной (`fake_claude.py`). События задач — настоящие, из пробы Claude Code
2.1.295 (`data/f48_bg_events.jsonl`: строки `system` из `.runs/f48-probe/out_a.jsonl` без `prompt` и id)."""

import json
from pathlib import Path

from hub.work import held as held_mod

from .oc_helpers import oc  # noqa: F401 — фикстура `oc`
from .oc_helpers import started as oc_started
from .test_held_compact import _compacts, _use
from .test_held_host import _alive, _rec, _running, _say, _session, _state, _turn_done
from .test_held_model import _patch
from .test_held_opencode import _oc_session
from .test_held_opencode import _running as _oc_running

PROBE = [
    json.loads(line)
    for line in (Path(__file__).parent / "data" / "f48_bg_events.jsonl").read_text("utf-8").splitlines()
    if line.strip()
]
AGENT_A, AGENT_B = "a93804abbb8d7ab37", "a720dd20af9142d3b"
CMD_C, CMD_B, CMD_FG = "bq2fvnjhk", "b9o1pnth4", "b6dqq0vno"


def _started(task_id: str, *, agent: bool = True, background: bool = True, run: str = "r1", **extra) -> dict:
    return {
        "type": "system",
        "subtype": "task_started",
        "task_id": task_id,
        "run_id": run,
        "description": extra.pop("description", f"задача {task_id}"),
        "task_type": "local_agent" if agent else "local_bash",
        "is_backgrounded": background,
        **({"subagent_type": "worker"} if agent else {}),
        **extra,
    }


def _done(task_id: str, *, run: str = "r1", status: str = "completed", summary: str = "готово") -> dict:
    return {
        "type": "system",
        "subtype": "task_notification",
        "task_id": task_id,
        "run_id": run,
        "status": status,
        "summary": summary,
    }


def _items(runner) -> dict[str, dict]:
    return {item["id"]: item for item in runner.bg_snapshot()["items"]}


async def _idle_runner(env, host, **session_kw):
    """Сессия с живым процессом после первого хода."""
    sid, _pid, _workdir = await _session(env, **session_kw)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    return sid, host.held.runners[sid]


async def _feed(runner, *messages) -> None:
    for message in messages:
        await runner.on_message(message)


async def _cycles(env, host, n: int = 6) -> None:
    for _ in range(n):
        await env.cycle(host)


# --- учёт (раздел A) ----------------------------------------------------------------------------------------


async def test_probe_events_are_tracked_and_reach_the_core(env):
    env.scenario(turns=[[{"text": "раз"}]])
    host = await env.started()
    sid, runner = await _idle_runner(env, host)
    assert (await _rec(env, sid))["bg"] is None  # задач нет и не было

    # До первого `task_updated`: два фоновых подагента, фоновая команда и фоновая команда подагента.
    first_update = next(i for i, m in enumerate(PROBE) if m["subtype"] == "task_updated")
    await _feed(runner, *PROBE[:first_update])
    items = _items(runner)
    assert set(items) == {AGENT_A, AGENT_B, CMD_C, CMD_B}  # нефоновая команда подагента не заводится
    assert CMD_FG not in runner.bg
    assert {i["state"] for i in items.values()} == {"running"}
    assert items[AGENT_A] == {
        "id": AGENT_A,
        "kind": "agent",
        "title": "счёт до двадцати",
        "agent_type": "worker",
        "parent": None,
        "state": "running",
        "started_ts": items[AGENT_A]["started_ts"],
        "ended_ts": None,
        "doing": "Running Wait 20 seconds and print A",
        "last_tool": "Bash",
        "tool_uses": 1,
        "tokens": 87382,
        "note": None,
    }
    assert items[CMD_C]["kind"] == "command" and items[CMD_C]["agent_type"] is None
    assert items[CMD_B]["kind"] == "command" and items[CMD_B]["parent"] == AGENT_B
    assert runner.bg_live == {AGENT_A, AGENT_B, CMD_C, CMD_B}
    assert runner.bg_holds(host.monotonic())
    # Снимок уходит ядру ближайшим кругом — без хода и без ожидания срока подтверждения.
    await env.until(host, lambda: runner.bg_dirty is False, what="снимок отправлен")
    rec = await _rec(env, sid)
    assert rec["state"] == "idle" and rec["bg"] == runner.bg_snapshot()

    # Подагент B «закончился» и позже продолжился под тем же id с новым `run_id`.
    second_run = "0mv13ivh8-f70ad5fa"
    revive = next(
        i for i, m in enumerate(PROBE) if m["subtype"] == "task_started" and m["run_id"] == second_run
    )
    await _feed(runner, *PROBE[first_update:revive])
    items = _items(runner)
    assert items[AGENT_B]["state"] == "running" and items[AGENT_B]["ended_ts"] is None  # список живых
    assert items[AGENT_A]["state"] == "completed" and items[AGENT_A]["note"] == "A"
    assert items[AGENT_A]["tokens"] == 92156 and items[AGENT_A]["ended_ts"] is not None
    assert items[CMD_C]["state"] == "completed" and items[CMD_B]["state"] == "completed"
    assert runner.bg_live == {AGENT_B}

    await _feed(runner, *PROBE[revive:])
    items = _items(runner)
    assert {i["state"] for i in items.values()} == {"completed"} and runner.bg_live == set()
    assert items[AGENT_B]["tool_uses"] == 3 and items[AGENT_B]["doing"].startswith("Running Show earlier")
    assert not runner.bg_running() and not runner.bg_holds(host.monotonic())
    await env.until(host, lambda: runner.bg_dirty is False, what="итог отправлен")
    assert (await _rec(env, sid))["bg"] == runner.bg_snapshot()
    assert "prompt" not in json.dumps((await _rec(env, sid))["bg"])

    # Выход процесса чистит учёт хоста; `asleep` чистит столбец ядра.
    await env.ui("POST", f"/sessions/{sid}/sleep")
    await env.until(host, _state(env, sid, "asleep"), what="усыплена")
    assert runner.bg == {} and runner.bg_live == set()
    assert (await _rec(env, sid))["bg"] is None


async def test_stale_run_secrets_unknown_status_and_caps(env):
    env.scenario(turns=[[{"text": "раз"}]])
    host = await env.started()
    _sid, runner = await _idle_runner(env, host)
    secret = "TOKEN=" + "a1B2c3D4" * 6
    await _feed(
        runner,
        _started("a1", description=f"выкатка {secret}", prompt="секретное задание"),
        _started("fg", agent=False, background=False),  # нефоновая команда — мимо
        _started("fg-agent", background=False),  # нефоновый подагент — учитывается
    )
    items = _items(runner)
    assert set(items) == {"a1", "fg-agent"}
    assert "a1B2c3D4" not in items["a1"]["title"] and "***" in items["a1"]["title"]
    assert "prompt" not in runner.bg["a1"]
    # Нефоновый подагент в список живых не входит — одним списком он не закрывается.
    await _feed(runner, {"type": "system", "subtype": "background_tasks_changed", "tasks": []})
    assert _items(runner)["fg-agent"]["state"] == "running"
    # Новый запуск под тем же id; запоздалое событие прежнего запуска его не закрывает.
    await _feed(runner, _done("a1", summary="x" * 500), _started("a1", run="r2"), _done("a1", run="r1"))
    item = _items(runner)["a1"]
    assert item["state"] == "running" and item["ended_ts"] is None and item["note"] is None
    await _feed(runner, _done("a1", run="r2", status="killed_by_something_very_long"))
    assert _items(runner)["a1"]["state"] == "killed_by_something_"  # как есть, до 20 символов
    await _feed(runner, _done("fg-agent", status="failed", summary="y" * 500))
    assert len(_items(runner)["fg-agent"]["note"]) == 300

    # Потолки: законченных — последние 10; живые — все, в снимке всего не больше 40.
    for n in range(15):
        await _feed(runner, _started(f"c{n}", agent=False), _done(f"c{n}"))
    finished = [e for e in runner.bg.values() if e["state"] != "running"]
    assert len(finished) == held_mod.BG_DONE_KEEP and "c14" in runner.bg and "c0" not in runner.bg
    for n in range(45):
        await _feed(runner, _started(f"live{n}", parent_task_id="c0"))
    assert sum(1 for e in runner.bg.values() if e["state"] == "running") == 45
    snapshot = runner.bg_snapshot()["items"]
    assert len(snapshot) == held_mod.BG_ITEMS_MAX and {i["state"] for i in snapshot} == {"running"}
    assert {i["parent"] for i in snapshot} == {None}  # родителя в снимке нет — ссылка не отдаётся
    await env.until(host, lambda: runner.bg_dirty is False, what="снимок принят ядром")


# --- задачи держат сессию (раздел B) ------------------------------------------------------------------------


async def test_live_agent_keeps_session_from_idle_close(env):
    env.scenario(turns=[[{"emit": _started("a1")}, {"text": "запустила подагента"}]])
    host = await env.started(held_idle_close_s=0.3)
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    runner = host.held.runners[sid]
    await env.until(host, lambda: runner.bg_dirty is False, what="снимок отправлен")
    runner.last_activity -= 60  # простой давно вышел
    await _cycles(env, host, 8)
    assert _alive(host) == [sid] and runner.reason is None
    rec = await _rec(env, sid)
    assert rec["state"] == "idle" and [i["state"] for i in rec["bg"]["items"]] == ["running"]
    # Задача закончилась — сессия усыпляется, как раньше.
    await _feed(runner, _done("a1"))
    runner.last_activity -= 60
    await env.until(host, _state(env, sid, "asleep"), what="усыплена по простою")
    assert (await _rec(env, sid))["bg"] is None


async def test_old_background_command_does_not_hold(env):
    env.scenario(turns=[[{"emit": _started("dev", agent=False)}, {"text": "сервер в фоне"}]])
    host = await env.started(held_idle_close_s=0.3)
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    runner = host.held.runners[sid]
    runner.last_activity -= 60
    await _cycles(env, host, 8)
    assert _alive(host) == [sid] and runner.bg_holds(host.monotonic())  # свежая команда держит
    host.config["held_bg_command_hold_s"] = 0.05
    runner.last_activity -= 60
    await env.until(host, _state(env, sid, "asleep"), what="старая фоновая команда не держит")


async def test_sleep_button_stops_session_with_live_agents(env):
    env.scenario(turns=[[{"emit": _started("a1")}, {"text": "раз"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    assert host.held.runners[sid].bg_holds(host.monotonic())
    r = await env.ui("POST", f"/sessions/{sid}/sleep")
    assert r.status_code == 200, r.text
    await env.until(host, _state(env, sid, "asleep"), what="усыплена сразу")
    assert sid not in host.held.runners and (await _rec(env, sid))["bg"] is None


async def test_session_with_live_agent_is_not_evicted_for_a_slot(env):
    env.scenario(
        sessions={
            "1": {"turns": [[{"emit": _started("a1")}, {"text": "жду подагента"}]]},
            "2": {"turns": [[{"text": "дождалась"}]]},
        }
    )
    host = await env.started(held_max_parallel=1)
    project = env.project("Initech")
    a, _, _ = await _session(env, project=project)
    b, _, _ = await _session(env, project=project)
    assert (a, b) == (1, 2)
    await _running(env, host, a)
    await _turn_done(env, host, a)
    await _say(env, b)

    async def waits():
        return (await _rec(env, b))["wait_reason"] == "slot"

    await env.until(host, waits, what="ждёт места")
    await _cycles(env, host)
    assert _alive(host) == [a] and len(env.calls()) == 1
    await _feed(host.held.runners[a], _done("a1"))
    await env.until(host, lambda: len(env.calls()) == 2, what="место освободилось")
    await env.until(host, _state(env, a, "asleep"), what="вытеснена после задач")


async def test_model_change_waits_for_live_agent(env):
    env.scenario(turns=[[{"emit": _started("a1")}, {"text": "раз"}]])
    host = await env.started()
    sid, _, _ = await _session(env, model="opus")
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    runner = host.held.runners[sid]
    await _patch(env, sid, model="sonnet")
    await _cycles(env, host)
    assert host.held.runners.get(sid) is runner and runner.reason is None
    await _feed(runner, _done("a1"))
    await env.until(host, lambda: sid not in host.held.runners, what="перезапуск после задач")
    assert (await _rec(env, sid))["state"] == "asleep"


async def test_no_compaction_while_agent_runs(env):
    env.scenario(
        turns=[[{"emit": _started("a1")}, {"text": "раз"}, _use(160_000, 0.5)]],
        compact={"pre_tokens": 161_000, "total_cost_usd": 0.75},
        init_model="claude-test",
    )
    host = await env.started(held_compact_min_growth=0, held_compact_tokens=150_000)
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    await _cycles(env, host, 8)
    assert _compacts(env) == 0
    await _feed(host.held.runners[sid], _done("a1"))
    await env.until(host, lambda: _compacts(env) == 1, what="сжатие после задач")


# --- OpenCode (раздел D) ------------------------------------------------------------------------------------


async def test_oc_child_session_is_an_agent_until_turn_end(env, oc):  # noqa: F811
    oc.scenario([{"ask": {"tool": "bash", "args": {"command": "ls"}}, "child": True}, {"text": "Готово"}])
    host = await oc_started(env)
    sid, _pid, _workdir = await _oc_session(env)
    await _oc_running(env, host, oc, sid, "посмотри")
    runner = host.held.runners[sid]
    await env.until(host, lambda: bool(runner.bg), what="дочерняя сессия учтена")
    [entry] = runner.bg.values()
    assert entry["kind"] == "agent" and entry["title"] == "подагент" and entry["parent"] is None
    assert entry["tokens"] is None and entry["id"].startswith("ses_")
    await _turn_done(env, host, sid)
    assert entry["state"] == "completed" and not runner.bg_holds(host.monotonic())
    await env.until(host, lambda: runner.bg_dirty is False, what="снимок отправлен")
    [item] = (await _rec(env, sid))["bg"]["items"]
    assert item["state"] == "completed" and item["ended_ts"] is not None


async def test_lost_end_event_does_not_hold_forever(env):
    """Задача с потерянным событием конца: фоновая — закрывается, пропав из списка живых; нефоновый
    подагент — концом хода. Фоновую конец хода не трогает."""
    env.scenario(turns=[[{"text": "раз"}]])
    host = await env.started()
    _sid, runner = await _idle_runner(env, host)
    listed = {"type": "system", "subtype": "background_tasks_changed"}
    await _feed(
        runner,
        {**listed, "tasks": [{"task_id": "bg1", "run_id": "r1", "task_type": "local_agent"}]},
        _started("bg1"),
        _started("fg-agent", background=False),
    )
    runner.bg_turn_over(only_fg=True)
    items = _items(runner)
    assert items["fg-agent"]["state"] == "completed" and items["bg1"]["state"] == "running"
    assert runner.bg_holds(host.monotonic())
    await _feed(runner, {**listed, "tasks": []})
    assert _items(runner)["bg1"]["state"] == "completed"
    assert not runner.bg_holds(host.monotonic())
