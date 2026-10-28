"""Постоянные сессии хаба на Cursor (Ф71-1, docs/architecture.md: Р1–Р8, поправки П1–П11).
Ядро — в процессе, `cursor-agent` — ПОДДЕЛЬНЫЙ (`fake_cursor.py`: один запуск — один ход, хуки из плагина
исполняются по-настоящему, через `hub-cursor-hook` и сокет хоста). Настоящий Cursor здесь не запускается."""

import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hub.core import engine as engine_mod
from hub.engine.cursor_process import RULES_NAME, strip_system
from hub.work import files
from hub.work import held as held_mod

from .conftest import FORBIDDEN_ENV, flag_value, pid_alive
from .test_held_autopilot import _free
from .test_held_autopilot_oc import _card, _decide
from .test_held_host import _events, _has_event, _rec, _running, _say, _session, _state, _turn_done
from .test_held_incoming import _core_rec
from .test_held_window import _event, _spy_live

FAKE = Path(__file__).with_name("fake_cursor.py")
PATCH = "@@ -1,2 +1,2 @@\n def greet(name):\n-    return nam\n+    return name\n"


class Cur:
    """Поддельный `cursor-agent`: бинарь, сценарий, журналы, секция `cursor` в `engine.json`."""

    def __init__(self, env) -> None:
        self.dir = env.tmp / "cursor"
        self.dir.mkdir()
        self.binary = self.dir / "cursor-agent"
        self.binary.write_text(f"#!{sys.executable}\n" + FAKE.read_text(encoding="utf-8"), encoding="utf-8")
        self.binary.chmod(self.binary.stat().st_mode | stat.S_IXUSR)
        engine_mod.setup_path(env.config_dir).write_text(
            json.dumps({"cursor": {"path": str(self.binary)}}), encoding="utf-8"
        )

    def scenario(self, *turns) -> None:
        (self.dir / "scenario.json").write_text(json.dumps({"turns": list(turns)}, ensure_ascii=False))

    def jsonl(self, name: str) -> list[dict]:
        path = self.dir / name
        if not path.exists():
            return []
        text = path.read_text(encoding="utf-8")
        return [json.loads(line) for line in text[: text.rfind("\n") + 1].splitlines() if line.strip()]

    def calls(self) -> list[dict]:
        return self.jsonl("calls.jsonl")

    def hooks(self) -> list[dict]:
        return self.jsonl("hooks.jsonl")


@pytest.fixture
def cur(env):
    return Cur(env)


async def started(env, **overrides):
    return await env.started(held_cursor_sidecar=False, **overrides)


async def _cur_session(env, **body):
    return await _session(env, engine="cursor", **body)


def _pre(tool: str, tool_input: dict) -> dict:
    return {"event": "preToolUse", "input": {"tool_name": tool, "tool_input": tool_input}}


async def _calls_n(env, host, cur, n: int) -> None:
    await env.until(host, lambda: len(cur.calls()) >= n, what=f"ход cursor-agent №{n}")


# --- запуск, поток, расход, продолжение ---------------------------------------------------------------------


async def test_cursor_launch_argv_plugin_stream_usage_and_resume(env, cur, monkeypatch):
    host = await started(env, held_live_min_s=0.01)
    posts = _spy_live(host, monkeypatch)
    sid, _pid, workdir = await _cur_session(env, role="тестер")
    app = str(workdir / "app.py")
    cur.scenario(
        [
            {"thinking": "думаю над задачей"},
            {"text": "Смотрю файл"},
            {
                "tool": "edit",
                "args": {"path": app},
                "result": {"success": {"path": app, "diffString": PATCH}},
            },
            {"text": "Готово"},
        ],
        [{"text": "второй ход"}],
    )
    for _ in range(3):
        await env.cycle(host)
    assert cur.calls() == [] and host.held.runners == {}  # лениво
    await _running(env, host, sid, "посмотри")
    await _calls_n(env, host, cur, 1)
    await _turn_done(env, host, sid)
    rec = await _rec(env, sid)
    assert env.calls() == []  # claude не запускался
    chat = rec["engine_session_id"]
    assert chat.startswith("chat-") and rec["state"] == "idle" and rec["queued_says"] == 0

    [call] = cur.calls()
    argv = call["argv"]
    plugin = files.cursor_plugin_dir(env.data_dir, sid)
    assert argv[:4] == ["-p", "--output-format", "stream-json", "--stream-partial-output"]
    assert flag_value(argv, "--model") == "grok-4.7-high"  # П11: пустое усилие — high
    assert {"--trust", "--force", "--approve-mcps"} <= set(argv) and "--resume" not in argv
    assert flag_value(argv, "--plugin-dir") == str(plugin)
    assert flag_value(argv, "--workspace") == str(workdir) and call["cwd"] == str(workdir)
    # Проба 19.10: правило плагина до модели не доходит — системный текст идёт блоком перед первой репликой.
    assert call["stdin"].startswith("<hub_session_rules>") and "постоянная сессия хаба" in call["stdin"]
    assert strip_system(call["stdin"]) == "посмотри"
    assert call["env"]["HUB_SESSION"] == str(sid) and call["env"]["HUB_SESSION_KEY"]
    assert call["env"]["HUB_SESSION_SUB_PROFILE"] == "all" and call["env"]["HUB_CURSOR_SOCK"]
    assert not set(FORBIDDEN_ENV) & set(call["env"]) and "HUB_WORK" not in call["env"]

    # Плагин сессии: хуки на хост с `failClosed`, MCP хаба с личностью через `${env:…}`, системный текст —
    # правилом; ключа запуска в файлах нет (П10).
    assert stat.S_IMODE(plugin.stat().st_mode) == 0o700
    hooks = json.loads((plugin / "hooks" / "hooks.json").read_text())["hooks"]
    assert sorted(hooks) == ["beforeMCPExecution", "preToolUse", "subagentStart"]
    for event, [entry] in hooks.items():
        assert entry["failClosed"] is True and entry["timeout"] == 86400
        assert entry["command"].endswith(f" {event}") and "cursor" in entry["command"]
    server = json.loads((plugin / "mcp.json").read_text())["mcpServers"]["hub-all"]
    assert server["args"][-3:] == ["mcp", "--profile", "all"]
    assert server["env"]["HUB_SESSION"] == "${env:HUB_SESSION}"
    assert server["env"]["HUB_SESSION_KEY"] == "${env:HUB_SESSION_KEY}"
    assert server["env"]["HUB_SESSION_SUB_PROFILE"] == "${env:HUB_SESSION_SUB_PROFILE}"
    assert "HUB_CONFIG_DIR" not in server["env"]  # у хоста не задан — не упоминается
    rules = (plugin / "rules" / RULES_NAME).read_text()
    assert rules.startswith("---\n") and "alwaysApply: true" in rules
    assert f"постоянная сессия хаба владельца #{sid}" in rules and "Cursor" in rules
    for path in plugin.rglob("*"):
        if path.is_file():
            assert call["env"]["HUB_SESSION_KEY"] not in path.read_text()
            assert stat.S_IMODE(path.stat().st_mode) == 0o600

    # Текст по мере набора и события ленты: размышления и текст — блоки `c<ход>-…`, текст — с `ref`.
    kinds = {body["block"]: body["kind"] for _t, body in posts}
    assert sorted(kinds.values()) == ["text", "text", "thinking"]
    assert all(block.startswith("c1-") for block in kinds)
    events = await _events(env, sid)
    texts = [(e["summary"], e["ref"]) for e in events if e["kind"] == "text"]
    assert [t for t, _r in texts] == ["Смотрю файл", "Готово"] and all(r in kinds for _t, r in texts)
    [edit] = [e for e in events if e["kind"] == "tool_done"]
    assert [e["summary"] for e in events if e["kind"] == "tool"] == ["app.py"]
    assert edit["brief"] == {"type": "diff", "file": "app.py", "added": 1, "removed": 1}
    assert (await _event(env, sid, edit["id"]))["detail"]["patch"].startswith("@@ -1,2 +1,2 @@")
    [end] = [e for e in events if e["kind"] == "turn_end"]
    assert end["brief"]["tokens"] == {"input": 100, "output": 20, "cache_read": 3000, "cache_write": 7}
    assert end["brief"]["cost_usd"] is None and not any(e["is_error"] for e in events)
    assert await _has_event(env, sid, "start", "запуск новой сессии Cursor (grok-4.7-high)")()

    # Второй ход — тот же процесс сессии, новый запуск движка с `--resume <chatId>`.
    await _say(env, sid, "ещё")
    await _calls_n(env, host, cur, 2)
    await _turn_done(env, host, sid)
    assert flag_value(cur.calls()[1]["argv"], "--resume") == chat
    assert "<hub_session_rules>" not in cur.calls()[1]["stdin"]  # тот же текст в разговоре — не повторяется
    assert await _has_event(env, sid, "text", "второй ход")()

    # Сон и подъём: продолжение того же разговора.
    await env.ui("POST", f"/sessions/{sid}/sleep")
    await env.until(host, _state(env, sid, "asleep"), what="усыплена")
    await env.until(host, lambda: sid not in host.held.runners, what="процесс сессии снят")
    await _running(env, host, sid, "проснись")
    await _calls_n(env, host, cur, 3)
    await _turn_done(env, host, sid)
    assert flag_value(cur.calls()[2]["argv"], "--resume") == chat
    assert (await _rec(env, sid))["engine_session_id"] == chat
    assert await _has_event(env, sid, "start", "продолжение сессии Cursor")()


async def test_cursor_says_during_turn_go_as_one_batch(env, cur):
    host = await started(env)
    sid, _pid, _workdir = await _cur_session(env)
    cur.scenario([{"hang": 1.5}, {"text": "раз"}], [{"text": "два"}])
    await _running(env, host, sid, "первая")
    await _calls_n(env, host, cur, 1)
    await env.until(host, _state(env, sid, "running"), what="ход идёт")
    await _say(env, sid, "вторая")
    await _say(env, sid, "третья")
    await _calls_n(env, host, cur, 2)
    await _turn_done(env, host, sid)
    second = cur.calls()[1]["stdin"]
    assert "вторая" in second and "третья" in second and "первая" not in second
    assert len(cur.calls()) == 2


async def test_cursor_stop_is_interrupt_not_error(env, cur):
    host = await started(env)
    sid, _pid, _workdir = await _cur_session(env)
    cur.scenario([{"text": "начал"}, {"hang": 30}], [{"text": "срочное принято"}])
    await _running(env, host, sid, "долгое")
    await _calls_n(env, host, cur, 1)
    await env.until(host, _has_event(env, sid, "text", "начал"), what="ход идёт")
    await _say(env, sid, "срочно", priority="now")
    await _calls_n(env, host, cur, 2)
    await _turn_done(env, host, sid)
    assert not pid_alive(cur.calls()[0]["pid"])
    events = await _events(env, sid)
    kinds = [e["kind"] for e in events]
    assert kinds.count("interrupt") == 1 and "error" not in kinds and not any(e["is_error"] for e in events)
    assert any(e["summary"] == "срочное принято" for e in events)
    # первый ход прерван без `result` — системный текст подаётся снова
    assert strip_system(cur.calls()[1]["stdin"]) == "срочно"


async def test_cursor_turn_without_result_is_error_and_session_goes_idle(env, cur):
    host = await started(env)
    sid, _pid, _workdir = await _cur_session(env)
    cur.scenario([{"text": "начал"}, {"exit": 1, "stderr": "Error: usage pool exhausted"}])
    await _running(env, host, sid)
    await _calls_n(env, host, cur, 1)
    await _turn_done(env, host, sid)
    assert await _has_event(env, sid, "error", "usage pool exhausted")()
    assert (await _rec(env, sid))["state"] == "idle"


# --- гард через хук (П1–П4, П7) -----------------------------------------------------------------------------


async def test_cursor_hook_guard_allow_deny_and_card(env, cur):
    host = await started(env)
    sid, _pid, workdir = await _cur_session(env)
    secret = str(env.home / ".config" / "hub" / "work" / "token")
    rules = str(workdir / "CLAUDE.md")
    cur.scenario(
        [
            {"tool": "shell", "args": {"command": "ls"}, "hook": _pre("Shell", {"command": "ls", "cwd": ""})},
            {"tool": "read", "args": {"path": secret}, "hook": _pre("Read", {"file_path": secret})},
            {
                "tool": "edit",
                "args": {"path": rules},
                "hook": _pre("Write", {"file_path": rules, "content": "x"}),
            },
            {
                "tool": "mcp",
                "args": {"toolName": "hub_read"},
                "hook": _pre("MCP:hub_read", {"channel_id": 1}),
            },
            {"text": "всё"},
        ]
    )
    await _running(env, host, sid)
    request = await _card(env, host, sid)
    assert request["tool"] == "Write" and request["input"]["file_path"] == rules
    assert request["rule"] == "project_rules" and "правка правил и настроек проекта" in request["reason"]
    await _decide(env, request)
    await _turn_done(env, host, sid)
    answers = [(h["event"], h["rc"], h["answer"]["permission"]) for h in cur.hooks()]
    assert answers == [
        ("preToolUse", 0, "allow"),
        ("preToolUse", 0, "deny"),
        ("preToolUse", 0, "deny"),
        ("preToolUse", 0, "allow"),  # `MCP:*` решает `beforeMCPExecution` (П2)
    ]
    assert "каталоги конфига и данных хаба" in cur.hooks()[1]["answer"]["agent_message"]
    assert cur.hooks()[2]["answer"]["agent_message"]
    assert await _has_event(env, sid, "warn", "каталоги конфига и данных хаба")()
    assert await _has_event(env, sid, "decision", "владелец отказал: Write")()
    [end] = [e for e in await _events(env, sid) if e["kind"] == "turn_end"]
    assert "отказов 2" in end["summary"]


async def test_cursor_card_allowed_by_owner_and_freed_rule_needs_no_card(env, cur):
    host = await started(env)
    await _free(env, "push")
    sid, _pid, workdir = await _cur_session(env, mode="auto")
    rules = str(workdir / "CLAUDE.md")
    push = _pre("Shell", {"command": "git push origin main", "cwd": ""})
    cur.scenario(
        [
            {"tool": "shell", "args": {"command": "git push origin main"}, "hook": push},  # снят — проходит
            {
                "tool": "edit",
                "args": {"path": rules},
                "hook": _pre("Write", {"file_path": rules, "content": "x"}),
            },
            {"text": "всё"},
        ]
    )
    await _running(env, host, sid)
    request = await _card(env, host, sid)
    assert request["rule"] == "project_rules"
    await _decide(env, request, "allow")
    await _turn_done(env, host, sid)
    assert [h["answer"]["permission"] for h in cur.hooks()] == ["allow", "allow"]
    assert len([e for e in await _events(env, sid) if e["kind"] == "request"]) == 1


async def test_cursor_unknown_tool_and_keys_are_cards_mcp_by_server_plugin_dir_denied(env, cur):
    host = await started(env)
    sid, _pid, workdir = await _cur_session(env)
    plugin = files.cursor_plugin_dir(env.data_dir, sid)
    mcp = {
        "event": "beforeMCPExecution",
        "input": {
            "mcp_server_name": f"plugin-s{sid}-hub-all",
            "tool_name": "hub_move_channel",
            "tool_input": json.dumps({"channel_id": 1, "project_id": 2}),
        },
    }
    cur.scenario(
        [
            {"tool": "shell", "args": {}, "hook": _pre("Teleport", {"where": "x"})},
            {"tool": "read", "args": {}, "hook": _pre("Read", {"file_path": str(workdir / "a"), "zz": 1})},
            {"tool": "mcp", "args": {}, "hook": mcp},
            {"tool": "shell", "args": {}, "hook": _pre("Shell", {"command": f"echo x > {plugin}/mcp.json"})},
            {"text": "всё"},
        ]
    )
    await _running(env, host, sid)
    first = await _card(env, host, sid)
    assert first["rule"] == "guard_failure" and "незнакомый вызов Cursor Teleport" in first["reason"]
    await _decide(env, first)
    second = await _card(env, host, sid)
    assert second["rule"] == "guard_failure" and "незнакомые параметры (zz)" in second["reason"]
    await _decide(env, second)
    third = await _card(env, host, sid)
    assert (
        third["tool"] == f"mcp__plugin-s{sid}-hub-all__hub_move_channel" and third["input"]["channel_id"] == 1
    )
    assert third["rule"] == "mcp_write"  # сервер плагина узнан как `hub-all` (П2)
    await _decide(env, third)
    await _turn_done(env, host, sid)
    answers = [h["answer"]["permission"] for h in cur.hooks()]
    assert answers == ["deny", "deny", "deny", "deny"]
    assert cur.hooks()[3]["answer"]["agent_message"] == held_mod.CURSOR_WRITE_DENIED


async def test_cursor_hook_waiting_for_card_gets_deny_when_turn_is_stopped(env, cur):
    host = await started(env)
    sid, _pid, workdir = await _cur_session(env)
    rules = str(workdir / "CLAUDE.md")
    cur.scenario(
        [{"tool": "edit", "args": {}, "hook": _pre("Write", {"file_path": rules, "content": "x"})}],
        [{"text": "после стопа"}],
    )
    await _running(env, host, sid)
    await _card(env, host, sid)
    r = await env.ui("POST", f"/sessions/{sid}/interrupt")
    assert r.status_code == 200, r.text
    await _turn_done(env, host, sid)

    async def no_pending():
        return not [
            r
            for r in (await env.ui("GET", f"/sessions/{sid}")).json()["requests"]
            if r["status"] == "pending"
        ]

    await env.until(host, no_pending, what="карточка снята")
    assert not pid_alive(cur.calls()[0]["pid"])


# --- модель и усилие (Р1, П11), выключатель, сжатие ---------------------------------------------------------


async def test_cursor_effort_low_is_raised_to_high_and_fast_is_refused(env, cur, caplog):
    host = await started(env, held_launch_retry_s=0)
    sid, _pid, _workdir = await _cur_session(env, effort="low")
    cur.scenario([{"text": "ок"}])
    with caplog.at_level("WARNING"):
        await _running(env, host, sid)
        await _calls_n(env, host, cur, 1)
        await _turn_done(env, host, sid)
    assert flag_value(cur.calls()[0]["argv"], "--model") == "grok-4.7-high"
    assert any("поднято до high" in r.getMessage() for r in caplog.records)

    fast, _pid, _workdir = await _cur_session(env, name="Fast", model="grok-4.7-fast")
    await _say(env, fast)

    async def failed():
        return (await _rec(env, fast))["failure"]

    assert "fast выключен решением владельца" in await env.until(host, failed, what="причина у сессии")
    assert len(cur.calls()) == 1


async def test_cursor_off_switch(env, cur):
    host = await started(env, held_cursor=False, held_launch_retry_s=0)
    sid, _pid, _workdir = await _cur_session(env)
    await _say(env, sid)

    async def failed():
        return (await _rec(env, sid))["failure"]

    assert await env.until(host, failed, what="причина") == held_mod.CURSOR_OFF
    assert cur.calls() == []


async def test_cursor_compact_request_is_cleared_without_compact(env, cur):
    host = await started(env)
    sid, _pid, _workdir = await _cur_session(env)
    cur.scenario([{"text": "ок"}])
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    r = await env.ui("POST", f"/sessions/{sid}/compact", {})
    assert r.status_code == 200, r.text

    def cleared():
        return _core_rec(env, sid).compact_requested_ts is None

    await env.until(host, cleared, what="просьба «Сжать» снята")
    assert await _has_event(env, sid, "status", "Cursor сжимает разговор сам")()
    assert len(cur.calls()) == 1  # `/compact` в движок не подавался
    assert not any(e["kind"] == "compact" for e in await _events(env, sid))


# --- MCP хаба для подписки между ходами ---------------------------------------------------------------------


async def test_cursor_sidecar_runs_hub_mcp_with_session_identity_while_process_lives(env, cur):
    fake_hub = env.tmp / "fake-hub"
    log = env.tmp / "fake-hub.json"
    fake_hub.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        f"open({str(log)!r}, 'w').write(json.dumps({{'argv': sys.argv[1:], 'pid': os.getpid(), "
        "'session': os.environ.get('HUB_SESSION'), 'key': bool(os.environ.get('HUB_SESSION_KEY'))}))\n"
        "sys.stdin.read()\n",
        encoding="utf-8",
    )
    fake_hub.chmod(0o700)
    host = await env.started(held_cursor_sidecar=True, held_cursor_hub=str(fake_hub))
    sid, _pid, _workdir = await _cur_session(env)
    cur.scenario([{"text": "ок"}])
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    await env.until(host, log.exists, what="MCP хаба запущен")
    side = json.loads(log.read_text())
    assert side["argv"] == ["mcp", "--profile", "all"] and side["session"] == str(sid) and side["key"]
    assert pid_alive(side["pid"])  # живёт между ходами
    server = json.loads((files.cursor_plugin_dir(env.data_dir, sid) / "mcp.json").read_text())
    assert server["mcpServers"]["hub-all"]["command"] == str(fake_hub)
    await env.ui("POST", f"/sessions/{sid}/sleep")
    await env.until(host, lambda: sid not in host.held.runners, what="процесс сессии снят")
    await env.until(host, lambda: not pid_alive(side["pid"]), what="MCP хаба погашен")


# --- повторная подача после перезапуска хоста (П9) ----------------------------------------------------------


def test_cursor_last_query_reads_the_last_user_query(tmp_path):
    chat = "c1e2-77"
    folder = tmp_path / ".cursor" / "projects" / "home-x-proj" / "agent-transcripts" / chat
    folder.mkdir(parents=True)
    lines = [
        {
            "role": "user",
            "message": {"content": [{"type": "text", "text": "<user_query>\nстарая\n</user_query>"}]},
        },
        {"role": "assistant", "message": {"content": [{"type": "text", "text": "ответ"}]}},
        {
            "role": "user",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": "<timestamp>Mon</timestamp>\n<user_query>\nновая реплика\n</user_query>",
                    }
                ]
            },
        },
        {
            "role": "assistant",
            "message": {"content": [{"type": "text", "text": "<user_query>не то</user_query>"}]},
        },
    ]
    (folder / f"{chat}.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n")
    assert held_mod.cursor_last_query(tmp_path, chat) == "новая реплика"
    assert held_mod.cursor_last_query(tmp_path, "other") is None
    assert held_mod.cursor_last_query(tmp_path, "../x") is None


async def test_cursor_has_say_compares_with_the_last_query(env, cur):
    host = await started(env)
    chat = "chat-abc"
    folder = env.home / ".cursor" / "projects" / "p" / "agent-transcripts" / chat
    runner = SimpleNamespace(sid=1, engine_session_id=chat)
    say = {"id": 5, "text": "уже поданная", "files": None}
    assert not await host.held._cursor_has_say(runner, say)  # noqa: SLF001 — записи нет: подать
    folder.mkdir(parents=True)
    stamp = "<timestamp>Monday, Oct 19, 2026, 7:01 PM (UTC+3)</timestamp>\n"
    at = 1_792_425_660_000  # 19.10.2026 16:01 UTC — время этой строки

    def write(text: str, head: str = stamp) -> None:
        record = {
            "role": "user",
            "message": {"content": [{"type": "text", "text": f"{head}<user_query>\n{text}\n</user_query>"}]},
        }
        (folder / f"{chat}.jsonl").write_text(json.dumps(record, ensure_ascii=False) + "\n")

    write("уже поданная")
    say = {**say, "created_ts": at + 20_000}
    assert await host.held._cursor_has_say(runner, say)  # noqa: SLF001
    assert not await host.held._cursor_has_say(runner, {**say, "text": "другая"})  # noqa: SLF001
    # ревью Ф71-1: то же «да» неделей раньше — не наша реплика; время не разобрано — тоже подать
    assert not await host.held._cursor_has_say(runner, {**say, "created_ts": at + 7 * 86_400_000})  # noqa: SLF001
    write("уже поданная", head="")
    assert not await host.held._cursor_has_say(runner, say)  # noqa: SLF001


def test_cursor_stamp_is_parsed_to_utc_minute():
    def rec(text: str) -> dict:
        return {"role": "user", "message": {"content": [{"type": "text", "text": text}]}}

    assert held_mod._cursor_stamp_ms(rec("<timestamp>Monday, Oct 19, 2026, 7:01 PM (UTC+3)</timestamp>")) == (
        1_792_425_660_000
    )
    assert held_mod._cursor_stamp_ms(
        rec("<timestamp>Monday, Oct 19, 2026, 12:01 AM (UTC-5:30)</timestamp>")
    ) == (1_792_387_860_000)
    assert held_mod._cursor_stamp_ms(rec("<timestamp>Mon</timestamp>")) is None


async def test_cursor_shell_without_cwd_relative_write_is_a_card(env, cur):
    """Ревью Ф71-1: оболочка Cursor держит папку между вызовами (`cd ~`, затем `>> .bashrc`), хук приходит с
    пустым `cwd` — относительная запись без своей папки у вызова судится вслепую: карточка; с папкой — нет."""
    host = await started(env)
    sid, _pid, workdir = await _cur_session(env)
    cur.scenario(
        [
            {
                "tool": "shell",
                "args": {"command": "cd ~"},
                "hook": _pre("Shell", {"command": "cd ~", "cwd": ""}),
            },
            {
                "tool": "shell",
                "args": {"command": "echo x >> .bashrc"},
                "hook": _pre("Shell", {"command": "echo x >> .bashrc", "cwd": ""}),
            },
            {
                "tool": "shell",
                "args": {"command": "echo x >> out.txt"},
                "hook": _pre("Shell", {"command": "echo x >> out.txt", "cwd": str(workdir)}),
            },
            {"text": "всё"},
        ]
    )
    await _running(env, host, sid)
    request = await _card(env, host, sid)
    assert request["tool"] == "Bash" and ".bashrc" in request["input"]["command"]
    await _decide(env, request)
    await _turn_done(env, host, sid)
    answers = [h["answer"]["permission"] for h in cur.hooks()]
    assert answers == ["allow", "deny", "allow"]


async def test_cursor_subagent_only_on_session_model(env, cur):
    """Слово владельца: подагенты Cursor — только Grok 4.7 (high/xhigh, без fast) из подписки Cursor; Task с
    чужой моделью (Claude из общих правил) — отказ с объяснением; без модели — модель сессии."""
    host = await started(env)
    sid, _pid, _workdir = await _cur_session(env)
    cur.scenario(
        [
            {
                "tool": "shell",
                "args": {"command": "true"},
                "hook": _pre("Task", {"description": "x", "prompt": "p", "model": "claude-sonnet-5-5-high"}),
            },
            {
                "tool": "shell",
                "args": {"command": "true"},
                "hook": _pre("Task", {"description": "x", "prompt": "p"}),
            },
            {
                "tool": "shell",
                "args": {"command": "true"},
                "hook": _pre("Task", {"prompt": "p", "model": "grok-4.7-xhigh", "subagent_type": "explore"}),
            },
            {
                "tool": "shell",
                "args": {"command": "true"},
                "hook": _pre("Task", {"prompt": "p", "model": "grok-4.7-fast"}),
            },
            {
                "tool": "shell",
                "args": {"command": "true"},
                "hook": _pre("Task", {"prompt": "p", "model": "grok-5"}),
            },
            {
                "tool": "shell",
                "args": {"command": "true"},
                "hook": _pre("Task", {"prompt": "p", "model": "grok-4.7-low"}),
            },
            {"text": "всё"},
        ]
    )
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    answers = [h["answer"]["permission"] for h in cur.hooks()]
    assert answers == ["deny", "allow", "allow", "deny", "deny", "deny"]
    assert "только на Grok 4.7" in cur.hooks()[0]["answer"]["agent_message"]
    rules = held_mod.cursor_mcp.global_rules(env.home, None, None)
    assert rules == "" or "параметр model у подагента не задавай" in rules


# --- реплики и сигналы посреди хода (Ф55-1 / Ф50-1 для Cursor) --------------------------------------------


async def test_cursor_say_mid_turn_goes_into_the_running_turn_with_the_card_answer(env, cur):
    """Реплика владельца, пока сессия ждёт карточку, входит в ИДУЩИЙ ход ответом того же хука
    (`additional_context`), второго хода нет; хук подагента (свой `conversation_id`) её не получает."""
    host = await started(env)
    sid, _pid, workdir = await _cur_session(env)
    rules = str(workdir / "CLAUDE.md")
    sub = _pre("Shell", {"command": "ls", "cwd": ""})
    sub["input"]["conversation_id"] = "chat-subagent"
    cur.scenario(
        [
            {
                "tool": "edit",
                "args": {"path": rules},
                "hook": _pre("Write", {"file_path": rules, "content": "x"}),
            },
            {"tool": "shell", "args": {"command": "ls"}, "hook": sub},
            {"text": "учёл"},
        ]
    )
    await _running(env, host, sid, "первая")
    request = await _card(env, host, sid)
    say_id = await _say(env, sid, "вторая посреди хода")
    await env.until(host, lambda: bool(host.held.runners[sid].mid_says), what="реплика подана посреди хода")
    await _decide(env, request, "allow")
    await _turn_done(env, host, sid)
    assert len(cur.calls()) == 1
    write, shell = cur.hooks()
    assert write["answer"]["permission"] == "allow"
    assert "вторая посреди хода" in write["answer"]["additional_context"]
    assert "additional_context" not in shell["answer"]
    runner = host.held.runners[sid]
    assert runner.mid == {} and runner.mid_says == {}
    assert say_id not in host.held.says.get(sid, {})


async def test_cursor_mid_say_denied_hook_carries_it_in_agent_message(env, cur):
    host = await started(env)
    sid, _pid, workdir = await _cur_session(env)
    rules = str(workdir / "CLAUDE.md")
    cur.scenario(
        [
            {
                "tool": "edit",
                "args": {"path": rules},
                "hook": _pre("Write", {"file_path": rules, "content": "x"}),
            },
            {"text": "понял"},
        ]
    )
    await _running(env, host, sid, "первая")
    request = await _card(env, host, sid)
    await _say(env, sid, "не трогай правила")
    await env.until(host, lambda: bool(host.held.runners[sid].mid_says), what="реплика подана посреди хода")
    await _decide(env, request)
    await _turn_done(env, host, sid)
    [hook] = cur.hooks()
    assert hook["answer"]["permission"] == "deny" and "не трогай правила" in hook["answer"]["agent_message"]
    assert len(cur.calls()) == 1


async def test_cursor_mid_say_without_hook_goes_next_turn_once(env, cur):
    """Хука до конца хода не было — строка уходит следующим ходом одним вводом (зазор Ф50-1 П4)."""
    host = await started(env)
    sid, _pid, _workdir = await _cur_session(env)
    cur.scenario([{"hang": 1.5}, {"text": "раз"}], [{"text": "два"}])
    await _running(env, host, sid, "первая")
    await env.until(host, lambda: host.held.runners[sid].busy, what="ход идёт")
    await _say(env, sid, "вторая")
    await env.until(host, lambda: bool(host.held.runners[sid].mid_says), what="реплика подана посреди хода")
    await _calls_n(env, host, cur, 2)
    await _turn_done(env, host, sid)
    assert strip_system(cur.calls()[1]["stdin"]) == "вторая"
    assert len(cur.calls()) == 2
    runner = host.held.runners[sid]
    assert runner.mid == {} and runner.mid_says == {} and runner.mid_gap is None


async def test_cursor_oauth_servers_disabled_by_claude_project_list(env, cur):
    """Ф71-3: серверы `~/.cursor/mcp.json` у сессии хаба на Cursor выключаются по `disabledMcpServers` проекта
    Claude Code — файлом Cursor `~/.cursor/projects/<слаг>/mcp-disabled.json` до запуска хода."""
    host = await started(env)
    sid, _pid, workdir = await _cur_session(env)
    (env.home / ".claude.json").write_text(
        json.dumps({"projects": {str(workdir): {"disabledMcpServers": ["claude.ai Notion - Initech"]}}}),
        encoding="utf-8",
    )
    (env.home / ".cursor").mkdir(exist_ok=True)
    (env.home / ".cursor" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"notion": {"url": "https://n"}, "globex-gitlab": {"url": "https://g"}}}),
        encoding="utf-8",
    )
    cur.scenario([{"text": "ок"}])
    await _running(env, host, sid, "привет")
    await _turn_done(env, host, sid)
    slug = held_mod.cursor_mcp.workspace_slug(held_mod.cursor_mcp.project_root(str(workdir)))
    target = env.home / ".cursor" / "projects" / slug / "mcp-disabled.json"
    assert json.loads(target.read_text(encoding="utf-8")) == ["notion"]
