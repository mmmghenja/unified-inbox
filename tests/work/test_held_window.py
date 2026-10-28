"""Ф41-3, части 3а и 3б в хосте постоянных сессий (docs/architecture.md, 4; 3.2, 3.4). Ядро — в процессе, `claude` —
подставной (`fake_claude.py`)."""

import asyncio
import time

from hub.agent.core_api import CoreUnavailable

from .conftest import flag_value
from .test_held_host import (
    _drop_interrupted,
    _events,
    _has_event,
    _rec,
    _running,
    _say,
    _session,
    _state,
    _turn_done,
)

HUNKS = [
    {"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 2, "lines": ["-x = 10", "+x = 20", "+y = 1"]}
]
USAGE = {
    "input_tokens": 26,
    "output_tokens": 454,
    "cache_read_input_tokens": 184_883,
    "cache_creation_input_tokens": 68_251,
    "iterations": [
        {"input_tokens": 8, "cache_read_input_tokens": 86_907, "cache_creation_input_tokens": 266}
    ],
}
MODEL_USAGE = {"sonnet": {"contextWindow": 200_000}}


async def _event(env, sid, event_id) -> dict:
    r = await env.ui("GET", f"/sessions/{sid}/events/{event_id}")
    assert r.status_code == 200, r.text
    return r.json()


def _spy_live(host, monkeypatch, *, fail=None):
    """Запросы хоста на ручку `live`: (монотонное время, тело). `fail` — исключение вместо отправки."""
    posts: list[tuple[float, dict]] = []
    real = host.core.post

    async def post(path, json=None):
        if path.endswith("/live"):
            posts.append((time.monotonic(), dict(json)))
            if fail is not None:
                raise fail
        return await real(path, json=json)

    monkeypatch.setattr(host.core, "post", post)
    return posts


# --- 3а: шаги и расход --------------------------------------------------------------------------------------


async def test_steps_carry_diff_output_input_and_turn_usage(env):
    project = pid, workdir = env.project("Initech")
    inside = str(workdir / "src" / "a.py")
    long_command = "set -e\n" + "echo шаг; " * 60
    env.scenario(
        init_model="sonnet",
        turns=[
            [
                {
                    "tool": "Edit",
                    "input": {"file_path": inside, "old_string": "x = 10", "new_string": "x = 20"},
                    "result": f"The file {inside} has been updated successfully.",
                    "use_result": {"filePath": inside, "structuredPatch": HUNKS, "originalFile": "x = 10\n"},
                },
                {
                    "tool": "Write",
                    "input": {"file_path": str(workdir / "new.txt"), "content": "раз\nдва\n"},
                    "result": "File created successfully",
                    "use_result": {"type": "create", "content": "раз\nдва\n", "structuredPatch": []},
                },
                {"tool": "Bash", "input": {"command": "ls"}, "result": "a.py\nb.py\nAPI_KEY=abc999\n"},
                {"tool": "Bash", "input": {"command": long_command, "description": "Шаги"}, "result": ""},
                {"tool": "Read", "input": {"file_path": "/etc/hosts"}, "result": "СОДЕРЖИМОЕ-ФАЙЛА"},
                {"tool": "mcp__hub__hub_read", "input": {"channel_id": 7}, "result": "лента"},
                {"tool": "Bash", "input": {"command": "false"}, "result": "сбой команды", "is_error": True},
                {"result_extra": {"usage": USAGE, "total_cost_usd": 0.5, "modelUsage": MODEL_USAGE}},
            ],
            [{"text": "готово"}, {"result_extra": {"usage": USAGE, "total_cost_usd": 0.75}}],
        ],
    )
    host = await env.started()
    sid, _, _ = await _session(env, project=project)
    assert (await _rec(env, sid))["usage"] is None
    await _running(env, host, sid, "правь")
    await _turn_done(env, host, sid)
    events = await _events(env, sid)
    tools = [e for e in events if e["kind"] == "tool"]
    done = [e for e in events if e["kind"] == "tool_done"]
    assert [e["tool"] for e in tools] == [
        "Edit",
        "Write",
        "Bash",
        "Bash",
        "Read",
        "mcp__hub__hub_read",
        "Bash",
    ]

    # 2.6 п.4: путь внутри рабочей папки — относительный, вне её — как есть.
    assert [e["summary"] for e in tools[:2]] == ["src/a.py", "new.txt"]
    assert tools[4]["summary"] == "/etc/hosts"
    # `tool`: у правки и чтения подробностей нет; короткая команда — тоже; длинная и прочие — вход по клику.
    assert [e["has_detail"] for e in tools] == [False, False, False, True, False, True, False]
    assert all(e["brief"] is None for e in tools)
    assert (await _event(env, sid, tools[3]["id"]))["detail"] == {
        "type": "input",
        "command": long_command,
        "description": "Шаги",
    }
    assert (await _event(env, sid, tools[5]["id"]))["detail"] == {"type": "input", "input": {"channel_id": 7}}

    # `tool_done`: diff правки и нового файла, вывод команды, у Read — ничего, ошибка — текст.
    edit, write, ls, quiet, read, mcp, failed = done
    assert edit["brief"] == {"type": "diff", "file": "src/a.py", "added": 2, "removed": 1}
    assert (await _event(env, sid, edit["id"]))["detail"] == {
        "type": "diff",
        "file": "src/a.py",
        "patch": "@@ -1,1 +1,2 @@\n-x = 10\n+x = 20\n+y = 1",
        "cut": False,
    }
    assert write["brief"] == {"type": "diff", "file": "new.txt", "added": 2, "removed": 0}
    assert (await _event(env, sid, write["id"]))["detail"]["patch"] == "@@ -0,0 +1,2 @@\n+раз\n+два"
    assert ls["brief"] == {"type": "output", "lines": 3}
    assert (await _event(env, sid, ls["id"]))["detail"] == {
        "type": "output",
        "text": "a.py\nb.py\nAPI_KEY=***\n",  # секреты в выводе — под маской
        "cut": False,
    }
    assert (quiet["brief"], quiet["has_detail"]) == ({"type": "output", "lines": 0}, False)
    assert (read["brief"], read["has_detail"], read["summary"]) == (None, False, "готово (16 симв.)")
    assert mcp["brief"] == {"type": "output", "lines": 1} and mcp["has_detail"] is True
    assert failed["is_error"] is True and failed["brief"] == {"type": "output", "lines": 1}
    assert (await _event(env, sid, failed["id"]))["detail"]["text"] == "сбой команды"
    # Старые клиенты читают строку: она прежняя.
    assert edit["summary"].startswith("готово (") and failed["summary"].startswith("ошибка (")
    with env.db.reader() as conn:
        dump = "\n".join(str(row) for row in conn.execute("SELECT summary, detail_json FROM main.held_event"))
    assert "СОДЕРЖИМОЕ-ФАЙЛА" not in dump and "abc999" not in dump

    # Конец хода: длительность, шаги, цена и токены; строка — прежняя.
    [end] = [e for e in events if e["kind"] == "turn_end"]
    brief = end["brief"]
    assert end["summary"].startswith("ход окончен: шагов ") and end["has_detail"] is False
    assert brief["type"] == "usage" and brief["cost_usd"] == 0.5 and brief["steps"] == 8
    assert isinstance(brief["duration_ms"], int) and brief["duration_ms"] >= 0
    assert brief["tokens"] == {"input": 26, "output": 454, "cache_read": 184_883, "cache_write": 68_251}
    # Расход — в записи сессии: контекст последнего вызова модели, предел модели хода, цена.
    usage = (await _rec(env, sid))["usage"]
    assert (usage["context_tokens"], usage["context_limit"], usage["cost_usd"]) == (87_181, 200_000, 0.5)

    # Второй ход того же процесса: `total_cost_usd` накопительный — цена хода есть разность, ядро копит сумму.
    await _say(env, sid, "ещё")
    await env.until(host, _turn_ends(env, sid, 2), what="конец второго хода")
    await _turn_done(env, host, sid)
    ends = [e for e in await _events(env, sid) if e["kind"] == "turn_end"]
    assert ends[1]["brief"]["cost_usd"] == 0.25
    usage = (await _rec(env, sid))["usage"]
    assert (usage["cost_usd"], usage["context_limit"]) == (0.75, None)  # во втором `result` нет `modelUsage`
    assert pid and len(env.calls()) == 1


def _turn_ends(env, sid, count):
    """Дождаться конца хода по ленте: «реплика подана» ещё не значит, что раннер отметил ход начатым."""

    async def check():
        return sum(1 for e in await _events(env, sid) if e["kind"] == "turn_end") >= count

    return check


async def test_engine_without_new_fields_gives_plain_events_and_turn_goes_on(env, monkeypatch):
    """П11: другая версия движка — нет `tool_use_result`, `usage`, частичных сообщений."""
    project = _pid, workdir = env.project("Initech")
    env.scenario(
        turns=[
            [
                {"text": "смотрю"},
                {"tool": "Edit", "input": {"file_path": str(workdir / "a.py")}, "result": "updated"},
                {
                    "tool": "Write",
                    "input": {"file_path": str(workdir / "b.py")},
                    "result": "ok",
                    "use_result": "x",
                },
                {"emit": {"type": "stream_event"}},
                {"emit": {"type": "stream_event", "event": {"type": "content_block_delta", "index": "x"}}},
                {"emit": {"type": "user", "message": {"content": [{"type": "tool_result", "content": 5}]}}},
                {"text": "итог"},
            ]
        ]
    )
    host = await env.started()
    posts = _spy_live(host, monkeypatch)
    sid, _, _ = await _session(env, project=project)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    events = await _events(env, sid)
    assert [e["kind"] for e in events if e["kind"] in ("text", "tool", "tool_done", "turn_end", "error")] == [
        "text",
        "tool",
        "tool_done",
        "tool",
        "tool_done",
        "tool_done",
        "text",
        "turn_end",
    ]
    done = [e for e in events if e["kind"] == "tool_done"]
    assert all((e["brief"], e["has_detail"]) == (None, False) for e in done)
    assert all(e["ref"] is None for e in events if e["kind"] == "text")  # потока не было — `ref` нет
    [end] = [e for e in events if e["kind"] == "turn_end"]
    assert end["brief"]["cost_usd"] is None and end["brief"]["tokens"] is None
    rec = await _rec(env, sid)
    assert rec["usage"] is None and rec["state"] == "idle"
    await asyncio.sleep(0.2)
    assert posts == []  # без частичных сообщений `live` не шлётся


async def test_missing_project_rules_file_is_not_a_feed_event(env):
    """2.6 п.1: у проекта просто нет `.claude/hub.md` — в ленте тишина; файл есть, но негодный — `warn`."""
    (env.home / ".claude").mkdir()
    (env.home / ".claude" / "hub-rules.md").write_text("ОБЩЕЕ", encoding="utf-8")
    host = await env.started()
    first, pid, workdir = await _session(env, name="без правил")
    await _running(env, host, first)
    await _turn_done(env, host, first)
    assert [e for e in await _events(env, first) if e["kind"] == "warn"] == []
    assert not (workdir / ".claude" / "hub.md").exists()
    # В состоянии хоста предупреждение остаётся, как раньше.
    [kept] = host.state["rules_warnings"][str(pid)]
    assert kept.endswith(".claude/hub.md: нет файла")

    second, _, broken = await _session(env, name="битые правила")
    (broken / ".claude").mkdir()
    (broken / ".claude" / "hub.md").symlink_to(broken / "нет-такого")
    await _running(env, host, second)
    await _turn_done(env, host, second)
    warns = [e["summary"] for e in await _events(env, second) if e["kind"] == "warn"]
    assert len(warns) == 1 and warns[0].startswith("правила сессии: ") and "hub.md: битая ссылка" in warns[0]


async def test_missing_global_rules_file_still_warns(env):
    host = await env.started()
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    warns = [e["summary"] for e in await _events(env, sid) if e["kind"] == "warn"]
    assert len(warns) == 1 and "hub-rules.md: нет файла" in warns[0]


async def test_tool_refused_after_interrupt_is_one_russian_line(env):
    """2.6 п.2: после «прервать» отказ инструмента — «инструмент прерван», без английского текста движка."""
    env.scenario(
        turns=[
            [
                {"tool": "Bash", "input": {"command": "false"}, "result": "обычная ошибка", "is_error": True},
                {"tool": "Bash", "input": {"command": "sleep 40"}, "result": "готово", "hold": 30},
            ],
            [{"tool": "Bash", "input": {"command": "false"}, "result": "снова ошибка", "is_error": True}],
        ]
    )
    host = await env.started()
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    await env.until(host, _has_event(env, sid, "tool", "sleep 40"), what="долгая команда")
    r = await env.ui("POST", f"/sessions/{sid}/interrupt")
    assert r.status_code == 200
    await env.until(host, _has_event(env, sid, "interrupt", "прервано"), what="прервано")
    await env.until(host, _state(env, sid, "idle"), what="idle")
    events = await _events(env, sid)
    done = [e for e in events if e["kind"] == "tool_done"]
    # Ошибка ДО прерывания — как раньше; отказ после него — одной строкой, не ошибкой и без подробностей.
    assert done[0]["is_error"] is True and done[0]["summary"].startswith("ошибка (")
    assert (done[1]["summary"], done[1]["is_error"], done[1]["brief"], done[1]["has_detail"]) == (
        "инструмент прерван",
        False,
        None,
        False,
    )
    assert "doesn't want" not in "\n".join(e["summary"] for e in events)
    # Следующий ход: прерывание кончилось вместе с ходом — ошибка снова ошибка.
    assert len(await _drop_interrupted(env, sid)) == 1  # Ф48-2 (A): иначе очередь стоит на прерванной
    await _say(env, sid, "дальше")
    await env.until(host, _turn_ends(env, sid, 1), what="конец второго хода")
    await _turn_done(env, host, sid)
    last = [e for e in await _events(env, sid) if e["kind"] == "tool_done"][-1]
    assert last["is_error"] is True and last["summary"].startswith("ошибка (")


# --- 3б: текст по мере набора -------------------------------------------------------------------------------


async def test_partial_messages_flag_is_on_by_default(env):
    env.scenario(sessions={"1": {"turns": [[{"sleep": 0.2}]]}})
    host = await env.started()
    sid, _, _ = await _session(env, project=env.project("Initech"))
    await _say(env, sid)
    await env.until(host, lambda: len(env.calls()) == 1, what="процесс сессии")
    argv = env.calls()[0]["argv"]
    assert "--include-partial-messages" in argv
    assert flag_value(argv, "--output-format") == "stream-json"
    await _turn_done(env, host, sid)


async def test_partial_messages_flag_has_a_switch(env):
    host = await env.started(held_partial=False)
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    assert "--include-partial-messages" not in env.calls()[0]["argv"]
    await _turn_done(env, host, sid)


async def test_text_deltas_become_live_states_with_throttle_done_and_ref(env, monkeypatch):
    chunks = [f"слово{i} " for i in range(12)]
    full = "".join(chunks)
    env.scenario(
        turns=[
            [
                {
                    "stream": {
                        "id": "msg_A",
                        "delay": 0.05,
                        "usage": {"input_tokens": 5, "cache_read_input_tokens": 1000},
                        "blocks": [
                            {"type": "thinking", "chunks": ["", ""]},  # у Claude Code текста размышлений нет
                            {"type": "text", "chunks": chunks},
                            {"type": "tool_use", "chunks": ['{"file', '_path": "a"}']},
                            {
                                "type": "text",
                                "chunks": [" ", "\n"],
                                "final": " \n",
                            },  # пустой блок: события нет
                            {"type": "text", "chunks": ["и ещё"]},
                        ],
                    }
                },
                # Поток подагента (`parent_tool_use_id`) в окно сессии не идёт.
                {
                    "stream": {
                        "id": "msg_SUB",
                        "parent": "toolu_1",
                        "blocks": [{"type": "text", "chunks": ["чужое"]}],
                    }
                },
                {"stream": {"id": "msg_B", "blocks": [{"type": "text", "chunks": ["второе ", "сообщение"]}]}},
            ]
        ]
    )
    host = await env.started(held_live_min_s=0.3)
    posts = _spy_live(host, monkeypatch)
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    await env.until(host, lambda: sid not in host.held.live_tasks, what="отправка live закончилась")

    assert all(body["kind"] == "text" and body["cut"] is False for _t, body in posts)
    assert {body["block"] for _t, body in posts} == {"msg_A:1", "msg_A:3", "msg_A:4", "msg_B:0"}
    first = [(t, body) for t, body in posts if body["block"] == "msg_A:1"]
    # Текст — весь на этот момент (не добавка) и только растёт; последнее состояние — `done` с полным текстом.
    texts = [body["text"] for _t, body in first]
    assert all(full.startswith(text) and text for text in texts) and texts == sorted(texts, key=len)
    assert first[-1][1] == {"block": "msg_A:1", "kind": "text", "text": full, "cut": False, "done": True}
    assert [body["done"] for _t, body in first[:-1]] == [False] * (len(first) - 1)
    # Не чаще раза в 300 мс: 12 дельт за ~0,6 с схлопнулись; `done` ушёл сразу, не дожидаясь срока.
    undone = [t for t, body in first if not body["done"]]
    assert 1 <= len(undone) < len(chunks)  # под нагрузкой ход идёт дольше — жёсткого числа кадров нет
    assert all(b - a >= 0.28 for a, b in zip(undone, undone[1:], strict=False))
    # `done` каждого блока дошёл, хотя следом сразу шёл следующий блок.
    assert [body["block"] for _t, body in posts if body["done"]] == [
        "msg_A:1",
        "msg_A:3",
        "msg_A:4",
        "msg_B:0",
    ]
    last = [body for _t, body in posts if body["block"] == "msg_B:0"][-1]
    assert (last["text"], last["done"]) == ("второе сообщение", True)

    # Окончательный текст — событием `text` с `ref` = тем же блоком (окно меняет «пишет…» на событие).
    texts = [(e["summary"], e["ref"]) for e in await _events(env, sid) if e["kind"] == "text"]
    assert texts == [(full.strip(), "msg_A:1"), ("и ещё", "msg_A:4"), ("второе сообщение", "msg_B:0")]
    # Расход: в `result` нет `iterations` — контекст взят из последнего `message_delta`.
    assert (await _rec(env, sid))["usage"]["context_tokens"] == 1005
    # Запись в памяти ядра снята концом хода.
    assert env.app.state.live.get(sid, int(time.time() * 1000)) is None


async def test_live_reaches_core_stream_while_turn_runs(env):
    env.scenario(
        turns=[
            [
                {"stream": {"id": "msg_A", "blocks": [{"type": "text", "chunks": ["пишу", " ответ"]}]}},
                {"sleep": 30},
            ]
        ]
    )
    host = await env.started(held_live_min_s=0.05)
    sid, _, _ = await _session(env)
    await _running(env, host, sid)

    def entry():
        got = env.app.state.live.get(sid, int(time.time() * 1000))
        return got if got is not None and got["done"] else None

    got = await env.until(host, entry, what="запись live в ядре")
    assert (got["block"], got["kind"], got["text"], got["done"]) == ("msg_A:0", "text", "пишу ответ", True)
    await env.ui("POST", f"/sessions/{sid}/sleep")
    await env.until(host, _state(env, sid, "asleep"), what="усыплена")
    assert env.app.state.live.get(sid, int(time.time() * 1000)) is None


STREAM_3 = {"id": "m", "delay": 0.15, "blocks": [{"type": "text", "chunks": ["раз ", "два ", "три"]}]}


async def test_done_does_not_wait_for_the_throttle(env, monkeypatch):
    """Живой стенд (прогон 1): конец блока ждал срока троттлинга. `done` будит отправку и уходит сразу —
    посреди хода, не дожидаясь ни срока, ни конца хода."""
    env.scenario(turns=[[{"stream": STREAM_3}, {"sleep": 4}]])
    host = await env.started(held_live_min_s=30)
    posts = _spy_live(host, monkeypatch)
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    await env.until(host, lambda: posts and posts[-1][1]["done"], limit_s=2.5, what="done посреди хода")
    assert host.held.runners[sid].busy  # ход ещё идёт (команда после текста)
    # Срок 30 с: до него дошли только первое состояние и `done` — промежуточные схлопнулись.
    (_t0, head), (_t1, tail) = posts
    assert head["done"] is False and head["text"] in ("раз ", "раз два ")
    assert (tail["text"], tail["done"]) == ("раз два три", True)
    await _turn_done(env, host, sid)
    assert len(posts) == 2


async def test_last_done_is_sent_before_the_turn_is_closed(env, monkeypatch):
    """Конец последнего блока и конец хода идут в потоке подряд: хост досылает `done` ДО `turn` (ядро держит
    запись `live` только при `running` и опоздавший `done` отбросило бы)."""
    env.scenario(turns=[[{"stream": STREAM_3}]] * 3)
    host = await env.started(held_live_min_s=30)
    order: list[str] = []
    spied = host.core.post

    async def post(path, json=None):
        if path.endswith("/live"):
            order.append("done" if json["done"] else "text")
        elif path.endswith("/turn") and (json or {}).get("state") == "idle":
            order.append("turn")
        result = await spied(path, json=json)
        if path.endswith("/live"):
            order.append("live принят")
        return result

    monkeypatch.setattr(host.core, "post", post)
    sid, _, _ = await _session(env)
    for _ in range(3):  # гонка читателя потока и задачи отправки — не один раз
        await _say(env, sid)
        await env.until(host, lambda: "turn" in order, what="конец хода")
        await _turn_done(env, host, sid)
        # Каждый `live` принят ядром раньше, чем ушёл `turn`; последний из них — `done`.
        assert order[-1] == "turn" and order[-3:-1] == ["done", "live принят"], order
        assert order.count("text") + order.count("done") == order.count("live принят")
        order.clear()


async def test_long_block_keeps_the_tail_and_marks_cut(env, monkeypatch):
    chunks = ["а" * 3000, "б" * 3000, "в" * 3000]
    env.scenario(
        turns=[[{"stream": {"id": "m", "delay": 0.02, "blocks": [{"type": "text", "chunks": chunks}]}}]]
    )
    host = await env.started(held_live_min_s=0.01)
    posts = _spy_live(host, monkeypatch)
    sid, _, _ = await _session(env)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    await env.until(host, lambda: sid not in host.held.live_tasks, what="отправка live закончилась")
    last = posts[-1][1]
    assert last["done"] is True and last["cut"] is True and len(last["text"]) == 8000
    assert last["text"] == ("а" * 3000 + "б" * 3000 + "в" * 3000)[-8000:]
    assert all(len(body["text"]) <= 8000 for _t, body in posts)
    [text] = [e for e in await _events(env, sid) if e["kind"] == "text"]
    assert text["ref"] == "m:0" and len(text["summary"]) == 8000


async def test_core_refusing_live_does_not_break_turn_or_events(env, monkeypatch, caplog):
    env.scenario(
        turns=[
            [
                {"stream": {"id": "m", "blocks": [{"type": "text", "chunks": ["ТЕКСТ-ОТВЕТА ", "целиком"]}]}},
                {"tool": "Bash", "input": {"command": "ls"}, "result": "a"},
            ]
        ]
    )
    host = await env.started(held_live_min_s=0.01)
    posts = _spy_live(host, monkeypatch, fail=CoreUnavailable("ядро недоступно"))
    sid, _, _ = await _session(env)
    with caplog.at_level("DEBUG", logger="hub.work"):
        await _running(env, host, sid)
        await _turn_done(env, host, sid)
        await env.until(host, lambda: sid not in host.held.live_tasks, what="отправка live закончилась")
    assert posts and posts[-1][1]["done"] is True
    kinds = [e["kind"] for e in await _events(env, sid)]
    assert "error" not in kinds and kinds.count("text") == 1 and "tool_done" in kinds and "turn_end" in kinds
    assert (await _rec(env, sid))["state"] == "idle"
    log = "\n".join(r.getMessage() for r in caplog.records)
    assert "ТЕКСТ-ОТВЕТА" not in log  # текст в журнал хоста не попадает и при сбое
    # Сбой пойман самой отправкой (задача не падает исключением) и записан тихо — уровнем debug.
    refused = [r.levelname for r in caplog.records if "live не принят (CoreUnavailable)" in r.getMessage()]
    assert refused and set(refused) == {"DEBUG"} and len(refused) == len(posts)
