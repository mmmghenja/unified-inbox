"""Ф56-1в (docs/architecture.md): хост постоянных сессий — таймер движка и
зависший ход.

- `ScheduleWakeup` в `assistant` хода: срок `delaySeconds` (прижат к 60…3600 с), `stop: true` снимает, шаг
  подагента — мимо; срок уходит ядру ключом `wake_at_ts` снимка `bg`; свой ход (источник `self`) срок снимает,
  ход по сигналу — нет;
- `last_step_at`: шаги потока (`assistant`, `user`, `system`) двигают, частичные `stream_event` — нет;
- «завис» докладывается один раз, пока нет шага; ждущая решения владельца карточка — не зависание;
- сквозь подставной `claude` и ядро: инструмент молчит дольше `held_stuck_s` — карточка `stuck` в очереди
  владельца, результат инструмента пришёл — карточка закрыта `stepped`."""

import json

from hub.work import files
from hub.work import held as held_mod

from .test_held_bg import _feed, _idle_runner
from .test_held_host import _rec, _running, _session, _turn_done


def _wakeup(delay=None, *, stop=None, parent=None) -> dict:
    tool_input: dict = {"prompt": "проверь сборку", "reason": "ждём CI"}
    if delay is not None:
        tool_input["delaySeconds"] = delay
    if stop is not None:
        tool_input["stop"] = stop
    return {
        "type": "assistant",
        "parent_tool_use_id": parent,
        "message": {
            "id": "m1",
            "content": [
                {"type": "tool_use", "id": f"w{delay}{stop}", "name": "ScheduleWakeup", "input": tool_input}
            ],
        },
    }


def _user_result(call_id="w") -> dict:
    return {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": call_id, "content": "ok"}]},
    }


def _stream() -> dict:
    return {"type": "stream_event", "event": {"type": "message_start", "message": {"id": "m2"}}}


# --- таймер движка (2.4) ------------------------------------------------------------------------------------


async def test_schedule_wakeup_sets_clamps_and_stops(env):
    env.scenario(turns=[[{"text": "раз"}]])
    host = await env.started()
    sid, runner = await _idle_runner(env, host)
    assert runner.wake_at_ts is None and "wake_at_ts" not in runner.bg_snapshot()

    before = runner.sessions.now_ms()
    await _feed(runner, _wakeup(120))
    assert before + 120_000 <= runner.wake_at_ts <= runner.sessions.now_ms() + 120_000
    assert runner.bg_dirty and runner.bg_snapshot()["wake_at_ts"] == runner.wake_at_ts
    # Снимок с пробуждением уходит ядру ближайшим кругом — ключом `wake_at_ts` в `bg`.
    await env.until(host, lambda: runner.bg_dirty is False, what="снимок отправлен")
    assert (await _rec(env, sid))["bg"] == {"items": [], "wake_at_ts": runner.wake_at_ts}

    # Прижим к 60…3600 с, как у движка.
    now = runner.sessions.now_ms()
    await _feed(runner, _wakeup(5))
    assert now + 60_000 <= runner.wake_at_ts <= runner.sessions.now_ms() + 60_000
    await _feed(runner, _wakeup(99_999))
    assert runner.wake_at_ts <= runner.sessions.now_ms() + 3_600_000
    assert runner.wake_at_ts >= now + 3_600_000
    # Кривой срок — без изменений; шаг подагента — мимо.
    kept = runner.wake_at_ts
    await _feed(runner, _wakeup("скоро"), _wakeup(True), _wakeup(60, parent="t1"))
    assert runner.wake_at_ts == kept
    # `stop: true` — пробуждение снято, ключа в снимке нет.
    await _feed(runner, _wakeup(stop=True))
    assert runner.wake_at_ts is None and "wake_at_ts" not in runner.bg_snapshot()
    await env.until(host, lambda: runner.bg_dirty is False, what="снимок без пробуждения")
    assert (await _rec(env, sid))["bg"] == {"items": []}


async def test_own_turn_clears_wakeup_signal_turn_does_not(env):
    env.scenario(turns=[[{"text": "раз"}]])
    host = await env.started()
    _sid, runner = await _idle_runner(env, host)
    # Ход по сигналу: срок остаётся.
    runner.wake_at_ts = runner.sessions.now_ms() + 600_000
    runner.turn_origin = "signal"
    runner.bg_dirty = False
    await _feed(runner, {"type": "assistant", "message": {"id": "m", "content": []}})
    assert runner.busy and runner.wake_at_ts is not None and runner.bg_dirty is False
    # Свой ход (сработало пробуждение, уведомление фоновой задачи): срок снят, снимок — ядру.
    runner.busy = False
    await _feed(
        runner, {"type": "assistant", "message": {"id": "m3", "content": [{"type": "text", "text": "я"}]}}
    )
    assert runner.busy and runner.wake_at_ts is None and runner.bg_dirty is True


# --- шаги и доклад «завис» (5.1) ----------------------------------------------------------------------------


async def test_last_step_moves_on_steps_not_on_stream_events(env):
    env.scenario(turns=[[{"text": "раз"}]])
    host = await env.started()
    _sid, runner = await _idle_runner(env, host)
    runner.busy = True
    runner.last_step_at = -1000.0
    await _feed(runner, _stream())
    assert runner.last_step_at == -1000.0  # частичное сообщение — не шаг
    await _feed(runner, _user_result())
    assert runner.last_step_at > 0
    runner.last_step_at = -1000.0
    await _feed(runner, {"type": "system", "subtype": "task_progress", "task_id": "x"})
    assert runner.last_step_at > 0  # событие фоновой задачи — шаг
    # Простаивающая сессия шагов не считает.
    runner.busy = False
    runner.last_step_at = -1000.0
    await _feed(runner, _user_result())
    assert runner.last_step_at == -1000.0


async def test_stuck_due_once_and_not_while_card_waits(env):
    env.scenario(turns=[[{"text": "раз"}]])
    host = await env.started()
    _sid, runner = await _idle_runner(env, host)
    assert files.DEFAULT_CONFIG["held_stuck_s"] == 900
    now = host.monotonic()
    runner.busy = True
    runner.last_step_at = now - 100
    assert runner.stuck_due(now, 900) is False
    assert runner.stuck_due(now, 60) is True
    runner.stuck_reported = True
    assert runner.stuck_due(now, 60) is False  # доложено — до следующего шага не повторяется
    runner.stuck_reported = False
    # Ход ждёт решения владельца по карточке: не зависание, отсчёт — от решения.
    runner.pending["r1"] = {"core_id": 1}
    assert runner.stuck_due(now, 60) is False and runner.last_step_at == now
    runner.pending.clear()
    runner.busy = False


async def test_stuck_tool_reaches_core_and_step_closes_it(env):
    env.scenario(
        turns=[
            [{"tool": "Bash", "input": {"command": "make"}, "result": "готово", "hold": 3}, {"text": "всё"}]
        ]
    )
    host = await env.started(held_stuck_s=0.5)
    sid, _pid, _workdir = await _session(env, role="программист")
    await _running(env, host, sid, "собери")
    runner = host.held.runners[sid]
    await env.until(host, lambda: runner.stuck_reported, what="доклад «завис»")

    async def stuck_cards():
        r = await env.ui("GET", "/owner_channel/queue")
        assert r.status_code == 200, r.text
        return [c for c in r.json()["items"] if c["kind"] == "stuck"]

    cards = await stuck_cards()
    assert [c["ref"] for c in cards] == [f"stuck:{sid}:1"]
    assert cards[0]["full"]["step"] == {"tool": "Bash"} and cards[0]["brief"].startswith("завис: шаг Bash")
    # Результат инструмента пришёл — шаг: карточка закрыта `stepped`, доклад сброшен.
    await env.until(host, lambda: not runner.stuck_reported, what="шаг после зависания")
    await _turn_done(env, host, sid)
    assert await stuck_cards() == []
    with env.db.reader() as conn:
        payload = json.loads(
            conn.execute("SELECT payload_json FROM main.owner_item WHERE kind = 'stuck'").fetchone()[0]
        )
    assert payload["action"] == "stepped"
    assert held_mod.STEP_KINDS >= {"assistant", "user", "system"}
