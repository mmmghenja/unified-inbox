"""Ф74-7 / 74-3 (docs/architecture.md): хук хаба для внешних окон Codex и Cursor —
`hub-ext-hook <codex|cursor> <pre|post|stop|start|end>`. Аналог плагина OpenCode `deploy/opencode/hub.ts`:

- `pre` — вызов инструмента MCP хаба, которому нужна сессия (`SESSION_TOOLS`): в аргументы подставляются
  `ext_session` (разговор движка: `session_id` Codex, `conversation_id` Cursor) и `ext_dir` (`cwd` Codex,
  `workspace_roots[0]` Cursor);
- `post` — ответ `hub_subscribe`: метки `HUB-WAIT {"engine", "pid", "argv"}` своего движка — в файл состояния
  разговора `~/.local/state/hub/<движок>/<разговор>.json` (0600); у Codex сразу (пере)запускается ожидатель;
- `stop` — Cursor: есть метки — держать `hub sub wait` и вернуть сигнал `followup_message` (ввод владельца хук
  `stop` не держит — проба 21.10); Codex: только проверить, что ожидатель жив (блокирующий `Stop` Codex
  держит ввод владельца до конца хука — проба 21.10, поэтому ожидатель отвязан и будит TUI `codex queue`);
- `start` — убрать файлы состояния разговоров, чьего процесса движка больше нет (Codex `/clear` — и прежние
  разговоры того же процесса);
- `end` — погасить ожидание, `hub sub leave`, убрать файл состояния.

Коды `hub sub wait` — как у плагина OpenCode: 0 — доставить и ждать снова; 6 — ждать снова; 2/3 — доставить
один раз и забыть метку; 4 — вытеснено другим ожиданием (молча); 5 и прочее — забыть метку.

Сессии хаба (`HUB_SESSION` в окружении) хук не трогает: выход 0 без вывода. Любой сбой — тоже 0 без вывода и
строка в журнале `<движок>/hook.log` (без текстов сообщений): подстановка личности — не граница безопасности,
без неё MCP хаба просто откажет. Метке из ответа доверяется не целиком: бинарь — всегда `hub` рядом с этим
интерпретатором, аргументы — только `sub wait <номер>|--channels --profile <имя> [--url <loopback>]`.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path

from . import session_registry

ENGINES = ("codex", "cursor")
WAIT_MARK = "HUB-WAIT "
# Инструменты MCP хаба, которым нужна сессия (у них `oc_session`/`ext_session`; сверяет тест с сервером).
SESSION_TOOLS = frozenset(
    {
        "hub_agent_inbox",
        "hub_agent_send",
        "hub_agent_state",
        "hub_agents",
        "hub_ask_owner",
        "hub_digest",
        "hub_draft",
        "hub_draft_new",
        "hub_group_create",
        "hub_group_link",
        "hub_group_members",
        "hub_group_ops",
        "hub_handled",
        "hub_human",
        "hub_human_cancel",
        "hub_humans",
        "hub_inbound_rules",
        "hub_join",
        "hub_press",
        "hub_presses",
        "hub_mute",
        "hub_propose_rule",
        "hub_react",
        "hub_remind",
        "hub_remind_done",
        "hub_reminders",
        "hub_request_session",
        "hub_role",
        "hub_subscribe",
        "hub_subscriptions",
        "hub_triage_mark",
        "hub_unsubscribe",
    }
)
SUBSCRIBE_TOOL = "hub_subscribe"
CODEX_TOOL_RE = re.compile(r"^mcp__(hub[A-Za-z0-9_-]*)__(hub_[a-z_]+)$")
CURSOR_TOOL_RE = re.compile(r"^MCP:(hub_[a-z_]+)$")
PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
TARGET_RE = re.compile(r"^(--channels|[1-9][0-9]{0,11})$")
LOOPBACK_URL_RE = re.compile(r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:[0-9]{1,5})?(/[A-Za-z0-9/_.-]*)?$")

RESTART_MIN_S = 2.0
POLL_S = 0.5
WATCH_S = 5.0
LEAVE_TIMEOUT_S = 10.0
QUEUE_TIMEOUT_S = 60.0
OUT_MAX = 64 * 1024
LOG_MAX = 1024 * 1024
TERM_GRACE_S = 3.0

EXIT_SIGNALS, EXIT_DENIED, EXIT_ENDED, EXIT_SUPERSEDED, EXIT_UNKNOWN = 0, 2, 3, 4, 6
# Ожидатель Codex: снятие подписки (3) в нить не доставляется — см. `run_waiter`.
CODEX_DELIVER_CODES = frozenset({EXIT_SIGNALS, EXIT_DENIED})


# --- окружение и файлы --------------------------------------------------------------------------------------


def state_root(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    if env.get("HUB_EXT_STATE_DIR"):
        return Path(env["HUB_EXT_STATE_DIR"])
    home = env.get("HOME") or os.path.expanduser("~")
    base = env.get("XDG_STATE_HOME") or os.path.join(home, ".local", "state")
    return Path(base) / "hub"


def engine_dir(engine: str, environ: Mapping[str, str] | None = None) -> Path:
    path = state_root(environ) / engine
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)
    return path


def hub_bin() -> str:
    """`hub` того же окружения, что и этот хук (`.venv/bin/hub` боевого дерева)."""
    return str(Path(os.path.abspath(sys.executable)).parent / "hub")


def log(engine: str, **fields) -> None:
    try:
        path = engine_dir(engine) / "hook.log"
        with contextlib.suppress(OSError):
            if path.stat().st_size > LOG_MAX:
                os.replace(path, path.with_suffix(".log.1"))
        line = json.dumps({"ts": round(time.time(), 3), "pid": os.getpid(), **fields}, ensure_ascii=False)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001, S110 — журнал не должен ронять хук
        pass


def _state_path(engine: str, conv: str) -> Path:
    return engine_dir(engine) / f"{conv}.json"


@contextlib.contextmanager
def locked(engine: str, conv: str):
    fd = os.open(engine_dir(engine) / f"{conv}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def load_state(engine: str, conv: str) -> dict | None:
    try:
        data = json.loads(_state_path(engine, conv).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    return data if isinstance(data, dict) and isinstance(data.get("marks"), dict) else None


def save_state(engine: str, conv: str, state: dict) -> None:
    path = _state_path(engine, conv)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{conv}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def drop_state(engine: str, conv: str) -> None:
    directory = engine_dir(engine)
    for name in (f"{conv}.json", f"{conv}.lock"):
        with contextlib.suppress(OSError):
            os.unlink(directory / name)


# --- процессы -----------------------------------------------------------------------------------------------


def proc_start(pid: int) -> int | None:
    stat = session_registry.proc_stat(pid) if pid > 0 else None
    return stat[1] if stat else None


def alive(pid: int | None, start: int | None) -> bool:
    return bool(pid) and start is not None and proc_start(int(pid)) == start


def proc_argv(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]


def engine_exe(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return None


def headless(engine: str, pid: int) -> bool:
    """`cursor-agent -p` / `codex exec`: хода после ответа не будет — ждать сигнал некому."""
    argv = proc_argv(pid)
    if engine == "cursor":
        return any(a in ("-p", "--print") for a in argv[1:])
    return "exec" in argv[1:2]


def kill_group(pid: int | None, start: int | None) -> None:
    """Погасить ожидатель (лидер своей группы процессов) вместе с его `hub sub wait`."""
    if not alive(pid, start):
        return
    with contextlib.suppress(OSError):
        os.killpg(int(pid), signal.SIGTERM)


# --- вход хука ----------------------------------------------------------------------------------------------


def conversation(engine: str, inp: Mapping) -> str | None:
    conv = inp.get("session_id")
    if engine == "cursor":
        conv = inp.get("conversation_id") or conv
    if isinstance(conv, str) and session_registry.EXT_SESSION_RE.fullmatch(conv):
        return conv
    return None


def conversation_dir(engine: str, inp: Mapping) -> str | None:
    if engine == "codex":
        found = inp.get("cwd")
    else:
        roots = inp.get("workspace_roots")
        found = roots[0] if isinstance(roots, list) and roots else None
    if isinstance(found, str) and found.startswith("/") and "\x00" not in found and len(found) < 4096:
        return found
    return None


def tool_of(engine: str, name: object) -> str | None:
    """Имя инструмента хаба из `tool_name` движка (`mcp__hub-all__hub_draft`, `MCP:hub_draft`)."""
    if not isinstance(name, str):
        return None
    m = (CODEX_TOOL_RE if engine == "codex" else CURSOR_TOOL_RE).match(name)
    if m is None:
        return None
    tool = m.group(m.lastindex)
    return tool if tool in SESSION_TOOLS else None


def _as_obj(value: object) -> object:
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            return json.loads(value)
    return value


def _strings(value: object, depth: int = 0):
    """Все строки ответа инструмента (строку JSON — с разбором), не глубже 8 уровней."""
    if depth > 8:
        return
    if isinstance(value, str):
        yield value
        parsed = _as_obj(value)
        if not isinstance(parsed, str):
            yield from _strings(parsed, depth + 1)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item, depth + 1)


def wait_args(argv: object) -> list[str] | None:
    """Аргументы `hub sub wait` из метки (без бинаря) — только известная форма, иначе `None`."""
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv) or len(argv) not in (6, 8):
        return None
    if argv[1:3] != ["sub", "wait"] or not TARGET_RE.match(argv[3]) or argv[4] != "--profile":
        return None
    if not PROFILE_RE.match(argv[5]):
        return None
    if len(argv) == 8 and (argv[6] != "--url" or not LOOPBACK_URL_RE.match(argv[7])):
        return None
    return argv[1:]


def parse_marks(engine: str, response: object) -> tuple[dict[str, list[str]], int | None]:
    """Метки своего движка из ответа `hub_subscribe`: {цель: аргументы ожидания}, pid процесса движка."""
    marks: dict[str, list[str]] = {}
    pid: int | None = None
    for text in _strings(response):
        for line in text.splitlines():
            if not line.startswith(WAIT_MARK):
                continue
            try:
                data = json.loads(line[len(WAIT_MARK) :])
            except ValueError:
                continue
            if not isinstance(data, dict) or data.get("engine") != engine:
                continue
            args = wait_args(data.get("argv"))
            mark_pid = data.get("pid")
            if args is None or not isinstance(mark_pid, int) or isinstance(mark_pid, bool) or mark_pid <= 0:
                continue
            marks.pop(args[2], None)
            marks[args[2]] = args
            pid = mark_pid
    return marks, pid


def ident_args(state: Mapping) -> list[str]:
    return [
        "--ext-session",
        str(state["session"]),
        "--ext-dir",
        str(state["dir"]),
        "--ext-pid",
        str(int(state["engine_pid"])),
    ]


def leave_argv(state: Mapping) -> list[str] | None:
    """`hub sub leave` той же личности: профиль и адрес — из любой метки разговора."""
    marks = list((state.get("marks") or {}).values())
    if not marks:
        return None
    args = marks[-1]
    argv = [hub_bin(), "sub", "leave", *ident_args(state), "--profile", args[4]]
    if len(args) == 7:
        argv += ["--url", args[6]]
    return argv


# --- ожидание -----------------------------------------------------------------------------------------------


class Waits:
    """`hub sub wait` на каждую метку разговора, перезапуск по кодам выхода (как `exitAction` плагина
    OpenCode). `poll()` отдаёт тексты для доставки; `marks` меняет файл состояния под замком."""

    def __init__(
        self,
        engine: str,
        conv: str,
        *,
        popen=subprocess.Popen,
        now=time.monotonic,
        deliver_codes: frozenset[int] = frozenset({EXIT_SIGNALS, EXIT_DENIED, EXIT_ENDED}),
    ) -> None:
        self.engine = engine
        self.conv = conv
        self.deliver_codes = deliver_codes
        self.popen = popen
        self.now = now
        self.children: dict[str, tuple[subprocess.Popen, object, float]] = {}
        self.next_start: dict[str, float] = {}
        self.done: set[str] = set()

    def _forget(self, target: str) -> None:
        with locked(self.engine, self.conv):
            state = load_state(self.engine, self.conv)
            if state is None:
                return
            state["marks"].pop(target, None)
            if state["marks"]:
                save_state(self.engine, self.conv, state)
            else:
                _state_path(self.engine, self.conv).unlink(missing_ok=True)

    def start_missing(self, state: Mapping) -> None:
        for target, args in state["marks"].items():
            if target in self.children or target in self.done:
                continue
            if self.now() < self.next_start.get(target, 0.0):
                continue
            out = tempfile.TemporaryFile()
            try:
                child = self.popen(
                    [hub_bin(), *args, *ident_args(state)],
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                )
            except OSError as exc:
                out.close()
                log(self.engine, ev="spawn_failed", target=target, err=str(exc)[:200])
                self.done.add(target)
                continue
            self.children[target] = (child, out, self.now())
            log(self.engine, ev="wait_start", conv=self.conv[:12], target=target)

    def poll(self) -> list[str]:
        texts: list[str] = []
        for target, (child, out, started) in list(self.children.items()):
            code = child.poll()
            if code is None:
                continue
            del self.children[target]
            out.seek(0)
            text = out.read(OUT_MAX).decode("utf-8", "replace").strip()
            out.close()
            log(self.engine, ev="wait_exit", conv=self.conv[:12], target=target, code=code)
            if code in self.deliver_codes and text:
                texts.append(text)
            if code in (EXIT_SIGNALS, EXIT_UNKNOWN):
                self.next_start[target] = started + RESTART_MIN_S
            elif code == EXIT_SUPERSEDED or code < 0:
                # Вытеснено (ждёт новый ожидатель / новый хук `stop`) или погашено сигналом (ожидатель
                # перезапускают) — метку не трогать: её уже держит тот, кто пришёл на смену.
                self.done.add(target)
            else:
                self.done.add(target)
                self._forget(target)
        return texts

    def stop(self) -> None:
        for child, _out, _started in self.children.values():
            with contextlib.suppress(OSError):
                child.terminate()
        deadline = time.monotonic() + TERM_GRACE_S
        for child, out, _started in self.children.values():
            try:
                child.wait(max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    child.kill()
            out.close()
        self.children.clear()


# --- события ------------------------------------------------------------------------------------------------


def on_pre(engine: str, inp: Mapping) -> dict | None:
    if tool_of(engine, inp.get("tool_name")) is None:
        return None
    conv, folder = conversation(engine, inp), conversation_dir(engine, inp)
    args = _as_obj(inp.get("tool_input"))
    if conv is None or folder is None or not isinstance(args, dict):
        log(engine, ev="pre_skip", tool=str(inp.get("tool_name"))[:60])
        return None
    updated = {**args, "ext_session": conv, "ext_dir": folder}
    if engine == "codex":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": updated,
            }
        }
    return {"permission": "allow", "updated_input": updated}


def on_post(engine: str, inp: Mapping) -> None:
    if tool_of(engine, inp.get("tool_name")) != SUBSCRIBE_TOOL:
        return
    conv, folder = conversation(engine, inp), conversation_dir(engine, inp)
    if conv is None or folder is None:
        return
    response = inp.get("tool_response") if engine == "codex" else inp.get("tool_output")
    marks, pid = parse_marks(engine, response)
    if not marks or pid is None:
        return
    start = proc_start(pid)
    if start is None or session_registry.ext_engine(pid) != engine:
        log(engine, ev="mark_bad_pid", conv=conv[:12])
        return
    with locked(engine, conv):
        state = load_state(engine, conv)
        if state is None or state.get("engine_pid") != pid or state.get("engine_start") != start:
            state = {"v": 1, "engine": engine, "session": conv, "marks": {}}
        state.update(dir=folder, engine_pid=pid, engine_start=start, engine_exe=engine_exe(pid))
        for target, args in marks.items():
            state["marks"].pop(target, None)
            state["marks"][target] = args
        save_state(engine, conv, state)
    log(engine, ev="marks", conv=conv[:12], targets=sorted(marks))
    if engine == "codex":
        respawn_waiter(engine, conv, force=True)


def on_stop(engine: str, inp: Mapping) -> dict | None:
    conv = conversation(engine, inp)
    if conv is None:
        return None
    state = load_state(engine, conv)
    if state is None or not state["marks"]:
        return None
    if not alive(state.get("engine_pid"), state.get("engine_start")):
        drop_state(engine, conv)
        return None
    if engine == "codex":
        respawn_waiter(engine, conv, force=False)
        return None
    if headless(engine, int(state["engine_pid"])):
        return None
    text = wait_once(engine, conv)
    return {"followup_message": text} if text else None


def wait_once(engine: str, conv: str, *, waits: Waits | None = None) -> str | None:
    """Cursor `stop`: ждать, пока какое-нибудь ожидание не отдаст текст; разговор кончился или меток не
    осталось — `None`. Отдав текст, остальные ожидания гасит (их вытеснит следующий `stop`)."""
    w = waits or Waits(engine, conv)
    last_watch = 0.0
    try:
        while True:
            state = load_state(engine, conv)
            if state is None or not state["marks"]:
                return None
            if time.monotonic() - last_watch >= WATCH_S:
                last_watch = time.monotonic()
                if not alive(state.get("engine_pid"), state.get("engine_start")):
                    return None
            w.start_missing(state)
            texts = w.poll()
            if texts:
                return "\n\n".join(texts)
            if not w.children and all(t in w.done for t in state["marks"]):
                return None
            time.sleep(POLL_S)
    finally:
        w.stop()


def respawn_waiter(engine: str, conv: str, *, force: bool) -> None:
    """Codex: ожидатель — отдельная группа процессов вне дерева хуков (хук не ждёт его вывода)."""
    with locked(engine, conv):
        state = load_state(engine, conv)
        if state is None or not state["marks"]:
            return
        prev = state.get("waiter") or {}
        if alive(prev.get("pid"), prev.get("start")):
            if not force:
                return
            kill_group(prev.get("pid"), prev.get("start"))
        cmd = [sys.executable, "-m", "hub.ext_hook", engine, "waiter", conv]
        child = subprocess.Popen(  # noqa: S603 — без shell, свой интерпретатор, номер разговора по шаблону
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        state["waiter"] = {"pid": child.pid, "start": proc_start(child.pid)}
        save_state(engine, conv, state)
    log(engine, ev="waiter_spawn", conv=conv[:12], waiter=child.pid)


def deliver_codex(state: Mapping, text: str, *, run=subprocess.run) -> None:
    exe = state.get("engine_exe") or shutil.which("codex") or "codex"
    try:
        res = run(
            [exe, "queue", "--thread", str(state["session"]), "--message", text],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=QUEUE_TIMEOUT_S,
        )
        log("codex", ev="queued", conv=str(state["session"])[:12], code=res.returncode, chars=len(text))
    except (OSError, subprocess.TimeoutExpired) as exc:
        log("codex", ev="queue_failed", conv=str(state["session"])[:12], err=str(exc)[:200])


def run_waiter(engine: str, conv: str, *, waits: Waits | None = None, deliver=deliver_codex) -> int:
    """Ожидатель Codex: держит ожидания разговора, сигналы кладёт в нить `codex queue` (будит простаивающий
    TUI, во время хода встаёт в очередь как реплика). Выходит, когда меток нет, процесс Codex кончился или
    файл состояния назвал другой ожидатель.

    «Подписка снята» (код 3) в нить не кладётся (факт 21.10, раздел «Ф74: автоотписка окна Codex» спеки
    74-02): разговоры держит общий фоновый app-server, окно могло закрыться давно, а `codex queue` в нить без
    окна заводит ход без владельца. Код 2 (ядро отказало в ожидании) по-прежнему доставляется: подписка не снята,
    сессии надо знать, что сигналов не будет."""
    w = waits or Waits(engine, conv, deliver_codes=CODEX_DELIVER_CODES)
    stopping = False

    def on_term(_sig, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, on_term)
    with locked(engine, conv):
        pass  # хук, запустивший ожидатель, держит замок, пока не запишет его pid в файл состояния
    last_watch = 0.0
    try:
        while not stopping:
            state = load_state(engine, conv)
            if state is None or not state["marks"]:
                break
            if (state.get("waiter") or {}).get("pid") not in (None, os.getpid()):
                break
            if time.monotonic() - last_watch >= WATCH_S:
                last_watch = time.monotonic()
                if not alive(state.get("engine_pid"), state.get("engine_start")):
                    log(engine, ev="engine_gone", conv=conv[:12])
                    drop_state(engine, conv)
                    break
            w.start_missing(state)
            texts = w.poll()
            if stopping:
                break
            for text in texts:
                deliver(state, text)
            if not w.children and all(t in w.done for t in state["marks"]):
                break
            time.sleep(POLL_S)
    finally:
        w.stop()
    return 0


def on_start(engine: str, inp: Mapping) -> None:
    """Прибрать разговоры, чьего процесса движка нет; Codex `/clear` — и прежние разговоры этого процесса."""
    conv = conversation(engine, inp)
    me = session_registry.ext_ancestor(os.getpid())
    directory = engine_dir(engine)
    for path in directory.glob("*.json"):
        other = path.stem
        if other == conv:
            continue
        state = load_state(engine, other)
        if state is None:
            continue
        gone = not alive(state.get("engine_pid"), state.get("engine_start"))
        cleared = (
            engine == "codex"
            and inp.get("source") == "clear"
            and me is not None
            and (state.get("engine_pid"), state.get("engine_start")) == (me[1], me[2])
        )
        if gone or cleared:
            end_conversation(engine, other, leave=cleared)


def end_conversation(engine: str, conv: str, *, leave: bool = True) -> None:
    with locked(engine, conv):
        state = load_state(engine, conv)
        drop_state(engine, conv)
    if state is None:
        return
    waiter = state.get("waiter") or {}
    kill_group(waiter.get("pid"), waiter.get("start"))
    argv = leave_argv(state) if leave else None
    if argv is None or not alive(state.get("engine_pid"), state.get("engine_start")):
        return
    try:
        res = subprocess.run(  # noqa: S603 — без shell, бинарь `hub` рядом с хуком, аргументы проверены
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=LEAVE_TIMEOUT_S,
        )
        log(engine, ev="leave", conv=conv[:12], code=res.returncode)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(engine, ev="leave_failed", conv=conv[:12], err=str(exc)[:200])


def on_end(engine: str, inp: Mapping) -> None:
    conv = conversation(engine, inp)
    if conv is not None:
        end_conversation(engine, conv)


HANDLERS = {"pre": on_pre, "post": on_post, "stop": on_stop, "start": on_start, "end": on_end}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if os.environ.get("HUB_SESSION"):
        return 0
    if len(args) < 2 or args[0] not in ENGINES:
        return 0
    engine, event = args[0], args[1]
    try:
        if event == "waiter" and len(args) == 3 and session_registry.EXT_SESSION_RE.fullmatch(args[2]):
            return run_waiter(engine, args[2])
        handler = HANDLERS.get(event)
        if handler is None:
            return 0
        raw = sys.stdin.read(4 * 1024 * 1024)
        inp = json.loads(raw) if raw.strip() else {}
        if not isinstance(inp, dict):
            return 0
        out = handler(engine, inp)
        if out:
            sys.stdout.write(json.dumps(out, ensure_ascii=False))
            sys.stdout.flush()
    except Exception as exc:  # noqa: BLE001 — хук не роняет движок
        log(engine, ev="error", event=event, err=f"{type(exc).__name__}: {str(exc)[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
