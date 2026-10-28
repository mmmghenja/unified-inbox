"""ПОДДЕЛЬНЫЙ `codex app-server` для тестов хоста (Ф74-5). Настоящий Codex не запускается.

JSON-RPC по stdio (строка — сообщение) по образцу проб 21.10 (`.runs/f745-probe/`): `initialize`,
`hooks/list` (хуки из флагов `-c hooks.<Событие>=[…]`, доверие — флагом `-c hooks.state=…` по хэшу команды),
`model/list`, `thread/start|resume|read`, `turn/start|steer|interrupt`, `thread/compact/start`,
`mcpServerStatus/list`, `config/mcpServer/reload`. Хук `PreToolUse` исполняется по-настоящему (`/bin/sh -c` с
JSON на stdin) и, как у Codex 0.163.0, при сбое хука инструмент НЕ блокируется (fail-open); блокирует только
`permissionDecision: deny` или код 2. Команда `Bash` исполняется по-настоящему (`/bin/bash -lc` в папке
вызова, с `updatedInput` хука).

Каталог подделки — каталог этого файла:
- `scenario.json` — `{"turns": [[шаг, …], …], "models": [...], "mcp": {имя: статус}}`; ход N — N-й список
  (кончились — последний), сжатия ходом не считаются;
- `calls.jsonl` — каждый запрос клиента (`method`, `params`) и запуск (`argv`, `env`, `pid`);
- `hooks.jsonl` — каждый хук: событие, вход, код выхода, ответ;
- `threads.json` — нити между запусками (id -> cwd, реплики).

Шаги хода: `{"text": "…"}`; `{"thinking": "…"}`; `{"bash": "команда", "workdir": "…"}`;
`{"patch": "текст патча"}`; `{"mcp": [сервер, инструмент, аргументы]}`; `{"spawn": {"model": …}}`;
`{"bg": "команда"}` — команда идёт дальше хода (не снимается `turn/interrupt`, как unified exec Codex);
`{"bypass": "commandExecution"}` — элемент без хука; `{"stdin": "текст"}` — ввод в запущенную команду;
`{"question": [вопросы]}` — `item/tool/requestUserInput`, ответ — в `calls.jsonl` как `answer`;
`{"elicit": "текст"}`; `{"hang": секунды}` (прерывается `turn/interrupt`); `{"usage": {...}}`;
`{"limit": {...}}` — `account/rateLimits/updated`; `{"fail": "codexErrorInfo"}` — ход `failed`;
`{"exit": код}` — выйти посреди хода."""

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import tomllib
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_KEYS = (
    "HUB_SESSION",
    "HUB_SESSION_KEY",
    "HUB_SESSION_SUB_PROFILE",
    "HUB_CODEX_SOCK",
    "HUB_CODEX_PID",
    "HUB_CODEX_HOOK_WAIT_S",
    "HUB_WORK",
    "ANTHROPIC_API_KEY",
    "CLAUDECODE",
)
LOCK = threading.Lock()
WRITE = threading.Lock()


def _append(name: str, record: dict) -> None:
    with LOCK, open(HERE / name, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _out(message: dict) -> None:
    with WRITE:
        sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def note(method: str, params: dict) -> None:
    _out({"jsonrpc": "2.0", "method": method, "params": params})


def _load(name: str, default):
    path = HERE / name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return default


def _save_threads(threads: dict) -> None:
    with LOCK:
        (HERE / "threads.json").write_text(json.dumps(threads, ensure_ascii=False), encoding="utf-8")


def _flags(argv: list[str]) -> dict:
    """Значения `-c ключ=значение` (TOML) одним словарём."""
    merged: dict = {}
    for i, arg in enumerate(argv):
        if arg == "-c" and i + 1 < len(argv):
            parsed = tomllib.loads(argv[i + 1])
            _merge(merged, parsed)
    return merged


def _merge(into: dict, add: dict) -> None:
    for key, value in add.items():
        if isinstance(value, dict) and isinstance(into.get(key), dict):
            _merge(into[key], value)
        else:
            into[key] = value


class Server:
    def __init__(self, argv: list[str]) -> None:
        self.flags = _flags(argv)
        self.scenario = _load("scenario.json", {})
        self.threads = _load("threads.json", {})
        self.thread: str | None = None
        self.cwd = os.getcwd()
        self.config: dict = {}
        self.turn: str | None = None
        self.turn_no = len(self._turn_log())
        self.interrupt = threading.Event()
        self.pending: dict[int, tuple[threading.Event, list]] = {}
        self.n = 1000
        self.steered: list = []
        _append(
            "calls.jsonl", {"argv": argv, "pid": os.getpid(), "env": {k: os.environ.get(k) for k in ENV_KEYS}}
        )

    @staticmethod
    def _turn_log() -> list:
        return [c for c in _load_jsonl("calls.jsonl") if c.get("method") == "turn/start"]

    # --- хуки ---------------------------------------------------------------------------------------------

    def hooks(self) -> list[dict]:
        state = (self.flags.get("hooks") or {}).get("state") or {}
        found = []
        for event, entries in (self.flags.get("hooks") or {}).items():
            if event == "state" or not isinstance(entries, list):
                continue
            for i, entry in enumerate(entries):
                for j, handler in enumerate(entry.get("hooks") or []):
                    snake = "".join("_" + c.lower() if c.isupper() else c for c in event).lstrip("_")
                    key = f"/flags/config.toml:{snake}:{i}:{j}"
                    digest = "sha256:" + hashlib.sha256(handler["command"].encode()).hexdigest()
                    trusted = (state.get(key) or {}).get("trusted_hash") == digest
                    found.append(
                        {
                            "key": key,
                            "eventName": event[0].lower() + event[1:],
                            "event": event,
                            "source": "sessionFlags",
                            "command": handler["command"],
                            "timeoutSec": handler.get("timeout", 600),
                            "currentHash": digest,
                            "enabled": True,
                            "trustStatus": "trusted" if trusted else "untrusted",
                        }
                    )
        return found

    def run_hook(self, event: str, data: dict) -> tuple[bool, dict | None]:
        """`(пропустить ли, updatedInput)`. Сбой хука — пропустить (fail-open, как Codex 0.163.0)."""
        hooks = [h for h in self.hooks() if h["event"] == event and h["trustStatus"] == "trusted"]
        if not hooks:
            return True, None
        hook = hooks[0]
        proc = subprocess.run(
            ["/bin/sh", "-c", hook["command"]],
            input=json.dumps(data, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=120,
            cwd=self.cwd,
        )
        answer = None
        try:
            answer = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else None
        except ValueError:
            answer = None
        _append(
            "hooks.jsonl",
            {"event": event, "input": data, "rc": proc.returncode, "answer": answer, "stderr": proc.stderr},
        )
        if proc.returncode == 2:
            return False, None
        out = (answer or {}).get("hookSpecificOutput") if isinstance(answer, dict) else None
        if isinstance(out, dict) and out.get("permissionDecision") == "deny":
            return False, None
        return True, (out or {}).get("updatedInput") if isinstance(out, dict) else None

    # --- RPC ----------------------------------------------------------------------------------------------

    def ask(self, method: str, params: dict) -> dict | None:
        self.n += 1
        number = self.n
        done = threading.Event()
        box: list = []
        self.pending[number] = (done, box)
        _out({"jsonrpc": "2.0", "id": number, "method": method, "params": params})
        while not done.wait(0.1):
            if self.interrupt.is_set():
                return None
        return box[0] if box else None

    def handle(self, message: dict) -> None:
        if "method" not in message:
            entry = self.pending.pop(message.get("id"), None)
            if entry is not None:
                entry[1].append(message.get("result"))
                entry[0].set()
            return
        method, params, number = message["method"], message.get("params") or {}, message.get("id")
        if number is None:
            return
        _append("calls.jsonl", {"method": method, "params": params})
        try:
            result = self.dispatch(method, params)
        except RuntimeError as exc:
            _out({"jsonrpc": "2.0", "id": number, "error": {"code": -32600, "message": str(exc)}})
            return
        _out({"jsonrpc": "2.0", "id": number, "result": result})
        if method in ("thread/start", "thread/resume"):
            threading.Thread(target=self.mcp_start, daemon=True).start()

    def dispatch(self, method: str, params: dict) -> dict:
        if method == "initialize":
            return {"userAgent": "fake-codex/0.163.0", "codexHome": str(HERE)}
        if method == "hooks/list":
            hooks = [{k: v for k, v in h.items() if k not in ("event", "command")} for h in self.hooks()]
            return {"data": [{"cwd": self.cwd, "hooks": hooks, "warnings": [], "errors": []}]}
        if method == "model/list":
            models = self.scenario.get("models") or DEFAULT_MODELS
            if not params.get("includeHidden"):
                models = [m for m in models if not m.get("hidden")]
            return {"data": models}
        if method == "thread/start":
            self.thread = str(uuid.uuid4())
            self.cwd = params.get("cwd") or self.cwd
            self.config = params.get("config") or {}
            self.threads[self.thread] = {"cwd": self.cwd, "turns": []}
            _save_threads(self.threads)
            return {"thread": {"id": self.thread, "path": str(HERE / f"rollout-{self.thread}.jsonl")}}
        if method == "thread/read":
            thread = self.threads.get(params.get("threadId"))
            if thread is None:
                raise RuntimeError("thread not found")
            return {
                "thread": {
                    "id": params["threadId"],
                    "turns": thread["turns"] if params.get("includeTurns") else [],
                }
            }
        if method == "thread/resume":
            if params.get("threadId") not in self.threads:
                raise RuntimeError("thread not found")
            self.thread = params["threadId"]
            self.cwd = params.get("cwd") or self.cwd
            self.config = params.get("config") or {}
            return {"thread": {"id": self.thread}}
        if method == "turn/start":
            if self.turn is not None:
                raise RuntimeError("turn already running")
            self.turn = str(uuid.uuid4())
            self.interrupt.clear()
            self._remember(params)
            threading.Thread(target=self.run_turn, args=(self.turn, False), daemon=True).start()
            return {"turn": {"id": self.turn, "status": "inProgress", "items": []}}
        if method == "turn/steer":
            if (
                self.scenario.get("no_steer")
                or self.turn is None
                or params.get("expectedTurnId") != self.turn
            ):
                raise RuntimeError("no active turn to steer")
            self._remember(params)
            self.steered.append(params.get("input"))
            return {"turnId": self.turn}
        if method == "turn/interrupt":
            if self.turn is None or params.get("turnId") != self.turn:
                raise RuntimeError("no active turn")
            self.interrupt.set()
            return {}
        if method == "thread/compact/start":
            if self.turn is not None:
                raise RuntimeError("turn running")
            self.turn = str(uuid.uuid4())
            threading.Thread(target=self.run_turn, args=(self.turn, True), daemon=True).start()
            return {}
        if method == "mcpServerStatus/list":
            servers = self.config.get("mcp_servers") or {}
            return {"data": [{"name": n, "tools": {}, "authStatus": "unsupported"} for n in sorted(servers)]}
        if method == "config/mcpServer/reload":
            return {}
        if method == "thread/inject_items":
            if self.scenario.get("no_inject") or params.get("threadId") not in self.threads:
                raise RuntimeError("inject refused")
            return {}
        raise RuntimeError(f"unknown method {method}")

    def _remember(self, params: dict) -> None:
        thread = self.threads.setdefault(self.thread or "", {"cwd": self.cwd, "turns": []})
        text = "".join(i.get("text", "") for i in params.get("input") or [] if i.get("type") == "text")
        thread["turns"].append(
            {
                "id": self.turn,
                "startedAt": int(time.time()),
                "status": "completed",
                "items": [
                    {
                        "type": "userMessage",
                        "id": str(uuid.uuid4()),
                        "clientId": params.get("clientUserMessageId"),
                        "content": [{"type": "text", "text": text}],
                    }
                ],
            }
        )
        _save_threads(self.threads)

    def mcp_start(self) -> None:
        wanted = self.scenario.get("mcp") or {}
        servers = self.config.get("mcp_servers") or {}
        for name in servers:
            note("mcpServer/startupStatus/updated", {"name": name, "status": "starting"})
        time.sleep(float(self.scenario.get("mcp_delay", 0.05)))
        for name in servers:
            note("mcpServer/startupStatus/updated", {"name": name, "status": wanted.get(name, "ready")})

    # --- ход ----------------------------------------------------------------------------------------------

    def run_turn(self, turn: str, compact: bool) -> None:
        tid = self.thread
        note("turn/started", {"threadId": tid, "turn": {"id": turn, "status": "inProgress", "items": []}})
        status, error = "completed", None
        if compact:
            item = {"type": "contextCompaction", "id": "cmp-" + turn[:8]}
            note("item/started", {"threadId": tid, "turnId": turn, "item": item})
            note("item/completed", {"threadId": tid, "turnId": turn, "item": item})
        else:
            turns = self.scenario.get("turns") or [[{"text": "ок"}]]
            steps = turns[min(self.turn_no, len(turns) - 1)]
            self.turn_no += 1
            for step in steps:
                if self.interrupt.is_set():
                    status = "interrupted"
                    break
                outcome = self.step(turn, step)
                if outcome == "interrupted":
                    status = "interrupted"
                    break
                if isinstance(outcome, dict):
                    status, error = "failed", outcome
                    break
            if self.interrupt.is_set():
                status = "interrupted"
            note(
                "thread/tokenUsage/updated",
                {
                    "threadId": tid,
                    "turnId": turn,
                    "tokenUsage": self.scenario.get("usage")
                    or {
                        "total": {
                            "totalTokens": 1300,
                            "inputTokens": 1200,
                            "cachedInputTokens": 1000,
                            "outputTokens": 100,
                        },
                        "last": {
                            "totalTokens": 1300,
                            "inputTokens": 1200,
                            "cachedInputTokens": 1000,
                            "outputTokens": 100,
                        },
                        "modelContextWindow": 475000,
                    },
                },
            )
        self.turn = None
        body = {"id": turn, "status": status, "items": [], "error": error}
        note("turn/completed", {"threadId": tid, "turn": body})

    def item(self, turn: str, item: dict, *, started: bool = True, completed: dict | None = None) -> None:
        if started:
            note("item/started", {"threadId": self.thread, "turnId": turn, "item": item})
        note(
            "item/completed", {"threadId": self.thread, "turnId": turn, "item": {**item, **(completed or {})}}
        )

    def pre(self, turn: str, tool: str, tool_input: dict) -> tuple[bool, dict | None, str]:
        call = "call-" + uuid.uuid4().hex[:12]
        data = {
            "session_id": self.thread,
            "turn_id": turn,
            "cwd": self.cwd,
            "hook_event_name": "PreToolUse",
            "permission_mode": "bypassPermissions",
            "tool_name": tool,
            "tool_input": tool_input,
            "tool_use_id": call,
        }
        ok, updated = self.run_hook("PreToolUse", data)
        return ok, updated, call

    def step(self, turn: str, step: dict):
        tid = self.thread
        if "text" in step:
            item = {"type": "agentMessage", "id": "msg-" + uuid.uuid4().hex[:8], "text": ""}
            note("item/started", {"threadId": tid, "turnId": turn, "item": item})
            for part in (step["text"][: len(step["text"]) // 2], step["text"][len(step["text"]) // 2 :]):
                if part:
                    note(
                        "item/agentMessage/delta",
                        {"threadId": tid, "turnId": turn, "itemId": item["id"], "delta": part},
                    )
            note("item/completed", {"threadId": tid, "turnId": turn, "item": {**item, "text": step["text"]}})
        elif "thinking" in step:
            item = {"type": "reasoning", "id": "rs-" + uuid.uuid4().hex[:8]}
            note("item/started", {"threadId": tid, "turnId": turn, "item": item})
            note(
                "item/reasoning/summaryTextDelta",
                {"threadId": tid, "turnId": turn, "itemId": item["id"], "delta": step["thinking"]},
            )
            note("item/completed", {"threadId": tid, "turnId": turn, "item": item})
        elif "bash" in step:
            tool_input = {"command": step["bash"]}
            ok, updated, call = self.pre(turn, "Bash", tool_input)
            if not ok:
                return None
            command = (updated or tool_input)["command"]
            cwd = step.get("workdir") or self.cwd
            item = {
                "type": "commandExecution",
                "id": call,
                "command": "/bin/bash -lc " + _quote(command),
                "cwd": cwd,
                "status": "inProgress",
            }
            note("item/started", {"threadId": tid, "turnId": turn, "item": item})
            proc = subprocess.run(
                ["/bin/bash", "-lc", command],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=60,
                stdin=subprocess.DEVNULL,
            )
            output = proc.stdout + proc.stderr
            _append("calls.jsonl", {"ran": command, "cwd": cwd, "rc": proc.returncode, "output": output})
            note(
                "item/commandExecution/outputDelta",
                {"threadId": tid, "turnId": turn, "itemId": call, "delta": output},
            )
            note(
                "item/completed",
                {
                    "threadId": tid,
                    "turnId": turn,
                    "item": {
                        **item,
                        "status": "completed",
                        "exitCode": proc.returncode,
                        "aggregatedOutput": output,
                    },
                },
            )
        elif "bg" in step:
            # Как unified exec Codex: команда идёт дальше хода, `turn/interrupt` её не снимает.
            tool_input = {"command": step["bg"]}
            ok, updated, call = self.pre(turn, "Bash", tool_input)
            if not ok:
                return None
            command = (updated or tool_input)["command"]
            item = {
                "type": "commandExecution",
                "id": call,
                "command": command,
                "cwd": self.cwd,
                "status": "inProgress",
            }
            note("item/started", {"threadId": tid, "turnId": turn, "item": item})
            proc = subprocess.Popen(["/bin/bash", "-lc", command], cwd=self.cwd, stdin=subprocess.DEVNULL)
            _append("calls.jsonl", {"bg": command, "pid": proc.pid})
        elif "patch" in step:
            ok, _updated, call = self.pre(turn, "apply_patch", {"command": step["patch"]})
            if not ok:
                return None
            paths = [
                line.split(":", 1)[1].strip()
                for line in step["patch"].splitlines()
                if line.startswith("*** ") and " File: " in line
            ]
            item = {
                "type": "fileChange",
                "id": call,
                "changes": [{"path": p, "kind": {"type": "update"}, "diff": ""} for p in paths],
                "status": "inProgress",
            }
            note("item/started", {"threadId": tid, "turnId": turn, "item": item})
            note("item/completed", {"threadId": tid, "turnId": turn, "item": {**item, "status": "completed"}})
        elif "mcp" in step:
            server, tool, args = step["mcp"]
            ok, _updated, call = self.pre(turn, f"mcp__{server}__{tool}", args)
            if not ok:
                return None
            item = {
                "type": "mcpToolCall",
                "id": call,
                "server": server,
                "tool": tool,
                "arguments": args,
                "status": "inProgress",
            }
            note("item/started", {"threadId": tid, "turnId": turn, "item": item})
            done = {"status": "completed", "result": {"content": [{"type": "text", "text": "mcp ok"}]}}
            note("item/completed", {"threadId": tid, "turnId": turn, "item": {**item, **done}})
        elif "spawn" in step:
            ok, _updated, call = self.pre(turn, "spawn_agent", dict(step["spawn"]))
            if not ok:
                return None
            kid = str(uuid.uuid4())
            item = {
                "type": "collabAgentToolCall",
                "id": call,
                "tool": "spawnAgent",
                "receiverThreadIds": [kid],
                "model": step["spawn"].get("model"),
                "prompt": "помоги",
                "agentsStates": {},
                "status": "inProgress",
            }
            note("item/started", {"threadId": tid, "turnId": turn, "item": item})
            note(
                "item/completed",
                {
                    "threadId": tid,
                    "turnId": turn,
                    "item": {**item, "status": "completed", "agentsStates": {kid: {"status": "running"}}},
                },
            )
            wait = {
                **item,
                "id": "wait-" + kid[:6],
                "tool": "wait",
                "agentsStates": {kid: {"status": "completed", "message": "готово"}},
            }
            note("item/completed", {"threadId": tid, "turnId": turn, "item": wait})
        elif "bypass" in step:
            item = {
                "type": step["bypass"],
                "id": "nohook-" + uuid.uuid4().hex[:6],
                "command": "/bin/bash -lc id",
                "status": "inProgress",
            }
            note("item/started", {"threadId": tid, "turnId": turn, "item": item})
            self.hang(5)
        elif "stdin" in step:
            note(
                "item/commandExecution/terminalInteraction",
                {"threadId": tid, "turnId": turn, "itemId": "x", "processId": "1", "stdin": step["stdin"]},
            )
            self.hang(5)
        elif "question" in step:
            answer = self.ask(
                "item/tool/requestUserInput",
                {"threadId": tid, "turnId": turn, "itemId": "q1", "questions": step["question"]},
            )
            _append("calls.jsonl", {"answer": answer})
        elif "elicit" in step:
            answer = self.ask(
                "mcpServer/elicitation/request",
                {
                    "threadId": tid,
                    "turnId": turn,
                    "serverName": "notion",
                    "mode": "form",
                    "message": step["elicit"],
                    "requestedSchema": {"type": "object", "properties": {}},
                },
            )
            _append("calls.jsonl", {"elicit_answer": answer})
        elif "hang" in step:
            if self.hang(float(step["hang"])):
                return "interrupted"
        elif "usage" in step:
            note("thread/tokenUsage/updated", {"threadId": tid, "turnId": turn, "tokenUsage": step["usage"]})
        elif "limit" in step:
            note("account/rateLimits/updated", {"rateLimits": step["limit"]})
        elif "fail" in step:
            return {"message": "лимит подписки исчерпан", "codexErrorInfo": step["fail"]}
        elif "exit" in step:
            sys.stdout.flush()
            os._exit(int(step["exit"]))
        return None

    def hang(self, seconds: float) -> bool:
        return self.interrupt.wait(seconds)


DEFAULT_MODELS = [
    {
        "id": "gpt-6.1-sol",
        "model": "gpt-6.1-sol",
        "isDefault": True,
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [
            {"reasoningEffort": e} for e in ("low", "medium", "high", "xhigh", "max")
        ],
    }
]


def _quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


def _load_jsonl(name: str) -> list[dict]:
    path = HERE / name
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def main() -> None:
    argv = sys.argv[1:]
    if argv[:2] == ["login", "status"]:
        _append("calls.jsonl", {"login": True, "env": {k: os.environ.get(k) for k in ENV_KEYS}})
        logged = not (HERE / "logged-out").exists()
        print("Logged in using ChatGPT" if logged else "Not logged in")
        sys.exit(0 if logged else 1)
    if argv[:1] != ["app-server"]:
        sys.stderr.write("fake codex: only app-server\n")
        sys.exit(2)
    server = Server(argv[1:])
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict):
            server.handle(message)


if __name__ == "__main__":
    main()
