"""Ф41-3, части 3а и 3б для постоянных сессий на OpenCode (docs/architecture.md): дельты текста и размышлений, diff
правки, расход и предел контекста. `opencode` — ПОДДЕЛЬНЫЙ сервер
`tests/engine/fake_opencode.py`; настоящий не запускается."""

import asyncio
import json

from .oc_helpers import (
    oc,  # noqa: F401 — фикстура `oc`
    started,
)
from .test_held_host import _drop_interrupted, _events, _has_event, _rec, _say, _session, _turn_done
from .test_held_opencode import _running
from .test_held_window import _event, _spy_live, _turn_ends

SECRET = "sk-КЛЮЧ-ПРОВАЙДЕРА-НЕ-ДОЛЖЕН-УТЕЧЬ"
PROVIDERS = {
    "providers": [
        {"id": "other", "key": SECRET, "models": {"deepseek-flash": {"limit": {"context": 5}}}},
        {
            "id": "deepseek",
            "key": SECRET,
            "models": {
                "deepseek-flash": {
                    "limit": {"context": 1_000_000, "output": 393_216},
                    "capabilities": {"input": {"image": True, "pdf": False}},
                }
            },
        },
    ]
}
TOKENS = {
    "total": 66_204,
    "input": 64_482,
    "output": 45,
    "reasoning": 13,
    "cache": {"read": 1664, "write": 7},
}


def _patch(path: str) -> str:
    return (
        f"Index: {path}\n=====\n--- {path}\n+++ {path}\n"
        "@@ -1,2 +1,2 @@\n def greet(name):\n-    return nam\n+    return name\n"
    )


async def test_oc_stream_diff_usage_and_no_provider_key_anywhere(env, oc, monkeypatch, caplog):  # noqa: F811
    project = _pid, workdir = env.project("Umbrella")
    path = str(workdir / "app.py")
    thinking = ["Simple. ", "Colors and edit."]
    answer = ["Красный, ", "оранжевый, ", "жёлтый."]
    oc.scenario(
        [
            {"stream": {"kind": "reasoning", "chunks": thinking, "delay": 0.02}},
            {
                "tool": {
                    "tool": "edit",
                    "args": {"filePath": path, "oldString": "nam", "newString": "name"},
                    "output": "Edit applied successfully.",
                    "metadata": {
                        "diff": _patch(path),
                        "filediff": {"file": path, "patch": _patch(path), "additions": 1, "deletions": 1},
                    },
                }
            },
            {
                "tool": {
                    "tool": "bash",
                    "args": {"command": "ls"},
                    "output": "a\nb\n",
                    "metadata": {"exit": 0},
                }
            },
            {"stream": {"kind": "text", "chunks": answer, "delay": 0.02}},
        ],
        providers=PROVIDERS,
        tokens=TOKENS,
    )
    host = await started(env, held_live_min_s=0.01)
    posts = _spy_live(host, monkeypatch)
    sid, _, _ = await _session(env, project=project, engine="opencode")
    with caplog.at_level("DEBUG"):
        await _running(env, host, oc, sid, "исправь")
        await _turn_done(env, host, sid)
        await env.until(host, lambda: sid not in host.held.live_tasks, what="отправка live закончилась")
    events = await _events(env, sid)

    # Текст по мере набора: размышления — `thinking`, ответ — `text`; ключ блока — id части.
    kinds = {body["block"]: body["kind"] for _t, body in posts}
    assert sorted(kinds.values()) == ["text", "thinking"]
    think_block = next(b for b, k in kinds.items() if k == "thinking")
    text_block = next(b for b, k in kinds.items() if k == "text")
    assert think_block.startswith("prt_") and text_block.startswith("prt_")
    final = {body["block"]: body for _t, body in posts if body["done"]}
    assert final[think_block]["text"] == "".join(thinking) and final[text_block]["text"] == "".join(answer)
    for block, full in ((think_block, "".join(thinking)), (text_block, "".join(answer))):
        texts = [body["text"] for _t, body in posts if body["block"] == block]
        assert all(full.startswith(t) and t for t in texts)
    # Размышления в историю не пишутся; окончательный текст — событием с `ref` = тем же блоком.
    texts = [(e["summary"], e["ref"]) for e in events if e["kind"] == "text"]
    assert texts == [("".join(answer), text_block)]

    # Diff правки и вывод команды.
    edit, bash = [e for e in events if e["kind"] == "tool_done"]
    assert [e["summary"] for e in events if e["kind"] == "tool"] == ["app.py", "ls"]  # путь — относительный
    assert edit["brief"] == {"type": "diff", "file": "app.py", "added": 1, "removed": 1}
    assert (await _event(env, sid, edit["id"]))["detail"] == {
        "type": "diff",
        "file": "app.py",
        "patch": "@@ -1,2 +1,2 @@\n def greet(name):\n-    return nam\n+    return name",
        "cut": False,
    }
    assert bash["brief"] == {"type": "output", "lines": 2}
    assert (await _event(env, sid, bash["id"]))["detail"]["text"] == "a\nb\n"

    # Расход хода и сессии: цена OpenCode — уже за ход; контекст — `tokens.total`, предел — модели.
    [end] = [e for e in events if e["kind"] == "turn_end"]
    assert end["brief"]["cost_usd"] == 0.001 and "$0.0010" in end["summary"]
    assert end["brief"]["tokens"] == {"input": 64_482, "output": 45, "cache_read": 1664, "cache_write": 7}
    usage = (await _rec(env, sid))["usage"]
    assert (usage["context_tokens"], usage["context_limit"], usage["cost_usd"]) == (66_204, 1_000_000, 0.001)
    runner = host.held.runners[sid]
    assert runner.process.model_info == {"context": 1_000_000, "image": True}

    # Ключ провайдера из `GET /config/providers` не попал никуда: ни в журналы, ни в базу, ни в состояние.
    assert any(c["path"] == "/config/providers" for c in oc.jsonl("calls.jsonl"))
    log = "\n".join(r.getMessage() for r in caplog.records)
    with env.db.reader() as conn:
        dump = "\n".join(conn.iterdump())
    state = json.dumps(host.state, ensure_ascii=False, default=str)
    for place in (log, dump, state, json.dumps(posts, ensure_ascii=False)):
        assert SECRET not in place


async def test_oc_without_deltas_limits_or_totals_works_plain(env, oc, monkeypatch):  # noqa: F811
    """П11: другая версия OpenCode — дельт нет, `GET /config/providers` нет, `tokens.total` нет, у правки нет
    `metadata`."""
    project = _pid, workdir = env.project("Umbrella")
    oc.scenario(
        [
            {"stream": {"kind": "reasoning", "chunks": ["думаю"], "no_delta": True}},
            {"tool": {"tool": "edit", "args": {"filePath": str(workdir / "a.py")}, "output": "ok"}},
            {"tool": {"tool": "write", "args": {"filePath": str(workdir / "b.py")}, "error": "нельзя"}},
            {"stream": {"kind": "text", "chunks": ["готово"], "no_delta": True}},
            {"text": "итог"},
        ]
    )
    host = await started(env, held_live_min_s=0.01)
    posts = _spy_live(host, monkeypatch)
    sid, _, _ = await _session(env, project=project, engine="opencode")
    await _running(env, host, oc, sid)
    await _turn_done(env, host, sid)
    await asyncio.sleep(0.2)
    assert posts == []  # без `message.part.delta` в `live` ничего не идёт
    events = await _events(env, sid)
    assert [e["summary"] for e in events if e["kind"] == "text"] == ["готово", "итог"]
    edit, failed = [e for e in events if e["kind"] == "tool_done"]
    assert (edit["brief"], edit["has_detail"]) == (None, False)
    assert failed["is_error"] is True and failed["brief"] == {"type": "output", "lines": 1}
    [end] = [e for e in events if e["kind"] == "turn_end"]
    assert end["brief"]["cost_usd"] == 0.001 and end["brief"]["tokens"]["input"] == 10
    rec = await _rec(env, sid)
    assert rec["usage"] is None and rec["state"] == "idle"  # размера контекста нет — расход не шлётся
    assert host.held.runners[sid].process.model_info is None
    assert "error" not in [e["kind"] for e in events]


async def test_oc_tool_finished_by_abort_is_one_russian_line(env, oc):  # noqa: F811
    """2.6 п.2 на OpenCode (живой стенд): прерванная команда кончается ОБЫЧНЫМ результатом с «User aborted
    the command» в выводе — в ленте это «инструмент прерван», а не «готово · N строк»."""
    aborted = "(no output)\n\n<shell_metadata>\nUser aborted the command\n</shell_metadata>"
    oc.scenario(
        [
            {"tool": {"tool": "bash", "args": {"command": "ls"}, "output": "a\nb\n"}},
            {
                "tool": {
                    "tool": "bash",
                    "args": {"command": "sleep 90"},
                    "output": "готово",
                    "hold": 30,
                    "aborted_output": aborted,
                }
            },
        ],
        [{"tool": {"tool": "bash", "args": {"command": "ls"}, "output": "после\n"}}],
    )
    host = await started(env)
    sid, _, _ = await _session(env, engine="opencode")
    await _running(env, host, oc, sid)
    await env.until(host, _has_event(env, sid, "tool", "sleep 90"), what="долгая команда")
    r = await env.ui("POST", f"/sessions/{sid}/interrupt")
    assert r.status_code == 200
    await env.until(host, _has_event(env, sid, "interrupt", "прервано"), what="прервано")
    await _turn_done(env, host, sid)
    events = await _events(env, sid)
    before, after = [e for e in events if e["kind"] == "tool_done"]
    assert before["brief"] == {"type": "output", "lines": 2}  # результат до прерывания — как обычно
    assert (after["summary"], after["is_error"], after["brief"], after["has_detail"]) == (
        "инструмент прерван",
        False,
        None,
        False,
    )
    with env.db.reader() as conn:
        dump = "\n".join(str(r) for r in conn.execute("SELECT summary, detail_json FROM main.held_event"))
    assert "aborted" not in dump and "error" not in [e["kind"] for e in events]
    # Следующий ход: прерывание кончилось вместе с ходом — результат снова обычный.
    assert len(await _drop_interrupted(env, sid)) == 1  # Ф48-2 (A): иначе очередь стоит на прерванной
    await _say(env, sid, "дальше")
    await env.until(host, _turn_ends(env, sid, 1), what="конец второго хода")
    await _turn_done(env, host, sid)
    last = [e for e in await _events(env, sid) if e["kind"] == "tool_done"][-1]
    assert last["brief"] == {"type": "output", "lines": 1} and last["summary"].startswith("готово (")
