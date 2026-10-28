"""Ф74-7 / 74-3: хук хаба внешних Codex и Cursor (`hub-ext-hook`): подстановка личности, метки ожидания,
ожидание и его коды, конец разговора, тишина в сессиях хаба. Процесс «движка» — сам pytest (его настоящий
`starttime`), его движок подменяется."""

import io
import json
import os
import signal
import stat

import pytest

from hub import ext_hook, session_registry

CONV = "01a15e8b-f2fb-74d1-b8ab-d0917d6c654e"
OTHER = "8d0c0920-44c7-424b-9679-1a09708f2eed"
HUB = "/opt/hub/.venv/bin/hub"
ME = os.getpid()


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    root = tmp_path / "state"
    monkeypatch.setenv("HUB_EXT_STATE_DIR", str(root))
    monkeypatch.delenv("HUB_SESSION", raising=False)
    monkeypatch.setattr(ext_hook, "hub_bin", lambda: HUB)
    return root


@pytest.fixture
def engine_is(monkeypatch):
    """Процесс pytest — «движок» нужного вида."""

    def set_engine(engine: str):
        monkeypatch.setattr(
            session_registry, "ext_engine", lambda pid, *a, **k: engine if pid == ME else None
        )
        start = ext_hook.proc_start(ME)
        monkeypatch.setattr(session_registry, "ext_ancestor", lambda pid, *a, **k: (engine, ME, start))

    return set_engine


def mark(engine: str, target: str = "17", *, pid: int = ME, url: str | None = None) -> str:
    argv = [HUB, "sub", "wait", target, "--profile", "all"] + (["--url", url] if url else [])
    return ext_hook.WAIT_MARK + json.dumps({"engine": engine, "pid": pid, "argv": argv})


def run_main(monkeypatch, capsys, argv, inp) -> str:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(inp)))
    assert ext_hook.main(argv) == 0
    return capsys.readouterr().out


def put_state(
    engine: str, conv: str = CONV, *, marks=None, pid: int = ME, start=None, folder="/tmp", **extra
):
    state = {
        "v": 1,
        "engine": engine,
        "session": conv,
        "dir": folder,
        "engine_pid": pid,
        "engine_start": ext_hook.proc_start(ME) if start is None else start,
        "engine_exe": "/usr/bin/codex",
        "marks": {"17": ["sub", "wait", "17", "--profile", "all"]} if marks is None else marks,
        **extra,
    }
    ext_hook.save_state(engine, conv, state)
    return state


# --- pre: личность в аргументах -----------------------------------------------------------------------------


def test_codex_pre_adds_identity_to_hub_session_tool(monkeypatch, capsys):
    out = run_main(
        monkeypatch,
        capsys,
        ["codex", "pre"],
        {
            "session_id": CONV,
            "cwd": "/w",
            "tool_name": "mcp__hub-all__hub_draft",
            "tool_input": {"text": "x"},
        },
    )
    spec = json.loads(out)["hookSpecificOutput"]
    assert spec["permissionDecision"] == "allow" and spec["hookEventName"] == "PreToolUse"
    assert spec["updatedInput"] == {"text": "x", "ext_session": CONV, "ext_dir": "/w"}


@pytest.mark.parametrize(
    "tool", ["mcp__hub-all__hub_inbox", "mcp__ru-edit__hub_draft", "mcp__hub-all__hub_draftx", "Bash"]
)
def test_codex_pre_skips_other_tools(monkeypatch, capsys, tool):
    out = run_main(
        monkeypatch,
        capsys,
        ["codex", "pre"],
        {"session_id": CONV, "cwd": "/w", "tool_name": tool, "tool_input": {}},
    )
    assert out == ""


def test_cursor_pre_uses_conversation_and_first_workspace_root(monkeypatch, capsys):
    inp = {
        "conversation_id": OTHER,
        "session_id": OTHER,
        "workspace_roots": ["/p1", "/p2"],
        "tool_name": "MCP:hub_subscribe",
        "tool_input": {"project_id": 3},
    }
    out = json.loads(run_main(monkeypatch, capsys, ["cursor", "pre"], inp))
    assert out == {
        "permission": "allow",
        "updated_input": {"project_id": 3, "ext_session": OTHER, "ext_dir": "/p1"},
    }


def test_hub_session_hooks_are_silent(monkeypatch, capsys, engine_is):
    engine_is("codex")
    monkeypatch.setenv("HUB_SESSION", "42")
    inp = {"session_id": CONV, "cwd": "/w", "tool_name": "mcp__hub-all__hub_draft", "tool_input": {}}
    for event in ("pre", "post", "stop", "start", "end"):
        assert run_main(monkeypatch, capsys, ["codex", event], inp) == ""
        assert run_main(monkeypatch, capsys, ["cursor", event], {**inp, "tool_name": "MCP:hub_draft"}) == ""
    assert not (ext_hook.state_root()).exists()


async def test_session_tools_match_mcp_server():
    """Список хука = инструменты сервера с `ext_session` (новый инструмент сессии без хука не останется)."""
    from mcp import Client

    from hub.mcp_server import server as srv

    async with Client(srv.build_server(srv.Hub.__new__(srv.Hub))) as client:
        tools = (await client.list_tools()).tools
    assert {
        t.name for t in tools if "ext_session" in t.input_schema.get("properties", {})
    } == ext_hook.SESSION_TOOLS


# --- метки --------------------------------------------------------------------------------------------------


def test_parse_marks_own_engine_last_per_target_and_cursor_json_string():
    text = "\n".join(
        [
            "Подписка #17",
            mark("codex", "--channels"),
            mark("cursor", "17"),
            mark("codex", "17"),
            mark("codex", "17"),
        ]
    )
    marks, pid = ext_hook.parse_marks("codex", {"content": [{"type": "text", "text": text}]})
    assert pid == ME and list(marks) == ["--channels", "17"]
    assert marks["17"] == ["sub", "wait", "17", "--profile", "all"]
    cursor_out = json.dumps(
        {"content": [{"type": "text", "text": mark("cursor", "17", url="http://127.0.0.1:8899/api/v1")}]}
    )
    marks, _pid = ext_hook.parse_marks("cursor", cursor_out)
    assert marks == {"17": ["sub", "wait", "17", "--profile", "all", "--url", "http://127.0.0.1:8899/api/v1"]}


@pytest.mark.parametrize(
    "argv",
    [
        ["/bin/sh", "sub", "wait", "17", "--profile", "all", "--url", "https://evil.example/api"],
        [HUB, "sub", "wait", "17", "--profile", "all", "--oc-dir", "/"],
        [HUB, "sub", "wait", "17; rm", "--profile", "all"],
        [HUB, "sub", "leave", "17", "--profile", "all"],
        [HUB, "sub", "wait", "17", "--profile", "../x"],
    ],
)
def test_parse_marks_rejects_foreign_shapes(argv):
    line = ext_hook.WAIT_MARK + json.dumps({"engine": "codex", "pid": ME, "argv": argv})
    assert ext_hook.parse_marks("codex", line) == ({}, None)


def test_wait_uses_own_hub_binary_not_marks_first_word(state_dir):
    """Бинарь ожидания — всегда `hub` рядом с хуком, первое слово метки отбрасывается."""
    line = ext_hook.WAIT_MARK + json.dumps(
        {"engine": "codex", "pid": ME, "argv": ["/tmp/evil", "sub", "wait", "17", "--profile", "all"]}
    )
    marks, _ = ext_hook.parse_marks("codex", line)
    state = put_state("codex", marks=marks)
    seen = []
    w = ext_hook.Waits("codex", CONV, popen=lambda argv, **kw: seen.append(argv) or FakeChild(None, "", kw))
    w.start_missing(state)
    assert seen[0][0] == HUB and "/tmp/evil" not in seen[0]
    assert seen[0][-6:] == ["--ext-session", CONV, "--ext-dir", "/tmp", "--ext-pid", str(ME)]


# --- post: файл состояния -----------------------------------------------------------------------------------


def test_cursor_post_saves_marks_privately(monkeypatch, capsys, engine_is, state_dir):
    engine_is("cursor")
    out = json.dumps(
        {"content": [{"type": "text", "text": "ok\n" + mark("cursor") + "\n" + mark("cursor", "--channels")}]}
    )
    inp = {
        "conversation_id": OTHER,
        "workspace_roots": ["/p"],
        "tool_name": "MCP:hub_subscribe",
        "tool_output": out,
    }
    assert run_main(monkeypatch, capsys, ["cursor", "post"], inp) == ""
    path = state_dir / "cursor" / f"{OTHER}.json"
    state = json.loads(path.read_text())
    assert (
        state["engine_pid"] == ME and state["dir"] == "/p" and sorted(state["marks"]) == ["--channels", "17"]
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_post_ignores_pid_that_is_not_the_engine(monkeypatch, capsys, engine_is, state_dir):
    engine_is("codex")  # pytest — Codex, а метка говорит «Cursor»
    out = json.dumps({"content": [{"type": "text", "text": mark("cursor")}]})
    inp = {
        "conversation_id": OTHER,
        "workspace_roots": ["/p"],
        "tool_name": "MCP:hub_subscribe",
        "tool_output": out,
    }
    run_main(monkeypatch, capsys, ["cursor", "post"], inp)
    assert not (state_dir / "cursor" / f"{OTHER}.json").exists()


def test_codex_post_merges_marks_and_restarts_waiter(monkeypatch, capsys, engine_is):
    engine_is("codex")
    put_state("codex", marks={"--channels": ["sub", "wait", "--channels", "--profile", "all"]})
    calls = []
    monkeypatch.setattr(ext_hook, "respawn_waiter", lambda e, c, *, force: calls.append((e, c, force)))
    inp = {
        "session_id": CONV,
        "cwd": "/w",
        "tool_name": "mcp__hub-all__hub_subscribe",
        "tool_response": {"content": [{"type": "text", "text": mark("codex")}]},
    }
    run_main(monkeypatch, capsys, ["codex", "post"], inp)
    assert sorted(ext_hook.load_state("codex", CONV)["marks"]) == ["--channels", "17"]
    assert calls == [("codex", CONV, True)]


# --- ожидание и коды ----------------------------------------------------------------------------------------


class FakeChild:
    def __init__(self, code, text, kw):
        self.code = code
        self.terminated = False
        kw["stdout"].write(text.encode())

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        pass


def scripted(plan):
    """popen, отдающий по цели ожидания очередной (код, текст); `None` — ещё ждёт."""
    started = []

    def popen(argv, **kw):
        target = argv[3]
        started.append(target)
        code, text = plan[target].pop(0)
        return FakeChild(code, text, kw)

    popen.started = started
    return popen


@pytest.mark.parametrize(
    ("code", "delivered", "kept", "restarts"),
    [
        (0, True, True, True),
        (6, False, True, True),
        (3, True, False, False),
        (2, True, False, False),
        (4, False, True, False),
        (5, False, False, False),
        (-15, False, True, False),
    ],
)
def test_wait_exit_codes(code, delivered, kept, restarts):
    put_state("codex")
    clock = [100.0]
    popen = scripted({"17": [(code, "Хаб, подписка #17: сигнал 5"), (None, "")]})
    w = ext_hook.Waits("codex", CONV, popen=popen, now=lambda: clock[0])
    w.start_missing(ext_hook.load_state("codex", CONV))
    texts = w.poll()
    assert (texts == ["Хаб, подписка #17: сигнал 5"]) is delivered
    state = ext_hook.load_state("codex", CONV)
    assert (state is not None and "17" in state["marks"]) is kept
    clock[0] += 0.5
    w.start_missing(state or {"marks": {}})
    assert len(popen.started) == 1  # перезапуск не раньше RESTART_MIN_S
    clock[0] += ext_hook.RESTART_MIN_S
    w.start_missing(state or {"marks": {}})
    assert (len(popen.started) == 2) is restarts


def test_cursor_wait_once_joins_texts_and_stops_the_rest(monkeypatch):
    put_state(
        "cursor",
        OTHER,
        marks={
            "17": ["sub", "wait", "17", "--profile", "all"],
            "--channels": ["sub", "wait", "--channels", "--profile", "all"],
            "18": ["sub", "wait", "18", "--profile", "all"],
        },
    )
    popen = scripted({"17": [(0, "A")], "--channels": [(0, "B")], "18": [(None, "")]})
    w = ext_hook.Waits("cursor", OTHER, popen=popen)
    assert ext_hook.wait_once("cursor", OTHER, waits=w) == "A\n\nB"
    assert not w.children


def test_cursor_wait_once_ends_when_all_superseded(monkeypatch):
    put_state("cursor", OTHER)
    w = ext_hook.Waits("cursor", OTHER, popen=scripted({"17": [(4, "")]}))
    assert ext_hook.wait_once("cursor", OTHER, waits=w) is None
    assert ext_hook.load_state("cursor", OTHER) is not None  # вытеснило новое ожидание — метка жива


def test_cursor_stop_returns_followup(monkeypatch, capsys, engine_is):
    engine_is("cursor")
    put_state("cursor", OTHER)
    monkeypatch.setattr(ext_hook, "headless", lambda engine, pid: False)
    monkeypatch.setattr(ext_hook, "wait_once", lambda engine, conv: "Хаб: сигнал")
    out = run_main(monkeypatch, capsys, ["cursor", "stop"], {"conversation_id": OTHER, "status": "completed"})
    assert json.loads(out) == {"followup_message": "Хаб: сигнал"}


def test_cursor_stop_in_print_mode_does_not_wait(monkeypatch, capsys):
    put_state("cursor", OTHER)
    monkeypatch.setattr(ext_hook, "proc_argv", lambda pid: ["cursor-agent", "-p", "привет"])
    monkeypatch.setattr(ext_hook, "wait_once", lambda *a, **k: pytest.fail("ждать нельзя"))
    assert run_main(monkeypatch, capsys, ["cursor", "stop"], {"conversation_id": OTHER}) == ""


def test_stop_without_subscription_or_with_dead_engine(monkeypatch, capsys):
    monkeypatch.setattr(ext_hook, "respawn_waiter", lambda *a, **k: pytest.fail("ожидатель не нужен"))
    assert run_main(monkeypatch, capsys, ["codex", "stop"], {"session_id": CONV}) == ""
    put_state("codex", start=1)  # процесс с этим pid стартовал не тогда — движка нет
    assert run_main(monkeypatch, capsys, ["codex", "stop"], {"session_id": CONV}) == ""
    assert ext_hook.load_state("codex", CONV) is None


def test_codex_stop_respawns_dead_waiter_without_blocking(monkeypatch, capsys):
    put_state("codex", waiter={"pid": 999999999, "start": 1})
    calls = []
    monkeypatch.setattr(ext_hook, "respawn_waiter", lambda e, c, *, force: calls.append(force))
    assert run_main(monkeypatch, capsys, ["codex", "stop"], {"session_id": CONV}) == ""
    assert calls == [False]


def test_respawn_waiter_keeps_live_one(monkeypatch):
    put_state("codex", waiter={"pid": ME, "start": ext_hook.proc_start(ME)})
    monkeypatch.setattr(ext_hook.subprocess, "Popen", lambda *a, **k: pytest.fail("второй ожидатель"))
    ext_hook.respawn_waiter("codex", CONV, force=False)


def test_codex_waiter_queues_signals_until_marks_gone(monkeypatch):
    put_state("codex", waiter={"pid": ME, "start": ext_hook.proc_start(ME)})
    delivered = []
    popen = scripted({"17": [(0, "сигнал 1"), (3, "подписка снята")]})
    w = ext_hook.Waits(
        "codex",
        CONV,
        popen=popen,
        now=iter(range(0, 10_000, 5)).__next__,
        deliver_codes=ext_hook.CODEX_DELIVER_CODES,
    )
    monkeypatch.setattr(ext_hook, "POLL_S", 0)
    old = signal.getsignal(signal.SIGTERM)
    try:
        assert (
            ext_hook.run_waiter("codex", CONV, waits=w, deliver=lambda st, text: delivered.append(text)) == 0
        )
    finally:
        signal.signal(signal.SIGTERM, old)
    # «Подписка снята» в нить не кладётся: окно Codex могло быть закрыто, `codex queue` завёл бы ход без владельца.
    assert delivered == ["сигнал 1"]
    assert ext_hook.load_state("codex", CONV) is None


def test_codex_waiter_default_waits_do_not_queue_ended(monkeypatch):
    """Ожидатель Codex сам (без `waits=`) собирает ожидания с кодами доставки Codex."""
    put_state("codex", waiter={"pid": ME, "start": ext_hook.proc_start(ME)})
    delivered = []
    popen = scripted({"17": [(3, "Хаб: подписка #17 снята — окно закрыто.")]})
    real = ext_hook.Waits
    monkeypatch.setattr(ext_hook, "Waits", lambda engine, conv, **kw: real(engine, conv, popen=popen, **kw))
    monkeypatch.setattr(ext_hook, "POLL_S", 0)
    old = signal.getsignal(signal.SIGTERM)
    try:
        ext_hook.run_waiter("codex", CONV, deliver=lambda st, text: delivered.append(text))
    finally:
        signal.signal(signal.SIGTERM, old)
    assert delivered == []
    assert ext_hook.load_state("codex", CONV) is None


def test_codex_waiter_leaves_when_another_waiter_named(monkeypatch):
    put_state("codex", waiter={"pid": 1, "start": 1})
    w = ext_hook.Waits("codex", CONV, popen=lambda *a, **k: pytest.fail("ждать не должен"))
    old = signal.getsignal(signal.SIGTERM)
    try:
        ext_hook.run_waiter("codex", CONV, waits=w, deliver=lambda *a: pytest.fail("нечего доставлять"))
    finally:
        signal.signal(signal.SIGTERM, old)


def test_deliver_codex_queues_into_thread():
    seen = []

    class Res:
        returncode = 0

    ext_hook.deliver_codex(
        {"session": CONV, "engine_exe": "/x/codex"},
        "сигнал",
        run=lambda argv, **kw: seen.append(argv) or Res(),
    )
    assert seen == [["/x/codex", "queue", "--thread", CONV, "--message", "сигнал"]]


# --- конец разговора ----------------------------------------------------------------------------------------


def test_end_leaves_with_identity_and_drops_state(monkeypatch, capsys):
    put_state(
        "codex", marks={"17": ["sub", "wait", "17", "--profile", "all", "--url", "http://127.0.0.1:9/api/v1"]}
    )
    runs, kills = [], []
    monkeypatch.setattr(
        ext_hook.subprocess, "run", lambda argv, **kw: runs.append(argv) or type("R", (), {"returncode": 0})
    )
    monkeypatch.setattr(ext_hook, "kill_group", lambda pid, start: kills.append(pid))
    run_main(monkeypatch, capsys, ["codex", "end"], {"session_id": CONV, "reason": "other"})
    assert runs == [
        [
            HUB,
            "sub",
            "leave",
            "--ext-session",
            CONV,
            "--ext-dir",
            "/tmp",
            "--ext-pid",
            str(ME),
            "--profile",
            "all",
            "--url",
            "http://127.0.0.1:9/api/v1",
        ]
    ]
    assert ext_hook.load_state("codex", CONV) is None and kills == [None]


def test_start_drops_dead_and_cleared_conversations(monkeypatch, capsys, engine_is):
    engine_is("codex")
    put_state("codex", "dead-conv-0001", start=1)
    put_state("codex", "same-proc-0001")
    put_state("codex", "other-proc-0001", pid=1, start=ext_hook.proc_start(1))
    left = []
    monkeypatch.setattr(
        ext_hook.subprocess,
        "run",
        lambda argv, **kw: left.append(argv[4]) or type("R", (), {"returncode": 0}),
    )
    run_main(monkeypatch, capsys, ["codex", "start"], {"session_id": CONV, "source": "clear"})
    assert ext_hook.load_state("codex", "dead-conv-0001") is None
    assert ext_hook.load_state("codex", "same-proc-0001") is None and left == ["same-proc-0001"]
    assert ext_hook.load_state("codex", "other-proc-0001") is not None
    put_state("codex", "same-proc-0002")
    run_main(monkeypatch, capsys, ["codex", "start"], {"session_id": CONV, "source": "startup"})
    assert ext_hook.load_state("codex", "same-proc-0002") is not None
