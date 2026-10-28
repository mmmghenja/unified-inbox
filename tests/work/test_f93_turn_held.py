"""Ф93-1 (F3, docs/architecture.md): отчёт конца хода не теряется, пока ядро недоступно.

Факт с прода 24.10: деплой перезапускает ядро раньше hub-work; конец хода, пришедшийся на этот зазор, хост
выбрасывал (`ядро недоступно для turn (ReadError)`) — карточка `turn` с «ВЛАДЕЛЬЦУ:» не появлялась никогда.
Теперь конец хода держится в очереди сессии (`late`), повторяется с нарастающей паузой, переживает остановку
хоста (`held-turns.json`) и уходит первым при следующем старте; ядро отбрасывает запоздавший отчёт, если
его ход не новее записанного. Ядро — в процессе, `claude` — подставной."""

import asyncio
import json
import stat

from hub.agent.core_api import CoreUnavailable
from hub.work import files
from hub.work import held as held_mod

from .test_held_host import _rec, _running, _session, _turn_done

ASK = "**ВЛАДЕЛЬЦУ:** нужен ключ API"


def _ask_scenario(env, n: int = 1) -> None:
    env.scenario(turns=[[{"text": f"готово {i}"}, {"text": ASK}] for i in range(n)])


async def _turn_cards(env) -> list[dict]:
    r = await env.ui("GET", "/owner_channel/queue")
    assert r.status_code == 200, r.text
    return [c for c in r.json()["items"] if c["kind"] == "turn"]


def _core_turns(env, sid) -> tuple[int, int | None]:
    with env.db.reader() as conn:
        row = conn.execute("SELECT turns, last_turn_ts FROM main.hub_session WHERE id = ?", (sid,)).fetchone()
    return row[0], row[1]


def _turn_end(path: str, body) -> bool:
    return (
        path.endswith("/turn")
        and isinstance(body, dict)
        and body.get("state") == "idle"
        and body.get("last_turn_ts") is not None
    )


def _core_down_for_turn_end(host, flag: dict, *, after_write: bool = False) -> None:
    """Ядро «недоступно» для конца хода, пока `flag["down"]`. `after_write` — ядро записало, а ответ не
    дошёл (ReadError посреди ответа при остановке ядра)."""
    real_post = host.core.post

    async def post(path, *args, **kw):
        body = kw.get("json", args[0] if args else None)
        if flag["down"] and _turn_end(path, body):
            flag["tries"] += 1
            if after_write:
                await real_post(path, *args, **kw)
            raise CoreUnavailable("ReadError")
        return await real_post(path, *args, **kw)

    host.core.post = post


async def _poll(predicate, *, limit_s: float = 5.0, what: str) -> None:
    """Ждать без кругов хоста: условие должно выполнить само повторение конца хода."""
    for _ in range(int(limit_s / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"не дождались: {what}")


async def test_turn_end_retried_until_core_takes_it_then_card(env, monkeypatch):
    monkeypatch.setattr(held_mod, "TURN_RETRY_FIRST_S", 0.05)
    _ask_scenario(env)
    host = await env.started()
    sid, _pid, _workdir = await _session(env, role="программист")
    flag = {"down": True, "tries": 0}
    _core_down_for_turn_end(host, flag)
    await _running(env, host, sid, "сделай")
    runner = host.held.runners[sid]
    await env.until(host, lambda: not runner.busy and sid in host.held.turns_held, what="конец хода отложен")
    assert await _turn_cards(env) == []  # ядро конца хода не видело
    path = files.held_turns_path(env.data_dir)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    [body] = json.loads(path.read_text(encoding="utf-8"))["turns"][str(sid)]
    assert body["late"] is True and body["answer"]["brief"] == "нужен ключ API"
    # Пока конец хода не принят, новое состояние сессии ядру не идёт (иначе конец хода откатил бы его).
    assert (await _rec(env, sid))["state"] == "running"

    await _poll(lambda: flag["tries"] >= 2, what="повтор с паузой")
    flag["down"] = False
    await _poll(lambda: sid not in host.held.turns_held, what="повтор принят ядром")
    [card] = await _turn_cards(env)
    assert card["ref"] == f"turn:{sid}:1" and card["brief"] == "нужен ключ API"
    assert not path.exists()  # очередь пуста — файла нет
    assert _core_turns(env, sid)[0] == 1
    await env.until(host, lambda: sid not in host.held.turn_retry, what="задача повтора кончилась")


async def test_answer_lost_after_core_wrote_is_not_counted_twice(env, monkeypatch):
    # Ядро записало конец хода, ответ до хоста не дошёл: повтор с `late` ядро отбрасывает — второй карточки,
    # второго номера хода и двойной цены нет.
    monkeypatch.setattr(held_mod, "TURN_RETRY_FIRST_S", 0.05)
    _ask_scenario(env)
    host = await env.started()
    sid, _pid, _workdir = await _session(env, role="программист")
    flag = {"down": True, "tries": 0}
    _core_down_for_turn_end(host, flag, after_write=True)
    await _running(env, host, sid, "сделай")
    runner = host.held.runners[sid]
    await env.until(host, lambda: not runner.busy and sid in host.held.turns_held, what="конец хода отложен")
    flag["down"] = False
    await _poll(lambda: sid not in host.held.turns_held, what="повтор отправлен")
    assert len(await _turn_cards(env)) == 1
    assert _core_turns(env, sid)[0] == 1
    assert (await _rec(env, sid))["state"] == "idle"


async def test_host_stop_with_unsent_turn_end_new_host_sends_it_first(env):
    _ask_scenario(env)
    host = await env.started()
    sid, _pid, _workdir = await _session(env, role="программист")
    flag = {"down": True, "tries": 0}
    _core_down_for_turn_end(host, flag)
    await _running(env, host, sid, "сделай")
    runner = host.held.runners[sid]
    await env.until(host, lambda: not runner.busy and sid in host.held.turns_held, what="конец хода отложен")
    await host.shutdown()  # ядро всё ещё «недоступно» для конца хода
    assert files.held_turns_path(env.data_dir).is_file()
    assert await _turn_cards(env) == []

    fresh = env.host()
    order: list[str] = []
    real_post = fresh.core.post

    async def post(path, *args, **kw):
        order.append(path.rsplit("/", 1)[-1])
        return await real_post(path, *args, **kw)

    fresh.core.post = post
    await fresh.start()
    assert order[:2] == ["turn", "recover"]  # старый конец хода — раньше сброса состояния
    [card] = await _turn_cards(env)
    assert card["ref"] == f"turn:{sid}:1" and card["brief"] == "нужен ключ API"
    assert not files.held_turns_path(env.data_dir).exists()
    assert sid not in fresh.held.turns_held
    assert (await _rec(env, sid))["state"] == "asleep"  # `recover` — после конца хода
    assert _core_turns(env, sid)[0] == 1


async def test_older_turn_end_from_file_does_not_roll_back_newer(env):
    # У ядра уже записан более свежий ход: запоздавший отчёт (и тот же самый) отбрасывается целиком.
    _ask_scenario(env, 2)
    host = await env.started()
    sid, _pid, _workdir = await _session(env, role="программист")
    await _running(env, host, sid, "сделай")
    await _turn_done(env, host, sid)
    await host.shutdown()
    turns, last_ts = _core_turns(env, sid)
    assert turns == 1 and last_ts is not None
    answer = {"full": "ВЛАДЕЛЬЦУ: старый вопрос", "brief": "старый вопрос", "brief_kind": "owner_channel", "cut": False}
    stale = [
        {"state": "idle", "last_turn_ts": last_ts - 60_000, "answer": answer, "summary": "s", "late": True},
        {"state": "idle", "last_turn_ts": last_ts, "answer": answer, "summary": "s", "late": True},
    ]
    files.save_held_turns(env.data_dir, {"turns": {str(sid): stale}})

    fresh = await env.started()
    assert not files.held_turns_path(env.data_dir).exists()
    assert sid not in fresh.held.turns_held
    assert _core_turns(env, sid) == (1, last_ts)
    cards = await _turn_cards(env)  # ни карточки второго хода, ни «старого вопроса»
    assert all(c["ref"] != f"turn:{sid}:2" for c in cards)
    assert all("старый вопрос" not in json.dumps(c, ensure_ascii=False) for c in cards)


async def test_running_and_interrupted_reports_are_not_held(env):
    host = await env.started()
    sid, _pid, _workdir = await _session(env)
    flag = {"down": True, "tries": 0}
    real_post = host.core.post

    async def post(path, *args, **kw):
        if flag["down"] and path.endswith("/turn"):
            raise CoreUnavailable("ReadError")
        return await real_post(path, *args, **kw)

    host.core.post = post
    held = host.held
    assert await held.turn_result(sid, state="running", source="say") is None
    stuck = {"since_ts": 1, "step": {"tool": "Bash"}}
    assert await held.turn_result(sid, state="running", stuck=stuck) is None
    assert await held.turn_result(sid, state="idle", last_turn_ts=held.now_ms(), interrupted_say=5) is None
    assert sid not in held.turns_held and not files.held_turns_path(env.data_dir).exists()


# --- ревью Ф93-1 (В3): очередь одной сессии не держит старт хоста -------------------------------------------


async def _fresh_with_held(env, *, error: str):
    """Хост остановлен; в файле — отложенный конец хода сессии `sid`, на который ядро отвечает `error`."""
    host = await env.started()
    sid, _pid, _workdir = await _session(env, role="программист")
    other, _pid2, _wd2 = await _session(env, role="тестер")
    await host.shutdown()
    _turns, last = _core_turns(env, sid)
    body = {"state": "idle", "last_turn_ts": max(last or 0, host.held.now_ms()) + 1000, "late": True}
    files.save_held_turns(env.data_dir, {"turns": {str(sid): [body]}})
    fresh = env.host()
    real_post = fresh.core.post
    calls: list[str] = []
    flag = {"down": True}

    async def post(path, *args, **kw):
        calls.append(path)
        if flag["down"] and path.endswith(f"/sessions/{sid}/turn"):
            raise CoreUnavailable(error)
        return await real_post(path, *args, **kw)

    fresh.core.post = post
    return fresh, sid, other, calls, flag


async def test_core_5xx_on_held_turn_does_not_block_host_start(env, monkeypatch, caplog):
    monkeypatch.setattr(held_mod, "TURN_RETRY_FIRST_S", 0.05)
    fresh, sid, other, calls, _flag = await _fresh_with_held(env, error="HTTP 500")
    await fresh.start()  # не бросает: старт не ждёт очередь одной сессии
    assert any(p.endswith("/recover") for p in calls)
    assert sid in fresh.held.turns_held and sid in fresh.held.turn_retry
    assert other not in fresh.held.turns_held
    assert (await _rec(env, sid))["state"] == "asleep"
    # Ядро отказывает дольше предела — отчёт снят с предупреждением, файл удалён.
    monkeypatch.setattr(held_mod, "TURN_REFUSED_MS", 0)
    await _poll(lambda: sid not in fresh.held.turns_held, what="отчёт снят")
    assert "отложенный конец хода снят: ядро отказывает (HTTP 500)" in caplog.text
    assert not files.held_turns_path(env.data_dir).exists()
    assert _core_turns(env, sid)[0] == 0


async def test_network_error_keeps_held_turn_then_late_turn_does_not_wake(env, monkeypatch):
    monkeypatch.setattr(held_mod, "TURN_RETRY_FIRST_S", 0.05)
    monkeypatch.setattr(held_mod, "TURN_REFUSED_MS", 0)  # предел — только для 5xx
    fresh, sid, _other, calls, flag = await _fresh_with_held(env, error="ConnectError")
    await fresh.start()
    assert any(p.endswith("/recover") for p in calls)
    await _poll(lambda: sum(p.endswith(f"/sessions/{sid}/turn") for p in calls) >= 3, what="повторы")
    assert sid in fresh.held.turns_held and files.held_turns_path(env.data_dir).is_file()
    flag["down"] = False
    await _poll(lambda: sid not in fresh.held.turns_held, what="принят")
    assert _core_turns(env, sid)[0] == 1
    assert (await _rec(env, sid))["state"] == "asleep"  # запоздавший конец хода спящую не будит
