"""Ф50-3 (docs/architecture.md, поправки П1, П2 и «Поправка по ревью:
набор по сокету»): набор снятых выключателей автопилота живёт в памяти хоста; хук гарда на каждый вызов
спрашивает его по unix-сокету в абстрактном пространстве имён и сверяет, что отвечает ровно процесс хоста.
Файла с набором нет. Хост меняет набор у живого процесса на месте."""

import io
import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import time

import pytest

from hub.work import free_sock, guard
from hub.work import held as held_mod

from .conftest import flag_value, make_repo
from .oc_helpers import oc  # noqa: F401 — фикстура `oc`
from .oc_helpers import started as oc_started
from .test_held_autopilot import _free, _free_args, _free_keys, _hold, _hook
from .test_held_host import _events, _running, _say, _session, _state, _turn_done
from .test_held_opencode import _oc_session
from .test_held_opencode import _running as _oc_running

EVERY = sorted(set(guard.RULES) - {guard.FAILURE})  # всё, что можно снять
ME = os.getpid()
NOTE = guard.FREE_NOTE


# --- гард и настоящий сокет ---------------------------------------------------------------------------------


@pytest.fixture
def box(tmp_path):
    home = tmp_path / "home"
    workdir = home / "proj"
    make_repo(workdir)
    return {"home": str(home), "wd": str(workdir)}


def _run(box, command, *free):
    event = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": box["wd"]}
    argv = ["--workdir", box["wd"], "--profile", "held", *free]
    return guard.run(argv, io.StringIO(json.dumps(event)), home=box["home"])


def _asked(result) -> str | None:
    code, out, _err = result
    if code != 0 or not out:
        return None
    return json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]


def _args(name, pid=ME, sid=7) -> list[str]:
    return ["--free-sock", name, "--free-pid", str(pid), "--free-session", str(sid)]


def test_guard_asks_the_host_on_every_call(box, free_sets):
    free_sets.sets[7] = ["push", "kill_agents"]
    args = free_sets.args(7)
    assert args == _args(free_sets.name) and free_sets.name.startswith(f"hub-work-free-{ME}-")
    assert guard.read_free_sock(free_sets.name, ME, 7) == ({"push", "kill_agents"}, None)
    assert _run(box, "git push", *args) == (0, "", "")
    assert _run(box, "pkill -f hub-work", *args) == (0, "", "")
    assert _asked(_run(box, "ssh prod ls", *args)).endswith("[hub-rule:server]")
    # Хост сменил набор в памяти — следующий вызов того же хука (та же командная строка) уже строгий.
    free_sets.sets[7] = ["server"]
    assert _asked(_run(box, "git push", *args)).endswith("[hub-rule:push]")
    assert _run(box, "pkill -f hub-work", *args)[0] == guard.EXIT_DENY
    assert _run(box, "ssh prod ls", *args) == (0, "", "")
    # Набор — по сессии: чужой и неизвестный id — пусто.
    free_sets.sets[8] = ["push"]
    assert guard.read_free_sock(free_sets.name, ME, 8) == ({"push"}, None)
    assert guard.read_free_sock(free_sets.name, ME, 9) == (frozenset(), None)
    assert _asked(_run(box, "git push", *free_sets.args(9))).endswith("[hub-rule:push]")


def test_wrong_pid_is_the_empty_set_with_a_note(box, free_sets):
    """На сокете не тот процесс, что назван хосту в командной строке хука, — набор пустой."""
    free_sets.sets[7] = EVERY
    assert guard.read_free_sock(free_sets.name, ME + 1, 7) == (frozenset(), "на сокете не хост хаба")
    reason = _asked(_run(box, "git push", *_args(free_sets.name, pid=ME + 1)))
    assert reason == f"гард хаба: push — решает владелец ({NOTE}: на сокете не хост хаба)\n[hub-rule:push]"
    code, _out, err = _run(box, "pkill -f hub-work", *_args(free_sets.name, pid=ME + 1))
    assert code == guard.EXIT_DENY and err.endswith(f"({NOTE}: на сокете не хост хаба)")
    # Вызов без вердикта пометки не несёт.
    assert _run(box, "ls", *_args(free_sets.name, pid=ME + 1)) == (0, "", "")


IMPOSTOR = """
import json, socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind("\\0" + sys.argv[1]); s.listen(8)
print("ready", flush=True)
while True:
    c, _ = s.accept()
    c.recv(64); c.sendall(json.dumps(sys.argv[2:]).encode() + b"\\n"); c.close()
"""


def test_another_process_on_the_name_is_not_the_host(box):
    """Имя занял чужой процесс того же пользователя (хост умер, имя освободилось): он отвечает полным
    набором, но `SO_PEERCRED` называет не pid хоста — набор пустой."""
    name = f"hub-work-free-{ME}-impostor{os.urandom(4).hex()}"
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", IMPOSTOR, name, *EVERY], stdout=subprocess.PIPE, text=True
    )
    try:
        assert proc.stdout.readline().strip() == "ready"
        assert guard.read_free_sock(name, proc.pid, 7) == (frozenset(EVERY), None)  # сам обмен работает
        assert guard.read_free_sock(name, ME, 7) == (frozenset(), "на сокете не хост хаба")
        assert _asked(_run(box, "git push", *_args(name))).endswith("[hub-rule:push]")
    finally:
        proc.kill()
        proc.wait()


def test_no_server_is_the_empty_set(box, free_sets):
    free_sets.sets[7] = EVERY
    name = free_sets.name
    free_sets.close()  # хост остановлен: имя свободно
    assert guard.read_free_sock(name, ME, 7) == (frozenset(), "нет соединения с хостом")
    reason = _asked(_run(box, "git push", *_args(name)))
    assert reason == f"гард хаба: push — решает владелец ({NOTE}: нет соединения с хостом)\n[hub-rule:push]"
    assert _run(box, "pkill -f hub-work", *_args(name))[0] == guard.EXIT_DENY


def _raw_server(reply):
    """Сервер-заглушка в этом же процессе: `reply(conn)` решает, что ответить (или молчит)."""
    name = f"hub-test-free-{ME}-{os.urandom(6).hex()}"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind("\0" + name)
    sock.listen(8)
    held: list = []

    def serve():
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            held.append(conn)
            reply(conn)

    threading.Thread(target=serve, daemon=True).start()
    return name, sock, held


def test_silent_server_is_the_empty_set_within_the_timeout(box, monkeypatch):
    name, sock, held = _raw_server(lambda conn: None)  # принял и молчит
    try:
        started = time.monotonic()
        assert guard.read_free_sock(name, ME, 7, timeout=0.3) == (frozenset(), "хост не ответил вовремя")
        assert time.monotonic() - started < 1.5
        assert guard.FREE_TIMEOUT_S == 2.0
        monkeypatch.setattr(guard, "FREE_TIMEOUT_S", 0.3)
        monkeypatch.setattr(guard.read_free_sock, "__kwdefaults__", {"timeout": 0.3})
        started = time.monotonic()
        reason = _asked(_run(box, "git push", *_args(name)))
        assert time.monotonic() - started < 1.5
        assert reason == f"гард хаба: push — решает владелец ({NOTE}: хост не ответил вовремя)\n[hub-rule:push]"
    finally:
        sock.close()
        for conn in held:
            conn.close()


def test_slow_drip_does_not_outlive_the_timeout():
    """Таймаут — на весь обмен, а не на каждое чтение."""

    def drip(conn):
        def go():
            try:
                for _ in range(40):
                    conn.sendall(b" ")
                    time.sleep(0.1)
            except OSError:
                pass

        threading.Thread(target=go, daemon=True).start()

    name, sock, held = _raw_server(drip)
    try:
        started = time.monotonic()
        assert guard.read_free_sock(name, ME, 7, timeout=0.4)[0] == frozenset()
        assert time.monotonic() - started < 1.5
    finally:
        sock.close()
        for conn in held:
            conn.close()


@pytest.mark.parametrize(
    "reply",
    [
        b"",  # закрыл без ответа
        b"\n",
        b"not json\n",
        b'{"push": true}\n',
        b'"push"\n',
        b'["push", "no such key"]\n',
        b'["push", 1]\n',
        b'["push"',  # оборван, без конца строки
        b'["push"]',  # без конца строки — ответ не дописан
        b"[" + b",".join([b'"push"'] * 5000) + b"]\n",  # больше предела
    ],
)
def test_broken_reply_is_the_empty_set(box, reply):
    def answer(conn):
        conn.recv(64)
        conn.sendall(reply)
        conn.close()

    name, sock, _held = _raw_server(answer)
    try:
        assert guard.read_free_sock(name, ME, 7) == (frozenset(), "ответ хоста неразборчив")
        assert _asked(_run(box, "git push", *_args(name))).endswith("[hub-rule:push]")
        assert _run(box, "pkill -f hub-work", *_args(name))[0] == guard.EXIT_DENY
    finally:
        sock.close()


def test_guard_failure_key_is_never_free():
    def answer(conn):
        conn.recv(64)
        conn.sendall(json.dumps(["push", guard.FAILURE]).encode() + b"\n")
        conn.close()

    name, sock, _held = _raw_server(answer)
    try:
        assert guard.read_free_sock(name, ME, 7) == ({"push"}, None)
    finally:
        sock.close()


def test_guard_knows_no_file_with_the_set(box, tmp_path):
    """Пути к набору у гарда нет: ни аргумента, ни чтения файла."""
    assert not hasattr(guard, "read_free_file") and not hasattr(guard, "FREE_FILE_MAX")
    path = tmp_path / "free.json"
    path.write_text(json.dumps(EVERY))
    reason = _asked(_run(box, "ls", "--free-file", str(path)))
    assert "неверные аргументы хука" in reason
    with pytest.raises(TypeError):
        guard.decide({}, workdir=box["wd"], home=box["home"], locked=("/x",))


# --- сервер -------------------------------------------------------------------------------------------------


def _ask_raw(name: str, payload: bytes) -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(3)
        sock.connect("\0" + name)
        sock.sendall(payload)
        sock.shutdown(socket.SHUT_WR)
        data = b""
        while chunk := sock.recv(4096):
            data += chunk
        return data


def test_server_answers_garbage_with_the_empty_list_and_survives():
    def lookup(sid):
        if sid == 13:
            raise RuntimeError("сбой")
        return {"push", "server", 5} if sid == 7 else []

    server = free_sock.FreeServer(lookup)
    assert server.args(7) == [] and server.name is None  # не запущен — аргументов нет
    server.start()
    first = server.name
    server.start()  # повторный запуск имя не меняет
    try:
        assert server.name == first
        assert _ask_raw(first, b"7\n") == b'["push", "server"]\n'
        for junk in (b"", b"7", b"abc\n", b"-7\n", b"7 8\n", b"9" * 30 + b"\n", b"13\n", b"\n"):
            assert _ask_raw(first, junk) == b"[]\n"
        assert _ask_raw(first, b"7\n") == b'["push", "server"]\n'
        # Имя занято: второй сокет на него не встанет, пока сервер жив.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as other, pytest.raises(OSError):
            other.bind("\0" + first)
    finally:
        server.close()
    assert server.name is None and server.args(7) == []
    assert guard.read_free_sock(first, ME, 7) == (frozenset(), "нет соединения с хостом")


# --- хост: набор у живого процесса --------------------------------------------------------------------------


def _hook_run(call: dict, command: str, home) -> tuple[int, str, str]:
    """Хук ровно с той командной строкой, что хост записал в настройки процесса."""
    argv = shlex.split(_hook(call))
    argv = argv[argv.index("--workdir") :]
    workdir = flag_value(argv, "--workdir")
    event = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": workdir}
    return guard.run(argv, io.StringIO(json.dumps(event)), home=str(home))


async def _notes(env, sid) -> list[str]:
    return [
        e["summary"] for e in await _events(env, sid) if (e.get("brief") or {}).get("type") == "autopilot"
    ]


async def test_tightening_mid_turn_keeps_the_turn_and_next_guard_call_is_strict(env):
    """Жалоба C: владелец поставил галочку посреди хода — ход идёт, процесс тот же, следующий вызов гарда уже под
    правилом: набор у хоста в памяти, хук спрашивает его по сокету."""
    env.scenario(turns=[[{"sleep": 1.5}, {"text": "дошёл"}]])
    await _free(env, "push", "server", "question")
    host = await env.started()
    sid, _, _ = await _session(env, mode="auto")
    await _running(env, host, sid)
    await env.until(host, _state(env, sid, "running"), what="ход идёт")
    [call] = env.calls()
    runner = host.held.runners[sid]
    assert _free_keys(call) == ["push", "server"]
    assert _hook_run(call, "git push", env.home) == (0, "", "")
    await _hold(env, "push", True)
    await env.until(host, lambda: runner.auto_free == {"server", "question"}, what="набор применён на месте")
    assert _free_keys(call) == ["server"]
    code, out, _err = _hook_run(call, "git push", env.home)
    assert code == 0 and "[hub-rule:push]" in out
    assert _hook_run(call, "ssh prod ls", env.home) == (0, "", "")
    # Ход не прерван, процесс тот же.
    assert host.held.runners.get(sid) is runner and runner.busy and runner.reason is None
    await _turn_done(env, host, sid)
    events = await _events(env, sid)
    assert "дошёл" in [e["summary"] for e in events]
    assert await _notes(env, sid) == [held_mod.AUTOPILOT_LIVE_NOTE]
    assert len(env.calls()) == 1 and host.held.runners.get(sid) is runner
    # Ключ, который решает сам хост (`question`), — тоже на месте хуку он не уходит.
    await _hold(env, "question", True)
    await env.until(host, lambda: runner.auto_free == {"server"}, what="вопросы снова карточкой")
    assert _free_keys(call) == ["server"] and host.held.runners.get(sid) is runner
    # Ослабление правила гарда — тоже на месте, без перезапуска.
    await _hold(env, "deploy", False)
    await env.until(host, lambda: runner.auto_free == {"server", "deploy"}, what="ослабление на месте")
    assert _free_keys(call) == ["deploy", "server"] and len(env.calls()) == 1
    assert await _notes(env, sid) == [held_mod.AUTOPILOT_LIVE_NOTE] * 3
    for _ in range(3):
        await env.cycle(host)
    assert (
        host.held.runners.get(sid) is runner and await _notes(env, sid) == [held_mod.AUTOPILOT_LIVE_NOTE] * 3
    )


async def test_mixed_change_applies_guard_part_now_and_settings_part_after_restart(env):
    """Разом: правило гарда поставлено, выключатель настроек процесса снят — первое действует сразу, второе
    ждёт перезапуска при простое; до него ключ у хоста стоит."""
    env.scenario(turns=[[{"sleep": 1.0}, {"text": "дошёл"}]])
    await _free(env, "push")
    host = await env.started()
    sid, _, _ = await _session(env, mode="auto")
    await _running(env, host, sid)
    await env.until(host, _state(env, sid, "running"), what="ход идёт")
    runner = host.held.runners[sid]
    for key, hold in (("plan_mode", False), ("push", True)):
        await _hold(env, key, hold)
    await env.until(host, lambda: runner.auto_free == frozenset(), what="правило гарда поставлено на месте")
    assert _free_keys(env.calls()[0]) == [] and runner.busy
    await env.until(host, lambda: sid not in host.held.runners, what="перезапуск при простое")
    assert "дошёл" in [e["summary"] for e in await _events(env, sid)]
    env.scenario(turns=[[{"text": "ок"}]])
    await _say(env, sid, "ещё")
    await env.until(host, lambda: len(env.calls()) == 2, what="запуск с новым набором")
    await _turn_done(env, host, sid)
    assert host.held.runners[sid].auto_free == {"plan_mode"}


async def test_host_serves_the_set_per_session_and_keeps_no_file(env):
    env.scenario(turns=[[{"text": "раз"}], [{"text": "два"}]])
    await _free(env, "push", "question")
    host = await env.started()
    assert host.held.free.name is None  # сокет поднимается с первым запуском сессии
    auto, pid, workdir = await _session(env, mode="auto")
    manual, _, _ = await _session(env, project=(pid, workdir))
    for sid in (auto, manual):
        await _running(env, host, sid)
        await _turn_done(env, host, sid)
    first, second = env.calls()
    name = host.held.free.name
    assert _free_args(first) == (name, ME, auto) and _free_args(second) == (name, ME, manual)  # один сокет
    assert _free_keys(first) == ["push"] and _free_keys(second) == []  # `question` решает хост, не гард
    assert host.held.free_keys(auto) == ["push"] and host.held.free_keys(manual) == []
    assert host.held.free_keys(auto + 100) == [] and guard.read_free_sock(name, ME, auto + 100)[0] == set()
    # В каталоге сессии — только настройки процесса; набора на диске нет нигде в данных хоста.
    held_dir = os.path.dirname(flag_value(first["argv"], "--settings"))
    assert sorted(os.listdir(held_dir)) == ["settings.json"]
    assert not [p for p in env.data_dir.rglob("*") if p.name.startswith("free")]
    # Собственная проверка хоста — из той же памяти: снятое правило молчит, стоящее — карточка.
    runner = host.held.runners[auto]
    assert runner._self_check("Bash", {"command": "git push"}) is None  # noqa: SLF001
    assert runner._self_check("Bash", {"command": "ssh prod ls"})[0] == "server"  # noqa: SLF001
    # Остановка хоста: процессы погашены, имя освобождено — прежняя командная строка хука даёт пустой набор.
    await host.shutdown()
    assert host.held.free.name is None
    assert guard.read_free_sock(name, ME, auto) == (frozenset(), "нет соединения с хостом")
    code, out, _err = _hook_run(first, "git push", env.home)
    assert code == 0 and guard.FREE_NOTE in out and "[hub-rule:push]" in out


async def test_restarted_host_launches_sessions_with_its_own_socket(env):
    """Рестарт хоста: процесс сессии поднимается заново, в его настройках — имя сокета нового хоста."""
    env.scenario(turns=[[{"text": "раз"}], [{"text": "два"}]])
    await _free(env, "push")
    host = await env.started()
    sid, _, _ = await _session(env, mode="auto")
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    old = _free_args(env.calls()[0])
    await host.shutdown()
    fresh = await env.started()
    await _say(env, sid, "ещё")
    await env.until(fresh, lambda: len(env.calls()) == 2, what="подъём после рестарта")
    await _turn_done(env, fresh, sid)
    new = _free_args(env.calls()[1])
    assert new == (fresh.held.free.name, ME, sid) and new[0] != old[0]
    assert _free_keys(env.calls()[1]) == ["push"]
    assert guard.read_free_sock(*old) == (frozenset(), "нет соединения с хостом")


async def test_socket_failure_means_the_empty_set_not_a_failed_launch(env, monkeypatch):
    def broken(self):
        raise OSError("нет сокета")

    monkeypatch.setattr(free_sock.FreeServer, "start", broken)
    env.scenario(turns=[[{"text": "раз"}]])
    await _free(env, "push")
    host = await env.started()
    sid, _, _ = await _session(env, mode="auto")
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    [call] = env.calls()
    assert "--free-sock" not in shlex.split(_hook(call))
    code, out, _err = _hook_run(call, "git push", env.home)
    assert code == 0 and "[hub-rule:push]" in out  # без сокета — всё под карточкой


async def test_oc_session_has_no_set_on_the_socket(env, oc):  # noqa: F811
    """У OpenCode хука нет (каждый вызов решает хост) — сокет про такую сессию отвечает пусто."""
    oc.scenario([{"text": "Готово"}])
    await _free(env, "push")
    host = await oc_started(env)
    sid, _, _ = await _oc_session(env, mode="auto")
    await _oc_running(env, host, oc, sid, "посмотри")
    await _turn_done(env, host, sid)
    assert "push" in host.held.runners[sid].auto_free and host.held.free_keys(sid) == []
