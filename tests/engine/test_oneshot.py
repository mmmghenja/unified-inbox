"""Разовый ход `hub.engine.oneshot` (Ф78-2, 3.5, П2, П6–П9) на подставных CLI: argv и вход каждого движка,
картинки, продолжение, неудачный ход не двигает разговор, тайм-аут и отмена гасят группу, «нет входа» через
`status`, записи CLI и их чистка, путь к `claude`."""

import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from hub.engine import choice, oneshot

FAKE = r"""
import json, os, subprocess, sys, time
from pathlib import Path
here = Path(__file__).resolve().parent
argv = sys.argv[1:]
if argv[:2] == ["login", "status"] or argv[:1] == ["status"]:
    st = json.loads((here / "status.json").read_text()) if (here / "status.json").exists() else {}
    print(st.get("out", "Logged in as someone"))
    sys.exit(st.get("rc", 0))
stdin = sys.stdin.read()
calls = here / "calls.jsonl"
n = len(calls.read_text().splitlines()) if calls.exists() else 0
with calls.open("a") as f:
    f.write(json.dumps({"argv": argv, "stdin": stdin, "cwd": os.getcwd()}) + "\n")
steps = json.loads((here / "plan.json").read_text())
step = steps[min(n, len(steps) - 1)]
if step.get("sleep"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    (here / "pids").write_text(f"{os.getpid()} {child.pid}")
    time.sleep(step["sleep"])
sys.stdout.write(step.get("stdout", ""))
sys.stderr.write(step.get("stderr", ""))
sys.exit(step.get("rc", 0))
"""


class Fake:
    def __init__(self, directory: Path, name: str) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.dir = directory
        self.path = directory / name
        self.path.write_text(f"#!{sys.executable}\n{FAKE}", encoding="utf-8")
        self.path.chmod(self.path.stat().st_mode | stat.S_IXUSR)

    def plan(self, *steps: dict) -> None:
        (self.dir / "plan.json").write_text(json.dumps(list(steps)), encoding="utf-8")

    def status(self, out: str, rc: int = 0) -> None:
        (self.dir / "status.json").write_text(json.dumps({"out": out, "rc": rc}), encoding="utf-8")

    def calls(self) -> list[dict]:
        path = self.dir / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def png(tmp_path) -> Path:
    path = tmp_path / "pic.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return path


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path / "brain"


def _ok_claude(text: str = "ответ", sid: str = "s-1") -> str:
    init = {"type": "system", "subtype": "init", "session_id": sid}
    result = {
        "type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": sid,
        "total_cost_usd": 0.002,
        "usage": {"input_tokens": 10, "cache_read_input_tokens": 5, "output_tokens": 3},
    }  # fmt: skip
    return json.dumps(init) + "\n" + json.dumps(result) + "\n"


def _codex_events(thread: str = "019a-thread-0001", text: str = "ответ") -> str:
    events = [
        {"type": "thread.started", "thread_id": thread},
        {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
        {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 7}},
    ]
    return "".join(json.dumps(e) + "\n" for e in events)


def _cursor_ok(sid: str = "chat-1", text: str = "ответ") -> str:
    usage = {"inputTokens": 4, "cacheReadTokens": 6, "outputTokens": 2}
    return json.dumps(
        {"type": "result", "is_error": False, "result": text, "session_id": sid, "usage": usage}
    )


# --- claude ------------------------------------------------------------------------------------------------


async def test_claude_argv_input_images_resume(tmp_path, png, root):
    fake = Fake(tmp_path / "bin", "claude")
    fake.plan({"stdout": _ok_claude(sid="s-1")}, {"stdout": _ok_claude(sid="s-1", text="второй")})
    with oneshot.Workspace("claude", root=root, home=tmp_path) as ws:
        t1 = await ws.run(
            prompt="что на картинке", system="СИС", model="haiku", effort="low", images=[png],
            timeout_s=10, binary=str(fake.path),
        )  # fmt: skip
        t2 = await ws.run(
            prompt="ещё", system="СИС", model="haiku", effort=None, resume=t1.session, timeout_s=10,
            binary=str(fake.path),
        )  # fmt: skip
        workdir = ws.path
    assert t1.ok and t1.text == "ответ" and t1.session == "s-1" and t1.cost_usd == 0.002
    assert (t1.tokens_in, t1.tokens_out, t1.engine, t1.model) == (15, 3, "claude", "haiku")
    assert t2.ok and t2.text == "второй"
    c1, c2 = fake.calls()
    argv = c1["argv"]
    assert argv[:7] == ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                        "--model"]  # fmt: skip
    assert argv[argv.index("--tools") + 1] == "" and argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--mcp-config") + 1] == oneshot.CLAUDE_EMPTY_MCP and "--strict-mcp-config" in argv
    assert argv[argv.index("--system-prompt") + 1] == "СИС" and argv[argv.index("--effort") + 1] == "low"
    assert "--resume" not in argv and "--no-session-persistence" not in argv
    content = json.loads(c1["stdin"])["message"]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/png"
    assert content[-1] == {"type": "text", "text": "что на картинке"}
    assert c2["argv"][c2["argv"].index("--resume") + 1] == "s-1" and "--effort" not in c2["argv"]
    assert c1["cwd"] == str(workdir) and workdir.parent == root
    assert not await asyncio.to_thread(workdir.exists)  # П2/П9: каталог удалён close()


async def test_claude_persist_false_flag(tmp_path, root):
    fake = Fake(tmp_path / "bin", "claude")
    fake.plan({"stdout": _ok_claude()})
    with oneshot.Workspace("claude", root=root) as ws:
        await ws.run(prompt="x", system="s", model="m", effort=None, timeout_s=10, persist=False,
                     binary=str(fake.path))  # fmt: skip
    assert "--no-session-persistence" in fake.calls()[0]["argv"]


async def test_claude_limit_is_pause_auth_is_auth_failure_keeps_resume(tmp_path, root):
    fake = Fake(tmp_path / "bin", "claude")
    limit = json.dumps(
        {"type": "result", "is_error": True, "result": "Claude AI usage limit reached|1999999999"}
    )
    fake.plan(
        {"stdout": limit, "rc": 1},
        {"stderr": "Invalid API key · Please run /login", "rc": 1},
        {"stdout": "мусор", "rc": 3},
    )
    with oneshot.Workspace("claude", root=root) as ws:
        kw = {"system": "s", "model": "m", "effort": None, "resume": "old", "timeout_s": 10,
              "binary": str(fake.path)}  # fmt: skip
        t_limit = await ws.run(prompt="1", **kw)
        t_auth = await ws.run(prompt="2", **kw)
        t_fail = await ws.run(prompt="3", **kw)
    assert t_limit.pause and t_limit.failure == "limit" and not t_limit.auth and t_limit.session == "old"
    assert t_auth.auth and t_auth.failure == "auth" and not t_auth.pause and t_auth.session == "old"
    assert "нет входа" in t_auth.error
    assert not t_fail.ok and t_fail.failure == "exit 3" and t_fail.session == "old"


# --- codex -------------------------------------------------------------------------------------------------


async def test_codex_argv_images_resume_and_session(tmp_path, png, root):
    fake = Fake(tmp_path / "bin", "codex")
    fake.plan({"stdout": _codex_events(thread="019a-thread-0001")}, {"stdout": _codex_events(text="2")})
    with oneshot.Workspace("codex", root=root) as ws:
        t1 = await ws.run(prompt="P1", system="СИС \"кавычки\"", model="gpt-6-luna", effort="low",
                          images=[png], timeout_s=10, persist=False, binary=str(fake.path))  # fmt: skip
        t2 = await ws.run(prompt="P2", system="СИС", model="gpt-6-luna", effort="low", resume=t1.session,
                          timeout_s=10, binary=str(fake.path))  # fmt: skip
    assert t1.ok and t1.session == "019a-thread-0001" and (t1.tokens_in, t1.tokens_out) == (100, 7)
    assert t1.cost_usd is None and t2.ok and t2.text == "2"
    c1, c2 = fake.calls()
    a1 = c1["argv"]
    assert a1[:2] == ["exec", "--json"] and a1[-1] == "-" and f"--image={png}" in a1
    assert a1[a1.index("-m") + 1] == "gpt-6-luna" and "--ephemeral" in a1
    assert 'model_reasoning_effort="low"' in a1
    assert 'developer_instructions="СИС \\"кавычки\\""' in a1
    for feature in oneshot.CODEX_DISABLE:
        assert feature in a1
    assert "--ignore-user-config" in a1 and "--ignore-rules" in a1 and "--skip-git-repo-check" in a1
    assert c1["stdin"] == "P1"
    a2 = c2["argv"]
    # П6: на продолжении модель и усилие подаются снова; нить — перед «-».
    assert a2[:3] == ["exec", "resume", "--json"] and a2[-2:] == ["019a-thread-0001", "-"]
    assert a2[a2.index("-m") + 1] == "gpt-6-luna" and 'model_reasoning_effort="low"' in a2


async def test_codex_failure_auth_by_status_else_exit(tmp_path, root):
    fake = Fake(tmp_path / "bin", "codex")
    err = json.dumps({"type": "turn.failed", "error": {"message": "плохо"}})
    fake.plan({"stdout": err, "rc": 1})
    fake.status("Not logged in", rc=1)
    with oneshot.Workspace("codex", root=root) as ws:
        kw = {"system": "s", "model": "m", "effort": None, "resume": "019a-old-0001", "timeout_s": 10,
              "binary": str(fake.path)}  # fmt: skip
        t_auth = await ws.run(prompt="1", **kw)
        fake.status("Logged in using ChatGPT")
        t_fail = await ws.run(prompt="2", **kw)
    assert t_auth.auth and t_auth.failure == "auth" and t_auth.session == "019a-old-0001"
    assert not t_fail.auth and t_fail.failure == "exit 1" and "плохо" in t_fail.error
    assert t_fail.session == "019a-old-0001"


# --- cursor ------------------------------------------------------------------------------------------------


async def test_cursor_permissions_images_system_first_turn_only(tmp_path, png, root):
    fake = Fake(tmp_path / "bin", "cursor-agent")
    fake.plan({"stdout": _cursor_ok("chat-1")}, {"stdout": _cursor_ok("chat-1", "второй")})
    sibling = root / "чужое"
    sibling.mkdir(parents=True)
    with oneshot.Workspace("cursor", root=root) as ws:
        t1 = await ws.run(prompt="P1", system="СИС", model="composer-2.5", effort=None, images=[png],
                          timeout_s=10, binary=str(fake.path))  # fmt: skip
        conf1 = json.loads((ws.path / ".cursor" / "cli.json").read_text(encoding="utf-8"))
        t2 = await ws.run(prompt="P2", system="СИС", model="composer-2.5", effort=None, images=[png],
                          resume=t1.session, timeout_s=10, binary=str(fake.path))  # fmt: skip
        conf2 = json.loads((ws.path / ".cursor" / "cli.json").read_text(encoding="utf-8"))
        workdir = ws.path
    assert (
        t1.ok and t1.session == "chat-1" and (t1.tokens_in, t1.tokens_out) == (10, 2) and t2.text == "второй"
    )
    c1, c2 = fake.calls()
    a1 = c1["argv"]
    assert a1[:6] == ["-p", "--mode", "ask", "--trust", "--output-format", "json"]
    assert a1[a1.index("--model") + 1] == "composer-2.5" and a1[a1.index("--workspace") + 1] == str(workdir)
    assert "--resume" not in a1 and c2["argv"][c2["argv"].index("--resume") + 1] == "chat-1"
    assert (
        c1["stdin"].startswith(oneshot.CURSOR_SYSTEM_OPEN)
        and "СИС" in c1["stdin"]
        and "img/1.png" in c1["stdin"]
    )
    assert oneshot.CURSOR_SYSTEM_OPEN not in c2["stdin"] and "img/2.png" in c2["stdin"]
    assert conf1["permissions"]["allow"] == ["Read(img/1.png)"]
    assert conf2["permissions"]["allow"] == ["Read(img/1.png)", "Read(img/2.png)"]
    deny = conf2["permissions"]["deny"]
    assert all(d in deny for d in oneshot.CURSOR_DENY)
    real_sibling, real_workdir = await asyncio.to_thread(
        lambda: (os.path.realpath(sibling), os.path.realpath(workdir))
    )
    assert f"Read({real_sibling}/**)" in deny  # соседи каталога по пути закрыты
    assert not any(real_workdir in d for d in deny)  # сам каталог — нет


async def test_cursor_effort_suffix_and_fast_refused(tmp_path, root):
    fake = Fake(tmp_path / "bin", "cursor-agent")
    fake.plan({"stdout": _cursor_ok()})
    with oneshot.Workspace("cursor", root=root) as ws:
        t = await ws.run(prompt="x", system="", model="grok-4.7", effort="high", timeout_s=10,
                         binary=str(fake.path))  # fmt: skip
        t_fast = await ws.run(prompt="x", system="", model="composer-fast", effort=None, timeout_s=10,
                              binary=str(fake.path))  # fmt: skip
    assert t.ok and fake.calls()[0]["argv"][fake.calls()[0]["argv"].index("--model") + 1] == "grok-4.7-high"
    assert not t_fast.ok and t_fast.failure == "model" and len(fake.calls()) == 1


async def test_cursor_garbage_with_login_is_auth(tmp_path, root):
    fake = Fake(tmp_path / "bin", "cursor-agent")
    fake.plan({"stdout": "", "stderr": "boom", "rc": 1})
    fake.status("Not logged in")
    with oneshot.Workspace("cursor", root=root) as ws:
        t = await ws.run(prompt="x", system="", model="composer-2.5", effort=None, resume="chat-9",
                         timeout_s=10, binary=str(fake.path))  # fmt: skip
    assert t.auth and t.failure == "auth" and t.session == "chat-9"


# --- общее -------------------------------------------------------------------------------------------------


async def test_bad_image_and_unknown_engine_fail_without_process(tmp_path, root):
    fake = Fake(tmp_path / "bin", "claude")
    fake.plan({"stdout": _ok_claude()})
    bad = tmp_path / "doc.pdf"
    bad.write_bytes(b"%PDF")
    with oneshot.Workspace("claude", root=root) as ws:
        t_img = await ws.run(prompt="x", system="s", model="m", effort=None, images=[bad], resume="r",
                             timeout_s=10, binary=str(fake.path))  # fmt: skip
        t_none = await ws.run(prompt="x", system="s", model="m", effort=None, images=[tmp_path / "нет.png"],
                              timeout_s=10, binary=str(fake.path))  # fmt: skip
    t_eng = await oneshot.run("gemini", prompt="x", system="s", model="m", effort=None, workdir=tmp_path,
                              timeout_s=1)  # fmt: skip
    assert t_img.failure == "image" and t_img.session == "r" and t_none.failure == "image"
    assert t_eng.failure == "engine" and fake.calls() == []


def _dead(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as fh:
            return fh.read().split(")")[-1].split()[0] == "Z"
    except OSError:
        return True


async def _wait_dead(pids: list[int]) -> bool:
    for _ in range(100):
        if all(_dead(p) for p in pids):
            return True
        await asyncio.sleep(0.05)
    return False


async def _wait_pids(directory: Path) -> list[int]:
    path = directory / "pids"
    for _ in range(200):
        if path.exists() and path.read_text().strip():
            return [int(x) for x in path.read_text().split()]
        await asyncio.sleep(0.05)
    raise AssertionError("подставной CLI не записал pid")


async def test_timeout_kills_group_and_keeps_session(tmp_path, root):
    fake = Fake(tmp_path / "bin", "codex")
    fake.plan({"sleep": 30, "stdout": _codex_events()})
    with oneshot.Workspace("codex", root=root) as ws:
        started = time.monotonic()
        task = asyncio.ensure_future(
            ws.run(prompt="x", system="s", model="m", effort=None, resume="019a-old-0001", timeout_s=1.5,
                   binary=str(fake.path))
        )  # fmt: skip
        pids = await _wait_pids(fake.dir)
        turn = await task
    assert time.monotonic() - started < 15
    assert turn.failure == "timeout" and turn.session == "019a-old-0001" and not turn.ok
    assert await _wait_dead(pids)


async def test_cancel_kills_group_and_propagates(tmp_path, root):
    fake = Fake(tmp_path / "bin", "claude")
    fake.plan({"sleep": 30, "stdout": _ok_claude()})
    with oneshot.Workspace("claude", root=root) as ws:
        task = asyncio.ensure_future(
            ws.run(prompt="x", system="s", model="m", effort=None, timeout_s=60, binary=str(fake.path))
        )
        pids = await _wait_pids(fake.dir)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert await _wait_dead(pids)


# --- записи CLI (П9) ---------------------------------------------------------------------------------------


def test_record_paths_and_forget_on_fake_home(tmp_path):
    home = tmp_path / "home"
    workdir = tmp_path / "rt" / "hub-brain" / "cursor-0123456789ab"
    workdir.mkdir(parents=True)
    # claude: каталог проекта по пути
    claude_dir = home / ".claude" / "projects" / oneshot.claude_slug(workdir)
    claude_dir.mkdir(parents=True)
    (claude_dir / "s-1.jsonl").write_text("{}")
    other_claude = home / ".claude" / "projects" / "-home-owner-projects-hub"
    other_claude.mkdir(parents=True)
    # codex: файл нити
    day = home / ".codex" / "sessions" / "2026" / "10" / "22"
    day.mkdir(parents=True)
    rollout = day / "rollout-2026-10-22T10-00-00-019a-thread-0001.jsonl"
    rollout.write_text("{}")
    other_rollout = day / "rollout-2026-10-22T10-00-00-019a-thread-0002.jsonl"
    other_rollout.write_text("{}")
    # cursor: чаты по md5 и проект по имени каталога
    chats = home / ".cursor" / "chats" / oneshot.cursor_chat_dir(workdir) / "chat-1"
    chats.mkdir(parents=True)
    project = home / ".cursor" / "projects" / ("run-user-1000-hub-brain-" + workdir.name)
    project.mkdir(parents=True)
    other_project = home / ".cursor" / "projects" / "home-owner-projects-hub"
    other_project.mkdir(parents=True)

    assert (
        oneshot.claude_slug("/run/user/1000/hub-brain/claude-ab.c") == "-run-user-1000-hub-brain-claude-ab-c"
    )
    assert oneshot.record_paths("claude", session=None, workdir=workdir, home=home) == [claude_dir]
    assert oneshot.record_paths("codex", session="019a-thread-0001", workdir=workdir, home=home) == [rollout]
    assert oneshot.record_paths("codex", session="../../x", workdir=workdir, home=home) == []
    assert oneshot.record_paths("opencode", session="ses_1", workdir=workdir, home=home) == []
    assert set(oneshot.record_paths("cursor", session="chat-1", workdir=workdir, home=home)) == {
        chats.parent,
        project,
    }
    oneshot.forget("claude", session=None, workdir=workdir, home=home)
    oneshot.forget("codex", session="019a-thread-0001", workdir=workdir, home=home)
    oneshot.forget("cursor", session="chat-1", workdir=workdir, home=home)
    assert (
        not claude_dir.exists()
        and not rollout.exists()
        and not chats.parent.exists()
        and not project.exists()
    )
    assert other_claude.exists() and other_rollout.exists() and other_project.exists()


async def test_workspace_close_forgets_every_session(tmp_path, root):
    home = tmp_path / "home"
    fake = Fake(tmp_path / "bin", "codex")
    fake.plan(
        {"stdout": _codex_events(thread="019a-thread-000a")},
        {"stdout": _codex_events(thread="019a-thread-000b")},
    )
    day = home / ".codex" / "sessions" / "2026" / "10" / "22"
    day.mkdir(parents=True)
    files = [day / f"rollout-t-019a-thread-000{x}.jsonl" for x in "abc"]
    for f in files:
        f.write_text("{}")
    ws = oneshot.Workspace("codex", root=root, home=home)
    assert stat.S_IMODE(ws.path.stat().st_mode) == 0o700 and ws.path.name.startswith("codex-")
    await ws.run(prompt="1", system="", model="m", effort=None, timeout_s=10, binary=str(fake.path))
    await ws.run(prompt="2", system="", model="m", effort=None, timeout_s=10, binary=str(fake.path))
    assert ws.sessions == {"019a-thread-000a", "019a-thread-000b"}
    ws.close()
    ws.close()  # повторно — безвредно
    assert [f.exists() for f in files] == [False, False, True] and not ws.path.exists()


def test_runtime_root_under_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert oneshot.runtime_root() == tmp_path / "hub-brain"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "нет"))
    assert oneshot.runtime_root().name == f"hub-brain-{os.getuid()}"


# --- путь к claude (П8) ------------------------------------------------------------------------------------


def test_claude_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("PATH", str(tmp_path / "пусто"))
    with pytest.raises(choice.EngineUnavailable):
        choice.claude_path(None, home)
    default = Fake(home / ".local" / "bin", "claude").path
    assert choice.claude_path(None, home) == str(default)
    other = Fake(home / "opt", "claude").path
    assert choice.claude_path("~/opt/claude", home) == str(other)
    assert choice.claude_path(str(other), home) == str(other)
    with pytest.raises(choice.EngineUnavailable):
        choice.claude_path("~/нет/claude", home)
    with pytest.raises(choice.EngineUnavailable):
        choice.claude_path("claude", home)  # относительный путь — не берём
    default.unlink()
    on_path = Fake(tmp_path / "pathbin", "claude").path
    monkeypatch.setenv("PATH", str(on_path.parent))
    assert choice.claude_path(None, home) == str(on_path)


def test_forget_refuses_foreign_workdir(tmp_path):
    """Чужой путь (папка проекта) — записи настоящих сессий не трогаются."""
    home = tmp_path / "home"
    project = tmp_path / "projects" / "hub"
    project.mkdir(parents=True)
    claude_dir = home / ".claude" / "projects" / oneshot.claude_slug(project)
    claude_dir.mkdir(parents=True)
    assert oneshot.forget("claude", session=None, workdir=project, home=home) == []
    assert claude_dir.exists()
