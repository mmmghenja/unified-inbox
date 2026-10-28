"""Поддельный `opencode serve` для тестов слоя движка (Ф16, кусок 16-2). Настоящий OpenCode в тестах не
запускается никогда.

Копируется во временный каталог с шебангом текущего интерпретатора (`conftest.install_fake_opencode`).
Разбирает `serve --hostname H --port P`, читает `OPENCODE_CONFIG`, `OPENCODE_SERVER_PASSWORD` и `FAKE_OC_DIR`
(каталог сценария и журналов), поднимает `ThreadingHTTPServer`, проверяет basic auth (`opencode:<пароль>`) и
`directory` в query каждого запроса. В `FAKE_OC_DIR`:
- `scenario.json` — `{"no_listen": bool, "exit_rc": int, "grandchild": bool, "grandchild_setsid": bool,
  "saved_permissions": [...], "turns": [[шаг, …], …]}`; на каждый `prompt_async` берётся следующий ход
  (очередь пуста — ход из одного текста «ok»). Шаги:
  `{"text": "…"}`; `{"ask": {"tool", "args", "permission"?}, "child"?: bool, "announce"?: bool,
  "foreign"?: bool, "wait_s"?: float, "hidden"?: bool, "drop_after"?: bool}` (запрос разрешения;
  `reject` -> часть инструмента с ошибкой F8); `{"question": {"questions": [...]}, "child"?: bool}`;
  `{"error": {"name", "message", "status"?}}` (`session.error`, F11 — ход кончается); `{"retry": "…"}`
  (`session.status retry`); `{"sleep": s}`; `{"drop_sse": true}`; `{"silent": true}` (дальше события хода
  не рассылаются); `{"permission_list_400": true}` (F9); `{"hang": true}` (ждать `abort`);
  `{"finish": "…"}` (итог записи вместо `stop`); Ф41-3: `{"stream": {"kind": "text"|"reasoning",
  "chunks": [...], "no_delta"?: bool, "delay"?: с}}` (часть по мере набора: `message.part.updated` с
  `time.start`, `message.part.delta` на кусок, `message.part.updated` с текстом и `time.end`);
  `{"tool": {"tool", "args", "output", "metadata"?, "error"?, "hold"?: с, "aborted_output"?}}` (вызов без
  запроса разрешения; `hold` — идёт столько секунд, `abort` кончает его выводом `aborted_output`);
  `"tokens": {…}` сценария — токены законченной записи ассистента вместо умолчания; `"providers": …` —
  ответ `GET /config/providers` (нет ключа — 404). Реплика в занятую сессию берёт следующий ход из очереди и
  выполняется в том же ходе после текущих шагов (F10). `"mcp_extra": {имя: {"status": …}}` — `GET /mcp`
  отдаёт эти серверы сверх конфига (глобальные MCP владельца). `"extra_agents": {имя: {"mode": …,
  "own_permission"?: {…}, "sticky"?: bool}}` — `GET /agent` отдаёт их как агентов проекта
  (`.opencode/agents/*.md`) с правами по умолчанию `"*": "allow"` (плюс свои), пока конфиг их не выключит
  (`disable: true`, ревью Ф16; `sticky` — выключение не действует).
- `sessions.json` — созданные сессии (без сообщений): следующий процесс поддельного сервера их видит, как
  настоящий OpenCode видит общие данные сессий (F16; нужно дежурной 16-3 — сессия дня между запусками).
- `--version` печатает `version` из `scenario.json` (по умолчанию 1.18.31).
- пишет `calls.jsonl` (метод, путь, query, тело), `replies.jsonl` (ответы на разрешения/вопросы и «без
  ответа»), `env.json` (окружение), `config.json` (копия конфига), `pids.json` (pid свой и внука),
  `starts.jsonl` (pid и агенты конфига на каждый старт сервера).
"""

import base64
import itertools
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.environ.get("FAKE_OC_DIR") or os.path.dirname(os.path.abspath(__file__))
LOCK = threading.RLock()
COUNTER = itertools.count(1)


def _path(name):
    return os.path.join(HERE, name)


def _load(name, default):
    try:
        with open(_path(name), encoding="utf-8") as f:
            return json.load(f)
    except OSError, ValueError:
        return default


def _append(name, value):
    with LOCK, open(_path(name), "a", encoding="utf-8") as f:
        f.write(json.dumps(value, ensure_ascii=False) + "\n")


def _now():
    return int(time.time() * 1000)


def _id(prefix):
    return f"{prefix}_{next(COUNTER):06d}{secrets.token_hex(6)}"


class State:
    def __init__(self, scenario):
        self.scenario = scenario
        self.turns = list(scenario.get("turns") or [])
        self.saved = scenario.get("saved_permissions") or []
        # sid -> {"info", "messages": [ {info, parts} ]}; сессии прошлых процессов — из `sessions.json` (F16)
        self.sessions = {
            info["id"]: {"info": info, "messages": []}
            for info in _load("sessions.json", [])
            if isinstance(info, dict) and isinstance(info.get("id"), str)
        }
        self.status = {}  # sid -> status dict (нет — idle)
        self.streams = []  # список очередей SSE
        self.permissions = {}  # rid -> {"props", "event", "reply"}
        self.questions = {}
        self.aborted = set()
        self.silent = set()
        self.permission_list_400 = False
        self.queued = {}  # sid -> [(реплика, шаги)] — пришли в занятую сессию (F10)
        self.extra_agents = scenario.get("extra_agents") or {}  # `/agent`: агенты проекта `{имя: {mode, …}}`
        self.compact = scenario.get("compact")  # `summarize`: None (успех), "fail", "late_fail", "hang"
        self.mcp_extra = scenario.get("mcp_extra") or {}  # `/mcp`: серверы сверх конфига (глобальные владельца)

    # --- события ---

    def broadcast(self, sid, etype, props):
        if sid in self.silent:
            return
        event = {"id": _id("evt"), "type": etype, "properties": props}
        with LOCK:
            for q in list(self.streams):
                q.append(event)
                q.cond.set()

    def drop_streams(self):
        with LOCK:
            for q in list(self.streams):
                q.closed = True
                q.cond.set()

    # --- сессии ---

    def create_session(self, directory, title, parent=None):
        sid = _id("ses")
        info = {"id": sid, "directory": directory, "title": title, "time": {"created": _now()}}
        if parent:
            info["parentID"] = parent
        with LOCK:
            self.sessions[sid] = {"info": info, "messages": []}
            with open(_path("sessions.json"), "w", encoding="utf-8") as f:
                json.dump([s["info"] for s in self.sessions.values()], f)
        return info

    def new_message(self, sid, role, parent=None):
        info = {"id": _id("msg"), "sessionID": sid, "role": role, "time": {"created": _now()}}
        if role == "assistant":
            info.update(
                parentID=parent or "",
                modelID="fake",
                providerID="fake",
                mode="build",
                agent="fake",
                path={"cwd": HERE, "root": HERE},
                cost=0.0,
                tokens={"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            )
        msg = {"info": info, "parts": []}
        with LOCK:
            self.sessions[sid]["messages"].append(msg)
        return msg


class Stream(list):
    def __init__(self):
        super().__init__()
        self.cond = threading.Event()
        self.closed = False


STATE = None


def _wait(event, sid, timeout=None):
    """Ждать `event`; `abort` сессии прерывает. True — дождались."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        if event.wait(0.05):
            return True
        if sid in STATE.aborted:
            return False
        if deadline is not None and time.monotonic() >= deadline:
            return False


def _sleep(sid, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if sid in STATE.aborted:
            return False
        time.sleep(0.02)
    return True


def _set_status(sid, status):
    with LOCK:
        if status.get("type") == "idle":
            STATE.status.pop(sid, None)
        else:
            STATE.status[sid] = status
    STATE.broadcast(sid, "session.status", {"sessionID": sid, "status": status})


def _part_event(sid, part):
    STATE.broadcast(sid, "message.part.updated", {"sessionID": sid, "part": part, "time": _now()})


def _child_session(sid, step):
    directory = STATE.sessions[sid]["info"]["directory"]
    if step.get("foreign"):
        info = STATE.create_session(directory, "чужая сессия")
    else:
        info = STATE.create_session(directory, "подагент", parent=sid)
    if step.get("announce", True):
        STATE.broadcast(sid, "session.created", {"sessionID": info["id"], "info": info})
    return info["id"]


def _ask(sid, msg, step):
    spec = step["ask"]
    target = sid
    target_msg = msg
    if step.get("child") or step.get("foreign"):
        target = _child_session(sid, step)
        target_msg = STATE.new_message(target, "assistant")
    tool = spec["tool"]
    call_id = _id("call")
    part = {
        "id": _id("prt"),
        "sessionID": target,
        "messageID": target_msg["info"]["id"],
        "type": "tool",
        "callID": call_id,
        "tool": tool,
        "state": {"status": "running", "input": spec.get("args") or {}, "time": {"start": _now()}},
    }
    if spec.get("no_input"):
        del part["state"]["input"]  # часть без входа: хост аргументов вызова не получит
    with LOCK:
        target_msg["parts"].append(part)
    _part_event(target, part)
    rid = _id("per")
    props = {
        "id": rid,
        "sessionID": target,
        "permission": spec.get("permission") or tool,
        "patterns": spec.get("patterns") or ["*"],
        "metadata": {},
        "always": ["*"],
        "tool": {"messageID": target_msg["info"]["id"], "callID": call_id},
    }
    entry = {"props": props, "event": threading.Event(), "reply": None}
    with LOCK:
        STATE.permissions[rid] = entry
    if not step.get("hidden"):
        STATE.broadcast(target, "permission.asked", props)
    if step.get("drop_after"):
        STATE.drop_streams()
    got = _wait(entry["event"], sid, step.get("wait_s"))
    with LOCK:
        STATE.permissions.pop(rid, None)
    reply = entry["reply"] if got else None
    _append(
        "replies.jsonl",
        {
            "kind": "permission",
            "id": rid,
            "session": target,
            "child": target != sid,
            "foreign": bool(step.get("foreign")),
            "tool": tool,
            "reply": (reply or {}).get("reply"),
            "message": (reply or {}).get("message"),
        },
    )
    end = _now()
    if reply and reply.get("reply") == "once":
        part["state"] = {
            "status": "completed",
            "input": part["state"].get("input") or {},
            "output": "ok",
            "title": tool,
            "metadata": {},
            "time": {"start": part["state"]["time"]["start"], "end": end},
        }
    else:
        text = (reply or {}).get("message") or ""
        part["state"] = {
            "status": "error",
            "input": part["state"].get("input") or {},
            "error": REJECTED + text,
            "time": {"start": part["state"]["time"]["start"], "end": end},
        }
    _part_event(target, part)


def _question(sid, msg, step):
    target = _child_session(sid, step) if step.get("child") else sid
    qid = _id("que")
    props = {
        "id": qid,
        "sessionID": target,
        "questions": step["question"].get("questions") or [],
        "tool": {"messageID": msg["info"]["id"], "callID": _id("call")},
    }
    entry = {"props": props, "event": threading.Event(), "reply": None}
    with LOCK:
        STATE.questions[qid] = entry
    STATE.broadcast(target, "question.asked", props)
    got = _wait(entry["event"], sid, step.get("wait_s"))
    with LOCK:
        STATE.questions.pop(qid, None)
    _append(
        "replies.jsonl",
        {"kind": "question", "id": qid, "session": target, **(entry["reply"] or {})}
        if got
        else {"kind": "question", "id": qid, "session": target, "reply": None},
    )


def _text(sid, msg, text):
    part = {"id": _id("prt"), "sessionID": sid, "messageID": msg["info"]["id"], "type": "text", "text": text}
    with LOCK:
        msg["parts"].append(part)
    _part_event(sid, part)


def _stream(sid, msg, spec):
    """Ф41-3: часть ответа по мере набора — как 1.18.31 (проба 5з)."""
    kind = spec.get("kind") or "text"
    chunks = spec.get("chunks") or []
    start = _now()
    part = {
        "id": _id("prt"),
        "sessionID": sid,
        "messageID": msg["info"]["id"],
        "type": kind,
        "text": "",
        "time": {"start": start},
    }
    with LOCK:
        msg["parts"].append(part)
    _part_event(sid, dict(part))
    for chunk in chunks:
        if sid in STATE.aborted:
            return
        if not spec.get("no_delta"):
            STATE.broadcast(
                sid,
                "message.part.delta",
                {
                    "sessionID": sid,
                    "messageID": msg["info"]["id"],
                    "partID": part["id"],
                    "field": "text",
                    "delta": chunk,
                },
            )
        time.sleep(float(spec.get("delay") or 0))
    part["text"] = "".join(chunks)
    part["time"] = {"start": start, "end": _now()}
    _part_event(sid, dict(part))


def _tool(sid, msg, spec):
    """Ф41-3: вызов инструмента без запроса разрешения — `running`, затем `completed` (или `error`)."""
    call_id = _id("call")
    start = _now()
    part = {
        "id": _id("prt"),
        "sessionID": sid,
        "messageID": msg["info"]["id"],
        "type": "tool",
        "tool": spec["tool"],
        "callID": call_id,
        "state": {"status": "running", "input": spec.get("args") or {}, "time": {"start": start}},
    }
    with LOCK:
        msg["parts"].append(part)
    _part_event(sid, json.loads(json.dumps(part)))
    # `hold` — команда идёт столько секунд; `abort` застаёт её посреди работы — как 1.18.31, она кончается
    # ОБЫЧНЫМ результатом с пометкой в выводе (`aborted_output`), а не ошибкой.
    output = spec.get("output", "")
    end = time.time() + float(spec.get("hold") or 0)
    while time.time() < end:
        if sid in STATE.aborted:
            output = spec.get("aborted_output", output)
            break
        time.sleep(0.02)
    state = {"input": spec.get("args") or {}, "time": {"start": start, "end": _now()}}
    if "error" in spec:
        state.update(status="error", error=spec["error"])
    else:
        state.update(status="completed", output=output, title=spec["tool"])
        if "metadata" in spec:
            state["metadata"] = spec["metadata"]
    part["state"] = state
    _part_event(sid, json.loads(json.dumps(part)))


def _error_obj(spec):
    data = {"message": spec.get("message") or ""}
    if "status" in spec:
        data["statusCode"] = spec["status"]
        data["isRetryable"] = False
    return {"name": spec.get("name") or "UnknownError", "data": data}


def run_turn(sid, user_msg, steps):
    """Ход: шаги реплики, затем реплики, пришедшие в занятую сессию (F10: они в очереди того же хода)."""
    _set_status(sid, {"type": "busy"})
    try:
        while True:
            _run_steps(sid, user_msg, steps)
            with LOCK:
                queue = STATE.queued.get(sid) or []
                if not queue or sid in STATE.aborted:
                    STATE.queued.pop(sid, None)
                    break
                user_msg, steps = queue.pop(0)
    finally:
        _set_status(sid, {"type": "idle"})
        STATE.broadcast(sid, "session.idle", {"sessionID": sid})
        with LOCK:
            STATE.aborted.discard(sid)
            STATE.silent.discard(sid)


def _run_steps(sid, user_msg, steps):
    msg = None
    finish = "stop"
    error = None
    for step in steps:
        if sid in STATE.aborted:
            break
        if "sleep" in step:
            _sleep(sid, float(step["sleep"]))
            continue
        if step.get("drop_sse"):
            STATE.drop_streams()
            continue
        if step.get("silent"):
            STATE.silent.add(sid)
            continue
        if step.get("permission_list_400"):
            STATE.permission_list_400 = True
            continue
        if "retry" in step:
            _set_status(
                sid, {"type": "retry", "attempt": 1, "message": step["retry"], "next": _now() + 1000}
            )
            continue
        if "finish" in step:
            finish = step["finish"]
            continue
        if step.get("hang"):
            while sid not in STATE.aborted:
                time.sleep(0.02)
            break
        if "error" in step:
            error = _error_obj(step["error"])
            break
        if msg is None:
            msg = STATE.new_message(sid, "assistant", user_msg["info"]["id"])
            STATE.broadcast(sid, "message.updated", {"sessionID": sid, "info": msg["info"]})
        if "text" in step:
            _text(sid, msg, step["text"])
        elif "stream" in step:
            _stream(sid, msg, step["stream"])
        elif "tool" in step:
            _tool(sid, msg, step["tool"])
        elif "ask" in step:
            _ask(sid, msg, step)
        elif "question" in step:
            _question(sid, msg, step)
    if sid in STATE.aborted:
        error = {"name": "MessageAbortedError", "data": {"message": "aborted"}}
    if error is not None:
        if msg is not None:
            msg["info"]["error"] = error
            msg["info"]["time"]["completed"] = _now()
        STATE.broadcast(sid, "session.error", {"sessionID": sid, "error": error})
    elif msg is not None:
        msg["info"]["finish"] = finish
        msg["info"]["tokens"] = STATE.scenario.get("tokens") or {
            "input": 10,
            "output": 5,
            "reasoning": 0,
            "cache": {"read": 0, "write": 0},
        }
        msg["info"]["cost"] = 0.001
        msg["info"]["time"]["completed"] = _now()
        STATE.broadcast(sid, "message.updated", {"sessionID": sid, "info": msg["info"]})


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def _send(self, code, body=None):
        data = b"" if body is None else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _auth_ok(self):
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Basic "):
            return False
        try:
            user, _, password = base64.b64decode(header[6:]).decode().partition(":")
        except ValueError:
            return False
        return user == "opencode" and password == PASSWORD

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return None
        try:
            return json.loads(self.rfile.read(length))
        except ValueError:
            return None

    def _handle(self, method):
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        body = self._body() if method == "POST" else None
        _append("calls.jsonl", {"method": method, "path": url.path, "query": query, "body": body})
        if not self._auth_ok():
            return self._send(401, {"error": "unauthorized"})
        directory = query.get("directory")
        if not directory or not os.path.isabs(directory):
            return self._send(400, {"error": "directory required"})
        parts = [p for p in url.path.split("/") if p]
        try:
            return self._route(method, parts, query, body, directory)
        except BrokenPipeError, ConnectionResetError:
            return None

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def _route(self, method, parts, query, body, directory):
        if method == "DELETE" and len(parts) == 2 and parts[0] == "session":
            # Ф41-2б: удаление сессии (`DELETE /session/{id}`) — и из `sessions.json`.
            with LOCK:
                found = STATE.sessions.pop(parts[1], None)
                with open(_path("sessions.json"), "w", encoding="utf-8") as f:
                    json.dump([s["info"] for s in STATE.sessions.values()], f)
            return self._send(200 if found else 404, bool(found))
        if method == "GET" and parts == ["config", "providers"]:
            # Ф41-3: как 1.18.31 — в ответе есть ключ провайдера (`key`); хаб обязан взять только `limit`.
            if "providers" not in STATE.scenario:
                return self._send(404, {"error": "no route"})
            return self._send(200, STATE.scenario["providers"])
        if method == "GET" and parts == ["agent"]:
            # Как 1.18.31: права — итоговый список правил (умолчание OpenCode, затем правила конфига по
            # порядку; проба 16-3; вложенный словарь — по правилу на шаблон, проба 16-6). Встроенные
            # `build`/`plan`/`general`/`explore` и агенты проекта (`extra_agents` сценария) — с правами
            # по умолчанию; правила конфига идут ПЕРЕД своими правилами агента проекта (проба 16-6: так
            # `"*": "deny"` из конфига не запирает агента `.md`); `disable: true` — агента нет.
            default = [{"permission": "*", "pattern": "*", "action": "allow"}]
            known = {name: {"mode": mode} for name, mode in BUILTIN_AGENTS.items()}
            for name, extra in (STATE.extra_agents or {}).items():
                known[name] = dict(extra)
            for name, cfg in (CONFIG.get("agent") or {}).items():
                known[name] = {**known.get(name, {}), **cfg, "_cfg": cfg}
            agents = []
            for name, info in known.items():
                cfg = info.pop("_cfg", {})
                if info.get("disable") and not info.get("sticky"):
                    continue
                info.pop("sticky", None)
                rules = list(default)
                for src in (cfg.get("permission"), info.get("own_permission")):
                    for key, value in (src or {}).items():
                        items = value.items() if isinstance(value, dict) else [("*", value)]
                        rules += [{"permission": key, "pattern": p, "action": a} for p, a in items]
                info.pop("own_permission", None)
                agents.append({"name": name, **info, "permission": rules})
            return self._send(200, agents)
        if method == "GET" and parts == ["mcp"]:
            mcp = {
                n: {"status": "connected" if c.get("enabled", True) else "disabled"}
                for n, c in (CONFIG.get("mcp") or {}).items()
            }
            mcp.update(STATE.mcp_extra)
            return self._send(200, mcp)
        if method == "GET" and parts == ["event"]:
            return self._sse()
        if method == "GET" and parts == ["permission"]:
            if STATE.permission_list_400:
                return self._send(
                    400, {"error": "Expected JSON value, got undefined at [0].metadata.include"}
                )
            with LOCK:
                return self._send(200, [e["props"] for e in STATE.permissions.values()])
        if method == "GET" and parts == ["api", "permission", "saved"]:
            return self._send(200, STATE.saved)
        if method == "POST" and len(parts) == 3 and parts[0] == "permission" and parts[2] == "reply":
            entry = STATE.permissions.get(parts[1])
            if entry is None or not isinstance(body, dict):
                return self._send(404, {"error": "unknown permission"})
            entry["reply"] = {"reply": body.get("reply"), "message": body.get("message")}
            entry["event"].set()
            return self._send(200, True)
        if (
            method == "POST"
            and len(parts) == 3
            and parts[0] == "question"
            and parts[2] in ("reply", "reject")
        ):
            entry = STATE.questions.get(parts[1])
            if entry is None:
                return self._send(404, {"error": "unknown question"})
            entry["reply"] = {"reply": parts[2], "answers": (body or {}).get("answers")}
            entry["event"].set()
            return self._send(200, True)
        if method == "POST" and parts == ["session"]:
            body = body or {}
            info = STATE.create_session(directory, body.get("title") or "", body.get("parentID"))
            STATE.broadcast(info["id"], "session.created", {"sessionID": info["id"], "info": info})
            return self._send(200, info)
        if method == "GET" and parts == ["session", "status"]:
            with LOCK:
                return self._send(200, dict(STATE.status))
        if len(parts) >= 2 and parts[0] == "session":
            sid = parts[1]
            session = STATE.sessions.get(sid)
            if session is None:
                return self._send(404, {"error": "session not found"})
            if method == "GET" and len(parts) == 2:
                return self._send(200, session["info"])
            if method == "GET" and parts[2:] == ["message"]:
                with LOCK:
                    return self._send(200, json.loads(json.dumps(session["messages"])))
            if method == "GET" and len(parts) == 4 and parts[2] == "message":
                with LOCK:
                    for m in session["messages"]:
                        if m["info"]["id"] == parts[3]:
                            return self._send(200, json.loads(json.dumps(m)))
                return self._send(404, {"error": "message not found"})
            if method == "POST" and parts[2:] == ["abort"]:
                with LOCK:
                    if sid in STATE.status:
                        STATE.aborted.add(sid)
                return self._send(200, True)
            if method == "POST" and parts[2:] == ["summarize"]:
                # Ф41-4б: сжатие — запрос в журнал, событие `session.compacted` (сценарий `compact`:
                # `"fail"` — отказ 500, `"hang"` — события не будет).
                _append("summarize.jsonl", {"session": sid, "body": body})
                mode = STATE.compact
                if mode == "fail":
                    return self._send(500, {"error": "compaction failed"})
                if mode == "late_fail":  # отказ не сразу: хост к этому времени сжатие уже не ждёт
                    time.sleep(0.6)
                    return self._send(500, {"error": "compaction failed"})
                if mode != "hang":
                    threading.Timer(
                        0.05, STATE.broadcast, args=(sid, "session.compacted", {"sessionID": sid})
                    ).start()
                return self._send(200, True)
            if method == "POST" and parts[2:] == ["prompt_async"]:
                user = STATE.new_message(sid, "user")
                text = "".join(p.get("text", "") for p in (body or {}).get("parts") or [])
                user["parts"].append({"type": "text", "text": text})
                with LOCK:
                    busy = sid in STATE.status
                    steps = STATE.turns.pop(0) if STATE.turns else [{"text": "ok"}]
                    if busy:  # занятая сессия — реплика в очередь текущего хода (F10)
                        STATE.queued.setdefault(sid, []).append((user, steps))
                if not busy:
                    threading.Thread(target=run_turn, args=(sid, user, steps), daemon=True).start()
                return self._send(204)
        return self._send(404, {"error": "no route"})

    def _sse(self):
        stream = Stream()
        with LOCK:
            STATE.streams.append(stream)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            self._write_event({"id": _id("evt"), "type": "server.connected", "properties": {}})
            last_beat = time.monotonic()
            while not stream.closed:
                stream.cond.wait(0.2)
                with LOCK:
                    events = list(stream)
                    stream.clear()
                    stream.cond.clear()
                for event in events:
                    self._write_event(event)
                if time.monotonic() - last_beat > 5:
                    self._write_event({"id": _id("evt"), "type": "server.heartbeat", "properties": {}})
                    last_beat = time.monotonic()
        except BrokenPipeError, ConnectionResetError:
            pass
        finally:
            with LOCK:
                if stream in STATE.streams:
                    STATE.streams.remove(stream)
            self.close_connection = True

    def _write_event(self, event):
        self.wfile.write(b"data: " + json.dumps(event, ensure_ascii=False).encode() + b"\n\n")
        self.wfile.flush()


def main():
    global STATE, PASSWORD, CONFIG
    argv = sys.argv[1:]
    if argv == ["--version"]:
        print(_load("scenario.json", {}).get("version") or "1.18.31")
        sys.exit(0)
    if not argv or argv[0] != "serve":
        print("fake opencode: только serve", file=sys.stderr)
        sys.exit(2)
    port = int(argv[argv.index("--port") + 1])
    host = argv[argv.index("--hostname") + 1]
    scenario = _load("scenario.json", {})
    with open(_path("env.json"), "w", encoding="utf-8") as f:
        json.dump(dict(os.environ), f)
    config_path = os.environ.get("OPENCODE_CONFIG") or ""
    try:
        with open(config_path, encoding="utf-8") as f:
            CONFIG = json.load(f)
    except OSError, ValueError:
        CONFIG = {}
    with open(_path("config.json"), "w", encoding="utf-8") as f:
        json.dump(CONFIG, f)
    PASSWORD = os.environ.get("OPENCODE_SERVER_PASSWORD") or ""
    pids = {"server": os.getpid()}
    if scenario.get("grandchild") or scenario.get("grandchild_setsid"):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time\nwhile True: time.sleep(1)"],
            start_new_session=bool(scenario.get("grandchild_setsid")),
        )
        pids["grandchild"] = child.pid
    with open(_path("pids.json"), "w", encoding="utf-8") as f:
        json.dump(pids, f)
    _append("starts.jsonl", {"pid": os.getpid(), "agents": sorted(CONFIG.get("agent") or {})})
    print("fake opencode: старт", flush=True)
    if "exit_rc" in scenario:
        print("fake opencode: падаю по сценарию", flush=True)
        sys.exit(int(scenario["exit_rc"]))
    if scenario.get("no_listen"):
        print("fake opencode: не слушаю порт (сценарий)", flush=True)
        while True:
            time.sleep(1)
    STATE = State(scenario)
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    print(f"opencode server listening on http://{host}:{port}", flush=True)
    server.serve_forever()


PASSWORD = ""
CONFIG = {}
BUILTIN_AGENTS = {"build": "primary", "plan": "primary", "general": "subagent", "explore": "subagent"}
REJECTED = "The user rejected permission to use this specific tool call with the following feedback: "

if __name__ == "__main__":
    main()
