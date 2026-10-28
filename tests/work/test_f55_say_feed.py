"""Ф55-1 (docs/architecture.md и 4): реплики владельца сессии хаба — всё накопленное одним
вводом (пачкой) и, у Claude Code, не дожидаясь конца хода: строкой посреди хода, в том числе при открытой
карточке разрешения.

Ядро — в процессе, `claude` — подставной (`fake_claude.py`); очередь ввода движка — `"lifecycle": true`, как у
Claude Code 2.1.295 (проба `.runs/f55-probe`: строки, записанные при ждущем `can_use_tool`, движок ставит в
очередь и отдаёт модели сразу за результатом инструмента). OpenCode — поддельный сервер."""

import asyncio
import base64
import json
import os
import signal

from hub.work import held as held_mod
from hub.work import say_files, session

from .oc_helpers import (
    oc,  # noqa: F401 — фикстура `oc`
    started,
)
from .test_held_host import (
    _controls,
    _drop_interrupted,
    _events,
    _full,
    _interrupted,
    _rec,
    _running,
    _say,
    _session,
    _state,
    _turn_done,
    _user_text,
    _users,
)
from .test_held_incoming import _warns
from .test_held_opencode import _oc_session
from .test_held_say_files import JPEG, PNG, say, upload

LONG = [{"sleep": 30}]


def _batch(*texts: str) -> str:
    total = len(texts)
    return "\n\n".join(f"Реплика {n} из {total}:\n{text}" for n, text in enumerate(texts, 1))


def _absorbed(env) -> list[str]:
    """`uuid` строк, вошедших в идущий ход на границе инструмента (подставной движок)."""
    path = env.bin_dir / "absorbed.jsonl"
    if not path.exists():
        return []
    return [json.loads(line)["uuid"] for line in path.read_text(encoding="utf-8").splitlines()]


def _says(events: list[dict]) -> list[tuple[str, str]]:
    return [(e["summary"], e["ref"]) for e in events if e["kind"] == "say"]


async def _cycles(env, host, n=4) -> None:
    for _ in range(n):
        await env.cycle(host)


async def _busy(env, host, sid, text="первая") -> int:
    say_id = await _running(env, host, sid, text)
    runner = host.held.runners[sid]
    await env.until(host, lambda: runner.busy and runner.lifecycle_seen, what="ход идёт")
    return say_id


async def _queued(env, sid) -> list[int]:
    return [s["id"] for s in (await _full(env, sid))["says"]]


async def _stop(env, sid) -> None:
    r = await env.ui("POST", f"/sessions/{sid}/interrupt")
    assert r.status_code == 200, r.text


# --- 2.1: пачка ---------------------------------------------------------------------------------------------


async def test_three_says_to_a_sleeping_session_are_one_input_and_one_turn(env):
    env.scenario(lifecycle=True, turns=[[{"text": "ответ на все три"}], [{"text": "лишний ход"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    a = await _say(env, sid, "раз")
    b = await _say(env, sid, "два")
    c = await _say(env, sid, "три")
    assert await _events(env, sid) == []  # 2.5: до доставки реплик в ленте нет
    await env.until(host, lambda: len(_users(env)) == 1, what="пачка подана")
    await _turn_done(env, host, sid)
    await _cycles(env, host)
    [line] = _users(env)
    assert line["uuid"] == session.held_say_uuid(a) and "priority" not in line
    assert line["message"]["content"] == [{"type": "text", "text": _batch("раз", "два", "три")}]
    events = await _events(env, sid)
    assert _says(events) == [
        ("владелец (next): раз", str(a)),
        ("владелец (next): два", str(b)),
        ("владелец (next): три", str(c)),
    ]
    assert [e["summary"] for e in events if e["kind"] == "text"] == ["ответ на все три"]
    assert [e["kind"] for e in events].count("turn_end") == 1 and len(env.calls()) == 1
    rec = await _rec(env, sid)
    assert (rec["state"], rec["queued_says"]) == ("idle", 0) and host.held.says.get(sid) is None


async def test_batch_carries_files_of_all_says_in_order(env):
    env.scenario(turns=[[{"text": "вижу"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    png = await upload(env, sid, PNG, "первый.png")
    jpg = await upload(env, sid, JPEG, "второй.jpg")
    a = await say(env, sid, [png], "картинка раз")
    await _say(env, sid, "между ними")
    await say(env, sid, [jpg], "")
    await env.until(host, lambda: len(_users(env)) == 1, what="пачка подана")
    await _turn_done(env, host, sid)
    [line] = _users(env)
    text, first, second = line["message"]["content"]
    assert line["uuid"] == session.held_say_uuid(a)
    assert text == {
        "type": "text",
        "text": _batch("картинка раз", "между ними", say_files.BATCH_EMPTY),
    }
    assert first["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": base64.b64encode(PNG).decode(),
    }
    assert second["source"]["data"] == base64.b64encode(JPEG).decode()
    assert (await _rec(env, sid))["queued_says"] == 0


async def test_batch_stops_before_the_say_whose_blocks_do_not_fit_the_budget(env, monkeypatch):
    # Бюджет блоков — на пачку целиком: вторая картинка в него не влезает и уходит следующим вводом
    # (блоком: у своей пачки бюджет свой), вместе со всем, что стоит за ней.
    monkeypatch.setattr(say_files, "BLOCKS_B64_MAX", say_files.b64_len(len(PNG)) + 8)
    env.scenario(turns=[[{"text": "раз"}], [{"text": "два"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    png = await upload(env, sid, PNG, "a.png")
    jpg = await upload(env, sid, JPEG, "b.jpg")
    a = await say(env, sid, [png], "первая")
    b = await say(env, sid, [jpg], "вторая")
    c = await _say(env, sid, "третья")
    await env.until(host, lambda: len(_users(env)) == 2, what="два ввода")
    await _turn_done(env, host, sid)
    one, two = _users(env)
    assert one["uuid"] == session.held_say_uuid(a) and two["uuid"] == session.held_say_uuid(b)
    assert [b_["type"] for b_ in one["message"]["content"]] == ["text", "image"]
    assert _user_text(one) == "первая"  # одна реплика — без обёртки
    assert [b_["type"] for b_ in two["message"]["content"]] == ["text", "image"]
    assert _user_text(two) == _batch("вторая", "третья")
    assert [ref for _summary, ref in _says(await _events(env, sid))] == [str(a), str(b), str(c)]


async def test_batch_text_and_membership_check():
    assert say_files.batch_text(["одна"]) == "одна"
    text = say_files.batch_text(["раз", "", "три\n"])
    assert text == f"Реплика 1 из 3:\nраз\n\nРеплика 2 из 3:\n{say_files.BATCH_EMPTY}\n\nРеплика 3 из 3:\nтри"
    assert say_files.in_batch(text, "раз") and say_files.in_batch(text, "три\n")
    assert say_files.in_batch(text, "") and not say_files.in_batch(text, "два")
    assert not say_files.in_batch("раз", "раз")  # одиночная реплика — не пачка
    noted = say_files.batch_text(["а", f"б\n\n{say_files.SAVED_NOTE}hub-files/x.txt"])
    assert say_files.in_batch(noted, "б")


# --- 2.2: посреди хода --------------------------------------------------------------------------------------


async def test_says_during_an_open_card_reach_the_model_with_the_decision(env):
    ask = {"tool": "Bash", "input": {"command": "git push origin main"}, "reason": "push"}
    env.scenario(lifecycle=True, turns=[[{"ask": ask}, {"text": "готово, и на реплики ответил"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    first = await _busy(env, host, sid)

    async def pending():
        return (await _full(env, sid))["requests"]

    [request] = await env.until(host, pending, what="карточка")
    runner = host.held.runners[sid]
    b = await _say(env, sid, "заодно посмотри логи")
    c = await _say(env, sid, "и не трогай прод")
    # Карточка открыта — реплики уходят движку сразу, одной строкой, без рамки сигнала и без `priority`.
    await env.until(host, lambda: len(_users(env)) == 2, what="реплики поданы при открытой карточке")
    line = _users(env)[1]
    assert line["uuid"] == session.held_say_mid_uuid(b, None, 1)
    assert "priority" not in line and "synthetic" not in line
    assert _user_text(line) == _batch("заодно посмотри логи", "и не трогай прод")
    await env.until(host, lambda: list(runner.mid.values()) == ["queued"], what="строка в очереди движка")
    await _cycles(env, host)
    # Движок их ещё не начал: в ядре они в очереди, в ленте их нет, второй раз не подаются; учёт хода прежний.
    assert await _queued(env, sid) == [b, c] and len(_users(env)) == 2
    assert _says(await _events(env, sid)) == [("владелец (next): первая", str(first))]
    assert runner.turn_say_id == first and runner.mid_say_ids() == {b, c}
    rec = await _rec(env, sid)
    assert (rec["state"], rec["pending_requests"]) == ("running", 1)

    r = await env.ui("POST", f"/session-requests/{request['id']}/decide", {"decision": "allow"})
    assert r.status_code == 200, r.text
    await _turn_done(env, host, sid)
    await _cycles(env, host)
    # Решение и реплики — в одном шаге того же хода: строка вошла в него сразу за результатом инструмента.
    assert _absorbed(env) == [line["uuid"]] and len(_users(env)) == 2
    events = await _events(env, sid)
    assert _says(events) == [
        ("владелец (next): первая", str(first)),
        ("владелец (next): заодно посмотри логи", str(b)),
        ("владелец (next): и не трогай прод", str(c)),
    ]
    assert [e["kind"] for e in events].count("turn_end") == 1
    assert [e["summary"] for e in events if e["kind"] == "text"] == ["готово, и на реплики ответил"]
    assert _warns(events) == [] and not [e for e in events if e["kind"] == "error"]
    rec = await _rec(env, sid)
    assert (rec["state"], rec["queued_says"]) == ("idle", 0)
    assert runner.mid == {} and runner.mid_says == {} and await _interrupted(env, sid) == []


async def test_started_says_are_not_fed_again_while_their_marks_are_on_the_way(env, monkeypatch):
    """Стенд `.runs/f55-stand` (настоящий claude): движок начал строку с двумя репликами, отметки «доставлено»
    ещё шли в ядро — круг хоста подал те же реплики второй строкой, и модель ответила на них дважды."""
    tool = {"tool": "Bash", "input": {"command": "sleep 1"}, "hold": 1.0}
    env.scenario(lifecycle=True, turns=[[tool, {"sleep": 2.0}, {"text": "готово"}], [{"text": "лишний ход"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    await _busy(env, host, sid)
    real = host.held.mark

    async def slow(path, session_id):
        if "/session-says/" in path:
            await asyncio.sleep(0.6)  # круг хоста успевает пройти, пока отметка в пути
        await real(path, session_id)

    monkeypatch.setattr(host.held, "mark", slow)
    await _say(env, sid, "раз")
    await _say(env, sid, "два")
    await env.until(host, lambda: len(_absorbed(env)) >= 1, what="строки вошли в идущий ход")
    for _ in range(12):
        await env.cycle(host)
        await asyncio.sleep(0.1)
    await _turn_done(env, host, sid)
    await _cycles(env, host)
    texts = [_user_text(m) for m in _users(env)]
    assert sum(text.count("раз") for text in texts) == 1 and sum(text.count("два") for text in texts) == 1
    events = await _events(env, sid)
    assert [e["kind"] for e in events].count("turn_end") == 1 and len(_says(events)) == 3
    assert (await _rec(env, sid))["queued_says"] == 0


async def test_say_started_inside_the_turn_is_not_returned_by_stop(env):
    tool = {"tool": "Bash", "input": {"command": "sleep 1"}, "hold": 1.0}
    env.scenario(lifecycle=True, turns=[[tool, {"sleep": 30}], [{"text": "дальше"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    first = await _busy(env, host, sid)
    second = await _say(env, sid, "вдогонку")

    async def taken():
        return await _queued(env, sid) == []

    await env.until(host, taken, what="реплика вошла в идущий ход")
    assert len(_absorbed(env)) == 1
    await _stop(env, sid)
    await env.until(host, lambda: _interrupted(env, sid), what="реплика хода вернулась прерванной")
    await _turn_done(env, host, sid)
    await _cycles(env, host)
    # 2.4: ход начат первой репликой — возвращается она; вошедшая посреди хода — нет.
    assert await _interrupted(env, sid) == [first] and await _queued(env, sid) == [first]
    assert len(_users(env)) == 2
    assert ("владелец (next): вдогонку", str(second)) in _says(await _events(env, sid))


async def test_stop_cancels_the_queued_say_line_and_the_say_stays_queued(env):
    env.scenario(lifecycle=True, turns=[LONG, [{"text": "принято"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    first = await _busy(env, host, sid)
    runner = host.held.runners[sid]
    second = await _say(env, sid, "следом")
    await env.until(host, lambda: list(runner.mid.values()) == ["queued"], what="строка в очереди движка")
    await _stop(env, sid)
    await env.until(host, lambda: _interrupted(env, sid), what="реплика хода вернулась прерванной")
    await _turn_done(env, host, sid)
    await _cycles(env, host, 6)
    # Движок выбросил строку (`cancel_queued`): реплика цела, обычная (не прерванная), и стоит за
    # прерванной — сама не подаётся; потерей сигнала это не считается.
    [stop] = _controls(env, "interrupt")
    assert stop["request"].get("cancel_queued") is True
    says = (await _full(env, sid))["says"]
    assert [(s["id"], s["status"]) for s in says] == [(first, "interrupted"), (second, "queued")]
    assert len(_users(env)) == 2 and runner.mid == {} and runner.mid_says == {}
    events = await _events(env, sid)
    assert _warns(events) == []
    assert str(second) not in [ref for _summary, ref in _says(events)]
    rec = await _rec(env, sid)
    assert rec["state"] == "idle"

    # «Убрать» прерванную — реплика подаётся снова, один раз, обычным вводом.
    assert await _drop_interrupted(env, sid) == [first]
    await env.until(host, lambda: len(_users(env)) == 3, what="реплика подана снова")
    await _turn_done(env, host, sid)
    await _cycles(env, host)
    again = _users(env)[2]
    assert again["uuid"] == session.held_say_uuid(second) != _users(env)[1]["uuid"]
    assert _user_text(again) == "следом" and len(_users(env)) == 3
    events = await _events(env, sid)
    assert [ref for _summary, ref in _says(events)].count(str(second)) == 1
    assert (await _rec(env, sid))["queued_says"] == 0


async def test_process_death_keeps_the_say_and_feeds_it_once_to_the_next_process(env):
    env.scenario(lifecycle=True, turns=[LONG])
    host = await env.started(held_launch_retry_s=0)
    sid, _, _ = await _session(env)
    await _busy(env, host, sid)
    runner = host.held.runners[sid]
    second = await _say(env, sid, "следом")
    await env.until(host, lambda: list(runner.mid.values()) == ["queued"], what="строка в очереди движка")
    env.scenario(lifecycle=True, turns=[[{"text": "принято"}]])
    os.kill(env.calls()[0]["pid"], signal.SIGKILL)
    await env.until(host, lambda: len(env.calls()) == 2, what="процесс поднят снова")
    await env.until(host, lambda: len(_users(env)) == 3, what="реплика подана новому процессу")
    await _turn_done(env, host, sid)
    await _cycles(env, host)
    users = _users(env)
    assert [_user_text(m) for m in users] == ["первая", "следом", "следом"] and len(users) == 3
    assert users[2]["uuid"] == session.held_say_uuid(second)
    events = await _events(env, sid)
    assert [ref for _summary, ref in _says(events)].count(str(second)) == 1
    assert "принято" in [e["summary"] for e in events if e["kind"] == "text"]
    rec = await _rec(env, sid)
    assert (rec["queued_says"], rec["interrupted_says"]) == (0, 0)


async def test_say_line_left_in_the_engine_queue_starts_the_next_turn_as_a_say_turn(env):
    env.scenario(lifecycle=True, turns=[[{"sleep": 1.0}, {"text": "первый"}], LONG, [{"text": "третий"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    await _busy(env, host, sid)
    runner = host.held.runners[sid]
    second = await _say(env, sid, "следом")
    await env.until(host, lambda: len(_users(env)) == 2, what="реплика подана во время хода")

    async def second_turn():
        events = await _events(env, sid)
        return runner.busy and runner.turn_says and "первый" in [e["summary"] for e in events]

    # Первый ход кончился без границы инструмента: движок начал по строке новый ход — это ход по реплике.
    await env.until(host, second_turn, what="ход по строке из очереди движка")
    assert [s["id"] for s in runner.turn_says] == [second] and runner.turn_say_id == second
    assert not runner.turn_by_signal and await _queued(env, sid) == []
    await env.until(host, _state(env, sid, "running"), what="ход идёт")
    # «Стоп» на таком ходе возвращает реплику прерванной, как у хода, начатого обычной подачей.
    await _stop(env, sid)
    await env.until(host, lambda: _interrupted(env, sid), what="реплика вернулась прерванной")
    await _turn_done(env, host, sid)
    assert await _interrupted(env, sid) == [second]
    rec = await _rec(env, sid)
    assert (rec["state"], rec["queued_says"], rec["interrupted_says"]) == ("idle", 1, 1)


async def test_gap_timeout_refeeds_the_say_without_a_lost_signal(env, monkeypatch):
    monkeypatch.setattr(held_mod, "MID_GAP_S", 1.0)
    env.scenario(lifecycle=True, lifecycle_stall=True, turns=[[{"sleep": 0.8}, {"text": "первый"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    await _busy(env, host, sid)
    runner = host.held.runners[sid]
    second = await _say(env, sid, "следом")
    await env.until(host, lambda: runner.mid_gap is not None, what="зазор: ход кончился, строка не начата")
    assert runner.busy and len(_users(env)) == 2
    await env.until(host, lambda: len(_users(env)) == 3, what="срок вышел — реплика подана снова")
    # Прежнюю строку движку велено выбросить; сигнал потерянным не объявлен.
    assert any(c["request"].get("cancel_queued") for c in _controls(env, "interrupt"))
    assert _users(env)[2]["uuid"] == session.held_say_uuid(second)
    assert _warns(await _events(env, sid)) == []
    with env.db.reader() as conn:
        lost = conn.execute("SELECT signal_lost_ts FROM main.hub_session WHERE id = ?", (sid,)).fetchone()
    assert lost[0] is None


async def test_pause_holds_the_say_of_a_busy_session(env):
    env.scenario(lifecycle=True, turns=[LONG])
    host = await env.started()
    sid, _, _ = await _session(env)
    await _busy(env, host, sid)
    host.on_rate_limit({"status": "rejected"})
    second = await _say(env, sid, "следом")
    await _cycles(env, host, 6)
    # П8 Ф41-4: на паузе лимита подписки реплика живому процессу не подаётся — и посреди хода тоже.
    assert len(_users(env)) == 1 and await _queued(env, sid) == [second]
    assert host.held.runners[sid].mid == {}


async def test_sleep_request_and_interrupted_say_hold_mid_turn_feed(env):
    env.scenario(lifecycle=True, turns=[LONG, LONG])
    host = await env.started()
    sid, _, _ = await _session(env)
    first = await _busy(env, host, sid)
    await _stop(env, sid)
    await env.until(host, lambda: _interrupted(env, sid), what="реплика вернулась прерванной")
    await _turn_done(env, host, sid)
    urgent = await _say(env, sid, "срочно", "now")
    await env.until(host, lambda: len(_users(env)) == 2, what="срочная подана")
    await env.until(host, lambda: host.held.runners[sid].busy, what="ход идёт")
    held_back = await _say(env, sid, "обычная за прерванной")
    await _cycles(env, host, 6)
    # За прерванной репликой несрочная не подаётся — и посреди идущего хода тоже.
    assert len(_users(env)) == 2 and _users(env)[1]["uuid"] == session.held_say_uuid(urgent)
    assert await _interrupted(env, sid) == [first] and held_back in await _queued(env, sid)
    assert host.held.runners[sid].mid == {}


async def test_engine_without_input_queue_events_gets_no_mid_turn_says(env):
    env.scenario(turns=[[{"sleep": 0.8}, {"text": "первый"}], [{"text": "второй"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    await _running(env, host, sid, "первая")
    runner = host.held.runners[sid]
    await env.until(host, lambda: runner.busy, what="ход идёт")
    await _say(env, sid, "следом")
    await _cycles(env, host)
    assert not runner.lifecycle_seen and len(_users(env)) == 1 and runner.mid == {}
    await env.until(host, lambda: len(_users(env)) == 2, what="реплика подана после хода")
    await _turn_done(env, host, sid)
    assert [e["summary"] for e in await _events(env, sid) if e["kind"] == "text"] == ["первый", "второй"]


# --- 2.4: срочная и «Стоп» на ходе пачки --------------------------------------------------------------------


async def test_urgent_say_drops_the_queued_line_and_goes_first(env):
    env.scenario(lifecycle=True, turns=[LONG, [{"text": "срочная принята"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    first = await _busy(env, host, sid)
    runner = host.held.runners[sid]
    plain = await _say(env, sid, "обычная")
    await env.until(host, lambda: list(runner.mid.values()) == ["queued"], what="строка в очереди движка")
    urgent = await _say(env, sid, "срочно", "now")
    await env.until(host, lambda: len(_users(env)) == 3, what="срочная подана")
    await _turn_done(env, host, sid)
    await _cycles(env, host)
    # Ход прерван один раз, строка с обычной репликой выброшена движком; оборванная первая держит обычную.
    [stop] = _controls(env, "interrupt")
    assert stop["request"].get("cancel_queued") is True
    assert _users(env)[2]["uuid"] == session.held_say_uuid(urgent) and _user_text(_users(env)[2]) == "срочно"
    says = (await _full(env, sid))["says"]
    assert [(s["id"], s["status"]) for s in says] == [(first, "interrupted"), (plain, "queued")]
    assert len(_users(env)) == 3
    assert _warns(await _events(env, sid)) == []


async def test_stop_on_a_batch_turn_returns_every_say_of_the_batch(env):
    env.scenario(lifecycle=True, turns=[LONG, [{"text": "вторая принята"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    a = await _say(env, sid, "раз")
    b = await _say(env, sid, "два")
    await env.until(host, lambda: len(_users(env)) == 1, what="пачка подана")
    await env.until(host, _state(env, sid, "running"), what="ход идёт")

    async def both_marked():
        return await _queued(env, sid) == []

    await env.until(host, both_marked, what="обе отмечены доставленными")
    await _stop(env, sid)
    await env.until(host, lambda: _interrupted(env, sid), what="реплики вернулись прерванными")
    await _turn_done(env, host, sid)
    await _cycles(env, host, 6)
    assert await _interrupted(env, sid) == [a, b]
    rec = await _rec(env, sid)
    assert (rec["state"], rec["queued_says"], rec["interrupted_says"]) == ("idle", 2, 2)
    events = await _events(env, sid)
    cut = [e["ref"] for e in events if e["kind"] == "say" and "прервана" in e["summary"]]
    assert sorted(cut) == sorted([str(a), str(b)]) and len(_users(env)) == 1
    # Решение владельца — по каждой: первую убрал, вторую отправил снова.
    r = await env.ui("DELETE", f"/sessions/{sid}/says/{a}")
    assert r.status_code == 200, r.text
    await _cycles(env, host)
    assert len(_users(env)) == 1
    r = await env.ui("POST", f"/sessions/{sid}/says/{b}/resend")
    assert r.status_code == 200, r.text
    await env.until(host, lambda: len(_users(env)) == 2, what="вторая подана снова")
    await _turn_done(env, host, sid)
    assert _user_text(_users(env)[1]) == "два"
    assert (await _rec(env, sid))["queued_says"] == 0


# --- 2.3: OpenCode ------------------------------------------------------------------------------------------


async def test_oc_says_wait_for_the_turn_end_and_go_as_one_batch(env, oc):  # noqa: F811
    oc.scenario([{"sleep": 1.0}, {"text": "первый"}], [{"text": "на обе"}])
    host = await started(env)
    sid, _, _ = await _oc_session(env, model="deepseek/deepseek-flash")
    await _say(env, sid, "раз")
    await env.until(host, lambda: len(oc.prompts()) == 1, what="первый ход")
    b = await _say(env, sid, "два")
    c = await _say(env, sid, "три")
    await _cycles(env, host)
    assert len(oc.prompts()) == 1  # посреди хода OpenCode реплики не получает
    await env.until(host, lambda: len(oc.prompts()) == 2, what="пачка подана")
    await _turn_done(env, host, sid)
    parts = oc.prompts()[1]["body"]["parts"]
    assert [p["text"] for p in parts if p["type"] == "text"] == [_batch("два", "три")]
    assert [ref for _summary, ref in _says(await _events(env, sid))][1:] == [str(b), str(c)]
    assert (await _rec(env, sid))["queued_says"] == 0


async def test_turn_end_carries_says_absorbed_inside_the_turn(env, monkeypatch):
    """Ревью Ф56, круг 2 (4): реплика вошла в идущий ход строкой посреди него, отметка «доставлено» до ядра
    не дошла — конец хода несёт её в `delivered_says` (как и реплику, начавшую ход), ядро отмечает её само."""
    tool = {"tool": "Bash", "input": {"command": "sleep 1"}, "hold": 1.0}
    env.scenario(lifecycle=True, turns=[[tool, {"sleep": 1.0}, {"text": "готово"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    first = await _busy(env, host, sid)
    real_mark, real_turn, bodies = host.held.mark, host.held.turn_result, []

    async def lost(path, session_id):
        if "/session-says/" not in path:
            await real_mark(path, session_id)  # отметка реплики не доходит никогда — только конец хода

    async def turn(session_id, **fields):
        bodies.append(fields)
        return await real_turn(session_id, **fields)

    monkeypatch.setattr(host.held, "mark", lost)
    monkeypatch.setattr(host.held, "turn_result", turn)
    second = await _say(env, sid, "вдогонку")
    await env.until(host, lambda: len(_absorbed(env)) >= 1, what="строка вошла в идущий ход")
    await _turn_done(env, host, sid)
    done = [b for b in bodies if b.get("state") == "idle" and "answer" in b]
    assert len(done) == 1 and done[0]["delivered_says"] == [first, second], bodies
    with env.db.reader() as conn:
        rows = conn.execute("SELECT id, status FROM main.held_say WHERE session_id = ? ORDER BY id", (sid,))
        assert rows.fetchall() == [(first, "delivered"), (second, "delivered")]
    assert (await _rec(env, sid))["queued_says"] == 0
