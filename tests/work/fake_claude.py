"""Подставной `claude` для тестов hub-work, говорящий stream-json по сценарию (настоящий `claude` в тестах не
запускается никогда). Копируется во временный каталог с шебангом текущего интерпретатора.

Рядом с собой читает и пишет:
- `scenario.json` — `{"mode", "init_mode", "tools", "turns": [[шаг, …], …], "unsolicited": {"after_s",
  "turn"}}`; ход — по одному на пришедшее сообщение `user` (кончились — ход «ok»). Шаги: `{"text"}`, `{"tool",
  "input", "result", "is_error"}`, `{"ask": {"tool", "input"}, "cancel_after"?}` (ждёт `control_response`),
  `{"rate_limit": {…}}`, `{"emit": {…}}` (строка потока как есть),
  `{"sleep": с}`, `{"spawn": "команда"}` (фоновый потомок в своей группе, pid — в
  `children.jsonl`), `{"exit": код}`, `{"result": "итог хода"}`; Ф41-3: у шага `tool` — `"use_result"` (поле
  `tool_use_result` строки с результатом) и `"hold": с` (пауза между вызовом и результатом — прерывание
  застаёт инструмент: результат приходит ошибкой, как у Claude Code), `{"stream": {"id", "blocks": [{"type",
  "chunks": [...]}]}}` — частичные сообщения (`stream_event`) и итоговые `assistant` по одному на блок,
  `{"result_extra": {…}}` — поля в `result` хода (`usage`, `total_cost_usd`, `modelUsage`); Ф11-6:
  `"init_gate": путь` — первый
  `system/init` процесса ждёт, пока появится файл; `"initialize_delay": {ключ: с}` — ответ на `initialize`
  через столько секунд (ключ — `HUB_WORK_TASK` процесса, у оркестратора — `"orch"`);
  `"mcp_status": [[сервер, …], …]` — ответы на `mcp_status` по порядку (последний повторяется; без ключа —
  серверов нет), `"mcp_status_silent": true` — на `mcp_status` не отвечать (движок без такого запроса);
  Ф87-1: `"mcp_status_launch": [ответы, …]` — то же по номеру запуска (`calls.jsonl`; последний повторяется);
  Ф50-1: `"lifecycle": true` — очередь ввода, как у Claude Code 2.1.295 (пробы `.runs/f50-probe`): строка
  `user`, пришедшая посреди хода, — `command_lifecycle queued`; после ближайшего результата инструмента она
  входит в идущий ход (`started`), иначе после `result` по ней начинается новый ход (`started` — после
  `result`); `interrupt` очередь не трогает (ход по ней начнётся следом), с `cancel_queued` — выбрасывает
  (`cancelled`); `"lifecycle_stall": true` — движок не начинает ход по оставшейся в очереди строке;
- `calls.jsonl` — по строке на запуск (argv, cwd, окружение, pid) — ДО работы;
- `stdin.jsonl` — каждое сообщение stdin как пришло; `responses.jsonl` — ответы хоста на `can_use_tool`;
- `ends.jsonl` — выход (pid, код, причина)."""

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
_out_lock = threading.Lock()


def _load(name, default):
    try:
        with open(os.path.join(HERE, name), encoding="utf-8") as f:
            return json.load(f)
    except OSError, ValueError:
        return default


def _load_lines(name):
    try:
        with open(os.path.join(HERE, name), encoding="utf-8") as f:
            return [line for line in f if line.strip()]
    except OSError:
        return []


def _append(name, value):
    with open(os.path.join(HERE, name), "a", encoding="utf-8") as f:
        f.write(json.dumps(value, ensure_ascii=False) + "\n")


def _user_text(msg):
    content = (msg.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    texts = [b.get("text") or "" for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
    return "".join(texts)


def emit(obj):
    with _out_lock:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def finish(rc, why):
    _append("ends.jsonl", {"pid": os.getpid(), "rc": rc, "why": why, "ended": time.time()})
    sys.stdout.flush()
    os._exit(rc)


def _flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


class Aborted(Exception):
    pass


class Fake:
    def __init__(self, argv):
        self.argv = argv
        self.scenario = _load("scenario.json", {})
        # Ф41-2б: сценарий постоянной сессии хаба — `"sessions": {"<HUB_SESSION>": {…}}` поверх общего.
        own = (self.scenario.get("sessions") or {}).get(os.environ.get("HUB_SESSION") or "")
        if isinstance(own, dict):
            self.scenario = {**self.scenario, **own}
        self.mode = self.scenario.get("mode") or _flag(argv, "--permission-mode") or "default"
        self.session = _flag(argv, "--session-id") or _flag(argv, "--resume") or str(uuid.uuid4())
        self.inbox = queue.Queue()
        self.pending_users = []
        self.turns = list(self.scenario.get("turns") or [])
        self.eof = False
        self.gate = self.scenario.get("init_gate")
        self.stale_done = False
        self.lifecycle = bool(self.scenario.get("lifecycle"))

    def reader(self):
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            _append("stdin.jsonl", msg)
            if msg.get("type") == "user" and os.environ.get("HUB_SESSION"):
                self.transcript(msg)
            self.inbox.put(msg)
        self.inbox.put({"type": "__eof__"})

    def transcript(self, msg):
        """Ф41-2б: стенограмма постоянной сессии, как у Claude Code, —
        `<home>/.claude/projects/<слаг>/<id>.jsonl` (`home` тестов — каталог `home` рядом с каталогом
        подставного `claude`): реплика с её `uuid`."""
        directory = os.path.join(os.path.dirname(HERE), "home", ".claude", "projects", "fake")
        os.makedirs(directory, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000Z"
        record = {"type": "user", "uuid": msg.get("uuid"), "timestamp": stamp, "promptSource": "sdk"}
        with open(os.path.join(directory, self.session + ".jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def handle_control(self, msg):
        """Управляющие сообщения, которые обслуживаются в любой момент. `True` — это interrupt."""
        request = msg.get("request") or {}
        subtype = request.get("subtype")
        if subtype == "initialize":
            response = {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": msg["request_id"],
                    "response": {"current_permission_mode": self.scenario.get("initialize_mode", self.mode)},
                },
            }
            key = os.environ.get("HUB_WORK_TASK") or "orch"
            delay = (self.scenario.get("initialize_delay") or {}).get(key)
            if delay:
                timer = threading.Timer(float(delay), emit, args=(response,))
                timer.daemon = True
                timer.start()
            else:
                emit(response)
            if self.scenario.get("initialize_exit"):
                # Ревью 2б (а): процесс отвечает на `initialize` и сам выходит, не начав хода.
                timer = threading.Timer(0.3, os._exit, args=(0,))
                timer.daemon = True
                timer.start()
            return False
        if subtype == "mcp_status":
            if self.scenario.get("mcp_status_silent"):
                return False
            answers = self.scenario.get("mcp_status") or [[]]
            by_launch = self.scenario.get("mcp_status_launch")
            if by_launch:
                launch_no = len(_load_lines("calls.jsonl")) - 1
                answers = by_launch[max(0, min(launch_no, len(by_launch) - 1))] or [[]]
            self.mcp_asked = getattr(self, "mcp_asked", 0) + 1
            servers = answers[min(self.mcp_asked, len(answers)) - 1]
            emit(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": msg["request_id"],
                        "response": {"mcpServers": servers},
                    },
                }
            )
            return False
        if subtype == "interrupt":
            cancelled = []
            if self.lifecycle and request.get("cancel_queued"):
                cancelled, self.pending_users = [m.get("uuid") for m in self.pending_users], []
                for item in cancelled:
                    self.life(item, "cancelled")
            answer = {"still_queued": [m.get("uuid") for m in self.pending_users] if self.lifecycle else []}
            if cancelled:
                answer["cancelled"] = cancelled
            emit(
                {
                    "type": "control_response",
                    "response": {"subtype": "success", "request_id": msg["request_id"], "response": answer},
                }
            )
            return True
        return False

    def life(self, command_uuid, state):
        emit({"type": "command_lifecycle", "command_uuid": command_uuid, "state": state})

    def queue_user(self, msg):
        self.pending_users.append(msg)
        if self.lifecycle:
            self.life(msg.get("uuid"), "queued")

    def absorb(self):
        """Ф50-1: граница инструмента — строки из очереди входят в идущий ход."""
        if not self.lifecycle:
            return
        taken, self.pending_users = self.pending_users, []
        for msg in taken:
            self.life(msg.get("uuid"), "started")
            _append("absorbed.jsonl", {"uuid": msg.get("uuid")})

    def next_message(self, timeout=None):
        try:
            msg = self.inbox.get(timeout=timeout)
        except queue.Empty:
            return None
        if msg.get("type") == "__eof__":
            self.eof = True
        return msg

    def poll_during_turn(self):
        """Во время хода: interrupt -> прервать; user -> в очередь; EOF — доработать ход."""
        while True:
            try:
                msg = self.inbox.get_nowait()
            except queue.Empty:
                return
            if msg.get("type") == "__eof__":
                self.eof = True
            elif msg.get("type") == "control_request":
                if self.handle_control(msg):
                    raise Aborted
            elif msg.get("type") == "user":
                self.queue_user(msg)

    def init(self):
        emit(
            {
                "type": "system",
                "subtype": "init",
                "session_id": self.session,
                "cwd": os.getcwd(),
                "permissionMode": self.scenario.get("init_mode", self.mode),
                # Ф41-3: модель хода (по ней берётся предел контекста из `modelUsage`); нет ключа — нет поля.
                **({"model": self.scenario["init_model"]} if "init_model" in self.scenario else {}),
                "tools": self.scenario.get("tools", ["Bash", "Read", "Edit", "mcp__hub-all__hub_draft"]),
            }
        )

    def run_turn(self, steps, uuids):
        start = time.time()
        denials = []
        extra = {}
        result_text = "ок"
        if self.gate:
            while not os.path.exists(self.gate):
                time.sleep(0.02)
            self.gate = None
        self.init()
        try:
            for step in steps:
                self.poll_during_turn()
                if "text" in step:
                    emit(
                        {
                            "type": "assistant",
                            "message": {"id": "m", "content": [{"type": "text", "text": step["text"]}]},
                        }
                    )
                elif "tool" in step:
                    tid = "toolu_" + uuid.uuid4().hex[:8]
                    emit(
                        {
                            "type": "assistant",
                            "message": {
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "id": tid,
                                        "name": step["tool"],
                                        "input": step.get("input", {}),
                                    }
                                ]
                            },
                        }
                    )
                    line = {
                        "type": "user",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": tid,
                                    "content": step.get("result", ""),
                                    "is_error": bool(step.get("is_error")),
                                }
                            ]
                        },
                    }
                    if "use_result" in step:
                        line["tool_use_result"] = step["use_result"]
                    try:
                        end = time.time() + float(step.get("hold") or 0)
                        while time.time() < end:
                            self.poll_during_turn()
                            time.sleep(0.02)
                    except Aborted:
                        # Прерывание посреди инструмента: Claude Code дописывает вызову отказ и кончает ход.
                        line["message"]["content"][0].update(
                            content="The user doesn't want to proceed with this tool use.", is_error=True
                        )
                        line.pop("tool_use_result", None)
                        emit(line)
                        raise
                    emit(line)
                    self.absorb()
                elif "stream" in step:
                    self.stream(step["stream"])
                elif "result_extra" in step:
                    extra.update(step["result_extra"])
                elif "ask" in step:
                    denied = self.ask(step)
                    if denied:
                        denials.append(step["ask"])
                    # Ф55-1 (проба `.runs/f55-probe`): строки, пришедшие, пока движок ждал решения, входят в
                    # идущий ход сразу за результатом инструмента — и после «разрешить», и после отказа.
                    self.absorb()
                elif "emit" in step:
                    emit(step["emit"])  # 9c: произвольная строка потока (например, system/permission_denied)
                elif "rate_limit" in step:
                    emit(
                        {
                            "type": "rate_limit_event",
                            "rate_limit_info": step["rate_limit"],
                            "session_id": self.session,
                        }
                    )
                elif "sleep" in step:
                    end = time.time() + step["sleep"]
                    while time.time() < end:
                        self.poll_during_turn()
                        time.sleep(0.02)
                elif "spawn" in step:
                    child = subprocess.Popen(step["spawn"], shell=True, start_new_session=True)  # noqa: S602
                    _append("children.jsonl", {"pid": child.pid})
                elif "exit" in step:
                    finish(step["exit"], "exit step")
                elif "reject" in step:
                    # Движок отклонил ход, не вызвав модель (лимит исчерпан на этом ходе).
                    emit(
                        {
                            "type": "result",
                            "subtype": "error_during_execution",
                            "is_error": True,
                            "num_turns": 0,
                            "duration_ms": 0,
                            "permission_denials": [],
                            "result": str(step["reject"]),
                            "user_message_uuids": uuids,
                        }
                    )
                    return
                elif "result" in step:
                    result_text = step["result"]
        except Aborted:
            emit(
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "num_turns": 1,
                    "duration_ms": int((time.time() - start) * 1000),
                    "permission_denials": denials,
                    "result": "",
                    "user_message_uuids": uuids,
                    "terminal_reason": "aborted_tools",
                    **extra,
                }
            )
            return
        emit(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "num_turns": max(1, len(steps)),
                "duration_ms": int((time.time() - start) * 1000),
                "permission_denials": denials,
                "result": result_text,
                "user_message_uuids": uuids,
                **extra,
            }
        )

    def compact(self):
        """Ф41-4б: ответ на `/compact`, как у настоящего Claude Code (проба `out_6_commands.jsonl`): статус,
        повторный `init`, `compact_boundary`, два `user` со сводкой и `result` с `num_turns: 0`. Сценарий
        `"compact"`: `{"fail": true}` — без границы, `{"hang": true}` — дальше тишина; `"pre_tokens"`."""
        spec = self.scenario.get("compact") or {}
        _append("compacts.jsonl", {"at": time.time()})
        emit({"type": "system", "subtype": "status", "status": "compacting", "session_id": self.session})
        if spec.get("hang"):
            return
        outcome = "failed" if spec.get("fail") else "success"
        emit(
            {
                "type": "system",
                "subtype": "status",
                "status": None,
                "compact_result": outcome,
                "session_id": self.session,
            }
        )
        if not spec.get("fail"):
            # На случай, если движок выдаёт сводку ещё и записью ассистента: в ленту она идти не должна.
            block = {"type": "text", "text": "СВОДКА-СЖАТИЯ текстом ассистента"}
            emit({"type": "assistant", "message": {"id": "msg-compact", "content": [block]}})
            self.init()
            emit(
                {
                    "type": "system",
                    "subtype": "compact_boundary",
                    "session_id": self.session,
                    "compact_metadata": {
                        "trigger": "manual",
                        "pre_tokens": spec.get("pre_tokens", 152_000),
                        "post_tokens": 9000,
                    },
                }
            )
            summary = "This session is being continued… СВОДКА-СЖАТИЯ"
            emit({"type": "user", "message": {"role": "user", "content": summary}})
            emit({"type": "user", "message": {"role": "user", "content": "<local-command-stdout>Compacted"}})
        emit(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "num_turns": 0,
                "duration_ms": 5,
                "result": "",
                "total_cost_usd": spec.get("total_cost_usd"),
                "usage": {"input_tokens": 0, "output_tokens": 0, "iterations": []},
            }
        )

    def stream(self, spec):
        """Ф41-3: частичные сообщения, как у `--include-partial-messages` (проба 5з): `message_start`,
        на каждый блок — `content_block_start` / дельты / `content_block_stop` и итоговый `assistant` с тем же
        `message.id`, в конце — `message_delta` (с `usage`) и `message_stop`."""
        mid = spec.get("id") or "msg_" + uuid.uuid4().hex[:8]

        def event(body, **top):
            emit({"type": "stream_event", "event": body, "session_id": self.session, **top})

        parent = spec.get("parent")
        top = {"parent_tool_use_id": parent} if parent else {"parent_tool_use_id": None}
        if not spec.get("no_start"):
            head = {"id": mid, "role": "assistant", "content": []}
            event({"type": "message_start", "message": head}, **top)
        for index, block in enumerate(spec.get("blocks") or []):
            btype = block.get("type", "text")
            start = {"type": btype}
            if btype == "text":
                start["text"] = ""
            elif btype == "thinking":
                start["thinking"] = ""
            else:
                start.update(id="toolu_" + uuid.uuid4().hex[:8], name=block.get("name", "Read"), input={})
            event({"type": "content_block_start", "index": index, "content_block": start}, **top)
            for chunk in block.get("chunks") or []:
                self.poll_during_turn()
                if btype == "text":
                    delta = {"type": "text_delta", "text": chunk}
                elif btype == "thinking":
                    delta = {"type": "thinking_delta", "thinking": chunk, "estimated_tokens": 50}
                else:
                    delta = {"type": "input_json_delta", "partial_json": chunk}
                event({"type": "content_block_delta", "index": index, "delta": delta}, **top)
                time.sleep(float(spec.get("delay") or 0))
            event({"type": "content_block_stop", "index": index}, **top)
            if btype == "text" and not parent:
                text = "".join(block.get("chunks") or [])
                emit(
                    {
                        "type": "assistant",
                        "message": {
                            "id": mid,
                            "content": [{"type": "text", "text": block.get("final", text)}],
                        },
                    }
                )
        delta = {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}
        if "usage" in spec:
            delta["usage"] = spec["usage"]
        event(delta, **top)
        event({"type": "message_stop"}, **top)

    def ask(self, step):
        spec = step["ask"]
        request_id = uuid.uuid4().hex
        tid = "toolu_" + uuid.uuid4().hex[:8]
        emit(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": tid, "name": spec["tool"], "input": spec.get("input", {})}
                    ]
                },
            }
        )
        emit(
            {
                "type": "control_request",
                "request_id": request_id,
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": spec["tool"],
                    "input": spec.get("input", {}),
                    "tool_use_id": tid,
                    "decision_reason_type": spec.get("reason_type", "rule"),
                    "decision_reason": spec.get("reason"),
                },
            }
        )
        deadline = time.time() + step["cancel_after"] if "cancel_after" in step else None
        while True:
            if deadline is not None and time.time() >= deadline:
                emit({"type": "control_cancel_request", "request_id": request_id})
                emit(
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": tid,
                                    "content": "cancelled",
                                    "is_error": True,
                                }
                            ]
                        },
                    }
                )
                return True
            msg = self.next_message(timeout=0.05)
            if msg is None:
                continue
            if msg.get("type") == "__eof__":
                # ФЧ2 п.8: stdin закрыт при висящем запросе — AbortError, инструмент не выполнен.
                emit(
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": tid,
                                    "content": "AbortError",
                                    "is_error": True,
                                }
                            ]
                        },
                    }
                )
                return True
            if msg.get("type") == "control_request":
                if self.handle_control(msg):
                    raise Aborted
                continue
            if msg.get("type") == "user":
                self.queue_user(msg)
                continue
            if (
                msg.get("type") == "control_response"
                and (msg.get("response") or {}).get("request_id") == request_id
            ):
                response = msg["response"].get("response") or {}
                _append("responses.jsonl", {"tool": spec["tool"], "response": response})
                if response.get("behavior") == "allow":
                    content = json.dumps(response.get("updatedInput"), ensure_ascii=False)
                    emit(
                        {
                            "type": "user",
                            "message": {
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": tid,
                                        "content": content,
                                        "is_error": False,
                                    }
                                ]
                            },
                        }
                    )
                    return False
                emit(
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": tid,
                                    "content": response.get("message", ""),
                                    "is_error": True,
                                }
                            ]
                        },
                    }
                )
                return True

    def main(self):
        _append(
            "calls.jsonl",
            {
                "argv": self.argv,
                "cwd": os.getcwd(),
                "env": dict(os.environ),
                "pid": os.getpid(),
                "started": time.time(),
            },
        )
        threading.Thread(target=self.reader, daemon=True).start()
        unsolicited = self.scenario.get("unsolicited")
        idle_since = time.time()
        while True:
            if self.pending_users:
                batch, self.pending_users = self.pending_users, []
                if any(_user_text(m) == "/compact" for m in batch):
                    if (self.scenario.get("compact") or {}).get("own_turn_first"):
                        # 10.10: движок сам начал ход (уведомление фоновой задачи), `/compact` — в очереди.
                        life = {"type": "command_lifecycle"}
                        for m in batch:
                            emit({**life, "command_uuid": m.get("uuid"), "state": "queued"})
                        self.run_turn([{"text": "СВОЙ-ХОД движка"}], [])
                        for m in batch:
                            emit({**life, "command_uuid": m.get("uuid"), "state": "started"})
                    self.compact()  # Ф41-4б: сжатие — не ход, сценарий ходов не тратится
                    idle_since = time.time()
                    continue
                if self.lifecycle and self.scenario.get("lifecycle_stall") and self.stale_done:
                    self.pending_users = batch  # строка так и стоит в очереди — хода по ней нет
                    time.sleep(0.05)
                    self.poll_idle()
                    continue
                steps = self.turns.pop(0) if self.turns else [{"text": "ок"}]
                if self.lifecycle:
                    for m in batch:
                        self.life(m.get("uuid"), "started")
                    self.stale_done = True
                if self.scenario.get("stale_turn") and not self.stale_done:
                    # Проба 10.10: после `--resume` движок сам делает пустой ход, ввод стоит в очереди.
                    self.stale_done = True
                    for m in batch:
                        emit({"type": "command_lifecycle", "command_uuid": m.get("uuid"), "state": "queued"})
                    self.init()
                    stale = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 0}
                    emit({**stale, "result": ""})
                    time.sleep(0.3)
                    for m in batch:
                        emit({"type": "command_lifecycle", "command_uuid": m.get("uuid"), "state": "started"})
                self.run_turn(steps, [m.get("uuid") for m in batch])
                idle_since = time.time()
                continue
            if self.eof:
                finish(0, "eof")
            msg = self.next_message(timeout=0.05)
            if msg is None:
                if unsolicited and time.time() - idle_since >= unsolicited.get("after_s", 1):
                    turn, unsolicited = (
                        unsolicited.get("turn", [{"text": "фоновая задача закончилась"}]),
                        None,
                    )
                    self.run_turn(turn, [])
                    idle_since = time.time()
                continue
            kind = msg.get("type")
            if kind == "control_request":
                self.handle_control(msg)
            elif kind == "user":
                self.queue_user(msg)

    def poll_idle(self):
        """Очередь стоит (`lifecycle_stall`): управляющие сообщения обслуживаются, ввод копится."""
        while True:
            try:
                msg = self.inbox.get_nowait()
            except queue.Empty:
                return
            if msg.get("type") == "__eof__":
                self.eof = True
                finish(0, "eof")
            elif msg.get("type") == "control_request":
                self.handle_control(msg)
            elif msg.get("type") == "user":
                self.queue_user(msg)


def main():
    argv = sys.argv[1:]
    if argv[:2] == ["auth", "status"]:
        print(
            json.dumps(
                _load("auth.json", {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty"})
            )
        )
        sys.exit(0)
    if argv[:2] == ["agents", "--json"]:
        # Ф41-2б П9: открытые сессии; `agents.json` — `{"raw": "текст как есть", "rc": код, "sleep": с}` или
        # список.
        spec = _load("agents.json", [])
        _append("agents_calls.jsonl", {"pid": os.getpid()})
        if isinstance(spec, dict):
            time.sleep(float(spec.get("sleep") or 0))
            sys.stdout.write(str(spec.get("raw") or ""))
            sys.exit(int(spec.get("rc") or 0))
        print(json.dumps(spec))
        sys.exit(0)
    signal.signal(signal.SIGTERM, lambda *_: finish(143, "sigterm"))
    Fake(argv).main()


if __name__ == "__main__":
    main()
