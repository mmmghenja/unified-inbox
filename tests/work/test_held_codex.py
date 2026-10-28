"""Постоянные сессии хаба на Codex (Ф74-5, docs/architecture.md). Ядро — в процессе,
`codex app-server` — ПОДДЕЛЬНЫЙ (`fake_codex.py`: JSON-RPC по stdio, хук `PreToolUse` из флагов `-c`
исполняется по-настоящему через `hub-codex-hook` и сокет процесса, команды `Bash` — тоже). Настоящий Codex
здесь не запускается."""

import contextlib
import json
import os
import signal
import stat
import sys
from pathlib import Path

import pytest

from hub.core import engine as engine_mod
from hub.engine import codex_process, codex_tools
from hub.work import held as held_mod

from .conftest import FORBIDDEN_ENV, pid_alive
from .test_held_autopilot import _free
from .test_held_autopilot_oc import _card, _decide
from .test_held_host import _events, _has_event, _rec, _running, _say, _session, _state, _turn_done
from .test_held_incoming import _core_rec

FAKE = Path(__file__).with_name("fake_codex.py")
USAGE = {
    "total": {"totalTokens": 5000, "inputTokens": 4000, "cachedInputTokens": 3000, "outputTokens": 1000},
    "last": {"totalTokens": 1300, "inputTokens": 1200, "cachedInputTokens": 1000, "outputTokens": 100},
    "modelContextWindow": 475000,
}


class Cdx:
    """Поддельный `codex`: бинарь, сценарий, журналы, секция `codex` в `engine.json`."""

    def __init__(self, env) -> None:
        self.dir = env.tmp / "codex"
        self.dir.mkdir()
        self.binary = self.dir / "codex"
        self.binary.write_text(f"#!{sys.executable}\n" + FAKE.read_text(encoding="utf-8"), encoding="utf-8")
        self.binary.chmod(self.binary.stat().st_mode | stat.S_IXUSR)
        engine_mod.setup_path(env.config_dir).write_text(
            json.dumps({"codex": {"path": str(self.binary), "version": "0.163.0"}}), encoding="utf-8"
        )
        self._scenario: dict = {}

    def scenario(self, *turns, **extra) -> None:
        self._scenario = {"turns": list(turns), **extra}
        (self.dir / "scenario.json").write_text(json.dumps(self._scenario, ensure_ascii=False))

    def jsonl(self, name: str) -> list[dict]:
        path = self.dir / name
        if not path.exists():
            return []
        text = path.read_text(encoding="utf-8")
        out = []
        for line in text[: text.rfind("\n") + 1].splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def calls(self) -> list[dict]:
        return self.jsonl("calls.jsonl")

    def starts(self) -> list[dict]:
        """Запуски app-server с ходами (без прохода «узнать доверие»): у них есть `thread/start|resume`."""
        launches = [c for c in self.calls() if "argv" in c]
        return [c for c in launches if any(flag.startswith("hooks.state=") for flag in c["argv"])]

    def methods(self, name: str) -> list[dict]:
        return [c["params"] for c in self.calls() if c.get("method") == name]

    def hooks(self) -> list[dict]:
        return self.jsonl("hooks.jsonl")

    def ran(self) -> list[dict]:
        return [c for c in self.calls() if "ran" in c]

    def texts(self, name: str = "turn/start") -> list[str]:
        return [
            "".join(i.get("text", "") for i in p.get("input") or [] if i.get("type") == "text")
            for p in self.methods(name)
        ]


@pytest.fixture
def cdx(env):
    codex_process._TRUST_CACHE.clear()
    codex_process._MODELS_CACHE.clear()
    return Cdx(env)


async def _cdx_session(env, **body):
    return await _session(env, engine="codex", **body)


async def _turns_n(env, host, cdx, n: int) -> None:
    await env.until(host, lambda: len(cdx.methods("turn/start")) >= n, what=f"ход Codex №{n}")


def _decisions(cdx) -> list[str]:
    out = []
    for hook in cdx.hooks():
        answer = (hook.get("answer") or {}).get("hookSpecificOutput") or {}
        out.append(answer.get("permissionDecision") or f"rc{hook['rc']}")
    return out


# --- запуск, поток, расход, продолжение ---------------------------------------------------------------------


async def test_host_reports_codex_models_to_status(env, cdx):
    """Ф74-5б: хост читает `model/list` Codex своим коротким app-server (без хуков и нити) и сообщает ядру —
    `/status` отдаёт профилю `codex` поле `models`: видимые модели, умолчание первым, без скрытых."""
    models = [
        {"id": "gpt-6-luna", "model": "gpt-6-luna", "defaultReasoningEffort": "medium",
         "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ("low", "medium", "max")]},
        {"id": "gpt-6.1-sol", "model": "gpt-6.1-sol", "isDefault": True, "defaultReasoningEffort": "low",
         "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ("low", "high", "ultra")]},
        {"id": "gpt-reserve", "model": "gpt-reserve", "hidden": True, "defaultReasoningEffort": "low",
         "supportedReasoningEfforts": [{"reasoningEffort": "low"}]},
    ]
    cdx.scenario([{"text": "x"}], models=models)
    host = await env.started(held_models_refresh_s=3600)

    async def listed():
        r = await env.ui("GET", "/status")
        codex = next(p for p in r.json()["session_engines"] if p["name"] == "codex")
        return codex.get("models")

    await env.until(host, listed, what="модели Codex в /status")
    assert await listed() == [
        {"id": "gpt-6.1-sol", "efforts": ["low", "high", "ultra"], "default_effort": "low"},
        {"id": "gpt-6-luna", "efforts": ["low", "medium", "max"], "default_effort": "medium"},
    ]
    launch = [c for c in cdx.calls() if "argv" in c]
    assert len(launch) == 1 and not any(a.startswith("hooks") for a in launch[0]["argv"])
    assert cdx.methods("model/list") == [{"includeHidden": False}]
    assert not cdx.methods("thread/start")
    for _ in range(3):
        await env.cycle(host)
    assert len(cdx.methods("model/list")) == 1  # срок обновления не вышел — второго чтения нет


@pytest.mark.parametrize("in_agents", [True, False])
async def test_codex_system_text_skips_claude_md_already_in_codex_agents_md(env, cdx, in_agents):
    """Ф74-5б: `~/.codex/AGENTS.md` с текстом `~/.claude/CLAUDE.md` Codex подаёт сам — в
    `developerInstructions` нити текста правил нет (шапка про подагентов есть); без него — правила в
    системном тексте, как раньше."""
    (env.home / ".claude").mkdir(exist_ok=True)
    (env.home / ".claude" / "CLAUDE.md").write_text("# Правила\n\nПравило-маяк владельца.\n", encoding="utf-8")
    (env.home / ".codex").mkdir(exist_ok=True)
    agents = "# Правила\n\nПравило-маяк владельца.\n\n## Вставка\n" if in_agents else "чужое\n"
    (env.home / ".codex" / "AGENTS.md").write_text(agents, encoding="utf-8")
    host = await env.started()
    sid, _pid, _workdir = await _cdx_session(env)
    cdx.scenario([{"text": "ок"}])
    await _running(env, host, sid, "привет")
    await _turn_done(env, host, sid)
    [start] = cdx.methods("thread/start")
    system = start["developerInstructions"]
    assert ("Правило-маяк владельца." in system) is not in_agents
    assert "spawn_agent" in system and "постоянная сессия хаба" in system


async def test_codex_launch_flags_hook_trust_thread_usage_and_resume(env, cdx):
    host = await env.started()
    sid, _pid, workdir = await _cdx_session(env, role="тестер")
    cdx.scenario(
        [
            {"thinking": "думаю"},
            {"text": "Смотрю папку"},
            {"bash": "echo привет > out.txt"},
            {"patch": "*** Begin Patch\n*** Update File: app.py\n@@\n-a\n+b\n*** End Patch\n"},
            {"text": "Готово"},
        ],
        [{"text": "второй ход"}],
    )
    for _ in range(3):
        await env.cycle(host)
    assert cdx.calls() == [] and host.held.runners == {}  # лениво
    await _running(env, host, sid, "посмотри")
    await _turns_n(env, host, cdx, 1)
    await _turn_done(env, host, sid)
    rec = await _rec(env, sid)
    assert env.calls() == []  # claude не запускался
    thread = rec["engine_session_id"]
    assert thread and rec["state"] == "idle" and rec["queued_says"] == 0
    assert _core_rec(env, sid).sys_pin  # Ф48-5: снимок системного текста при создании нити

    # Проход доверия, затем запуск с хуками и доверием по хэшу; ключа запуска нет в флагах.
    launches = [c for c in cdx.calls() if "argv" in c]
    assert len(launches) == 2
    learn, run = launches
    assert not any(a.startswith("hooks.state=") for a in learn["argv"])
    assert any(a.startswith("hooks.PreToolUse=") for a in run["argv"])
    assert any(a.startswith("hooks.state=") for a in run["argv"])
    key = run["env"]["HUB_SESSION_KEY"]
    assert key and run["env"]["HUB_SESSION"] == str(sid) and run["env"]["HUB_SESSION_SUB_PROFILE"] == "all"
    assert run["env"]["HUB_CODEX_SOCK"] and run["env"]["HUB_CODEX_PID"]
    assert learn["env"]["HUB_CODEX_SOCK"] is None
    assert not set(FORBIDDEN_ENV) & {k for k, v in run["env"].items() if v} and run["env"]["HUB_WORK"] is None
    assert all(key not in a for a in run["argv"])

    [start] = cdx.methods("thread/start")
    assert start["cwd"] == str(workdir) and start["approvalPolicy"] == "never"
    assert start["sandbox"] == "danger-full-access" and start["model"] == "gpt-6.1-sol"
    assert "постоянная сессия хаба" in start["developerInstructions"]
    assert "роль «тестер»" in start["developerInstructions"]
    config = start["config"]
    assert config["features"] == {"apps": False, "default_mode_request_user_input": True}
    assert config["agents"]["default_subagent_model"] == "gpt-6.1-sol"
    hub = config["mcp_servers"]["hub-all"]
    assert hub["args"] == ["mcp", "--profile", "all"]
    assert hub["env_vars"] == ["HUB_SESSION", "HUB_SESSION_KEY", "HUB_SESSION_SUB_PROFILE"]
    assert key not in json.dumps(start)
    assert cdx.texts() == ["посмотри"]

    # Команда исполнена в папке сессии через привязку хоста; правка — в ленте.
    [ran] = cdx.ran()
    assert ran["ran"].startswith(codex_tools.BOUND_HEAD) and ran["cwd"] == str(workdir)
    assert (workdir / "out.txt").read_text().strip() == "привет"
    assert _decisions(cdx) == ["allow", "allow"]
    events = await _events(env, sid)
    texts = [e["summary"] for e in events if e["kind"] == "text"]
    assert texts == ["Смотрю папку", "Готово"]
    [end] = [e for e in events if e["kind"] == "turn_end"]
    assert end["brief"]["tokens"]["input"] == 200 and end["brief"]["tokens"]["cache_read"] == 1000
    assert end["brief"]["tokens"]["output"] == 100
    assert not any(e["is_error"] for e in events)
    assert await _has_event(env, sid, "start", "запуск новой сессии Codex (gpt-6.1-sol")()
    usage = (await _rec(env, sid))["usage"]
    assert (usage["context_tokens"], usage["context_limit"]) == (1200, 475000)

    # Второй ход — тот же процесс, та же нить.
    await _say(env, sid, "ещё")
    await _turns_n(env, host, cdx, 2)
    await _turn_done(env, host, sid)
    assert cdx.texts()[1] == "ещё"
    assert len([c for c in cdx.calls() if "argv" in c]) == 2  # процесс не перезапускался

    # Сон и подъём: продолжение той же нити (`thread/resume`), системный текст не подаётся снова.
    await env.ui("POST", f"/sessions/{sid}/sleep")
    await env.until(host, _state(env, sid, "asleep"), what="усыплена")
    await env.until(host, lambda: sid not in host.held.runners, what="процесс сессии снят")
    assert not pid_alive(run["pid"])
    await _running(env, host, sid, "проснись")
    await _turns_n(env, host, cdx, 3)
    await _turn_done(env, host, sid)
    [resume] = cdx.methods("thread/resume")
    assert resume["threadId"] == thread and "developerInstructions" not in resume
    assert cdx.texts()[2] == "проснись"
    assert (await _rec(env, sid))["engine_session_id"] == thread
    assert await _has_event(env, sid, "start", "продолжение сессии Codex")()


# --- реплики посреди хода, «Прервать», ошибки хода ----------------------------------------------------------


async def test_codex_say_mid_turn_is_steered_into_the_running_turn(env, cdx):
    host = await env.started(held_midturn_after_s=0)
    sid, _pid, _workdir = await _cdx_session(env)
    cdx.scenario([{"text": "начал"}, {"hang": 2.0}, {"text": "дошёл"}], [{"text": "лишний"}])
    await _running(env, host, sid, "первая")
    await env.until(host, _has_event(env, sid, "text", "начал"), what="ход идёт")
    await _say(env, sid, "вторая посреди хода")
    await env.until(host, lambda: bool(cdx.methods("turn/steer")), what="реплика подана в идущий ход")
    await _turn_done(env, host, sid)
    [steer] = cdx.methods("turn/steer")
    assert steer["input"][-1]["text"].endswith("вторая посреди хода")
    assert steer["clientUserMessageId"] and steer["expectedTurnId"]
    for _ in range(3):
        await env.cycle(host)
    assert len(cdx.methods("turn/start")) == 1  # второго хода нет
    full = (await env.ui("GET", f"/sessions/{sid}")).json()
    assert not [s for s in full["says"] if s["status"] in ("queued", "sending")]


async def test_codex_refused_steer_goes_next_turn_once(env, cdx):
    host = await env.started(held_midturn_after_s=0)
    sid, _pid, _workdir = await _cdx_session(env)
    cdx.scenario([{"hang": 1.5}, {"text": "раз"}], [{"text": "два"}], no_steer=True)
    await _running(env, host, sid, "первая")
    await env.until(host, lambda: host.held.runners[sid].busy, what="ход идёт")
    await _say(env, sid, "вторая")
    await _turns_n(env, host, cdx, 2)
    await _turn_done(env, host, sid)
    assert cdx.texts()[0] == "первая" and cdx.texts()[1].endswith("вторая")
    for _ in range(3):
        await env.cycle(host)
    assert len(cdx.methods("turn/start")) == 2


async def test_codex_urgent_say_interrupts_without_error(env, cdx):
    host = await env.started()
    sid, _pid, _workdir = await _cdx_session(env)
    cdx.scenario([{"text": "начал"}, {"hang": 30}], [{"text": "срочное принято"}])
    await _running(env, host, sid, "долгое")
    await env.until(host, _has_event(env, sid, "text", "начал"), what="ход идёт")
    await _say(env, sid, "срочно", priority="now")
    await _turns_n(env, host, cdx, 2)
    await _turn_done(env, host, sid)
    assert len(cdx.methods("turn/interrupt")) == 1
    events = await _events(env, sid)
    kinds = [e["kind"] for e in events]
    assert kinds.count("interrupt") == 1 and "error" not in kinds and not any(e["is_error"] for e in events)
    assert cdx.texts()[1] == "срочно" and await _has_event(env, sid, "text", "срочное принято")()
    assert len([c for c in cdx.calls() if "argv" in c]) == 2  # процесс тот же


async def test_codex_interrupt_button_stops_the_turn_without_error(env, cdx):
    host = await env.started()
    sid, _pid, _workdir = await _cdx_session(env)
    cdx.scenario([{"text": "начал"}, {"hang": 60}], [{"text": "дальше"}])
    await _running(env, host, sid, "долгое")
    await env.until(host, _has_event(env, sid, "text", "начал"), what="ход идёт")
    r = await env.ui("POST", f"/sessions/{sid}/interrupt")
    assert r.status_code == 200, r.text
    await _turn_done(env, host, sid)
    assert len(cdx.methods("turn/interrupt")) == 1
    events = await _events(env, sid)
    kinds = [e["kind"] for e in events]
    assert kinds.count("interrupt") == 1 and "error" not in kinds


async def test_codex_interrupt_kills_commands_of_the_interrupted_turn_only(env, cdx):
    """Ф74-5б: Codex на `turn/interrupt` команду оставляет идти — хост снимает команды прерванного хода по
    метке в окружении; команда прошлого хода (фоновая, законно идущая дальше) жива."""
    host = await env.started()
    sid, _pid, workdir = await _cdx_session(env)
    keep, cut = Path(workdir) / "keep.pid", Path(workdir) / "cut.pid"
    cdx.scenario(
        [{"bg": "echo $$ > keep.pid; exec sleep 300"}, {"text": "фон запущен"}],
        [{"bg": "echo $$ > cut.pid; exec sleep 300"}, {"text": "начал"}, {"hang": 60}],
    )
    await _running(env, host, sid, "фон")
    await _turn_done(env, host, sid)
    await _say(env, sid, "долгое")
    await env.until(host, lambda: cut.exists() and cut.read_text().strip(), what="команда хода начата")
    keep_pid, cut_pid = int(keep.read_text()), int(cut.read_text())
    try:
        bound = next(c["bg"] for c in cdx.calls() if "bg" in c and "cut.pid" in c["bg"])
        assert f"export {codex_tools.CMD_MARK_ENV}=" in bound
        assert codex_tools.unbound(bound) == "echo $$ > cut.pid; exec sleep 300"
        r = await env.ui("POST", f"/sessions/{sid}/interrupt")
        assert r.status_code == 200, r.text
        await _turn_done(env, host, sid)
        gone = lambda: not pid_alive(cut_pid)  # noqa: E731
        await env.until(host, gone, what="команда прерванного хода снята", limit_s=15)
        assert pid_alive(keep_pid)
    finally:
        for pid in (keep_pid, cut_pid):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


async def test_codex_failed_turn_is_error_and_limit_pauses_host(env, cdx):
    host = await env.started()
    sid, _pid, _workdir = await _cdx_session(env)
    limit = {"primary": {"usedPercent": 100, "resetsAt": 4_102_444_800}, "rateLimitReachedType": "x"}
    cdx.scenario([{"text": "начал"}, {"limit": limit}, {"fail": "usageLimitExceeded"}])
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    assert await _has_event(env, sid, "error", "лимит подписки исчерпан")()
    pause = host.state["pauses"]["codex"]
    assert pause["reason"] == "лимит подписки Codex" and pause["until"] == (4_102_444_800 + 60) * 1000
    assert host.held._hold({"engine": "codex"})[1] == "пауза лимита подписки"  # noqa: SLF001
    assert host.held._hold({"engine": "claude"}) is None  # noqa: SLF001 — пауза только своего движка


async def test_codex_limit_error_without_reset_pauses_for_minimum(env, cdx):
    host = await env.started()
    sid, _pid, _workdir = await _cdx_session(env)
    cdx.scenario([{"fail": "usageLimitExceeded"}])
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    pause = host.state["pauses"]["codex"]
    assert pause["reason"] == "лимит подписки Codex" and pause["until"] > host._now_ms()  # noqa: SLF001


# --- гард через хук -----------------------------------------------------------------------------------------


async def test_codex_hook_guard_allow_deny_card_and_mcp(env, cdx):
    host = await env.started()
    sid, _pid, workdir = await _cdx_session(env)
    cdx.scenario(
        [
            {"bash": "echo ok > a.txt"},
            {"bash": "cat ~/.codex/auth.json"},
            {"bash": "git push origin main"},
            {"patch": "*** Begin Patch\n*** Update File: CLAUDE.md\n@@\n-a\n+b\n*** End Patch\n"},
            {"mcp": ["hub-all", "hub_read", {"channel_id": 1}]},
            {"mcp": ["hub-all", "hub_move_channel", {"channel_id": 1, "project_id": 2}]},
            {"text": "всё"},
        ]
    )
    await _running(env, host, sid)
    push = await _card(env, host, sid)
    assert push["tool"] == "Bash" and push["input"]["command"] == "git push origin main"
    await _decide(env, push)
    rules = await _card(env, host, sid)
    assert rules["tool"] == "Edit" and rules["input"]["file_path"] == str(workdir / "CLAUDE.md")
    assert rules["rule"] == "project_rules" and "*** Update File: CLAUDE.md" in rules["input"]["patch"]
    await _decide(env, rules, "allow")
    write = await _card(env, host, sid)
    assert write["tool"] == "mcp__hub-all__hub_move_channel" and write["rule"] == "mcp_write"
    await _decide(env, write)
    await _turn_done(env, host, sid)
    assert _decisions(cdx) == ["allow", "deny", "deny", "allow", "allow", "deny"]
    reason = cdx.hooks()[1]["answer"]["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason.startswith("гард хаба")
    assert [r["ran"].splitlines()[-1] for r in cdx.ran()] == ["echo ok > a.txt"]
    assert await _has_event(env, sid, "decision", "владелец отказал: Bash")()
    [end] = [e for e in await _events(env, sid) if e["kind"] == "turn_end"]
    assert "отказов 3" in end["summary"]


async def test_codex_freed_push_needs_no_card(env, cdx):
    host = await env.started()
    await _free(env, "push")
    sid, _pid, _workdir = await _cdx_session(env, mode="auto")
    cdx.scenario([{"bash": "git push origin main || true"}, {"text": "всё"}])
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    assert _decisions(cdx) == ["allow"]
    assert not [e for e in await _events(env, sid) if e["kind"] == "request"]


async def test_codex_crashed_hook_blocks_the_call(env, cdx, monkeypatch):
    """Codex 0.163.0 при сбое хука вызов ПРОПУСКАЕТ (fail-open): обёртка команды хука превращает любой сбой
    (нет файла, падение интерпретатора) в код 2 — отказ."""
    host = await env.started()
    monkeypatch.setattr(type(host), "codex_hook_command", lambda self: [str(env.tmp / "нет-такого-хука")])
    sid, _pid, workdir = await _cdx_session(env)
    cdx.scenario([{"bash": "echo x > crashed.txt"}, {"text": "всё"}])
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    [hook] = cdx.hooks()
    assert hook["rc"] == 2 and codex_process.HOOK_FAILED in hook["stderr"]
    assert cdx.ran() == [] and not (workdir / "crashed.txt").exists()


async def test_codex_bypass_of_hook_interrupts_the_turn(env, cdx):
    host = await env.started()
    sid, _pid, _workdir = await _cdx_session(env)
    cdx.scenario([{"bypass": "commandExecution"}], [{"stdin": "rm -rf ~\n"}], [{"text": "ок"}])
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    assert await _has_event(env, sid, "error", codex_process.BYPASS_TEXT.format(what="команда"))()
    assert len(cdx.methods("turn/interrupt")) == 1
    await _say(env, sid, "дальше")
    await _turns_n(env, host, cdx, 2)
    await _turn_done(env, host, sid)
    stdin = codex_process.BYPASS_TEXT.format(what="ввод в запущенную команду")
    assert await _has_event(env, sid, "error", stdin)()
    assert len(cdx.methods("turn/interrupt")) == 2


async def test_codex_tty_and_subagent_model(env, cdx):
    host = await env.started()
    sid, _pid, _workdir = await _cdx_session(env)
    cdx.scenario(
        [
            {"bash": "cat /dev/tty"},
            {"spawn": {"model": "gpt-5", "message": "x"}},
            {"spawn": {"message": "помоги"}},
            {"text": "всё"},
        ]
    )
    await _running(env, host, sid)
    tty = await _card(env, host, sid)
    assert tty["rule"] == "guard_failure" and "/dev/tty" in tty["reason"]
    await _decide(env, tty)
    await _turn_done(env, host, sid)
    assert _decisions(cdx) == ["deny", "deny", "allow"]
    denied = cdx.hooks()[1]["answer"]["hookSpecificOutput"]["permissionDecisionReason"]
    assert "только на модели сессии" in denied


# --- вопросы, сжатие, продолжение, системный текст ----------------------------------------------------------


QUESTION = {
    "id": "stand",
    "header": "Стенд",
    "question": "Какой стенд?",
    "options": [{"label": "dev", "description": "разработка"}, {"label": "prod", "description": "боевой"}],
}


async def test_codex_question_with_options_is_a_card_and_answer_goes_back(env, cdx):
    host = await env.started()
    sid, _pid, _workdir = await _cdx_session(env)
    cdx.scenario([{"question": [QUESTION]}, {"text": "беру dev"}])
    await _running(env, host, sid)
    request = await _card(env, host, sid)
    assert request["tool"] == "AskUserQuestion"
    [q] = request["input"]["questions"]
    assert q["question"] == "Какой стенд?" and [o["label"] for o in q["options"]] == ["dev", "prod"]
    r = await env.ui(
        "POST",
        f"/session-requests/{request['id']}/decide",
        {"decision": "allow", "answers": {"Какой стенд?": "dev"}},
    )
    assert r.status_code == 200, r.text
    await _turn_done(env, host, sid)
    [answer] = [c["answer"] for c in cdx.calls() if "answer" in c]
    assert answer == {"answers": {"stand": {"answers": ["dev"]}}}


async def test_codex_compact_is_thread_compact(env, cdx):
    host = await env.started()
    sid, _pid, _workdir = await _cdx_session(env)
    cdx.scenario([{"text": "ок"}])
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    r = await env.ui("POST", f"/sessions/{sid}/compact", {})
    assert r.status_code == 200, r.text
    runner = host.held.runners[sid]
    await env.until(host, lambda: bool(cdx.methods("thread/compact/start")), what="сжатие запущено")
    await env.until(host, lambda: runner.compacting is None, what="сжатие закончено")
    await env.until(host, _has_event(env, sid, "compact"), what="событие сжатия")
    events = await _events(env, sid)
    assert [e["kind"] for e in events].count("compact") == 1
    assert len(cdx.methods("turn/start")) == 1  # сжатие — не реплика
    # Живой стенд 21.10: после сжатия следующая реплика начинает ход.
    await _say(env, sid, "после сжатия")
    await _turns_n(env, host, cdx, 2)
    await _turn_done(env, host, sid)
    assert cdx.texts()[1].endswith("после сжатия")


async def test_codex_missing_thread_is_a_failure_not_a_new_thread(env, cdx):
    host = await env.started(held_launch_retry_s=0)
    sid, _pid, _workdir = await _cdx_session(env)
    cdx.scenario([{"text": "ок"}])
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    await env.ui("POST", f"/sessions/{sid}/sleep")
    await env.until(host, lambda: sid not in host.held.runners, what="процесс сессии снят")
    (cdx.dir / "threads.json").write_text("{}", encoding="utf-8")
    await _say(env, sid, "ещё")
    assert codex_process.THREAD_GONE in await _failure(env, host, sid)
    assert len(cdx.methods("thread/start")) == 1 and cdx.methods("thread/resume") == []


async def test_codex_role_change_is_corrected_on_resume(env, cdx):
    host = await env.started()
    sid, _pid, _workdir = await _cdx_session(env, role="тестер")
    cdx.scenario([{"text": "ок"}])
    await _running(env, host, sid, "первая")
    await _turn_done(env, host, sid)
    await env.ui("POST", f"/sessions/{sid}/sleep")
    await env.until(host, lambda: sid not in host.held.runners, what="процесс сессии снят")
    headers = await env.ui_headers()
    r = await env.client.patch(f"/api/v1/sessions/{sid}", json={"role": "программист"}, headers=headers)
    assert r.status_code == 200, r.text
    await _running(env, host, sid, "вторая")
    await _turns_n(env, host, cdx, 2)
    await _turn_done(env, host, sid)
    # Поправка — сообщением разработчика в историю нити (стенд 21.10: в реплике модель её не слушала).
    [inject] = cdx.methods("thread/inject_items")
    [item] = inject["items"]
    assert item["role"] == "developer" and item["type"] == "message"
    fix = item["content"][0]["text"]
    assert held_mod.PIN_FIX_HEAD in fix and "Твоя роль теперь: «программист» 1" in fix
    assert cdx.texts()[1] == "вторая"
    assert "developerInstructions" not in cdx.methods("thread/resume")[0]
    await _say(env, sid, "третья")
    await _turns_n(env, host, cdx, 3)
    await _turn_done(env, host, sid)
    assert cdx.texts()[2] == "третья"  # поправка — один раз


async def test_codex_pin_fix_falls_back_to_reply_text_when_inject_is_refused(env, cdx):
    host = await env.started()
    sid, _pid, _workdir = await _cdx_session(env, role="тестер")
    cdx.scenario([{"text": "ок"}], no_inject=True)
    await _running(env, host, sid, "первая")
    await _turn_done(env, host, sid)
    await env.ui("POST", f"/sessions/{sid}/sleep")
    await env.until(host, lambda: sid not in host.held.runners, what="процесс сессии снят")
    headers = await env.ui_headers()
    r = await env.client.patch(f"/api/v1/sessions/{sid}", json={"role": "программист"}, headers=headers)
    assert r.status_code == 200, r.text
    await _running(env, host, sid, "вторая")
    await _turns_n(env, host, cdx, 2)
    await _turn_done(env, host, sid)
    text = cdx.texts()[1]
    assert held_mod.PIN_FIX_HEAD in text and "Твоя роль теперь: «программист» 1" in text and "вторая" in text


# --- выключатель, вход, модель, MCP -------------------------------------------------------------------------


async def _failure(env, host, sid) -> str:
    async def failed():
        return (await _rec(env, sid))["failure"]

    return await env.until(host, failed, what="причина у сессии")


async def test_codex_off_switch(env, cdx):
    host = await env.started(held_codex=False, held_launch_retry_s=0)
    sid, _pid, _workdir = await _cdx_session(env)
    await _say(env, sid)
    assert await _failure(env, host, sid) == held_mod.ENGINE_OFF.format(label="Codex")
    assert [c for c in cdx.calls() if "argv" in c] == []


async def test_codex_logged_out_is_refused(env, cdx):
    (cdx.dir / "logged-out").write_text("", encoding="utf-8")
    host = await env.started(held_launch_retry_s=0)
    sid, _pid, _workdir = await _cdx_session(env)
    await _say(env, sid)
    assert "codex login" in await _failure(env, host, sid)
    assert [c for c in cdx.calls() if "argv" in c] == []
    [login] = [c for c in cdx.calls() if c.get("login")]
    assert login["env"]["HUB_SESSION"] is None  # проверка входа — без личности сессии


async def test_codex_unknown_model_is_refused_not_replaced(env, cdx):
    host = await env.started(held_launch_retry_s=0)
    sid, _pid, _workdir = await _cdx_session(env, model="gpt-0-nope")
    await _say(env, sid)
    assert "модели «gpt-0-nope» нет у движка" in await _failure(env, host, sid)
    assert cdx.methods("thread/start") == []


async def test_codex_mcp_set_like_claude_and_reload_reports_statuses(env, cdx):
    (env.home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"notion": {"type": "http", "url": "https://n.example"}}}), encoding="utf-8"
    )
    (env.home / ".codex").mkdir(exist_ok=True)
    (env.home / ".codex" / "config.toml").write_text(
        '[mcp_servers.hub-ext]\ncommand = "hub"\n[mcp_servers.notion]\nurl = "https://n.example"\n',
        encoding="utf-8",
    )
    cdx.scenario([{"text": "раз"}], mcp={"notion": "failed"})
    host = await env.started(held_signal_wake_gap_s=0)
    sid, _pid, _workdir = await _cdx_session(env)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    [start] = cdx.methods("thread/start")
    assert sorted(start["config"]["mcp_servers"]) == ["hub-all", "hub-ext", "notion"]
    # Ф74-8: флаг нить теряет — выключение ещё и записью в `config` нити.
    assert start["config"]["mcp_servers"]["hub-ext"] == {"enabled": False}
    run = [c for c in cdx.calls() if "argv" in c][-1]
    assert "mcp_servers.hub-ext.enabled=false" in run["argv"]
    assert not any("notion" in a and "enabled=false" in a for a in run["argv"])
    thread = (await _rec(env, sid))["engine_session_id"]
    r = await env.ui("POST", f"/sessions/{sid}/mcp-reload")
    assert r.status_code == 200, r.text

    async def reloaded():
        return [
            e["summary"]
            for e in await _events(env, sid)
            if e["kind"] == "status" and e["summary"].startswith(held_mod.MCP_RELOAD_HEAD)
        ]

    [text] = await env.until(host, reloaded, what="итог «Перечитать MCP»")
    assert "hub-all ✓" in text and "notion — не подключился" in text
    assert (await _rec(env, sid))["engine_session_id"] == thread
    assert cdx.methods("thread/resume")[0]["threadId"] == thread
    assert len(cdx.methods("turn/start")) == 1  # без хода
