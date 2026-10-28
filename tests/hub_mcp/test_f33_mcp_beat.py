"""Ф33-1 (docs/architecture.md п.2, п.4 и 12а К1): пульс MCP-сервера сессии Claude Code
ядру на другой машине — `POST /subs/beat`, кэш стенограммы на месте, `gone` только при мёртвом процессе
сессии."""

import json
import os

import httpx

from hub import hostid
from hub import session_registry as reg
from hub.core import transcript

from .conftest import write_profile

LAPTOP = "1a" * 8
# Ф77-1 (B3): машина клиента в пульсе — подменена (имя хоста и версия этой машины тестам не нужны).
MACHINE = {"hostname": "laptop", "route": "s1", "version": "abcdef12"}
ME = reg.SessionInfo(100, 5000, "sess-1", "/work/iip", None, None, "interactive", host=LAPTOP)


class Core:
    def __init__(self, answer=None, status=200):
        self.answer = answer if answer is not None else {"remote": True, "subscribed": True, "beat": True}
        self.status = status
        self.seen: list[tuple[str, str, dict | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.seen.append((request.method, request.url.path.removeprefix("/api/v1"), body))
        return httpx.Response(self.status, json=self.answer)


def _hub(make_server, config_dir, core, *, session=ME, environ=None):
    write_profile(config_dir, "all", "hubt_x")
    _server, hub = make_server("all", transport=httpx.MockTransport(core.handler))
    hub.environ = environ or {}
    hub.getppid = lambda: 4242
    hub.local_machine = lambda: MACHINE

    def find(start):
        if isinstance(session, BaseException):
            raise session
        return session

    hub.find_session = find
    return hub


def test_session_body_carries_host():
    assert ME.session() == {"pid": 100, "proc_start": 5000, "session_id": "sess-1", "host": LAPTOP}
    assert ME.session_in()["host"] == LAPTOP
    assert "host" not in reg.SessionInfo(100, 5000, "s", "/w", None, None, "interactive").session()


def test_find_session_names_machine(tmp_path, monkeypatch):
    """`find_session` ставит машину из `hostid` (переопределение `HUB_HOST_ID`)."""
    proc = tmp_path / "proc" / "100"
    proc.mkdir(parents=True)
    tail = " ".join(["0"] * 17)  # поля 5..21
    (proc / "stat").write_text(f"100 (claude) S 1 {tail} 5000 0\n")
    sessions = tmp_path / "claude" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "100.json").write_text(
        json.dumps({"pid": 100, "procStart": 5000, "sessionId": "s-1", "cwd": "/w", "kind": "interactive"})
    )
    info = reg.find_session(
        100, proc_root=tmp_path / "proc", claude_config=tmp_path / "claude", environ={hostid.ENV: LAPTOP}
    )
    assert info.session()["host"] == LAPTOP


def test_process_alive_real_and_dead():
    start = reg.proc_stat(os.getpid())[1]
    assert reg.process_alive(os.getpid(), start)
    assert not reg.process_alive(os.getpid(), start + 1)
    assert not reg.process_alive(2**30, 1)


async def test_beat_once_sends_session_and_cache(config_dir, make_server, monkeypatch):
    monkeypatch.setattr(
        transcript,
        "state_for",
        lambda sid: {"expires_ts": 9_000, "context_tokens": 77, "ttl_s": 300, "model": "m", "last_ts": 1},
    )
    core = Core()
    hub = _hub(make_server, config_dir, core)
    assert await hub.beat_once() is True
    assert hub.beat_remote is True
    assert await hub.beat_once() is True
    # Первый пульс — без стенограммы (ядро ещё не сказало «другая машина»), со второго — кэш и контекст.
    assert core.seen == [
        ("POST", "/subs/beat", {"session": ME.session(), "machine": MACHINE}),
        (
            "POST",
            "/subs/beat",
            {
                "session": ME.session(),
                "machine": MACHINE,
                "cache_until_ms": 9_000,
                "context_tokens": 77,
                "ttl_s": 300,
                "model": "m",
            },
        ),
    ]


async def test_no_transcript_read_for_own_core(config_dir, make_server, monkeypatch):
    """Своё ядро (`remote: false`) — стенограмма не читается ни на одном пульсе."""
    calls = []
    monkeypatch.setattr(transcript, "state_for", lambda sid: calls.append(sid))
    hub = _hub(make_server, config_dir, Core({"remote": False, "subscribed": False, "beat": False}))
    for _ in range(3):
        await hub.beat_once()
    assert calls == []


async def test_neighbours_dead_get_gone_alive_kept(config_dir, make_server, monkeypatch):
    """Ревью П1: ответ пульса называет соседей той же машины и токена; за мёртвых по своему `/proc` —
    `gone` тем же телом (машина своя), живых не трогать, мусор в списке пропускается."""
    monkeypatch.setattr(transcript, "state_for", lambda sid: None)
    alive = {(200, 7): True, (300, 8): False}
    monkeypatch.setattr(reg, "process_alive", lambda pid, start: alive.get((pid, start), False))
    neighbours = [
        {"pid": 200, "proc_start": 7, "session_id": "s-200"},
        {"pid": 300, "proc_start": 8, "session_id": "s-300"},
        {"pid": "x"},
        "мусор",
    ]
    core = Core({"remote": True, "subscribed": True, "beat": True, "neighbours": neighbours})
    hub = _hub(make_server, config_dir, core)
    assert await hub.beat_once() is True
    gones = [body for _m, _p, body in core.seen if body.get("gone")]
    assert gones == [
        {"session": {"pid": 300, "proc_start": 8, "session_id": "s-300", "host": LAPTOP}, "gone": True}
    ]
    own = Core({"remote": False, "subscribed": False, "beat": False, "neighbours": neighbours})
    hub = _hub(make_server, config_dir, own)
    await hub.beat_once()
    assert [b for _m, _p, b in own.seen if b.get("gone")] == []  # своё ядро — соседей не судим


async def test_beat_stops_when_core_says_so(config_dir, make_server, monkeypatch):
    monkeypatch.setattr(transcript, "state_for", lambda sid: None)
    core = Core({"remote": False, "subscribed": False, "beat": False})
    hub = _hub(make_server, config_dir, core)
    assert await hub.beat_once() is False
    assert core.seen[0][2] == {"session": ME.session(), "machine": MACHINE}
    old = Core(status=404, answer={"detail": "Not Found"})  # старое ядро без ручки — не падать
    assert await _hub(make_server, config_dir, old).beat_once() is False


async def test_no_beat_for_opencode_hub_and_unnamed(config_dir, make_server):
    core = Core()
    hub = _hub(make_server, config_dir, core, environ={"OPENCODE_PID": "4096"})
    assert await hub.beat_once() is False
    hub = _hub(make_server, config_dir, core, environ={"HUB_SESSION": "7"})
    assert await hub.beat_once() is False
    hub = _hub(make_server, config_dir, core, session=reg.SessionInfo(1, 2, "s", "/w", None, None, "i"))
    assert await hub.beat_once() is False  # машину не назвать — пульс не нужен ядру
    assert core.seen == []


async def test_gone_only_when_session_process_dead(config_dir, make_server, monkeypatch):
    """К1: stdin закрыт, процесс сессии жив (`/mcp`, выход идёт) — `gone` сам не шлётся, а запускается сторож
    отдельной группой процессов; мёртв — `gone` шлётся сразу."""
    monkeypatch.setattr(transcript, "state_for", lambda sid: None)
    core = Core()
    hub = _hub(make_server, config_dir, core)
    await hub.beat_once()
    alive = {"v": True}
    monkeypatch.setattr(reg, "process_alive", lambda pid, start: alive["v"])
    spawned = []
    hub.spawn = lambda argv, **kw: spawned.append((argv, kw))
    assert await hub.beat_gone() == "watch"
    assert [body for _m, _p, body in core.seen if body.get("gone")] == []
    argv, kw = spawned[0]
    assert argv[1:3] == ["-m", "hub.mcp_server.gone_watch"]
    args = dict(zip(argv[3::2], argv[4::2], strict=True))
    assert args["--profile"] == "all"
    assert args["--config-dir"] == str(config_dir)
    assert (args["--pid"], args["--proc-start"], args["--session-id"], args["--host"]) == (
        "100",
        "5000",
        "sess-1",
        LAPTOP,
    )
    assert "hubt_x" not in argv  # токен — из профиля, не в аргументах
    assert kw["start_new_session"] is True
    alive["v"] = False
    assert await hub.beat_gone() == "sent"
    assert core.seen[-1] == ("POST", "/subs/beat", {"session": ME.session(), "gone": True})
    assert len(spawned) == 1


async def test_gone_not_sent_to_local_core(config_dir, make_server, monkeypatch):
    monkeypatch.setattr(transcript, "state_for", lambda sid: None)
    monkeypatch.setattr(reg, "process_alive", lambda pid, start: False)
    core = Core({"remote": False, "subscribed": False, "beat": False})
    hub = _hub(make_server, config_dir, core)
    await hub.beat_once()
    spawned = []
    hub.spawn = lambda argv, **kw: spawned.append(argv)
    assert await hub.beat_gone() == "none"
    assert hub.spawn_gone_watch() is False
    assert len(core.seen) == 1
    assert spawned == []


def test_gone_watch_sends_only_after_death(monkeypatch, tmp_path):
    """Сторож: сессия жива всё окно — молча выходит; умерла — `gone`."""
    from hub.mcp_server import gone_watch

    state = {"alive": True}
    monkeypatch.setattr(reg, "process_alive", lambda pid, start: state["alive"])
    ticks = iter(range(1000))
    assert gone_watch.wait_dead(100, 5000, 3, sleep=lambda s: None, monotonic=lambda: next(ticks)) is False
    sent = []

    async def fake_send(args):
        sent.append(args)
        return True

    monkeypatch.setattr(gone_watch, "send_gone", fake_send)
    argv = ["--profile", "all", "--config-dir", str(tmp_path), "--url", "http://x/api/v1", "--pid", "100"]
    argv += ["--proc-start", "5000", "--session-id", "sess-1", "--host", LAPTOP, "--wait-s", "0"]
    assert gone_watch.main(argv) == 0
    assert sent == []
    state["alive"] = False
    assert gone_watch.main(argv) == 0
    assert (sent[0].pid, sent[0].proc_start, sent[0].session_id, sent[0].host) == (
        100,
        5000,
        "sess-1",
        LAPTOP,
    )


async def test_gone_watch_body(monkeypatch, config_dir):
    from hub.mcp_server import client as client_mod
    from hub.mcp_server import gone_watch

    write_profile(config_dir, "all", "hubt_x")
    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content), request.headers.get("authorization")))
        return httpx.Response(200, json={"remote": True, "subscribed": False, "beat": False, "ended": 1})

    real = client_mod.HubClient
    monkeypatch.setattr(
        gone_watch, "HubClient", lambda **kw: real(**kw, transport=httpx.MockTransport(handler))
    )
    args = gone_watch.parse(
        ["--profile", "all", "--config-dir", str(config_dir), "--url", "http://x/api/v1", "--pid", "100"]
        + ["--proc-start", "5000", "--session-id", "sess-1", "--host", LAPTOP]
    )
    await gone_watch.send_gone(args)
    assert seen == [("/api/v1/subs/beat", {"session": ME.session(), "gone": True}, "Bearer hubt_x")]


async def test_gone_retries_three_times(config_dir, make_server, monkeypatch):
    """Ревью П1 п.7: `gone` — до 3 попыток с паузой; дошло на третьей — `sent`, пауз две."""
    from hub.mcp_server import gone_watch

    monkeypatch.setattr(transcript, "state_for", lambda sid: None)
    calls = {"n": 0}

    class Flaky(Core):
        def handler(self, request):
            body = json.loads(request.content)
            if body.get("gone"):
                calls["n"] += 1
                if calls["n"] < 3:
                    return httpx.Response(503, json={"detail": "нет"})
            return super().handler(request)

    core = Flaky()
    hub = _hub(make_server, config_dir, core)
    await hub.beat_once()
    pauses = []

    async def _sleep(seconds):
        pauses.append(seconds)

    hub.sleep = _sleep
    monkeypatch.setattr(reg, "process_alive", lambda pid, start: False)
    assert await hub.beat_gone() == "sent"
    assert calls["n"] == 3
    assert pauses == [gone_watch.PAUSE_S, gone_watch.PAUSE_S]
    calls["n"] = -10  # все попытки неудачны
    pauses.clear()
    assert await hub.beat_gone() == "none"
    assert len(pauses) == gone_watch.TRIES - 1


def test_sighup_and_sigterm_exit_with_signal_code(tmp_path):
    """Ревью П1 п.5: закрытое окно (SIGHUP) и SIGTERM — тот же обработчик, выход с кодом 128+сигнал и сразу
    (чтение stdin в потоке не держит)."""
    import signal
    import subprocess
    import sys
    import time

    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
    }
    env = {"HUB_CONFIG_DIR": str(tmp_path), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    for sig in (signal.SIGHUP, signal.SIGTERM):
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from hub.cli import main; main()",
                "mcp",
                "--profile",
                "t",
                "--url",
                "http://127.0.0.1:9/api/v1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        proc.stdin.write((json.dumps(msg) + "\n").encode())
        proc.stdin.flush()
        assert proc.stdout.readline()
        started = time.monotonic()
        proc.send_signal(sig)
        assert proc.wait(timeout=10) == 128 + sig
        assert time.monotonic() - started < 5
